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
    NotificationFilter,
    NotificationFilterFields,
    NotificationOrderBy,
    StringFieldFilter,
    StringFilterLookup,
)

from .capabilities import supports
from .errors import issue
from .query import NotificationListQuery, WireFilter


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


# --- search filters (POST /notifications/versions) --------------------------------------

# Wire filter field -> the Python filter vocabulary's field name.
SEARCH_FIELD_TO_PYTHON: dict[str, str] = {
    "status": "status",
    "notificationType": "notification_type",
    "adapterUsed": "adapter_used",
    "userId": "user_id",
    "bodyTemplate": "body_template",
    "subjectTemplate": "subject_template",
    "contextName": "context_name",
    "tenant": "tenant",
    "requestedTemplateVersion": "requested_template_version",
    "usedTemplateVersion": "used_template_version",
    "hasSendAfter": "has_send_after",
    "hasRequestedTemplateVersion": "has_requested_template_version",
    "sendAfterRange": "send_after_range",
    "createdAtRange": "created_at_range",
    "sentAtRange": "sent_at_range",
    "readAtRange": "read_at_range",
}

# Fields whose wire strings the Python vocabulary takes as enum members.
_ENUM_FIELDS: dict[str, Any] = {
    "status": NotificationStatus,
    "notificationType": NotificationTypes,
}

# Fields sent to the core as bare strings, which it reads as a case-sensitive exact match.
_EXACT_STRING_FIELDS = ("userId", "bodyTemplate", "subjectTemplate", "contextName")
# The string-lookup capabilities a bare string needs. A backend reporting either ``False``
# (a case-insensitive collation, say) would match more than the exact string asked for.
_EXACT_STRING_CAPABILITIES = ("stringLookups.exact", "stringLookups.caseSensitive")


def to_backend_filter(wire_filter: WireFilter) -> NotificationFilter:
    """A validated search filter as the core takes it: the same tree, snake_case fields.

    Strings stay bare, which the vocabulary reads as a case-sensitive exact match: the
    contract says search strings match exactly, so no lookup is negotiated here the way the
    listing does. Dates were already read by the request schema.
    """
    group: dict[str, Any] | None = None
    if "and" in wire_filter or "or" in wire_filter:
        operator = "and" if "and" in wire_filter else "or"
        group = {operator: [to_backend_filter(child) for child in wire_filter[operator]]}
    elif "not" in wire_filter:
        group = {"not": to_backend_filter(wire_filter["not"])}
    if group is not None:
        return cast("NotificationFilter", group)

    fields: dict[str, Any] = {}
    for wire_field, value in wire_filter.items():
        enum = _ENUM_FIELDS.get(wire_field)
        if enum is not None:
            value = [enum(item) for item in value] if isinstance(value, list) else enum(value)
        fields[SEARCH_FIELD_TO_PYTHON[wire_field]] = value
    return cast_filter(fields)


def unsupported_filter_features(
    wire_filter: WireFilter,
    capabilities: dict[str, Any],
    path: str = "filter",
    negated: bool = False,
) -> list[dict[str, str]]:
    """Everything in ``wire_filter`` the backend reports it cannot do, as 400 issues.

    Empty when it can do all of it. Checked up front because a backend refuses such a filter
    with a plain error, which would reach the client as a 500 -- or, worse, ignores it and
    matches more than was asked. Reads ``logical.and``, ``logical.or``, ``logical.not``,
    ``logical.notNested`` (a ``not`` around a group), ``fields.<field>`` and, under a ``not``,
    ``negation.<field>``. A key the report leaves out counts as supported, as the core's
    capability map does; the core reports the presence filters (``fields.hasSendAfter`` and
    the like) as ``False`` unless the backend evaluates them, so those are refused for a
    backend that would ignore them. ``userId``, ``bodyTemplate``, ``subjectTemplate`` and
    ``contextName`` go to the core as bare strings, a case-sensitive exact match, so they also
    read ``stringLookups.exact`` and ``stringLookups.caseSensitive``. The reference's
    ``unsupportedFilterFeatures``.
    """

    def refuses(key: str) -> bool:
        return capabilities.get(key) is False

    def unsupported(at: str, key: str) -> dict[str, str]:
        return issue(at, f"The backend does not support {key}.")

    found: list[dict[str, str]] = []
    for operator in ("and", "or"):
        if operator in wire_filter:
            if refuses(f"logical.{operator}"):
                found.append(unsupported(f"{path}.{operator}", f"logical.{operator}"))
            if negated and refuses("logical.notNested"):
                found.append(unsupported(path, "logical.notNested"))
            for index, child in enumerate(wire_filter[operator]):
                found.extend(
                    unsupported_filter_features(
                        child, capabilities, f"{path}.{operator}.{index}", negated
                    )
                )
            return found

    if "not" in wire_filter:
        if refuses("logical.not"):
            found.append(unsupported(f"{path}.not", "logical.not"))
        if negated and refuses("logical.notNested"):
            found.append(unsupported(path, "logical.notNested"))
        found.extend(
            unsupported_filter_features(wire_filter["not"], capabilities, f"{path}.not", True)
        )
        return found

    for field in wire_filter:
        if refuses(f"fields.{field}"):
            found.append(unsupported(f"{path}.{field}", f"fields.{field}"))
        if negated and refuses(f"negation.{field}"):
            found.append(unsupported(f"{path}.{field}", f"negation.{field}"))
        if field in _EXACT_STRING_FIELDS:
            for key in _EXACT_STRING_CAPABILITIES:
                if refuses(key):
                    found.append(unsupported(f"{path}.{field}", key))
    return found
