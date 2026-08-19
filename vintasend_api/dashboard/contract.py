"""Wire contract for the VintaSend dashboard API.

These schemas describe the JSON payloads exchanged over HTTP. They intentionally avoid
importing anything from ``vintasend``: any implementation of this contract (the
TypeScript one in ``vintasend-ts-api``, this Python one) must produce exactly these
shapes, and any UI consuming the API only needs these definitions.

Attribute names are camelCase on purpose, so these classes can be read side by side
with ``openapi.yaml`` and with the TypeScript reference's ``src/contract/types.ts``.
``ruff``'s N815 is disabled for this module in ``pyproject.toml`` for that reason.

All timestamps are ISO-8601 strings in UTC, and are ``null`` when unset -- never absent.
"""

from typing import Any, Generic, Literal, TypeVar

from ninja import Schema

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer
from typing_extensions import Annotated


API_VERSION = "v1"

API_BASE_PATH = f"/api/{API_VERSION}"

NotificationStatus = Literal["PENDING_SEND", "SENT", "FAILED", "READ", "CANCELLED"]

NotificationType = Literal["EMAIL", "SMS", "PUSH", "IN_APP"]

NotificationOrderByField = Literal["sendAfter", "sentAt", "readAt", "createdAt", "updatedAt"]

NotificationOrderDirection = Literal["asc", "desc"]

# Machine-readable error codes. Clients branch on these, not on messages.
ApiErrorCode = Literal[
    "BAD_REQUEST",
    "UNAUTHORIZED",
    "NOT_FOUND",
    "CONFLICT",
    "PREVIEW_UNAVAILABLE",
    "UPSTREAM_ERROR",
    "INTERNAL_ERROR",
]


class NotificationAttachmentOut(Schema):
    """Attachment metadata exposed on notification details."""

    id: str
    filename: str
    contentType: str
    size: int
    # Optional in the contract: present as a string or absent entirely, never null.
    description: str | None = None

    @model_serializer(mode="wrap")
    def _drop_absent_description(self, handler: SerializerFunctionWrapHandler) -> Any:
        serialized = handler(self)
        if serialized.get("description") is None:
            serialized.pop("description", None)
        return serialized


class NotificationBaseOut(Schema):
    """Fields shared by user and one-off notifications in list responses."""

    id: str
    notificationType: NotificationType
    title: str | None
    contextName: str
    status: NotificationStatus
    sendAfter: str | None
    sentAt: str | None
    readAt: str | None
    createdAt: str | None
    updatedAt: str | None
    adapterUsed: str | None
    bodyTemplate: str
    subjectTemplate: str | None
    gitCommitSha: str | None
    tenant: str | None


class UserNotificationOut(NotificationBaseOut):
    """A notification addressed to a known user."""

    kind: Literal["user"] = "user"
    userId: str


class OneOffNotificationOut(NotificationBaseOut):
    """A notification addressed to a raw email/phone, without a user record."""

    kind: Literal["one-off"] = "one-off"
    emailOrPhone: str
    firstName: str | None
    lastName: str | None


# `kind` discriminates the two variants so clients never have to sniff for the presence
# of a field.
NotificationOut = Annotated[
    UserNotificationOut | OneOffNotificationOut, Field(discriminator="kind")
]


class _NotificationDetailFields(Schema):
    """The potentially large payloads that list responses leave out."""

    contextUsed: Any | None
    contextParameters: Any | None
    extraParams: Any | None
    attachments: list[NotificationAttachmentOut]


class UserNotificationDetailOut(UserNotificationOut, _NotificationDetailFields):
    pass


class OneOffNotificationDetailOut(OneOffNotificationOut, _NotificationDetailFields):
    pass


NotificationDetailOut = Annotated[
    UserNotificationDetailOut | OneOffNotificationDetailOut, Field(discriminator="kind")
]


T = TypeVar("T")


class PaginatedResponse(Schema, Generic[T]):
    """Envelope returned by every paginated endpoint. ``page`` is 1-indexed."""

    data: list[T]
    page: int
    pageSize: int
    # True when the page came back full, meaning another page may exist. Backends are
    # not required to report a total count.
    hasMore: bool


class DataResponse(Schema, Generic[T]):
    """Envelope returned by every single-resource endpoint."""

    data: T


class NotificationPreviewOut(Schema):
    """Payload of ``GET /api/v1/notifications/{id}/preview`` on success."""

    gitCommitSha: str
    bodyTemplatePath: str
    subjectTemplatePath: str | None
    renderedBodyHtml: str
    renderedSubjectHtml: str


class CancelledNotificationOut(Schema):
    """Payload of ``POST /api/v1/notifications/{id}/cancel`` on success."""

    id: str
    status: NotificationStatus


class HealthOut(Schema):
    status: Literal["ok"] = "ok"
    apiVersion: str = API_VERSION


class ApiErrorBody(Schema):
    code: ApiErrorCode
    message: str
    # Optional machine-readable context, such as field issues.
    details: Any | None = None

    @model_serializer(mode="wrap")
    def _drop_absent_details(self, handler: SerializerFunctionWrapHandler) -> Any:
        serialized = handler(self)
        if serialized.get("details") is None:
            serialized.pop("details", None)
        return serialized


class ApiErrorResponse(Schema):
    """Error envelope returned with every non-2xx response."""

    error: ApiErrorBody
