"""Assembles the HTTP application: error envelope, auth, and the notification routes.

Each handler maps HTTP input to a VintaSend service call and the result back to the wire
contract -- no business logic beyond the translation itself.
"""

from collections.abc import Callable, Sequence
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI, Query, Status
from ninja.errors import AuthenticationError, HttpError, ValidationError

from .auth import ApiKeyAuth
from .bodies import JsonBodyParser, refuse_an_empty_json_body
from .contract import (
    API_VERSION,
    URLS_NAMESPACE,
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
from .errors import STATUS_BY_CODE, ApiError, invalid_request, issue
from .filters import build_backend_filter, build_order_by
from .hooks import REQUEST_ID_HEADER, report_unhandled_error, request_id_for
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


NotificationPage = PaginatedResponse[NotificationOut]

# Error responses are produced by the exception handlers below rather than returned from
# a view, so they are declared purely so the generated schema documents them the way
# `openapi.yaml` does. Each route declares the subset the contract lists for it, and a test
# pins the two together.
#
# Every authenticated route can refuse the caller twice over: 401 when no valid credential
# was presented, 403 (FORBIDDEN) when a host authenticated the caller and then refused it.
AUTH_ERRORS: dict[int, Any] = {401: ApiErrorResponse, 403: ApiErrorResponse}
LIST_ERRORS: dict[int, Any] = {400: ApiErrorResponse, **AUTH_ERRORS}
LOOKUP_ERRORS: dict[int, Any] = {**AUTH_ERRORS, 404: ApiErrorResponse}
PREVIEW_ERRORS: dict[int, Any] = {
    **LOOKUP_ERRORS,
    409: ApiErrorResponse,
    502: ApiErrorResponse,
}
RESEND_ERRORS: dict[int, Any] = {
    400: ApiErrorResponse,
    **AUTH_ERRORS,
    409: ApiErrorResponse,
}
# The request body is optional, so an omitted one falls back to this. Shared rather
# than constructed per call because it is only ever read.
DEFAULT_RESEND_BODY = ResendBody(useStoredContext=False)

CANCEL_ERRORS: dict[int, Any] = {
    **LOOKUP_ERRORS,
    409: ApiErrorResponse,
}

api = NinjaAPI(
    title="VintaSend Dashboard API",
    version="1.0.0",
    description=(
        "HTTP contract between a VintaSend notification service and the VintaSend "
        "dashboard UI. openapi.yaml in the repository root is the source of truth."
    ),
    # Unique, so the templates management API can be mounted beside it. See contract.py.
    urls_namespace=URLS_NAMESPACE,
    auth=ApiKeyAuth(),
    # Reads a body under the contract's media-type rule; see `bodies.py`.
    parser=JsonBodyParser(),
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
    """A safety net for Ninja's own authentication failure.

    ``ApiKeyAuth`` refuses every caller by raising ``ApiError`` itself -- a missing or
    non-bearer header as much as a wrong key -- and a host's authenticator does the same, so
    Ninja only raises this if an auth callable returns nothing. It still gets the envelope.
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
        issue(
            ".".join(str(part) for part in _issue_path(failure.get("loc", ()))),
            str(failure.get("msg", "")),
        )
        for failure in exc.errors
    ]
    return handle_api_error(request, invalid_request(issues))


def _issue_path(loc: Any) -> list[Any]:
    parts = list(loc)
    if parts and parts[0] in {"query", "body", "path", "form", "header", "cookie"}:
        parts = parts[1:]
    # Ninja names the request-body argument after the view parameter ("payload"), which
    # is an implementation detail the client never sent and should not be told about.
    if parts and parts[0] == "payload":
        parts = parts[1:]
    return parts


@api.exception_handler(HttpError)
def handle_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    """Put Ninja's own refusals in the error envelope.

    Ninja raises ``HttpError`` when it cannot read a request body, wrapping whatever the
    parser raised. ``JsonBodyParser`` raises the contract's 400, so that is unwrapped and
    answered as it stands. Any other 400 gets the same shape. Anything else is unexpected.
    """
    if isinstance(exc.__cause__, ApiError):
        return handle_api_error(request, exc.__cause__)
    if exc.status_code == 400:
        return handle_api_error(request, invalid_request([issue("", str(exc))]))
    return handle_unexpected_error(request, exc)


@api.exception_handler(Http404)
def handle_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
    return _envelope(
        request,
        "NOT_FOUND",
        f"No route matches {request.method} {request.path}.",
    )


@api.exception_handler(Exception)
def handle_unexpected_error(request: HttpRequest, exc: Exception) -> HttpResponse:
    """Report unexpected errors generically, and never log them whole.

    The client gets a fixed message, so backend internals -- connection strings, credentials
    in driver messages -- never leak to it. The log gets one line by default: the error's
    class, a request id, the method and the route pattern. An error from the notification
    store, a provider or a context generator can carry notification content, recipients or
    context values, which can be health data, so its message, its traceback and the concrete
    path are not logged. A host that wants more sets ``VINTASEND_UNHANDLED_ERROR_HANDLER`` --
    see ``hooks``. The response carries the request id in ``X-Request-Id``, to match a
    client's report to the log line.
    """
    request_id = request_id_for(request)
    report_unhandled_error(exc, request, request_id)
    response = _envelope(
        request,
        "INTERNAL_ERROR",
        "An unexpected error occurred while handling the request.",
    )
    response[REQUEST_ID_HEADER] = request_id
    # Django logs every 5xx response again on ``django.request``, with the concrete path and
    # the request object attached -- which mail_admins or an error tracker on the root logger
    # turns into a report with request data. This error has been reported above, so that
    # second record is suppressed.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


# --- helpers -------------------------------------------------------------------------


def _paginate(
    read: Callable[[int, int], Sequence[AnyNotification]], page: int, page_size: int
) -> dict[str, Any]:
    """One page of a listing, and whether the next page has a row.

    Backends are not required to count, so that is asked directly: a full page is followed by
    a one-row read of the first row after it, which is page ``page * page_size + 1`` of one-row
    pages. A short page is the last one without asking. ``read`` takes the contract's
    1-indexed pages; ``ServiceCaller`` converts them for the backend.
    """
    notifications = read(page, page_size)
    has_more = len(notifications) == page_size and bool(read(page * page_size + 1, 1))
    data: list[ListNotificationOut] = [
        serialize_notification(notification) for notification in notifications
    ]
    return {"data": data, "page": page, "pageSize": page_size, "hasMore": has_more}


def _find_notification(service: ServiceCaller, notification_id: str) -> AnyNotification:
    notification = service.get_notification(notification_id)
    if notification is None:
        raise ApiError.not_found(f"Notification with ID {notification_id} was not found.")
    return notification


# --- system --------------------------------------------------------------------------


@api.get(
    "/capabilities",
    response={200: DataResponse[dict[str, bool]], **AUTH_ERRORS},
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
    backend_filter = build_backend_filter(query, capabilities)
    order_by = build_order_by(query, capabilities)

    return _paginate(
        lambda page, page_size: service.filter_notifications(
            backend_filter, page, page_size, order_by
        ),
        query.page,
        query.pageSize,
    )


@api.get(
    "/notifications/pending",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_pending_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    return _paginate(service.get_pending_notifications, query.page, query.pageSize)


@api.get(
    "/notifications/future",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_future_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    return _paginate(service.get_future_notifications, query.page, query.pageSize)


@api.get(
    "/notifications/one-off",
    response={200: NotificationPage, **LIST_ERRORS},
    tags=["notifications"],
)
def list_one_off_notifications(
    request: HttpRequest, query: Query[PaginationQuery]
) -> dict[str, Any]:
    service = get_service_caller()
    return _paginate(service.get_one_off_notifications, query.page, query.pageSize)


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
    refuse_an_empty_json_body(request)
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
