"""Request validation schemas.

These define, precisely, what the API accepts. They are the Python counterpart of the
TypeScript reference's ``src/domain/schemas.ts`` and reject the same inputs with the
same 400 responses.

Query parameter names are camelCase to match the contract, so the attribute names here
are camelCase too (``ruff``'s N815 is disabled for this module in ``pyproject.toml``).
"""

import datetime
from collections.abc import Callable
from typing import Any, get_args

from ninja import Field, Schema

from pydantic import TypeAdapter, ValidationError, field_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

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

ISO_DATE_MESSAGE = "Must be an ISO-8601 date string"
INTEGER_MESSAGE = "Expected an integer"


def _iso_date(value: Any) -> datetime.datetime:
    """Read an ISO-8601 date string, and nothing else.

    Mirrors the reference's ``z.string().refine(Date.parse)``: a JSON number is refused rather
    than read as a Unix timestamp, which Pydantic would otherwise do. A time without a zone is
    read as UTC, the zone every VintaSend backend writes. Left naive, it could not be compared
    with the service's timezone-aware "now".
    """
    if not isinstance(value, str):
        raise ValueError(ISO_DATE_MESSAGE)
    try:
        parsed = _DATETIME.validate_python(value)
    except ValidationError as error:
        raise ValueError(ISO_DATE_MESSAGE) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _json_integer(value: Any) -> int:
    """Accept a JSON integer only.

    Mirrors the reference's ``z.number().int()``. A string such as ``"3"`` is refused rather
    than coerced, and so are ``true`` and ``null``. A JSON number with an integral value, such
    as ``3.0``, is the same number as ``3`` to a JavaScript client and to the reference, so it
    is accepted.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(INTEGER_MESSAGE)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(INTEGER_MESSAGE)
        return int(value)
    return value


def _version_token(value: Any) -> str:
    """Accept a version token: a JSON string of 1 to 256 characters, kept exactly as sent.

    Opaque, as in the reference's ``z.string().min(1).max(256)``: only its length is checked,
    so a token from any backend passes through untouched. ``null`` is refused, like any other
    value that is not a string.
    """
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    if not 1 <= len(value) <= MAX_VERSION_TOKEN_LENGTH:
        raise ValueError(f"Must be 1 to {MAX_VERSION_TOKEN_LENGTH} characters")
    return value


def _json_boolean(value: Any) -> bool:
    """Accept a JSON boolean only: ``"false"``, ``0`` and ``null`` are refused, not coerced."""
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean")
    return value


# The longest version token a request may carry.
MAX_VERSION_TOKEN_LENGTH = 256


class RequeueBody(Schema):
    """Body of ``POST /notifications/{id}/requeue``. The reference's ``requeueBodySchema``.

    ``sendAfter`` left out keeps the notification's own schedule, and ``null`` sends it as soon
    as it is requeued. The two are told apart with ``model_fields_set``, so the default below
    is never read as a value. ``templateVersion`` pins it in the same write,
    ``expectedVersion`` refuses if it changed since it was listed, and ``send: false`` leaves it
    for the pending sender. Those three take no ``null``: left out is the only way to not send
    one.
    """

    sendAfter: datetime.datetime | None = None
    # `ge=0` because the contract admits version 0, as it does for the list filters.
    templateVersion: int | None = Field(default=None, ge=0)
    expectedVersion: str | None = None
    send: bool | None = None

    @field_validator("sendAfter", mode="before")
    @classmethod
    def _iso_date_string_or_null(cls, value: Any) -> Any:
        return None if value is None else _iso_date(value)

    @field_validator("templateVersion", mode="before")
    @classmethod
    def _template_version(cls, value: Any) -> int:
        return _json_integer(value)

    @field_validator("expectedVersion", mode="before")
    @classmethod
    def _expected_version(cls, value: Any) -> str:
        return _version_token(value)

    @field_validator("send", mode="before")
    @classmethod
    def _send(cls, value: Any) -> bool:
        return _json_boolean(value)


class RepinBody(Schema):
    """Body of ``POST /notifications/{id}/repin``. The reference's ``repinBodySchema``.

    ``templateVersion`` left out pins the version the template renderer reports as current.
    ``ge=0`` because the contract admits version 0, as it does for the list filters.
    ``expectedVersion`` refuses if the notification changed since it was listed.
    """

    templateVersion: int | None = Field(default=None, ge=0)
    expectedVersion: str | None = None

    @field_validator("templateVersion", mode="before")
    @classmethod
    def _template_version(cls, value: Any) -> int:
        return _json_integer(value)

    @field_validator("expectedVersion", mode="before")
    @classmethod
    def _expected_version(cls, value: Any) -> str:
        return _version_token(value)


# --- POST /notifications/versions ----------------------------------------------------------

# How deep `and` / `or` / `not` may nest in a search filter.
MAX_FILTER_DEPTH = 6
# How many sub-filters one `and` or `or` may hold, and how many values one list may.
MAX_FILTER_ITEMS = 50
# The versions search returns IDs only, so it pages larger than the listings.
DEFAULT_VERSIONS_PAGE_SIZE = 100
MAX_VERSIONS_PAGE_SIZE = 1000

FILTER_GROUP_KEYS = ("and", "or", "not")

# A search filter as it arrives on the wire, once validated: the same tree, camelCase field
# names, strings trimmed and ISO dates read into aware datetimes. `filters.to_backend_filter`
# turns it into the core's snake_case `NotificationFilter`.
WireFilter = dict[str, Any]

_Loc = tuple[str | int, ...]


class _FilterIssues:
    """The issues found in one search filter, each at its location inside ``filter``."""

    def __init__(self) -> None:
        self.found: list[InitErrorDetails] = []

    def add(self, loc: _Loc, message: str, value: Any) -> None:
        self.found.append(
            InitErrorDetails(
                type=PydanticCustomError("invalid_filter", message), loc=loc, input=value
            )
        )


def _enum_value(choices: tuple[str, ...]) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"Expected one of {', '.join(choices)}")
        return value

    return check


def _non_empty_string(value: Any) -> str:
    """Trim, then refuse what is left if it is empty: the reference's ``z.string().trim().min(1)``."""
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("String should have at least 1 character")
    return trimmed


def _template_version_value(value: Any) -> int:
    version = _json_integer(value)
    if version < 0:
        raise ValueError("Must be greater than or equal to 0")
    return version


def _one_or_many(check: Callable[[Any], Any]) -> Callable[[Any, _Loc, _FilterIssues], Any]:
    """A field that takes one value, or a list of 1 to ``MAX_FILTER_ITEMS`` of them."""

    def parse(value: Any, loc: _Loc, issues: _FilterIssues) -> Any:
        if not isinstance(value, list):
            return _one(check)(value, loc, issues)
        if not 1 <= len(value) <= MAX_FILTER_ITEMS:
            issues.add(loc, f"Must list 1 to {MAX_FILTER_ITEMS} values", value)
            return None
        return [_one(check)(item, (*loc, index), issues) for index, item in enumerate(value)]

    return parse


def _one(check: Callable[[Any], Any]) -> Callable[[Any, _Loc, _FilterIssues], Any]:
    def parse(value: Any, loc: _Loc, issues: _FilterIssues) -> Any:
        try:
            return check(value)
        except ValueError as error:
            issues.add(loc, str(error), value)
            return None

    return parse


def _date_range(value: Any, loc: _Loc, issues: _FilterIssues) -> Any:
    """``{ from?, to? }`` of ISO-8601 strings, and no other key."""
    if not isinstance(value, dict):
        issues.add(loc, "Expected an object with from and/or to", value)
        return None
    parsed: dict[str, datetime.datetime] = {}
    for key, bound in value.items():
        if key not in ("from", "to"):
            issues.add((*loc, key), "Unknown key", bound)
            continue
        date = _one(_iso_date)(bound, (*loc, key), issues)
        if date is not None:
            parsed[key] = date
    return parsed


_FILTER_FIELDS: dict[str, Callable[[Any, _Loc, _FilterIssues], Any]] = {
    "status": _one_or_many(_enum_value(get_args(NotificationStatus))),
    "notificationType": _one_or_many(_enum_value(get_args(NotificationType))),
    "adapterUsed": _one_or_many(_non_empty_string),
    "userId": _one(_non_empty_string),
    "bodyTemplate": _one(_non_empty_string),
    "subjectTemplate": _one(_non_empty_string),
    "contextName": _one(_non_empty_string),
    "tenant": _one_or_many(_non_empty_string),
    "requestedTemplateVersion": _one_or_many(_template_version_value),
    "usedTemplateVersion": _one_or_many(_template_version_value),
    "hasSendAfter": _one(_json_boolean),
    "hasRequestedTemplateVersion": _one(_json_boolean),
    "sendAfterRange": _date_range,
    "createdAtRange": _date_range,
    "sentAtRange": _date_range,
    "readAtRange": _date_range,
}


def _parse_filter(value: Any, loc: _Loc, nesting: int, issues: _FilterIssues) -> WireFilter:
    """Validate one node of a search filter, returning it with its values read.

    A node is a set of fields (ANDed), or exactly one of ``and`` / ``or`` / ``not``. ``nesting``
    counts the groups around this node. A group past ``MAX_FILTER_DEPTH`` is reported once, at
    the filter itself as the reference does, and not descended into, so a deeply nested body
    cannot make this recurse further than the limit.
    """
    if not isinstance(value, dict):
        issues.add(loc, "Expected a filter object", value)
        return {}

    groups = [key for key in FILTER_GROUP_KEYS if key in value]
    if groups:
        if len(value) != 1:
            issues.add(loc, "A filter is a set of fields, or one of and, or, not on its own", value)
            return {}
        if nesting >= MAX_FILTER_DEPTH:
            issues.add((), f"and/or/not may nest at most {MAX_FILTER_DEPTH} levels deep", value)
            return {}
        operator = groups[0]
        children = value[operator]
        if operator == "not":
            return {"not": _parse_filter(children, (*loc, "not"), nesting + 1, issues)}
        if not isinstance(children, list) or not 1 <= len(children) <= MAX_FILTER_ITEMS:
            issues.add((*loc, operator), f"Must list 1 to {MAX_FILTER_ITEMS} filters", children)
            return {}
        return {
            operator: [
                _parse_filter(child, (*loc, operator, index), nesting + 1, issues)
                for index, child in enumerate(children)
            ]
        }

    fields: WireFilter = {}
    for field, field_value in value.items():
        parse = _FILTER_FIELDS.get(field)
        if parse is None:
            issues.add((*loc, field), "Unknown filter field", field_value)
            continue
        fields[field] = parse(field_value, (*loc, field), issues)
    return fields


def parse_search_filter(value: Any) -> WireFilter:
    """Validate a search filter, raising every issue found with its path inside ``filter``.

    The reference's ``notificationFilterSchema``: strict objects, so an unknown field is a 400
    rather than a filter that silently matches more than asked.

    raises ValidationError: when anything in the filter is invalid.
    """
    issues = _FilterIssues()
    parsed = _parse_filter(value, (), 0, issues)
    if issues.found:
        raise ValidationError.from_exception_data("filter", issues.found)
    return parsed


class NotificationVersionsSearchBody(Schema):
    """Body of ``POST /notifications/versions``. The reference's
    ``notificationVersionsSearchBodySchema``.

    An omitted ``filter`` matches every notification. ``null`` is not an omitted filter, and
    is refused. Keys this schema does not know are ignored, as the reference's ``z.object``
    strips them.
    """

    filter: WireFilter = Field(default_factory=dict)  # noqa: A003
    page: int = Field(default=DEFAULT_PAGE, ge=1)
    pageSize: int = Field(
        default=DEFAULT_VERSIONS_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_VERSIONS_PAGE_SIZE
    )

    @field_validator("filter", mode="before")
    @classmethod
    def _search_filter(cls, value: Any) -> WireFilter:
        return parse_search_filter(value)

    @field_validator("page", "pageSize", mode="before")
    @classmethod
    def _page_number(cls, value: Any) -> int:
        return _json_integer(value)
