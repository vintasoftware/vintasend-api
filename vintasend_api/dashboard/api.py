"""Assembles the HTTP application: error envelope, auth, and the notification routes.

Each handler maps HTTP input to a VintaSend service call and the result back to the wire
contract -- no business logic beyond the translation itself.
"""

import logging
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI, Query, Status
from ninja.errors import AuthenticationError, ValidationError

from .auth import ApiKeyAuth
from .contract import (
    API_VERSION,
    ApiErrorResponse,
    CancelledNotificationOut,
    DataResponse,
    HealthOut,
    NotificationDetailOut,
    NotificationOut,
    NotificationPreviewOut,
    PaginatedResponse,
    UserNotificationOut,
)
from .errors import STATUS_BY_CODE, ApiError
from .filters import build_backend_filter, build_order_by
from .preview import build_notification_preview
from .query import NotificationListQuery, PaginationQuery, ResendBody
from .serialize import (
    AnyNotification,
    ListNotificationOut,
    serialize_notification,
    serialize_notification_detail,
    serialize_user_notification,
)
from .service import ServiceCaller, get_service_caller
from .template_source import get_template_client


logger = logging.getLogger(__name__)

NotificationPage = PaginatedResponse[NotificationOut]

# Error responses are produced by the exception handlers below rather than returned from
# a view, so they are declared purely so the generated schema documents them the way
# `openapi.yaml` does. Each route declares the subset the contract lists for it.
LIST_ERRORS: dict[int, Any] = {400: ApiErrorResponse, 401: ApiErrorResponse}
LOOKUP_ERRORS: dict[int, Any] = {401: ApiErrorResponse, 404: ApiErrorResponse}
PREVIEW_ERRORS: dict[int, Any] = {
    401: ApiErrorResponse,
    404: ApiErrorResponse,
    409: ApiErrorResponse,
    502: ApiErrorResponse,
}
RESEND_ERRORS: dict[int, Any] = {
    400: ApiErrorResponse,
    401: ApiErrorResponse,
    409: ApiErrorResponse,
}
# The request body is optional, so an omitted one falls back to this. Shared rather
# than constructed per call because it is only ever read.
DEFAULT_RESEND_BODY = ResendBody(useStoredContext=False)

CANCEL_ERRORS: dict[int, Any] = {
    401: ApiErrorResponse,
    404: ApiErrorResponse,
    409: ApiErrorResponse,
}

api = NinjaAPI(
    title="VintaSend Dashboard API",
    version="1.0.0",
    description=(
        "HTTP contract between a VintaSend notification service and the VintaSend "
        "dashboard UI. openapi.yaml in the repository root is the source of truth."
    ),
    urls_namespace="vintasend_api",
    auth=ApiKeyAuth(),
    # The contract's own envelope is emitted by the handlers below, so Ninja's default
    # 404/validation bodies are never used.
    docs_url="/docs",
)


# --- error envelope ------------------------------------------------------------------


def _envelope(
    request: HttpRequest, code: str, message: str, details: Any | None = None
) -> HttpResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JsonResponse({"error": error}, status=STATUS_BY_CODE[code])


@api.exception_handler(ApiError)
def handle_api_error(request: HttpRequest, exc: ApiError) -> HttpResponse:
    return _envelope(request, exc.code, exc.message, exc.details)


@api.exception_handler(AuthenticationError)
def handle_authentication_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    """Covers a missing or non-bearer ``Authorization`` header.

    A wrong key never reaches here -- ``ApiKeyAuth`` raises ``ApiError`` itself -- but a
    header Ninja cannot parse as a bearer token is rejected before the auth class runs.
    """
    return _envelope(request, "UNAUTHORIZED", "A valid API key is required.")


@api.exception_handler(ValidationError)
def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    """Report invalid input as a 400 listing the offending fields.

    ``loc`` arrives as ``("query", "status")`` / ``("body", "payload", "useStoredContext")``.
    The leading source segment and Ninja's synthetic body-argument name are dropped so
    the reported path is the field name the client actually sent.
    """
    issues = [
        {
            "path": ".".join(str(part) for part in _issue_path(issue.get("loc", ()))),
            "message": issue.get("msg", ""),
        }
        for issue in exc.errors
    ]
    return _envelope(request, "BAD_REQUEST", "Invalid request.", {"issues": issues})


def _issue_path(loc: Any) -> list[Any]:
    parts = list(loc)
    if parts and parts[0] in {"query", "body", "path", "form", "header", "cookie"}:
        parts = parts[1:]
    # Ninja names the request-body argument after the view parameter ("payload"), which
    # is an implementation detail the client never sent and should not be told about.
    if parts and parts[0] == "payload":
        parts = parts[1:]
    return parts


@api.exception_handler(Http404)
def handle_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
    return _envelope(
        request,
        "NOT_FOUND",
        f"No route matches {request.method} {request.path}.",
    )


@api.exception_handler(Exception)
def handle_unexpected_error(request: HttpRequest, exc: Exception) -> HttpResponse:
    """Log unexpected errors in full but report them generically, so backend internals
    -- connection strings, credentials in driver messages -- never leak to a client."""
    logger.exception("Unhandled error while handling %s %s", request.method, request.path)
    return _envelope(
        request,
        "INTERNAL_ERROR",
        "An unexpected error occurred while handling the request.",
    )


# --- helpers -------------------------------------------------------------------------


def _paginate(notifications: list[AnyNotification], page: int, page_size: int) -> dict[str, Any]:
    data: list[ListNotificationOut] = [
        serialize_notification(notification) for notification in notifications
    ]
    return {
        "data": data,
        "page": page,
        "pageSize": page_size,
        # True when the page came back full, meaning another page may exist. Backends
        # are not required to report a total count.
        "hasMore": len(data) == page_size,
    }


def _find_notification(service: ServiceCaller, notification_id: str) -> AnyNotification:
    notification = service.get_notification(notification_id)
    if notification is None:
        raise ApiError.not_found(f"Notification with ID {notification_id} was not found.")
    return notification


# --- system --------------------------------------------------------------------------


@api.get(
    "/capabilities",
    response={200: DataResponse[dict[str, bool]], 401: ApiErrorResponse},
    tags=["system"],
)
def get_capabilities(request: HttpRequest) -> dict[str, Any]:
    return {"data": get_service_caller().get_capabilities()}


# --- notifications -------------------------------------------------------------------
#
# The literal collection paths are registered before `/notifications/{id}` on purpose:
# routes match in registration order, so declaring the detail route first would make it
# swallow `/notifications/pending`.


@api.get(
    "/notifications",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_notifications(request: HttpRequest, query: Query[NotificationListQuery]) -> dict[str, Any]:
    service = get_service_caller()
    capabilities = service.get_capabilities()

    # Page numbers stay in the contract's 1-indexed terms here. `ServiceCaller` converts
    # to whatever the configured backend uses, which it learns from the backend's own
    # `pagination.oneIndexed` capability rather than assuming.
    notifications = service.filter_notifications(
        build_backend_filter(query, capabilities),
        query.page,
        query.pageSize,
        build_order_by(query, capabilities),
    )

    return _paginate(notifications, query.page, query.pageSize)


@api.get(
    "/notifications/pending",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_pending_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    notifications = service.get_pending_notifications(query.page, query.pageSize)
    return _paginate(notifications, query.page, query.pageSize)


@api.get(
    "/notifications/future",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_future_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    notifications = service.get_future_notifications(query.page, query.pageSize)
    return _paginate(notifications, query.page, query.pageSize)


@api.get(
    "/notifications/one-off",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_one_off_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    notifications = service.get_one_off_notifications(query.page, query.pageSize)
    return _paginate(list(notifications), query.page, query.pageSize)


@api.get(
    "/notifications/{id}",
    response={200: DataResponse[NotificationDetailOut], **LOOKUP_ERRORS},
    tags=["notifications"],
)
def get_notification(request: HttpRequest, id: str) -> dict[str, Any]:  # noqa: A002
    service = get_service_caller()
    notification = _find_notification(service, id)
    return {"data": serialize_notification_detail(notification)}


@api.get(
    "/notifications/{id}/preview",
    response={200: DataResponse[NotificationPreviewOut], **PREVIEW_ERRORS},
    tags=["notifications"],
)
def preview_notification(request: HttpRequest, id: str) -> dict[str, Any]:  # noqa: A002
    service = get_service_caller()
    notification = _find_notification(service, id)

    preview = build_notification_preview(service, get_template_client(), notification)
    return {"data": preview}


@api.post(
    "/notifications/{id}/resend",
    response={201: DataResponse[UserNotificationOut], **RESEND_ERRORS},
    tags=["notifications"],
)
def resend_notification(
    request: HttpRequest,
    id: str,  # noqa: A002
    payload: ResendBody = DEFAULT_RESEND_BODY,
) -> Status:
    service = get_service_caller()
    resent = service.resend_notification(id, payload.useStoredContext)

    if resent is None:
        raise ApiError.conflict(
            "The notification could not be resent. It may not exist, may be a one-off "
            "notification, or may be scheduled for the future."
        )

    return Status(201, {"data": serialize_user_notification(resent)})


@api.post(
    "/notifications/{id}/cancel",
    response={200: DataResponse[CancelledNotificationOut], **CANCEL_ERRORS},
    tags=["notifications"],
)
def cancel_notification(request: HttpRequest, id: str) -> dict[str, Any]:  # noqa: A002
    service = get_service_caller()
    notification = _find_notification(service, id)

    if notification.status != "PENDING_SEND":
        raise ApiError.conflict("Only notifications in PENDING_SEND status can be cancelled.")

    service.cancel_notification(id)

    return {"data": CancelledNotificationOut(id=id, status="CANCELLED")}


# --- health --------------------------------------------------------------------------
#
# Unauthenticated and outside the versioned prefix: load balancers and container health
# checks have no API key.

health_api = NinjaAPI(
    version="health",
    urls_namespace="vintasend_api_health",
    auth=None,
    docs_url=None,
)


@health_api.get("/health", response=HealthOut, tags=["system"])
def health(request: HttpRequest) -> dict[str, str]:
    return {"status": "ok", "apiVersion": API_VERSION}
