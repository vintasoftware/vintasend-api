"""The optional notification actions over HTTP.

Requeue and repin with ``templateVersion``, ``expectedVersion`` and ``send``, the ``{ id, version }``
search at ``POST /notifications/versions``, ``details.reason`` on every refusal, and the action
capabilities. Mirrors the TypeScript reference's ``test/conditional-actions.test.ts``.

Every refusal is checked for what it did not do: the service is not called when the API refuses,
and, through a real VintaSend service, nothing reaches the backend when the service does.
"""

import datetime
from pathlib import Path
from typing import Any, Callable

from django.http import HttpRequest
from django.test import Client

import pytest
from asgiref.sync import async_to_sync
from vintasend.constants import UNSET, NotificationStatus, NotificationTypes
from vintasend.exceptions import NotificationActionError, UnsupportedNotificationActionError
from vintasend.services.dataclasses import NotificationContextDict, NotificationVersion
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_backends.stubs.fake_backend import (
    FakeAsyncIOFileBackend,
    FakeFileBackend,
)
from vintasend.services.notification_service import (
    AsyncIONotificationService,
    NotificationService,
)

from ..errors import ApiError
from ..service import ServiceCaller, set_service_caller
from .fixtures import (
    ALL_ACTION_CAPABILITIES,
    AUTH_HEADERS,
    NO_ACTION_CAPABILITIES,
    CapableFakeService,
    FakeService,
    make_user_notification,
)


pytestmark = pytest.mark.django_db

REQUEUE = "/api/v1/notifications/notif-1/requeue"
REPIN = "/api/v1/notifications/notif-1/repin"
VERSIONS = "/api/v1/notifications/versions"
CAPABILITIES = "/api/v1/capabilities"


def refuse_as_forbidden(request: HttpRequest) -> None:
    raise ApiError("FORBIDDEN", "Not allowed.")


def refusal(reason: Any, action: Any = "requeue") -> NotificationActionError:
    return NotificationActionError(
        f"Refused: {reason}", action=action, reason=reason, notification_id="notif-1"
    )


def issues(response: Any) -> list[dict[str, str]]:
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    return error["details"]["issues"]


def paths(response: Any) -> list[str]:
    return [found["path"] for found in issues(response)]


def assert_unsupported(response: Any) -> None:
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["details"] == {"reason": "UNSUPPORTED"}


def version(notification_id: Any, token: str) -> NotificationVersion:
    return NotificationVersion(id=notification_id, version=token)


# --- POST /notifications/{id}/requeue --------------------------------------------------------


def test_requeue_passes_template_version_expected_version_and_send_to_the_service(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    response = post(
        REQUEUE,
        {"templateVersion": 4, "expectedVersion": "2026-01-01T00:00:00.001Z", "send": False},
    )

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == (
        "notif-1",
        {"template_version": 4, "expected_version": "2026-01-01T00:00:00.001Z", "send": False},
    )


@pytest.mark.parametrize(
    ("body", "passed"),
    [
        ({"templateVersion": 0}, {"template_version": 0}),
        ({"templateVersion": 3.0}, {"template_version": 3}),
        ({"expectedVersion": "v" * 256}, {"expected_version": "v" * 256}),
        ({"send": True}, {"send": True}),
        (
            {"sendAfter": None, "send": False},
            {"send_after": None, "send": False},
        ),
    ],
)
def test_requeue_passes_only_the_options_the_caller_sent(
    post: Callable[..., Any],
    install_service: Callable[..., FakeService],
    body: dict[str, Any],
    passed: dict[str, Any],
) -> None:
    service = install_service(CapableFakeService())

    response = post(REQUEUE, body)

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", passed)


def test_requeue_returns_the_status_the_send_left(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService(requeue_result=make_user_notification(status="FAILED")))

    body = post(REQUEUE).json()

    assert body["data"]["status"] == "FAILED"


@pytest.mark.parametrize("reason", ["CHANGED", "UNSUPPORTED", "INVALID_STATUS"])
def test_requeue_answers_a_refusal_with_a_409_carrying_the_reason(
    post: Callable[..., Any], install_service: Callable[..., FakeService], reason: str
) -> None:
    install_service(CapableFakeService(requeue_error=refusal(reason)))

    response = post(REQUEUE, {"expectedVersion": "v1"})

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "CONFLICT",
            "message": f"Refused: {reason}",
            "details": {"reason": reason},
        }
    }


def test_requeue_carries_the_reason_on_a_404_too(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService(requeue_error=refusal("NOT_FOUND")))

    response = post("/api/v1/notifications/missing/requeue")

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "Refused: NOT_FOUND",
        "details": {"reason": "NOT_FOUND"},
    }


@pytest.mark.parametrize(
    "body",
    [{"templateVersion": 4}, {"expectedVersion": "v1"}, {"send": False}, {"send": True}],
)
def test_requeue_refuses_the_new_options_for_a_service_that_predates_them(
    post: Callable[..., Any], install_service: Callable[..., FakeService], body: dict[str, Any]
) -> None:
    service = install_service(FakeService())

    response = post(REQUEUE, body)

    assert_unsupported(response)
    assert not service.called("requeue_notification")


def test_the_unsupported_refusal_names_every_option_sent(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService())

    response = post(REQUEUE, {"templateVersion": 4, "expectedVersion": "v1", "send": False})

    assert response.json()["error"]["message"] == (
        "The configured service does not support templateVersion, expectedVersion, send; "
        "it predates these options."
    )


def test_requeue_still_works_with_send_after_alone_on_such_a_service(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService())

    response = post(REQUEUE, {"sendAfter": None})

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", {"send_after": None})


class _RequeueWithoutSend(CapableFakeService):
    """Reports action capabilities, but its requeue does not take ``send``."""

    def requeue_notification(  # type: ignore[override]
        self,
        notification_id: Any,
        send_after: Any = UNSET,
        *,
        template_version: Any = None,
        expected_version: Any = None,
    ) -> Any:
        self._record("requeue_notification", notification_id)
        return self.requeue_result


def test_requeue_refuses_an_option_the_service_method_does_not_take(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Passed on, the keyword would raise a ``TypeError`` and reach the client as a 500."""
    service = install_service(_RequeueWithoutSend())

    refused = post(REQUEUE, {"send": False, "templateVersion": 2})
    accepted = post(REQUEUE, {"templateVersion": 2})

    assert_unsupported(refused)
    assert "does not support send;" in refused.json()["error"]["message"]
    assert accepted.status_code == 200
    assert [name for name, _ in service.calls].count("requeue_notification") == 1


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ({"templateVersion": -1}, "templateVersion"),
        ({"templateVersion": 1.5}, "templateVersion"),
        ({"templateVersion": "3"}, "templateVersion"),
        ({"templateVersion": True}, "templateVersion"),
        ({"templateVersion": None}, "templateVersion"),
        ({"expectedVersion": ""}, "expectedVersion"),
        ({"expectedVersion": "v" * 257}, "expectedVersion"),
        ({"expectedVersion": 7}, "expectedVersion"),
        ({"expectedVersion": None}, "expectedVersion"),
        ({"expectedVersion": True}, "expectedVersion"),
        ({"send": "no"}, "send"),
        ({"send": "false"}, "send"),
        ({"send": 0}, "send"),
        ({"send": None}, "send"),
    ],
)
def test_requeue_rejects_an_invalid_option(
    post: Callable[..., Any],
    install_service: Callable[..., FakeService],
    body: dict[str, Any],
    path: str,
) -> None:
    service = install_service(CapableFakeService())

    response = post(REQUEUE, body)

    assert paths(response) == [path]
    assert not service.called("requeue_notification")


# --- POST /notifications/{id}/repin ----------------------------------------------------------


def test_repin_passes_expected_version_to_the_service(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    response = post(REPIN, {"templateVersion": 4, "expectedVersion": "v1"})

    assert response.status_code == 200
    assert service.call_args("repin_notification") == ("notif-1", 4, {"expected_version": "v1"})


def test_repin_passes_no_expected_version_when_none_was_sent(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    post(REPIN)

    assert service.call_args("repin_notification") == ("notif-1", None, {})


@pytest.mark.parametrize(
    "reason", ["CHANGED", "UNSUPPORTED", "INVALID_STATUS", "NO_TEMPLATE_VERSION"]
)
def test_repin_answers_a_refusal_with_a_409_carrying_the_reason(
    post: Callable[..., Any], install_service: Callable[..., FakeService], reason: str
) -> None:
    install_service(CapableFakeService(repin_error=refusal(reason, action="repin")))

    response = post(REPIN, {"expectedVersion": "v1"})

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"reason": reason}


def test_repin_carries_the_reason_on_a_404_too(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService(repin_error=refusal("NOT_FOUND", action="repin")))

    response = post("/api/v1/notifications/missing/repin")

    assert response.status_code == 404
    assert response.json()["error"]["details"] == {"reason": "NOT_FOUND"}


def test_repin_refuses_expected_version_for_a_service_that_predates_it(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService())

    response = post(REPIN, {"templateVersion": 2, "expectedVersion": "v1"})

    assert_unsupported(response)
    assert not service.called("repin_notification")


class _RepinWithoutExpectedVersion(CapableFakeService):
    def repin_notification(  # type: ignore[override]
        self, notification_id: Any, template_version: Any = None
    ) -> Any:
        self._record("repin_notification", notification_id, template_version)
        return self.repin_result


def test_repin_refuses_expected_version_when_its_method_does_not_take_it(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(_RepinWithoutExpectedVersion())

    response = post(REPIN, {"expectedVersion": "v1"})

    assert_unsupported(response)
    assert not service.called("repin_notification")


@pytest.mark.parametrize("expected_version", ["", "v" * 257, 7, None, True, [], {}])
def test_repin_rejects_an_invalid_expected_version(
    post: Callable[..., Any], install_service: Callable[..., FakeService], expected_version: Any
) -> None:
    service = install_service(CapableFakeService())

    response = post(REPIN, {"expectedVersion": expected_version})

    assert paths(response) == ["expectedVersion"]
    assert not service.called("repin_notification")


# --- POST /notifications/versions ------------------------------------------------------------

THE_FILTER = {
    "and": [
        {"bodyTemplate": "welcome", "createdAtRange": {"from": "2026-01-01T00:00:00.000Z"}},
        {
            "or": [
                {
                    "and": [
                        {
                            "status": "PENDING_SEND",
                            "hasSendAfter": True,
                            "hasRequestedTemplateVersion": True,
                        },
                        {"not": {"requestedTemplateVersion": 4}},
                    ]
                },
                {"status": "FAILED"},
            ]
        },
    ]
}

THE_BACKEND_FILTER = {
    "and": [
        {
            "body_template": "welcome",
            "created_at_range": {
                "from": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
            },
        },
        {
            "or": [
                {
                    "and": [
                        {
                            "status": NotificationStatus.PENDING_SEND,
                            "has_send_after": True,
                            "has_requested_template_version": True,
                        },
                        {"not": {"requested_template_version": 4}},
                    ]
                },
                {"status": NotificationStatus.FAILED},
            ]
        },
    ]
}


def test_versions_runs_the_filter_tree_and_returns_ids_and_versions_only(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        CapableFakeService(version_pages=[[version(7, "a"), version("9", "b")], [version(11, "c")]])
    )

    response = post(VERSIONS, {"filter": THE_FILTER, "page": 1, "pageSize": 2})

    assert response.status_code == 200
    assert response.json() == {
        "data": [{"id": "7", "version": "a"}, {"id": "9", "version": "b"}],
        "page": 1,
        "pageSize": 2,
        "hasMore": True,
    }
    calls = [args for name, args in service.calls if name == "filter_notification_versions"]
    # The backend is 1-indexed; the probe for `hasMore` is one-row page 3, the row after page 1.
    assert calls == [(THE_BACKEND_FILTER, 1, 2, None), (THE_BACKEND_FILTER, 3, 1, None)]


def test_versions_translates_every_field_to_the_python_vocabulary(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())
    wire = {
        "status": ["FAILED", "CANCELLED"],
        "notificationType": "EMAIL",
        "adapterUsed": [" smtp "],
        "userId": "42",
        "bodyTemplate": "welcome",
        "subjectTemplate": "subject",
        "contextName": "ctx",
        "tenant": "acme",
        "requestedTemplateVersion": [3, 4.0],
        "usedTemplateVersion": 0,
        "hasSendAfter": False,
        "hasRequestedTemplateVersion": False,
        "sendAfterRange": {"to": "2026-01-02T03:04:05"},
        "createdAtRange": {},
        "sentAtRange": {"from": "2026-01-01T00:00:00+02:00"},
        "readAtRange": {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"},
    }

    response = post(VERSIONS, {"filter": wire})

    utc = datetime.timezone.utc
    assert response.status_code == 200
    assert service.call_args("filter_notification_versions")[0] == {
        "status": [NotificationStatus.FAILED, NotificationStatus.CANCELLED],
        "notification_type": NotificationTypes.EMAIL,
        "adapter_used": ["smtp"],
        "user_id": "42",
        "body_template": "welcome",
        "subject_template": "subject",
        "context_name": "ctx",
        "tenant": "acme",
        "requested_template_version": [3, 4],
        "used_template_version": 0,
        "has_send_after": False,
        "has_requested_template_version": False,
        "send_after_range": {"to": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=utc)},
        "created_at_range": {},
        "sent_at_range": {"from": datetime.datetime(2025, 12, 31, 22, tzinfo=utc)},
        "read_at_range": {
            "from": datetime.datetime(2026, 1, 1, tzinfo=utc),
            "to": datetime.datetime(2026, 2, 1, tzinfo=utc),
        },
    }


@pytest.mark.parametrize("content_type", [None, "text/plain"])
def test_versions_matches_everything_with_no_body_a_page_of_100(
    client: Client, install_service: Callable[..., FakeService], content_type: str | None
) -> None:
    service = install_service(CapableFakeService())
    headers = (
        AUTH_HEADERS if content_type is None else {**AUTH_HEADERS, "Content-Type": content_type}
    )

    response = client.generic("POST", VERSIONS, b"", content_type="", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"data": [], "page": 1, "pageSize": 100, "hasMore": False}
    assert service.call_args("filter_notification_versions") == ({}, 1, 100, None)


def test_an_empty_body_declaring_json_is_malformed(
    client: Client, install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    response = client.generic(
        "POST",
        VERSIONS,
        b"",
        content_type="",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
    )

    assert issues(response) == [{"path": "", "message": "Malformed JSON in request body"}]
    assert not service.called("filter_notification_versions")


def test_versions_pages_a_zero_indexed_backend_in_its_own_numbering(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService(capabilities={"pagination.oneIndexed": False}))

    response = post(VERSIONS, {"page": 3})

    assert response.json()["page"] == 3
    assert service.call_args("filter_notification_versions") == ({}, 2, 100, None)


def test_versions_reads_the_configured_backend(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService(), backend_identifier="replica")

    post(VERSIONS)

    assert service.call_args("filter_notification_versions") == ({}, 1, 100, "replica")
    assert service.call_args("get_backend_supported_action_capabilities") == ("replica",)


def test_a_full_last_page_has_no_more(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService(version_pages=[[version(1, "a")], []]))

    body = post(VERSIONS, {"pageSize": 1}).json()

    assert body["hasMore"] is False


def test_a_short_page_does_not_probe_for_more(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService(version_pages=[[version(1, "a")]]))

    body = post(VERSIONS, {"pageSize": 2}).json()

    assert body["hasMore"] is False
    assert [name for name, _ in service.calls].count("filter_notification_versions") == 1


def test_versions_refuses_a_filter_the_backend_reports_it_cannot_run(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        CapableFakeService(capabilities={"logical.or": False, "negation.sendAfterRange": False})
    )

    response = post(
        VERSIONS,
        {
            "filter": {
                "and": [
                    {"or": [{"status": "FAILED"}, {"status": "CANCELLED"}]},
                    {"not": {"sendAfterRange": {"from": "2026-01-01T00:00:00.000Z"}}},
                ]
            }
        },
    )

    assert response.json() == {
        "error": {
            "code": "BAD_REQUEST",
            "message": "The backend cannot run this filter.",
            "details": {
                "issues": [
                    {
                        "path": "filter.and.0.or",
                        "message": "The backend does not support logical.or.",
                    },
                    {
                        "path": "filter.and.1.not.sendAfterRange",
                        "message": "The backend does not support negation.sendAfterRange.",
                    },
                ]
            },
        }
    }
    assert not service.called("filter_notification_versions")


def test_versions_refuses_presence_filters_a_backend_does_not_report(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The core reports these four ``False`` unless the backend evaluates them. Ignored, a
    presence leaf in an ``and`` would widen the match -- and a bulk move would move more."""
    service = install_service(
        CapableFakeService(
            capabilities={
                "fields.hasSendAfter": False,
                "negation.hasSendAfter": False,
                "fields.hasRequestedTemplateVersion": False,
                "negation.hasRequestedTemplateVersion": False,
                "logical.notNested": False,
            }
        )
    )

    response = post(
        VERSIONS,
        {
            "filter": {
                "and": [
                    {"hasSendAfter": True},
                    {"not": {"hasRequestedTemplateVersion": True}},
                    {"not": {"or": [{"status": "FAILED"}]}},
                ]
            }
        },
    )

    assert issues(response) == [
        {
            "path": "filter.and.0.hasSendAfter",
            "message": "The backend does not support fields.hasSendAfter.",
        },
        {
            "path": "filter.and.1.not.hasRequestedTemplateVersion",
            "message": "The backend does not support fields.hasRequestedTemplateVersion.",
        },
        {
            "path": "filter.and.1.not.hasRequestedTemplateVersion",
            "message": "The backend does not support negation.hasRequestedTemplateVersion.",
        },
        {"path": "filter.and.2.not", "message": "The backend does not support logical.notNested."},
    ]
    assert not service.called("filter_notification_versions")


def _nest(levels: int) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for _ in range(levels):
        nested = {"not": nested}
    return nested


def test_versions_accepts_six_levels_of_nesting(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService())

    assert post(VERSIONS, {"filter": _nest(6)}).status_code == 200


@pytest.mark.parametrize(
    ("body", "expected_paths"),
    [
        ({"filter": {"recipientEmail": "a@b.c"}}, ["filter.recipientEmail"]),
        ({"filter": {"status": "FAILED", "or": [{"status": "SENT"}]}}, ["filter"]),
        ({"filter": {"and": [], "or": []}}, ["filter"]),
        ({"filter": {"or": []}}, ["filter.or"]),
        ({"filter": {"and": {"status": "FAILED"}}}, ["filter.and"]),
        ({"filter": {"and": [{"status": "FAILED"}] * 51}}, ["filter.and"]),
        ({"filter": {"status": ["FAILED"] * 51}}, ["filter.status"]),
        ({"filter": {"status": []}}, ["filter.status"]),
        ({"filter": {"status": "DONE"}}, ["filter.status"]),
        ({"filter": {"status": ["FAILED", "DONE"]}}, ["filter.status.1"]),
        ({"filter": {"notificationType": "FAX"}}, ["filter.notificationType"]),
        ({"filter": {"bodyTemplate": "  "}}, ["filter.bodyTemplate"]),
        ({"filter": {"userId": 42}}, ["filter.userId"]),
        ({"filter": {"requestedTemplateVersion": -1}}, ["filter.requestedTemplateVersion"]),
        ({"filter": {"usedTemplateVersion": [1, 1.5]}}, ["filter.usedTemplateVersion.1"]),
        ({"filter": {"hasSendAfter": "yes"}}, ["filter.hasSendAfter"]),
        ({"filter": {"hasRequestedTemplateVersion": None}}, ["filter.hasRequestedTemplateVersion"]),
        ({"filter": {"sentAtRange": {"from": "yesterday"}}}, ["filter.sentAtRange.from"]),
        ({"filter": {"sentAtRange": {"since": "2026-01-01"}}}, ["filter.sentAtRange.since"]),
        ({"filter": {"sentAtRange": "2026-01-01"}}, ["filter.sentAtRange"]),
        ({"filter": {"not": {"and": [{"status": "FAILED"}, 3]}}}, ["filter.not.and.1"]),
        ({"filter": _nest(7)}, ["filter"]),
        ({"filter": None}, ["filter"]),
        ({"filter": []}, ["filter"]),
        ({"filter": "status=FAILED"}, ["filter"]),
        ({"pageSize": 1001}, ["pageSize"]),
        ({"pageSize": 0}, ["pageSize"]),
        ({"pageSize": 1.5}, ["pageSize"]),
        ({"page": 0}, ["page"]),
        ({"page": "2"}, ["page"]),
        ({"page": True}, ["page"]),
        (
            {"filter": {"tenant": "", "contextName": 3}, "page": 0},
            ["filter.tenant", "filter.contextName", "page"],
        ),
    ],
)
def test_versions_rejects_an_invalid_body(
    post: Callable[..., Any],
    install_service: Callable[..., FakeService],
    body: dict[str, Any],
    expected_paths: list[str],
) -> None:
    service = install_service(CapableFakeService())

    response = post(VERSIONS, body)

    assert paths(response) == expected_paths
    assert not service.called("filter_notification_versions")


def test_versions_reports_nesting_past_six_levels_by_name(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService())

    response = post(VERSIONS, {"filter": {"and": [_nest(6)]}})

    assert issues(response) == [
        {"path": "filter", "message": "and/or/not may nest at most 6 levels deep"}
    ]


def test_versions_ignores_unknown_top_level_keys(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The reference's ``z.object`` strips keys it does not know; the filter itself is strict."""
    install_service(CapableFakeService())

    assert post(VERSIONS, {"orderBy": "id"}).status_code == 200


def test_versions_answers_409_unsupported_for_a_service_that_predates_it(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService())

    response = post(VERSIONS)

    assert_unsupported(response)
    assert response.json()["error"]["message"] == (
        "The configured backend cannot list notification versions (versions.filter)."
    )
    assert not service.called("filter_notification_versions")


def test_versions_answers_409_unsupported_for_a_backend_that_cannot_list_versions(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        CapableFakeService(
            action_capabilities={**ALL_ACTION_CAPABILITIES, "versions.filter": False}
        )
    )

    response = post(VERSIONS, {"filter": {"status": "FAILED"}})

    assert_unsupported(response)
    assert not service.called("filter_notification_versions")


@pytest.mark.parametrize("capability", ["stringLookups.caseSensitive", "stringLookups.exact"])
def test_versions_refuses_exact_string_fields_a_backend_cannot_match_exactly(
    post: Callable[..., Any], install_service: Callable[..., FakeService], capability: str
) -> None:
    """These four go to the core as bare strings, a case-sensitive exact match. A backend on a
    case-insensitive collation would match more, and a bulk move would move more."""
    service = install_service(CapableFakeService(capabilities={capability: False}))

    response = post(
        VERSIONS,
        {
            "filter": {
                "and": [
                    {"userId": "user-1", "status": "FAILED"},
                    {"or": [{"bodyTemplate": "welcome"}, {"subjectTemplate": "welcome"}]},
                    {"not": {"contextName": "welcome_context"}},
                ]
            }
        },
    )

    message = f"The backend does not support {capability}."
    assert issues(response) == [
        {"path": "filter.and.0.userId", "message": message},
        {"path": "filter.and.1.or.0.bodyTemplate", "message": message},
        {"path": "filter.and.1.or.1.subjectTemplate", "message": message},
        {"path": "filter.and.2.not.contextName", "message": message},
    ]
    assert not service.called("filter_notification_versions")


def test_versions_runs_other_fields_on_a_backend_that_cannot_match_strings_exactly(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        CapableFakeService(
            capabilities={"stringLookups.caseSensitive": False, "stringLookups.exact": False}
        )
    )

    response = post(VERSIONS, {"filter": {"status": "FAILED", "requestedTemplateVersion": 4}})

    assert response.status_code == 200
    assert service.called("filter_notification_versions")


def test_versions_answers_a_backend_s_unsupported_error_with_409_unsupported(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    class _Refusing(CapableFakeService):
        def filter_notification_versions(self, *args: Any, **kwargs: Any) -> Any:
            raise UnsupportedNotificationActionError("cannot filter on user_id='user-1'")

    install_service(_Refusing())

    response = post(VERSIONS, {"filter": {"userId": "user-1"}})

    assert_unsupported(response)
    assert response.json()["error"]["message"] == (
        "The configured backend refused to list notification versions for this filter."
    )
    assert "user-1" not in response.content.decode()


def test_versions_answers_any_other_failure_with_a_generic_500(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    class _Failing(CapableFakeService):
        def filter_notification_versions(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("row for Jane Roe, diagnosis-letter")

    install_service(_Failing())

    response = post(VERSIONS)

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred while handling the request.",
        }
    }
    assert "Jane Roe" not in response.content.decode()


def test_versions_requires_an_api_key(
    client: Client, install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    response = client.post(VERSIONS)

    assert response.status_code == 401
    assert service.calls == []


def test_versions_a_host_authenticator_can_refuse_with_403(
    client: Client, settings: Any, install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_forbidden"

    response = client.post(VERSIONS, headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert service.calls == []


def test_versions_refuses_a_non_json_body(
    client: Client, install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService())

    response = client.generic(
        "POST",
        VERSIONS,
        '{"filter":{"status":"FAILED"}}',
        content_type="",
        headers={**AUTH_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert issues(response) == [
        {"path": "", "message": "Send the request body as application/json."}
    ]
    assert not service.called("filter_notification_versions")


# --- GET /capabilities -----------------------------------------------------------------------


def test_capabilities_adds_the_action_capabilities(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(CapableFakeService(capabilities={"logical.or": True}))

    response = get(CAPABILITIES)

    assert response.json() == {"data": {"logical.or": True, **ALL_ACTION_CAPABILITIES}}


def test_capabilities_reports_every_action_false_for_a_service_that_predates_them(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(capabilities={}))

    assert get(CAPABILITIES).json() == {"data": NO_ACTION_CAPABILITIES}


def test_capabilities_keeps_every_action_key_explicit(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """A missing key and a truthy non-boolean both read ``False``: for actions, missing means
    unsupported."""
    install_service(
        CapableFakeService(capabilities={}, action_capabilities={"requeue.templateVersion": "yes"})
    )

    assert get(CAPABILITIES).json() == {"data": NO_ACTION_CAPABILITIES}


def test_capabilities_reports_false_for_an_option_the_service_cannot_be_given(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(_RepinWithoutExpectedVersion(capabilities={}))

    assert get(CAPABILITIES).json() == {
        "data": {**ALL_ACTION_CAPABILITIES, "repin.expectedVersion": False}
    }


def test_capabilities_asks_the_configured_backend(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(CapableFakeService(), backend_identifier="replica")

    get(CAPABILITIES)

    assert service.call_args("get_backend_supported_action_capabilities") == ("replica",)


# --- through a real VintaSend service --------------------------------------------------------


def _create(service: NotificationService, **overrides: Any) -> Any:
    """A notification scheduled far ahead, so creating it sends nothing."""
    kwargs: dict[str, Any] = {
        "user_id": 1,
        "notification_type": NotificationTypes.EMAIL.value,
        "title": "Title",
        "body_template": "welcome",
        "context_name": "conditional_actions_context",
        "context_kwargs": NotificationContextDict({}),
        "send_after": datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc),
        "subject_template": "subject",
        "preheader_template": "preheader",
    }
    kwargs.update(overrides)
    return service.create_notification(**kwargs)


def _row(backend: FakeFileBackend, notification_id: Any) -> tuple[Any, str]:
    """The stored notification and its version token, to show nothing was written."""
    tokens = {str(v.id): v.version for v in backend.filter_notification_versions({}, 1, 1000)}
    return backend.get_notification(notification_id), tokens[str(notification_id)]


@pytest.fixture
def real_backend(tmp_path: Path) -> FakeFileBackend:
    return FakeFileBackend(database_file_name=str(tmp_path / "notifications.json"))


@pytest.fixture
def real_service(real_backend: FakeFileBackend) -> NotificationService:
    # No adapters: `send: false` sends nothing, so none are needed.
    service: NotificationService = NotificationService(
        notification_adapters=[], notification_backend=real_backend
    )
    set_service_caller(ServiceCaller(service))
    return service


def test_the_brief_s_filter_selects_what_it_should_through_a_real_service(
    post: Callable[..., Any], real_backend: FakeFileBackend, real_service: NotificationService
) -> None:
    pinned_old = _create(real_service, requested_template_version=3)
    _create(real_service, requested_template_version=4)
    _create(real_service)
    _create(real_service, send_after=None, requested_template_version=3)
    failed = _create(real_service)
    real_backend.mark_pending_as_failed(failed.id)
    sent = _create(real_service)
    real_backend.mark_pending_as_sent(sent.id)
    _create(real_service, body_template="other", requested_template_version=3)

    response = post(
        VERSIONS,
        {
            "filter": {
                "and": [
                    {"bodyTemplate": "welcome"},
                    {
                        "or": [
                            {
                                "and": [
                                    {"status": "PENDING_SEND"},
                                    {"hasSendAfter": True},
                                    {"hasRequestedTemplateVersion": True},
                                    {"not": {"requestedTemplateVersion": 4}},
                                ]
                            },
                            {"status": "FAILED"},
                        ]
                    },
                ]
            }
        },
    )

    assert response.status_code == 200
    found = {row["id"] for row in response.json()["data"]}
    assert found == {str(pinned_old.id), str(failed.id)}


def test_a_conditional_requeue_through_a_real_service(
    post: Callable[..., Any], real_backend: FakeFileBackend, real_service: NotificationService
) -> None:
    failed = _create(real_service)
    real_backend.mark_pending_as_failed(failed.id)
    [listed] = post(VERSIONS, {"filter": {"status": "FAILED"}}).json()["data"]

    response = post(
        f"/api/v1/notifications/{failed.id}/requeue",
        {"expectedVersion": listed["version"], "templateVersion": 4, "send": False},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "PENDING_SEND"
    assert response.json()["data"]["requestedTemplateVersion"] == 4


def test_a_stale_version_is_refused_with_changed_and_nothing_written(
    post: Callable[..., Any], real_backend: FakeFileBackend, real_service: NotificationService
) -> None:
    failed = _create(real_service)
    real_backend.mark_pending_as_failed(failed.id)
    [listed] = post(VERSIONS).json()["data"]
    real_service.repin_notification(failed.id, 2)
    before = _row(real_backend, failed.id)

    requeue = post(
        f"/api/v1/notifications/{failed.id}/requeue",
        {"expectedVersion": listed["version"], "send": False},
    )
    repin = post(
        f"/api/v1/notifications/{failed.id}/repin",
        {"expectedVersion": listed["version"], "templateVersion": 5},
    )

    for response in (requeue, repin):
        assert response.status_code == 409
        assert response.json()["error"]["details"] == {"reason": "CHANGED"}
        assert "Title" not in response.content.decode()
    assert _row(real_backend, failed.id) == before


def test_a_status_refusal_through_a_real_service_writes_nothing(
    post: Callable[..., Any], real_backend: FakeFileBackend, real_service: NotificationService
) -> None:
    sent = _create(real_service)
    real_backend.mark_pending_as_sent(sent.id)
    before = _row(real_backend, sent.id)

    requeue = post(f"/api/v1/notifications/{sent.id}/requeue", {"send": False})
    repin = post(f"/api/v1/notifications/{sent.id}/repin", {"templateVersion": 5})

    assert requeue.json()["error"]["details"] == {"reason": "INVALID_STATUS"}
    assert repin.json()["error"]["details"] == {"reason": "INVALID_STATUS"}
    assert _row(real_backend, sent.id) == before


def test_an_unknown_id_through_a_real_service_is_a_404_not_found(
    post: Callable[..., Any], real_service: NotificationService
) -> None:
    response = post("/api/v1/notifications/missing/requeue", {"send": False})

    assert response.status_code == 404
    assert response.json()["error"]["details"] == {"reason": "NOT_FOUND"}


def test_capabilities_through_a_real_service(
    get: Callable[..., Any], real_service: NotificationService
) -> None:
    data = get(CAPABILITIES).json()["data"]

    assert {key: data[key] for key in ALL_ACTION_CAPABILITIES} == ALL_ACTION_CAPABILITIES
    assert data["fields.hasSendAfter"] is True


class _NoActionsBackend(FakeFileBackend):
    """A backend written before the optional actions: it reports none and implements none."""

    def requeue_notification(  # type: ignore[override]
        self, notification_id: Any, send_after: Any = UNSET
    ) -> Any:
        return super().requeue_notification(notification_id, send_after)

    repin_notification = BaseNotificationBackend.repin_notification  # type: ignore[assignment]
    filter_notification_versions = BaseNotificationBackend.filter_notification_versions  # type: ignore[assignment]

    def get_action_capabilities(self) -> dict[str, bool]:
        return {}


def test_the_service_s_unsupported_refusal_is_passed_on_with_nothing_written(
    post: Callable[..., Any], get: Callable[..., Any], tmp_path: Path
) -> None:
    """The service knows the options, its backend does not: the service refuses, not the API."""
    backend = _NoActionsBackend(database_file_name=str(tmp_path / "old.json"))
    service: NotificationService = NotificationService(
        notification_adapters=[], notification_backend=backend
    )
    set_service_caller(ServiceCaller(service))
    failed = _create(service)
    backend.mark_pending_as_failed(failed.id)
    before = backend.get_notification(failed.id)

    requeue = post(
        f"/api/v1/notifications/{failed.id}/requeue",
        {"templateVersion": 4, "expectedVersion": "v1"},
    )
    repin = post(f"/api/v1/notifications/{failed.id}/repin", {"expectedVersion": "v1"})
    versions = post(VERSIONS)

    for response in (requeue, repin, versions):
        assert_unsupported(response)
    assert backend.get_notification(failed.id) == before
    assert get(CAPABILITIES).json()["data"]["versions.filter"] is False


def test_an_asyncio_service_is_served_the_same(post: Callable[..., Any], tmp_path: Path) -> None:
    backend = FakeAsyncIOFileBackend(database_file_name=str(tmp_path / "asyncio.json"))
    service: AsyncIONotificationService = AsyncIONotificationService(
        notification_adapters=[], notification_backend=backend
    )
    set_service_caller(ServiceCaller(service))
    created = async_to_sync(service.create_notification)(
        user_id=1,
        notification_type=NotificationTypes.EMAIL.value,
        title="Title",
        body_template="welcome",
        context_name="conditional_actions_context",
        context_kwargs=NotificationContextDict({}),
        send_after=datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc),
        subject_template="subject",
        preheader_template="preheader",
    )
    async_to_sync(backend.mark_pending_as_failed)(created.id)

    [listed] = post(VERSIONS, {"filter": {"status": "FAILED"}}).json()["data"]
    response = post(
        f"/api/v1/notifications/{created.id}/requeue",
        {"expectedVersion": listed["version"], "send": False},
    )

    assert listed["id"] == str(created.id)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "PENDING_SEND"


# --- a replica configured as the API's backend -----------------------------------------------


class _ReplayingReplica(FakeFileBackend):
    """A replica written before 3.8.0's token replication: it neither applies snapshots nor
    takes the primary's token on the writes it replays, so it holds tokens of its own."""

    apply_replication_snapshot_if_newer = (
        BaseNotificationBackend.apply_replication_snapshot_if_newer
    )

    def get_action_capabilities(self) -> dict[str, bool]:
        return {
            **super().get_action_capabilities(),
            "writes.notificationVersion": False,
        }


def _replicated_service(
    tmp_path: Path, replica: FakeFileBackend
) -> tuple[NotificationService, FakeFileBackend]:
    """A service whose primary replicates inline to ``replica``, and the API reading the replica
    (the service names it ``backend-1``)."""
    primary = FakeFileBackend(database_file_name=str(tmp_path / "primary.json"))
    service: NotificationService = NotificationService(
        notification_adapters=[], notification_backend=primary, additional_backends=[replica]
    )
    assert service._get_backend("backend-1") is replica
    set_service_caller(ServiceCaller(service, "backend-1"))
    return service, primary


def _tokens(backend: FakeFileBackend) -> dict[str, str]:
    return {str(v.id): v.version for v in backend.filter_notification_versions({}, 1, 1000)}


def test_a_version_listed_from_a_replica_moves_the_notification_on_the_primary(
    post: Callable[..., Any], tmp_path: Path
) -> None:
    replica = FakeFileBackend(database_file_name=str(tmp_path / "replica.json"))
    service, primary = _replicated_service(tmp_path, replica)
    cancelled = _create(service)
    service.cancel_notification(cancelled.id)
    assert _tokens(replica) == _tokens(primary)

    [listed] = post(VERSIONS, {"filter": {"status": "CANCELLED"}}).json()["data"]
    response = post(
        f"/api/v1/notifications/{cancelled.id}/requeue",
        {"expectedVersion": listed["version"], "templateVersion": 4, "send": False},
    )

    assert response.status_code == 200
    assert primary.get_notification(cancelled.id).status == NotificationStatus.PENDING_SEND.value
    # The replica took the primary's new token with the requeue, so the next listing from it
    # hands out a version the primary accepts.
    assert _tokens(replica) == _tokens(primary)
    assert _tokens(replica)[str(cancelled.id)] != listed["version"]


def test_a_replica_with_tokens_of_its_own_cannot_list_versions(
    get: Callable[..., Any], post: Callable[..., Any], tmp_path: Path
) -> None:
    replica = _ReplayingReplica(database_file_name=str(tmp_path / "replica.json"))
    _replicated_service(tmp_path, replica)

    capabilities = get(CAPABILITIES).json()["data"]
    response = post(VERSIONS)

    # Moves run on the primary, which supports them; versions would come from the replica,
    # whose tokens the primary would always refuse.
    assert capabilities["requeue.expectedVersion"] is True
    assert capabilities["repin.expectedVersion"] is True
    assert capabilities["versions.filter"] is False
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"reason": "UNSUPPORTED"}
