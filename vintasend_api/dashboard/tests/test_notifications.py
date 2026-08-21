"""The notification endpoints, asserted against the contract.

Mirrors the TypeScript reference's ``test/notifications.test.ts`` case for case, so a
behaviour change on either side shows up as a test that exists in one suite and not the
other.
"""

import datetime
from typing import Any, Callable

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import NotificationResendError

from .fixtures import (
    FakeService,
    FakeTemplateClient,
    make_one_off_notification,
    make_user_notification,
)


pytestmark = pytest.mark.django_db


# --- GET /api/v1/notifications -------------------------------------------------------


def test_serializes_user_notifications_with_iso_dates_and_a_kind_discriminator(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(filter_results=[make_user_notification()]))

    response = get("/api/v1/notifications")
    body = response.json()

    assert response.status_code == 200
    assert len(body["data"]) == 1
    assert (
        body["data"][0]
        | {
            "kind": "user",
            "id": "notif-1",
            "userId": "user-1",
            "status": "SENT",
            "sentAt": "2024-01-15T10:00:00.000Z",
            "createdAt": "2024-01-15T09:00:00.000Z",
            "updatedAt": "2024-01-15T09:30:00.000Z",
            "sendAfter": None,
            "tenant": "tenant-1",
        }
        == body["data"][0]
    )
    # List payloads stay small: no context blobs.
    assert "contextUsed" not in body["data"][0]
    assert "attachments" not in body["data"][0]


def test_serializes_one_off_notifications_with_their_recipient_fields(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(filter_results=[make_one_off_notification()]))

    body = get("/api/v1/notifications").json()

    assert body["data"][0]["kind"] == "one-off"
    assert body["data"][0]["id"] == "oneoff-1"
    assert body["data"][0]["emailOrPhone"] == "test@example.com"
    assert body["data"][0]["firstName"] == "John"
    assert body["data"][0]["lastName"] == "Doe"
    assert "userId" not in body["data"][0]


def test_defaults_to_page_1_with_page_size_20(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The contract is 1-indexed and so are VintaSend's Python backends, so the page
    number passes through unchanged."""
    service = install_service()

    body = get("/api/v1/notifications").json()

    backend_filter, page, page_size, order_by, backend_identifier = service.call_args(
        "filter_notifications"
    )
    assert backend_filter == {}
    assert page == 1
    assert page_size == 20
    assert order_by == {"field": "created_at", "direction": "desc"}
    assert backend_identifier is None
    assert body["page"] == 1
    assert body["pageSize"] == 20
    assert body["hasMore"] is False


def test_reports_has_more_when_a_full_page_comes_back(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(
        FakeService(
            filter_results=[
                make_user_notification(),
                make_user_notification(id="notif-2"),
            ]
        )
    )

    body = get("/api/v1/notifications?pageSize=2").json()

    assert body["hasMore"] is True


def test_maps_filters_onto_the_backend_filter_negotiating_string_lookups(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    get(
        "/api/v1/notifications?status=SENT&notificationType=EMAIL&adapterUsed=mailgun"
        "&userId=user-1&tenant=tenant-1&bodyTemplate=welcome"
        "&createdAtFrom=2024-01-01T00:00:00.000Z&sentAtTo=2024-02-01T00:00:00.000Z"
    )

    backend_filter = service.call_args("filter_notifications")[0]

    assert backend_filter == {
        "status": NotificationStatus.SENT,
        "notification_type": NotificationTypes.EMAIL,
        "adapter_used": "mailgun",
        "user_id": "user-1",
        "tenant": "tenant-1",
        "body_template": {"lookup": "includes", "value": "welcome", "case_sensitive": False},
        "created_at_range": {"from": datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)},
        "sent_at_range": {"to": datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc)},
    }


def test_falls_back_to_an_exact_match_when_the_backend_cannot_do_includes(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        FakeService(
            capabilities={
                "stringLookups.includes": False,
                "stringLookups.caseInsensitive": True,
            }
        )
    )

    get("/api/v1/notifications?contextName=welcome")

    assert service.call_args("filter_notifications")[0] == {
        "context_name": {"lookup": "exact", "value": "welcome", "case_sensitive": False}
    }


def test_falls_back_to_plain_equality_when_the_backend_can_do_neither(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        FakeService(
            capabilities={
                "stringLookups.includes": False,
                "stringLookups.caseInsensitive": False,
            }
        )
    )

    get("/api/v1/notifications?contextName=welcome")

    # A bare string is read by the filter vocabulary as a case-sensitive exact match.
    assert service.call_args("filter_notifications")[0] == {"context_name": "welcome"}


def test_a_backend_that_cannot_fold_case_gets_a_case_sensitive_lookup(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        FakeService(
            capabilities={
                "stringLookups.includes": True,
                "stringLookups.caseInsensitive": False,
            }
        )
    )

    get("/api/v1/notifications?contextName=welcome")

    assert service.call_args("filter_notifications")[0] == {
        "context_name": {"lookup": "includes", "value": "welcome"}
    }


def test_case_insensitivity_is_never_inferred_from_the_case_sensitive_capability(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """``stringLookups.caseSensitive`` and ``stringLookups.caseInsensitive`` are separate
    capabilities, not a flag and its negation.

    A backend on a case-insensitive collation reports ``caseSensitive: False`` -- it can
    match case-insensitively and nothing else. Treating that as ``caseInsensitive: False``
    would decline the one lookup it actually supports.
    """
    service = install_service(
        FakeService(
            capabilities={
                "stringLookups.includes": True,
                "stringLookups.caseSensitive": False,
            }
        )
    )

    get("/api/v1/notifications?contextName=welcome")

    assert service.call_args("filter_notifications")[0] == {
        "context_name": {"lookup": "includes", "value": "welcome", "case_sensitive": False}
    }


def test_drops_ordering_the_backend_does_not_support_instead_of_failing(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService(capabilities={"orderBy.sentAt": False}))

    response = get("/api/v1/notifications?orderByField=sentAt")

    assert response.status_code == 200
    assert service.call_args("filter_notifications")[3] is None


def test_maps_wire_order_by_fields_onto_the_python_filter_vocabulary(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    get("/api/v1/notifications?orderByField=sendAfter&orderByDirection=asc")

    assert service.call_args("filter_notifications")[3] == {
        "field": "send_after",
        "direction": "asc",
    }


def test_forwards_the_configured_backend_identifier(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(backend_identifier="replica")

    get("/api/v1/notifications")

    assert service.call_args("get_backend_supported_filter_capabilities") == ("replica",)
    assert service.call_args("filter_notifications")[4] == "replica"


def test_rejects_invalid_query_parameters_with_a_400_and_field_details(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = get("/api/v1/notifications?status=NOPE")
    body = response.json()

    assert response.status_code == 400
    assert body["error"]["code"] == "BAD_REQUEST"
    assert body["error"]["details"]["issues"][0]["path"] == "status"


def test_rejects_a_page_size_above_the_maximum(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    assert get("/api/v1/notifications?pageSize=500").status_code == 400


def test_rejects_a_blank_string_filter(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    assert get("/api/v1/notifications?userId=%20%20").status_code == 400


# --- collection shortcuts ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method", "results_attr"),
    [
        ("/api/v1/notifications/pending", "get_pending_notifications", "pending_results"),
        ("/api/v1/notifications/future", "get_future_notifications", "future_results"),
    ],
)
def test_collection_shortcuts_delegate_to_their_service_method(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    path: str,
    method: str,
    results_attr: str,
) -> None:
    service = install_service(FakeService(**{results_attr: [make_user_notification()]}))

    response = get(f"{path}?page=2&pageSize=5")
    body = response.json()

    assert response.status_code == 200
    assert service.call_args(method) == (2, 5, None)
    assert body["page"] == 2
    assert body["pageSize"] == 5


def test_one_off_listing_uses_a_native_service_method_when_there_is_one(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = FakeService()

    def get_one_off_notifications(page: int, page_size: int, backend_identifier: Any = None) -> Any:
        service.calls.append(("get_one_off_notifications", (page, page_size, backend_identifier)))
        return [make_one_off_notification()]

    service.get_one_off_notifications = get_one_off_notifications  # type: ignore[attr-defined]
    install_service(service)

    body = get("/api/v1/notifications/one-off?page=2&pageSize=5").json()

    assert service.call_args("get_one_off_notifications") == (2, 5, None)
    assert body["data"][0]["kind"] == "one-off"


def test_one_off_listing_falls_back_to_filtering_the_notification_stream(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The Python library has no one-off listing method, so the API scans and filters.

    Only the one-offs come back, and user notifications in the same stream are skipped
    without consuming a slot in the requested page.
    """
    install_service(
        FakeService(
            filter_results=[
                make_user_notification(id="user-a"),
                make_one_off_notification(id="oneoff-a"),
                make_user_notification(id="user-b"),
                make_one_off_notification(id="oneoff-b"),
            ]
        )
    )

    body = get("/api/v1/notifications/one-off?pageSize=2").json()

    assert [row["id"] for row in body["data"]] == ["oneoff-a", "oneoff-b"]
    assert all(row["kind"] == "one-off" for row in body["data"])


def test_shortcut_routes_do_not_shadow_the_detail_route(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(notification=make_user_notification(id="pending-lookalike")))

    body = get("/api/v1/notifications/pending-lookalike").json()

    assert body["data"]["id"] == "pending-lookalike"


# --- GET /api/v1/notifications/{id} --------------------------------------------------


def test_detail_returns_the_context_payloads(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(notification=make_user_notification()))

    body = get("/api/v1/notifications/notif-1").json()

    assert body["data"]["kind"] == "user"
    assert body["data"]["contextUsed"] == {"key": "value"}
    assert body["data"]["contextParameters"] == {"param": "test"}
    assert body["data"]["extraParams"] is None
    assert body["data"]["attachments"] == []


def test_detail_serves_one_off_notifications_too(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """``get_notification`` returns whichever variant matches the id, so the contract's
    "user first, then one-off" lookup is one call here rather than two."""
    install_service(FakeService(notification=make_one_off_notification()))

    body = get("/api/v1/notifications/oneoff-1").json()

    assert body["data"]["kind"] == "one-off"
    assert body["data"]["emailOrPhone"] == "test@example.com"


def test_detail_404s_when_the_notification_does_not_exist(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = get("/api/v1/notifications/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- template versions ---------------------------------------------------------------
#
# Which version of a template a notification renders is a property of the notification,
# not of the template store: `requestedTemplateVersion` is what was pinned when it was
# created or updated, `usedTemplateVersion` is what the renderer reported once it went
# out. Both are null for a service whose renderer has no versions, which is every
# file-based one -- so the dashboard has to treat null as "not applicable", not as zero.


def test_list_rows_carry_the_template_versions(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(filter_results=[make_user_notification()]))

    row = get("/api/v1/notifications").json()["data"][0]

    assert row["requestedTemplateVersion"] == 3
    assert row["usedTemplateVersion"] == 3


def test_filtering_by_the_requested_template_version(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    get("/api/v1/notifications?requestedTemplateVersion=3")

    assert service.call_args("filter_notifications")[0] == {"requested_template_version": 3}


def test_filtering_by_the_used_template_version(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The audit question: which notifications actually went out on version 3?"""
    service = install_service()

    get("/api/v1/notifications?usedTemplateVersion=3")

    assert service.call_args("filter_notifications")[0] == {"used_template_version": 3}


def test_the_two_version_filters_combine_with_the_rest(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    get("/api/v1/notifications?tenant=acme&requestedTemplateVersion=2&usedTemplateVersion=3")

    assert service.call_args("filter_notifications")[0] == {
        "tenant": "acme",
        "requested_template_version": 2,
        "used_template_version": 3,
    }


def test_version_zero_is_forwarded_rather_than_dropped_as_falsy(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Version numbering belongs to the renderer, so 0 is a value and not an absence."""
    service = install_service()

    get("/api/v1/notifications?requestedTemplateVersion=0")

    assert service.call_args("filter_notifications")[0] == {"requested_template_version": 0}


def test_omitting_the_version_filters_leaves_them_out_of_the_backend_filter(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    get("/api/v1/notifications")

    assert service.call_args("filter_notifications")[0] == {}


def test_rejects_a_negative_template_version_with_a_400(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = get("/api/v1/notifications?usedTemplateVersion=-1")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_rejects_a_non_numeric_template_version_with_a_400(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = get("/api/v1/notifications?requestedTemplateVersion=latest")

    assert response.status_code == 400


def test_a_backend_declining_the_version_fields_says_so_to_the_dashboard(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """`fields.*` reaches the dashboard untouched, which is how it greys the control out.

    This API does not drop an unsupported field filter server-side -- it does that only for
    ordering -- so a backend that cannot evaluate these publishes the fact and the UI is
    what stops offering them.
    """
    install_service(
        FakeService(
            capabilities={
                "fields.requestedTemplateVersion": False,
                "fields.usedTemplateVersion": False,
            }
        )
    )

    capabilities = get("/api/v1/capabilities").json()["data"]

    assert capabilities["fields.requestedTemplateVersion"] is False
    assert capabilities["fields.usedTemplateVersion"] is False


def test_the_detail_payload_carries_them_too(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(notification=make_user_notification()))

    body = get("/api/v1/notifications/notif-1").json()

    assert body["data"]["requestedTemplateVersion"] == 3
    assert body["data"]["usedTemplateVersion"] == 3


def test_one_off_notifications_carry_them_as_well(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(notification=make_one_off_notification()))

    body = get("/api/v1/notifications/oneoff-1").json()

    assert body["data"]["requestedTemplateVersion"] == 3
    assert body["data"]["usedTemplateVersion"] == 3


def test_an_unpinned_notification_reports_only_what_it_rendered(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """No version was asked for, so the renderer took the latest -- and said which."""
    install_service(
        FakeService(
            notification=make_user_notification(
                requested_template_version=None, used_template_version=7
            )
        )
    )

    body = get("/api/v1/notifications/notif-1").json()

    assert body["data"]["requestedTemplateVersion"] is None
    assert body["data"]["usedTemplateVersion"] == 7


def test_a_notification_that_has_not_been_sent_has_no_used_version_yet(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(
        FakeService(
            notification=make_user_notification(
                status="PENDING_SEND",
                sent_at=None,
                requested_template_version=3,
                used_template_version=None,
            )
        )
    )

    body = get("/api/v1/notifications/notif-1").json()

    assert body["data"]["requestedTemplateVersion"] == 3
    assert body["data"]["usedTemplateVersion"] is None


def test_a_service_whose_renderer_has_no_versions_reports_null_rather_than_omitting(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The fields are always present -- the contract has no optional keys here."""
    install_service(
        FakeService(
            filter_results=[
                make_user_notification(requested_template_version=None, used_template_version=None)
            ]
        )
    )

    row = get("/api/v1/notifications").json()["data"][0]

    assert row["requestedTemplateVersion"] is None
    assert row["usedTemplateVersion"] is None


# --- GET /api/v1/notifications/{id}/preview ------------------------------------------


def test_preview_renders_templates_fetched_at_the_notification_commit(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    install_service(FakeService(notification=make_user_notification()))
    template_client = install_template_client()

    body = get("/api/v1/notifications/notif-1/preview").json()

    assert template_client.call_args("get_template_content_by_commit") == (
        "emails/body.html",
        "abc123",
    )
    assert body["data"] == {
        "gitCommitSha": "abc123",
        "bodyTemplatePath": "emails/body.html",
        "subjectTemplatePath": "emails/subject.txt",
        "renderedBodyHtml": "<p>Body</p>",
        "renderedSubjectHtml": "<h1>Subject</h1>",
    }


def test_preview_renders_the_stored_context_when_one_is_present(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    service = install_service(FakeService(notification=make_user_notification()))
    install_template_client()

    get("/api/v1/notifications/notif-1/preview")

    _, template_content, context = service.call_args("render_email_template_from_content")
    assert context == {"key": "value"}
    assert template_content.body_template == "Hello {{ name }}"
    assert template_content.subject_template == "Hello {{ name }}"
    assert not service.called("get_notification_context")


def test_preview_regenerates_the_context_when_the_notification_has_none(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    service = install_service(FakeService(notification=make_user_notification(context_used=None)))
    install_template_client()

    get("/api/v1/notifications/notif-1/preview")

    assert service.called("get_notification_context")
    assert service.call_args("render_email_template_from_content")[2] == {"generated": True}


def test_preview_uses_the_latest_main_commit_for_pending_notifications_without_a_sha(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    install_service(
        FakeService(notification=make_user_notification(git_commit_sha=None, status="PENDING_SEND"))
    )
    template_client = install_template_client()

    body = get("/api/v1/notifications/notif-1/preview").json()

    assert template_client.called("get_latest_main_commit_sha")
    assert body["data"]["gitCommitSha"] == "main-sha"


def test_preview_409s_when_a_sent_notification_has_no_tracked_commit(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    install_service(FakeService(notification=make_user_notification(git_commit_sha=None)))
    install_template_client()

    response = get("/api/v1/notifications/notif-1/preview")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PREVIEW_UNAVAILABLE"


def test_preview_skips_the_subject_fetch_when_there_is_no_subject_template(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    install_service(FakeService(notification=make_user_notification(subject_template="")))
    template_client = install_template_client()

    body = get("/api/v1/notifications/notif-1/preview").json()

    assert template_client.call_count("get_template_content_by_commit") == 1
    assert body["data"]["subjectTemplatePath"] is None


def test_preview_502s_when_the_template_source_cannot_be_reached(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    install_template_client: Callable[..., FakeTemplateClient],
) -> None:
    from ..template_source import TemplateSourceError

    install_service(FakeService(notification=make_user_notification()))
    install_template_client(
        FakeTemplateClient(content_error=TemplateSourceError("GitHub request failed"))
    )

    response = get("/api/v1/notifications/notif-1/preview")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_preview_reports_an_internal_error_without_leaking_backend_details(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(notification_error=RuntimeError("db credentials rejected")))

    response = get("/api/v1/notifications/notif-1/preview")
    body = response.json()

    assert response.status_code == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "db credentials" not in response.content.decode()


# --- POST /api/v1/notifications/{id}/resend ------------------------------------------


def test_resend_with_the_stored_context_returns_the_new_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService(resend_result=make_user_notification(id="notif-2")))

    response = post("/api/v1/notifications/notif-1/resend", {"useStoredContext": True})
    body = response.json()

    assert response.status_code == 201
    assert service.call_args("resend_notification") == ("notif-1", True)
    assert body["data"]["kind"] == "user"
    assert body["data"]["id"] == "notif-2"


def test_resend_defaults_use_stored_context_to_false_when_the_body_is_empty(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService(resend_result=make_user_notification()))

    post("/api/v1/notifications/notif-1/resend")

    assert service.call_args("resend_notification") == ("notif-1", False)


def test_resend_409s_when_the_notification_does_not_exist(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = post("/api/v1/notifications/notif-1/resend")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_resend_409s_when_the_service_refuses(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """A one-off, or one still scheduled in the future, raises ``NotificationResendError``."""
    install_service(
        FakeService(resend_error=NotificationResendError("One-off notifications cannot be resent"))
    )

    response = post("/api/v1/notifications/notif-1/resend")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_resend_surfaces_a_send_failure_rather_than_calling_it_a_conflict(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """A dead mail host is not "this cannot be resent"; reporting it as a 409 would tell
    the dashboard the wrong thing."""
    install_service(FakeService(resend_error=RuntimeError("smtp unreachable")))

    response = post("/api/v1/notifications/notif-1/resend")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_resend_rejects_a_malformed_body(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    response = post("/api/v1/notifications/notif-1/resend", {"useStoredContext": "yes"})

    assert response.status_code == 400


# --- POST /api/v1/notifications/{id}/cancel ------------------------------------------


def test_cancel_a_pending_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(
        FakeService(notification=make_user_notification(status="PENDING_SEND"))
    )

    response = post("/api/v1/notifications/notif-1/cancel")

    assert response.status_code == 200
    assert service.call_args("cancel_notification") == ("notif-1",)
    assert response.json() == {"data": {"id": "notif-1", "status": "CANCELLED"}}


def test_cancel_409s_when_the_notification_is_not_pending(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(FakeService(notification=make_user_notification(status="SENT")))

    response = post("/api/v1/notifications/notif-1/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert not service.called("cancel_notification")


def test_cancel_404s_for_an_unknown_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service()

    assert post("/api/v1/notifications/missing/cancel").status_code == 404


# --- GET /api/v1/capabilities --------------------------------------------------------


def test_capabilities_returns_the_backend_capability_map(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(capabilities={"orderBy.sentAt": False}))

    body = get("/api/v1/capabilities").json()

    assert body == {"data": {"orderBy.sentAt": False}}


def test_a_zero_indexed_backend_gets_zero_indexed_pages(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The wire stays 1-indexed; the conversion is read from the backend, not assumed.

    A `vintasend-ts`-style backend pages from 0, and nothing raises if this is wrong --
    the client just silently gets the wrong page.
    """
    service = install_service(FakeService(capabilities={"pagination.oneIndexed": False}))

    get("/api/v1/notifications?page=3")

    assert service.call_args("filter_notifications")[1] == 2


def test_a_backend_that_says_nothing_is_treated_as_one_indexed(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Missing key means the library's documented convention, which every VintaSend
    Python backend follows."""
    service = install_service(FakeService(capabilities={}))

    get("/api/v1/notifications?page=3")

    assert service.call_args("filter_notifications")[1] == 3


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/notifications/pending", "get_pending_notifications"),
        ("/api/v1/notifications/future", "get_future_notifications"),
    ],
)
def test_the_collection_shortcuts_convert_pages_too(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    path: str,
    method: str,
) -> None:
    """A backend has one pagination convention across every paginated method, so the
    conversion cannot live only on the filter route."""
    service = install_service(FakeService(capabilities={"pagination.oneIndexed": False}))

    get(f"{path}?page=4")

    assert service.call_args(method)[0] == 3


def test_the_pagination_convention_is_not_published_to_the_dashboard(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The wire contract is unconditionally 1-indexed and this API does the conversion,
    so telling a client about the backend's convention could only make it convert twice."""
    install_service(
        FakeService(capabilities={"pagination.oneIndexed": False, "orderBy.sentAt": True})
    )

    body = get("/api/v1/capabilities").json()

    assert body == {"data": {"orderBy.sentAt": True}}


def test_the_whole_pagination_namespace_is_withheld_from_the_dashboard(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Filtered as a namespace, not key by key, so a `pagination.*` key added to either
    library later is withheld without an edit here -- otherwise this API and
    `vintasend-ts-api` would start publishing different maps to the same dashboard."""
    install_service(
        FakeService(
            capabilities={
                "pagination.oneIndexed": True,
                "pagination.somethingAddedLater": False,
                "orderBy.sentAt": True,
            }
        )
    )

    body = get("/api/v1/capabilities").json()

    assert body == {"data": {"orderBy.sentAt": True}}


def test_the_page_response_still_reports_the_requested_page(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Whatever the backend was asked for, the envelope echoes the client's own page."""
    install_service(FakeService(capabilities={"pagination.oneIndexed": False}))

    body = get("/api/v1/notifications?page=3").json()

    assert body["page"] == 3


def test_the_backend_capability_report_is_fetched_once_per_process(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Capabilities are a static property of a backend implementation, and the pagination
    conversion needs them on every paginated route -- so they are cached rather than
    re-fetched per request."""
    service = install_service()

    get("/api/v1/notifications")
    get("/api/v1/notifications/pending")
    get("/api/v1/capabilities")

    fetches = [
        name for name, _ in service.calls if name == "get_backend_supported_filter_capabilities"
    ]
    assert len(fetches) == 1


def test_capabilities_passes_both_case_sensitivity_keys_through_unchanged(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """Both libraries spell these the same, and they say different things, so neither is
    rewritten or derived from the other on the way out."""
    install_service(
        FakeService(
            capabilities={
                "stringLookups.caseSensitive": False,
                "stringLookups.caseInsensitive": True,
            }
        )
    )

    body = get("/api/v1/capabilities").json()

    assert body == {
        "data": {
            "stringLookups.caseSensitive": False,
            "stringLookups.caseInsensitive": True,
        }
    }
