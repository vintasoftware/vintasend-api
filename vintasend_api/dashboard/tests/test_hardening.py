"""Request bodies read the way the contract says, one 400 envelope, pages that never offer an
empty next one, and unexpected errors that never reach the log whole.
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any, Callable

from django.http import HttpRequest
from django.test import Client

import pytest

from .fixtures import AUTH_HEADERS, FakeService, make_user_notification


if TYPE_CHECKING:
    from django.conf import LazySettings


pytestmark = pytest.mark.django_db

RESEND = "/api/v1/notifications/notif-1/resend"
SECRET = "Jane Synthetic, diagnosis that must never be logged"
LOGGER = "vintasend_api"


def send(client: Client, path: str, body: bytes | str, content_type: str | None) -> Any:
    """POST raw bytes with exactly the ``Content-Type`` given, or none at all.

    Sent as a header rather than as ``content_type``, which the test client drops when the
    body is empty.
    """
    headers = (
        AUTH_HEADERS if content_type is None else {**AUTH_HEADERS, "Content-Type": content_type}
    )
    return client.generic("POST", path, body, content_type="", headers=headers)


@pytest.fixture
def service(install_service: Callable[..., FakeService]) -> FakeService:
    return install_service(FakeService(resend_result=make_user_notification(id="notif-2")))


def assert_body_refused(response: Any, message: str) -> None:
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["details"]["issues"] == [{"path": "", "message": message}]


# --- the resend body ---------------------------------------------------------------------


def test_a_form_encoded_body_is_refused_rather_than_read(
    client: Client, service: FakeService
) -> None:
    """What `curl -d` sends. Read as `{}`, it would resend with a regenerated context."""
    response = send(
        client, RESEND, '{"useStoredContext":true}', "application/x-www-form-urlencoded"
    )

    assert_body_refused(response, "Send the request body as application/json.")
    assert not service.called("resend_notification")


def test_a_body_with_no_content_type_is_refused(client: Client, service: FakeService) -> None:
    response = send(client, RESEND, b'{"useStoredContext":true}', None)

    assert_body_refused(response, "Send the request body as application/json.")
    assert not service.called("resend_notification")


@pytest.mark.parametrize("content_type", [None, "text/plain"])
def test_an_empty_body_in_another_media_type_is_an_omitted_body(
    client: Client, service: FakeService, content_type: str | None
) -> None:
    response = send(client, RESEND, b"", content_type)

    assert response.status_code == 201
    assert service.call_args("resend_notification") == ("notif-1", False)


def test_a_structured_json_media_type_is_read_as_json(client: Client, service: FakeService) -> None:
    response = send(client, RESEND, '{"useStoredContext":true}', "application/merge-patch+json")

    assert response.status_code == 201
    assert service.call_args("resend_notification") == ("notif-1", True)


@pytest.mark.parametrize("body", ["", '{"useStoredContext":'])
def test_an_empty_or_malformed_json_body_is_refused_in_the_envelope(
    client: Client, service: FakeService, body: str
) -> None:
    response = send(client, RESEND, body, "application/json")

    assert_body_refused(response, "Malformed JSON in request body")
    assert not service.called("resend_notification")


@pytest.mark.parametrize("body", ["null", "[]", "true"])
def test_a_json_body_that_is_not_an_object_is_refused(
    client: Client, service: FakeService, body: str
) -> None:
    response = send(client, RESEND, body, "application/json")

    assert_body_refused(response, "The request body must be a JSON object.")
    assert not service.called("resend_notification")


# --- hasMore -----------------------------------------------------------------------------


class PagedService(FakeService):
    """A backend holding ``total`` rows, 1-indexed like VintaSend's Python backends."""

    def __init__(self, total: int) -> None:
        super().__init__()
        self.rows = [make_user_notification(id=f"n-{index}") for index in range(total)]

    def _slice(self, page: int, page_size: int) -> list[Any]:
        start = (page - 1) * page_size
        return self.rows[start : start + page_size]

    def filter_notifications(
        self,
        backend_filter: Any,
        page: int,
        page_size: int,
        order_by: Any = None,
        backend_identifier: str | None = None,
    ) -> list[Any]:
        super().filter_notifications(backend_filter, page, page_size, order_by, backend_identifier)
        return self._slice(page, page_size)

    def get_pending_notifications(
        self, page: int, page_size: int, backend_identifier: str | None = None
    ) -> list[Any]:
        super().get_pending_notifications(page, page_size, backend_identifier)
        return self._slice(page, page_size)


@pytest.mark.parametrize("path", ["/api/v1/notifications", "/api/v1/notifications/pending"])
@pytest.mark.parametrize(("total", "page", "has_more"), [(4, 2, False), (4, 1, True), (5, 2, True)])
def test_has_more_means_the_next_page_has_a_row(
    get: Callable[..., Any],
    install_service: Callable[..., FakeService],
    path: str,
    total: int,
    page: int,
    has_more: bool,
) -> None:
    """A full page is not proof of another: a list that exactly fills it ends there."""
    install_service(PagedService(total))

    body = get(f"{path}?page={page}&pageSize=2").json()

    assert len(body["data"]) == 2
    assert body["hasMore"] is has_more


def test_has_more_asks_with_the_same_filter_and_order_and_only_after_a_full_page(
    get: Callable[..., Any], install_service: Callable[..., FakeService]
) -> None:
    service = install_service(PagedService(5))

    get("/api/v1/notifications?page=2&pageSize=2&status=SENT")

    reads = [args for name, args in service.calls if name == "filter_notifications"]
    assert len(reads) == 2
    page, probe = reads
    assert probe[0] == page[0]
    assert probe[1:4] == (5, 1, page[3])

    get("/api/v1/notifications?page=3&pageSize=2")
    reads = [args for name, args in service.calls if name == "filter_notifications"]
    assert len(reads) == 3


# --- unexpected errors -------------------------------------------------------------------


HANDLED: list[tuple[str, str]] = []


def record_unhandled(exc: Exception, request: HttpRequest, request_id: str) -> None:
    HANDLED.append((type(exc).__name__, request_id))


def failing_handler(exc: Exception, request: HttpRequest, request_id: str) -> None:
    raise ValueError(f"the handler itself failed while handling {exc}")


@pytest.fixture
def failing(install_service: Callable[..., FakeService]) -> FakeService:
    HANDLED.clear()
    return install_service(
        FakeService(notification_error=RuntimeError(f"db read failed: {SECRET}"))
    )


def test_an_unexpected_error_is_logged_as_one_redacted_line(
    client: Client, failing: FakeService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.get(
            "/api/v1/notifications/notif-1", headers={**AUTH_HEADERS, "X-Request-Id": "req-42"}
        )

    assert response.status_code == 500
    assert response.headers["X-Request-Id"] == "req-42"
    assert SECRET not in response.content.decode()
    records = [record for record in caplog.records if record.name.startswith(LOGGER)]
    assert [record.getMessage() for record in records] == [
        "Unhandled RuntimeError (request req-42) on GET api/v1/notifications/<id>"
    ]
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None or SECRET not in str(record.exc_info[1])
        # Django's own 5xx record would repeat the concrete path and attach the request.
        assert "notif-1" not in record.getMessage()


def test_an_unsafe_client_request_id_is_replaced(
    client: Client, failing: FakeService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        response = client.get(
            "/api/v1/notifications/notif-1", headers={**AUTH_HEADERS, "X-Request-Id": "x\nFORGED"}
        )

    assert uuid.UUID(response.headers["X-Request-Id"])
    assert "FORGED" not in caplog.records[0].getMessage()


def test_a_configured_handler_receives_the_error_instead(
    get: Callable[..., Any],
    failing: FakeService,
    settings: "LazySettings",
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.VINTASEND_UNHANDLED_ERROR_HANDLER = f"{__name__}.record_unhandled"

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        response = get("/api/v1/notifications/notif-1")

    assert response.status_code == 500
    assert HANDLED == [("RuntimeError", response.headers["X-Request-Id"])]
    assert [record for record in caplog.records if record.name.startswith(LOGGER)] == []


def test_a_failing_handler_falls_back_to_the_redacted_line(
    get: Callable[..., Any],
    failing: FakeService,
    settings: "LazySettings",
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.VINTASEND_UNHANDLED_ERROR_HANDLER = failing_handler

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        response = get("/api/v1/notifications/notif-1")

    assert response.status_code == 500
    messages = [record.getMessage() for record in caplog.records if record.name.startswith(LOGGER)]
    assert len(messages) == 1
    assert messages[0].startswith("Unhandled RuntimeError")
    assert all(SECRET not in message for message in messages)


def test_an_unusable_handler_setting_fails_the_startup_checks(settings: "LazySettings") -> None:
    from ..apps import check_api_configuration

    settings.VINTASEND_UNHANDLED_ERROR_HANDLER = "nowhere.at_all"

    errors = check_api_configuration(None)

    assert "vintasend_api.E003" in [error.id for error in errors]
