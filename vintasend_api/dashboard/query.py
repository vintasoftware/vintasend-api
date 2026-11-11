"""Request validation schemas.

These define, precisely, what the API accepts. They are the Python counterpart of the
TypeScript reference's ``src/domain/schemas.ts`` and reject the same inputs with the
same 400 responses.

Query parameter names are camelCase to match the contract, so the attribute names here
are camelCase too (``ruff``'s N815 is disabled for this module in ``pyproject.toml``).
"""

import datetime
from typing import Any

from ninja import Field, Schema

from pydantic import TypeAdapter, ValidationError, field_validator

from .contract import (
    NotificationOrderByField,
    NotificationOrderDirection,
    NotificationStatus,
    NotificationType,
)


DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 100


class PaginationQuery(Schema):
    page: int = Field(DEFAULT_PAGE, ge=1)
    pageSize: int = Field(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE)


class NotificationListQuery(PaginationQuery):
    status: NotificationStatus | None = None
    notificationType: NotificationType | None = None
    adapterUsed: str | None = None
    userId: str | None = None
    bodyTemplate: str | None = None
    subjectTemplate: str | None = None
    contextName: str | None = None
    tenant: str | None = None
    # Template versions are integers, so `ge=0` rejects a negative outright rather than
    # serving an empty page for it. The floor is 0 and not 1 deliberately: version numbering
    # is the template renderer's business, and this API has no basis for assuming it is
    # 1-based.
    requestedTemplateVersion: int | None = Field(None, ge=0)
    usedTemplateVersion: int | None = Field(None, ge=0)
    createdAtFrom: datetime.datetime | None = None
    createdAtTo: datetime.datetime | None = None
    sentAtFrom: datetime.datetime | None = None
    sentAtTo: datetime.datetime | None = None
    orderByField: NotificationOrderByField | None = None
    orderByDirection: NotificationOrderDirection | None = None

    @field_validator(
        "adapterUsed",
        "userId",
        "bodyTemplate",
        "subjectTemplate",
        "contextName",
        "tenant",
    )
    @classmethod
    def _non_empty_after_trim(cls, value: str | None) -> str | None:
        """Trim, then reject what is left if it is empty.

        Mirrors the reference's ``z.string().trim().min(1)``: a parameter present but
        blank is a client bug worth a 400, not a filter that silently matches nothing.
        """
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("String should have at least 1 character")
        return trimmed


class ResendBody(Schema):
    # Reuse the context stored with the original notification instead of regenerating
    # it from current data.
    #
    # `strict` so only a real JSON boolean is accepted. Pydantic would otherwise read
    # "yes" / "on" / "1" as True, while the contract's `type: boolean` and the
    # TypeScript reference's `z.boolean()` both reject a string outright -- and a client
    # sending `"false"` as a string would silently get the opposite of what it asked for.
    useStoredContext: bool = Field(False, strict=True)


_DATETIME = TypeAdapter(datetime.datetime)


class RequeueBody(Schema):
    """Body of ``POST /notifications/{id}/requeue``. The reference's ``requeueBodySchema``.

    ``sendAfter`` left out keeps the notification's own schedule, and ``null`` sends it as soon
    as it is requeued. The two are told apart with ``model_fields_set``, so the default below
    is never read as a value.
    """

    sendAfter: datetime.datetime | None = None

    @field_validator("sendAfter", mode="before")
    @classmethod
    def _iso_date_string_or_null(cls, value: Any) -> Any:
        """Accept an ISO-8601 date string or ``null``, and nothing else.

        Mirrors the reference's ``z.string().refine(Date.parse)``: a JSON number is refused
        rather than read as a Unix timestamp, which Pydantic would otherwise do.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be an ISO-8601 date string")
        try:
            parsed = _DATETIME.validate_python(value)
        except ValidationError as error:
            raise ValueError("Must be an ISO-8601 date string") from error
        # A time without a zone is read as UTC, the zone every VintaSend backend writes. Left
        # naive, it could not be compared with the service's timezone-aware "now".
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed


class RepinBody(Schema):
    """Body of ``POST /notifications/{id}/repin``. The reference's ``repinBodySchema``.

    ``templateVersion`` left out pins the version the template renderer reports as current.
    ``ge=0`` because the contract admits version 0, as it does for the list filters.
    """

    templateVersion: int | None = Field(default=None, ge=0)

    @field_validator("templateVersion", mode="before")
    @classmethod
    def _json_integer(cls, value: Any) -> Any:
        """Accept a JSON integer only.

        Mirrors the reference's ``z.number().int()``. A string such as ``"3"`` is refused
        rather than coerced, and so are ``true`` and ``null``. A JSON number with an integral
        value, such as ``3.0``, is the same number as ``3`` to a JavaScript client and to the
        reference, so it is accepted.
        """
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Expected an integer")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("Expected an integer")
            return int(value)
        return value
