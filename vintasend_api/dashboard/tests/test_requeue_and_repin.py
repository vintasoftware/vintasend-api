"""``POST /notifications/{id}/requeue`` and ``POST /notifications/{id}/repin``.

Mirrors the TypeScript reference's ``test/notifications.test.ts`` for the two operations, and
covers every status ``openapi.yaml`` declares for each of them: 200, 400, 401, 403, 404 and
409. The test client also fails any response whose status the operation does not declare.
"""

import datetime
from typing import Any, Callable

from django.http import HttpRequest
from django.test import Client

import pytest
from vintasend.exceptions import NotificationActionError

from ..errors import ApiError
from .fixtures import AUTH_HEADERS, FakeService, make_one_off_notification, make_user_notification


pytestmark = pytest.mark.django_db

REQUEUE = "/api/v1/notifications/notif-1/requeue"
REPIN = "/api/v1/notifications/notif-1/repin"


def refuse_as_forbidden(request: HttpRequest) -> None:
    raise ApiError("FORBIDDEN", "Not allowed.")


def action_error(reason: Any, action: Any = "requeue") -> NotificationActionError:
    return NotificationActionError(
        f"Refused: {reason}", action=action, reason=reason, notification_id="notif-1"
    )


def send(client: Client, path: str, body: bytes | str, content_type: str | None) -> Any:
    """POST raw bytes with exactly the ``Content-Type`` given, or none at all."""
    headers = (
        AUTH_HEADERS if content_type is None else {**AUTH_HEADERS, "Content-Type": content_type}
    )
    return client.generic("POST", path, body, content_type="", headers=headers)


def issues(response: Any) -> list[dict[str, str]]:
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    return error["details"]["issues"]


# --- both operations -----------------------------------------------------------------------


@pytest.mark.parametrize("path", [REQUEUE, REPIN])
def test_requires_an_api_key(
    client: Client, install_service: Callable[..., FakeService], path: str
) -> None:
    service = install_service()

    response = client.post(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert service.calls == []


@pytest.mark.parametrize("path", [REQUEUE, REPIN])
def test_a_host_authenticator_can_refuse_with_403(
    client: Client,
    settings: Any,
    install_service: Callable[..., FakeService],
    path: str,
) -> None:
    service = install_service()
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_forbidden"

    response = client.post(path, headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "FORBIDDEN", "message": "Not allowed."}}
    assert service.calls == []


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        # What `curl -d` sends: read as `{}`, it would requeue with the notification's own
        # schedule, or repin to the current version, instead of what the caller asked for.
        (REQUEUE, '{"sendAfter":null}', "application/x-www-form-urlencoded"),
        (REPIN, '{"templateVersion":2}', "application/x-www-form-urlencoded"),
        (REQUEUE, '{"sendAfter":null}', None),
        (REPIN, '{"templateVersion":2}', "text/plain"),
    ],
)
def test_a_non_empty_body_without_a_json_media_type_is_refused(
    client: Client,
    install_service: Callable[..., FakeService],
    path: str,
    body: str,
    content_type: str | None,
) -> None:
    service = install_service()

    response = send(client, path, body, content_type)

    assert issues(response) == [
        {"path": "", "message": "Send the request body as application/json."}
    ]
    assert not service.called("requeue_notification")
    assert not service.called("repin_notification")


@pytest.mark.parametrize("path", [REQUEUE, REPIN])
@pytest.mark.parametrize("body", ["", '{"sendAfter":'])
def test_a_json_media_type_must_carry_valid_json(
    client: Client, install_service: Callable[..., FakeService], path: str, body: str
) -> None:
    service = install_service()

    response = send(client, path, body, "application/json")

    assert issues(response) == [{"path": "", "message": "Malformed JSON in request body"}]
    assert service.calls == []


@pytest.mark.parametrize("path", [REQUEUE, REPIN])
@pytest.mark.parametrize("body", ["null", "[]", "3"])
def test_a_json_body_that_is_not_an_object_is_refused(
    client: Client, install_service: Callable[..., FakeService], path: str, body: str
) -> None:
    service = install_service()

    response = send(client, path, body, "application/json")

    assert issues(response) == [{"path": "", "message": "The request body must be a JSON object."}]
    assert service.calls == []


# --- POST /notifications/{id}/requeue --------------------------------------------------------


def test_requeue_returns_the_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    response = post(REQUEUE)
    body = response.json()

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", {})
    assert body["data"]["kind"] == "user"
    assert body["data"]["id"] == "notif-1"
    assert body["data"]["status"] == "PENDING_SEND"
    assert body["data"]["sentAt"] is None


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_an_empty_body_is_an_omitted_body(
    client: Client, install_service: Callable[..., FakeService], content_type: str | None
) -> None:
    service = install_service()

    response = send(client, REQUEUE, b"", content_type)

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", {})


def test_an_empty_object_keeps_the_notification_s_own_schedule(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    response = post(REQUEUE, {})

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", {})


def test_send_after_is_passed_through_as_a_date(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    post(REQUEUE, {"sendAfter": "2999-01-01T00:00:00.000Z"})

    assert service.call_args("requeue_notification") == (
        "notif-1",
        {"send_after": datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)},
    )


def test_a_send_after_without_a_zone_is_read_as_utc(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    post(REQUEUE, {"sendAfter": "2999-01-01T09:30:00"})

    _, kwargs = service.call_args("requeue_notification")
    assert kwargs["send_after"] == datetime.datetime(
        2999, 1, 1, 9, 30, tzinfo=datetime.timezone.utc
    )


def test_a_null_send_after_is_passed_through_meaning_now(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    post(REQUEUE, {"sendAfter": None})

    assert service.call_args("requeue_notification") == ("notif-1", {"send_after": None})


@pytest.mark.parametrize("send_after", ["tomorrow-ish", "", 1700000000, True, {}, []])
def test_a_send_after_that_is_not_an_iso_date_string_is_refused(
    post: Callable[..., Any], install_service: Callable[..., FakeService], send_after: Any
) -> None:
    service = install_service()

    response = post(REQUEUE, {"sendAfter": send_after})

    assert [issue["path"] for issue in issues(response)] == ["sendAfter"]
    assert not service.called("requeue_notification")


def test_unknown_fields_are_ignored(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    """The reference's ``z.object`` strips keys it does not know."""
    service = install_service()

    response = post(REQUEUE, {"useStoredContext": True})

    assert response.status_code == 200
    assert service.call_args("requeue_notification") == ("notif-1", {})


def test_a_requeued_notification_serializes_exactly_like_a_list_row(
    get: Callable[..., Any],
    post: Callable[..., Any],
    install_service: Callable[..., FakeService],
) -> None:
    requeued = make_user_notification(
        status="PENDING_SEND",
        sent_at=None,
        send_after=datetime.datetime(2999, 1, 1, 8, tzinfo=datetime.timezone.utc),
    )
    install_service(FakeService(requeue_result=requeued, filter_results=[requeued]))

    listed = get("/api/v1/notifications").json()["data"][0]
    data = post(REQUEUE).json()["data"]

    assert data == listed
    assert data["sendAfter"] == "2999-01-01T08:00:00.000Z"
    assert data["createdAt"] == "2024-01-15T09:00:00.000Z"
    assert data["sentAt"] is None
    assert data["readAt"] is None
    assert "contextUsed" not in data


def test_a_requeued_one_off_notification_serializes_as_one_off(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(requeue_result=make_one_off_notification(status="PENDING_SEND")))

    data = post(REQUEUE).json()["data"]

    assert data["kind"] == "one-off"
    assert data["emailOrPhone"] == "test@example.com"
    assert "userId" not in data


def test_requeue_404s_when_the_service_finds_no_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(requeue_error=action_error("NOT_FOUND")))

    response = post("/api/v1/notifications/missing/requeue")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Refused: NOT_FOUND",
            "details": {"reason": "NOT_FOUND"},
        }
    }


def test_requeue_409s_when_the_status_does_not_allow_it(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(requeue_error=action_error("INVALID_STATUS")))

    response = post(REQUEUE)

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "CONFLICT",
            "message": "Refused: INVALID_STATUS",
            "details": {"reason": "INVALID_STATUS"},
        }
    }


def test_requeue_answers_any_other_failure_with_a_generic_500(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(requeue_error=RuntimeError("db credentials for Jane Roe")))

    response = post(REQUEUE)

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred while handling the request.",
        }
    }
    assert "db credentials" not in response.content.decode()
    assert "Jane Roe" not in response.content.decode()


# --- POST /notifications/{id}/repin ----------------------------------------------------------


def test_repin_to_the_current_version_when_none_is_given(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    response = post(REPIN)

    assert response.status_code == 200
    assert service.call_args("repin_notification") == ("notif-1", None)
    assert response.json()["data"]["kind"] == "user"


def test_repin_with_no_body_at_all(
    client: Client, install_service: Callable[..., FakeService]
) -> None:
    service = install_service()

    response = send(client, REPIN, b"", None)

    assert response.status_code == 200
    assert service.call_args("repin_notification") == ("notif-1", None)


@pytest.mark.parametrize(("sent", "passed"), [(0, 0), (3, 3), (3.0, 3)])
def test_repin_to_the_version_the_caller_names(
    post: Callable[..., Any], install_service: Callable[..., FakeService], sent: Any, passed: int
) -> None:
    service = install_service()

    response = post(REPIN, {"templateVersion": sent})

    assert response.status_code == 200
    assert service.call_args("repin_notification") == ("notif-1", passed)


@pytest.mark.parametrize("template_version", [-1, 1.5, "3", True, None, [], {}])
def test_repin_rejects_a_template_version_that_is_not_a_non_negative_integer(
    post: Callable[..., Any], install_service: Callable[..., FakeService], template_version: Any
) -> None:
    service = install_service()

    response = post(REPIN, {"templateVersion": template_version})

    assert [issue["path"] for issue in issues(response)] == ["templateVersion"]
    assert not service.called("repin_notification")


def test_a_repinned_one_off_notification_serializes_as_one_off(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(repin_result=make_one_off_notification(status="FAILED")))

    data = post(REPIN, {"templateVersion": 2}).json()["data"]

    assert data["kind"] == "one-off"


@pytest.mark.parametrize("reason", ["INVALID_STATUS", "NO_TEMPLATE_VERSION"])
def test_repin_409s_on_a_refusal(
    post: Callable[..., Any], install_service: Callable[..., FakeService], reason: str
) -> None:
    install_service(FakeService(repin_error=action_error(reason, action="repin")))

    response = post(REPIN)

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "CONFLICT",
            "message": f"Refused: {reason}",
            "details": {"reason": reason},
        }
    }


def test_repin_404s_when_the_service_finds_no_notification(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(repin_error=action_error("NOT_FOUND", action="repin")))

    response = post("/api/v1/notifications/missing/repin")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_repin_answers_any_other_failure_with_a_generic_500(
    post: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    install_service(FakeService(repin_error=RuntimeError("template store password")))

    response = post(REPIN)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "password" not in response.content.decode()
