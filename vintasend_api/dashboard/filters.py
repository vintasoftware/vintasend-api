"""Translates validated query parameters into VintaSend backend filters.

String filters and ordering are negotiated against the backend's advertised
capabilities: a backend that cannot do case-insensitive ``includes`` gets an exact match
instead, and ordering by an unsupported field is dropped rather than failing the
request.

Two naming layers meet in this module and both are deliberate:

* The **wire** uses camelCase everywhere -- query parameters (``notificationType``),
  order-by fields (``sentAt``) and capability keys (``orderBy.sentAt``).
* The **Python filter vocabulary** uses snake_case field names (``notification_type``,
  ``sent_at_range``) and snake_case string-lookup values (``starts_with``), because it
  is an in-process API that ``mypy`` checks and developers type by hand. See the module
  docstring of ``vintasend.services.notification_backends.filters``.

The TypeScript reference needs no such translation, since its library is camelCase on
both sides. Everything the dashboard sees is identical either way.
"""

import datetime
from typing import Any, cast

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.notification_backends.filters import (
    NotificationFilterFields,
    NotificationOrderBy,
    StringFieldFilter,
    StringFilterLookup,
)

from .capabilities import supports
from .query import NotificationListQuery


DEFAULT_ORDER_BY_FIELD = "createdAt"
DEFAULT_ORDER_BY_DIRECTION = "desc"

# Wire order-by field -> the Python filter vocabulary's field name.
ORDER_BY_FIELD_TO_PYTHON: dict[str, str] = {
    "sendAfter": "send_after",
    "sentAt": "sent_at",
    "readAt": "read_at",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


def build_string_filter(value: str, capabilities: dict[str, bool]) -> StringFieldFilter:
    """Pick the most precise string lookup the backend supports.

    Case-insensitive ``includes`` when available, otherwise a case-insensitive exact
    match, otherwise a plain equality match (a bare string, which the filter vocabulary
    reads as a case-sensitive ``exact``).
    """
    supports_includes = supports(capabilities, "stringLookups.includes")
    supports_case_insensitive = supports(capabilities, "stringLookups.caseInsensitive")

    if supports_includes:
        lookup: StringFilterLookup = {"lookup": "includes", "value": value}
        if supports_case_insensitive:
            lookup["case_sensitive"] = False
        return lookup

    if supports_case_insensitive:
        return {"lookup": "exact", "value": value, "case_sensitive": False}

    return value


def _date_range(
    start: datetime.datetime | None, end: datetime.datetime | None
) -> dict[str, datetime.datetime]:
    date_range: dict[str, datetime.datetime] = {}
    if start is not None:
        date_range["from"] = start
    if end is not None:
        date_range["to"] = end
    return date_range


def build_backend_filter(
    query: NotificationListQuery, capabilities: dict[str, bool]
) -> NotificationFilterFields:
    """Map the validated query onto the composable filter the backend evaluates.

    Filters are combined with AND, which is what a bare field filter means.
    """
    backend_filter: dict[str, Any] = {}

    if query.status:
        backend_filter["status"] = NotificationStatus(query.status)
    if query.notificationType:
        backend_filter["notification_type"] = NotificationTypes(query.notificationType)
    if query.adapterUsed:
        backend_filter["adapter_used"] = query.adapterUsed
    if query.userId:
        backend_filter["user_id"] = query.userId
    if query.tenant:
        backend_filter["tenant"] = query.tenant

    # Compared against None rather than tested for truthiness: version 0 is a legitimate
    # value the query schema accepts, and `if query.requestedTemplateVersion:` would drop it.
    if query.requestedTemplateVersion is not None:
        backend_filter["requested_template_version"] = query.requestedTemplateVersion
    if query.usedTemplateVersion is not None:
        backend_filter["used_template_version"] = query.usedTemplateVersion

    if query.bodyTemplate:
        backend_filter["body_template"] = build_string_filter(query.bodyTemplate, capabilities)
    if query.subjectTemplate:
        backend_filter["subject_template"] = build_string_filter(
            query.subjectTemplate, capabilities
        )
    if query.contextName:
        backend_filter["context_name"] = build_string_filter(query.contextName, capabilities)

    if query.createdAtFrom or query.createdAtTo:
        backend_filter["created_at_range"] = _date_range(query.createdAtFrom, query.createdAtTo)

    if query.sentAtFrom or query.sentAtTo:
        backend_filter["sent_at_range"] = _date_range(query.sentAtFrom, query.sentAtTo)

    return cast_filter(backend_filter)


def cast_filter(raw: dict[str, Any]) -> NotificationFilterFields:
    """Narrow a dict built key by key to the filter TypedDict.

    ``NotificationFilterFields`` is a ``total=False`` TypedDict, so it cannot be built
    incrementally under ``mypy`` without this one cast.
    """
    return raw  # type: ignore[return-value]


def build_order_by(
    query: NotificationListQuery, capabilities: dict[str, bool]
) -> NotificationOrderBy | None:
    """Resolve ordering, dropping it when the backend says it cannot honour the field.

    Dropping unsupported ordering is the contract's choice; failing the request is not.
    """
    wire_field = query.orderByField or DEFAULT_ORDER_BY_FIELD
    direction = query.orderByDirection or DEFAULT_ORDER_BY_DIRECTION

    if not supports(capabilities, f"orderBy.{wire_field}"):
        return None

    order_by: dict[str, str] = {
        "field": ORDER_BY_FIELD_TO_PYTHON[wire_field],
        "direction": direction,
    }
    # Both values come from closed maps above -- the wire field was validated against
    # the order-by enum, the direction against asc/desc -- so this narrowing is safe.
    return cast("NotificationOrderBy", order_by)
