"""Test doubles mirroring the TypeScript reference's ``test/helpers/fixtures.ts``.

Keeping the two fixture sets aligned is what makes the two test suites comparable: a
behaviour asserted there should be assertable here with the same inputs.
"""

import datetime
from typing import Any

from vintasend.exceptions import NotificationNotFoundError
from vintasend.services.dataclasses import Notification, NotificationVersion, OneOffNotification
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    TemplatedEmail,
)

from ..service import ServiceCaller


TEST_API_KEY = "test-api-key"

AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}

DEFAULT_CAPABILITIES = {
    "stringLookups.includes": True,
    "stringLookups.caseInsensitive": True,
    "orderBy.createdAt": True,
    "orderBy.sentAt": True,
    "orderBy.sendAfter": True,
    "orderBy.readAt": True,
    "orderBy.updatedAt": True,
}

# What `/capabilities` reports for the optional notification actions: always all four.
ALL_ACTION_CAPABILITIES = {
    "repin.expectedVersion": True,
    "requeue.expectedVersion": True,
    "requeue.templateVersion": True,
    "versions.filter": True,
}
NO_ACTION_CAPABILITIES = dict.fromkeys(ALL_ACTION_CAPABILITIES, False)


def make_user_notification(**overrides: Any) -> Notification:
    defaults: dict[str, Any] = {
        "id": "notif-1",
        "user_id": "user-1",
        "notification_type": "EMAIL",
        "title": "Test Notification",
        "context_name": "testContext",
        "context_kwargs": {"param": "test"},
        "context_used": {"key": "value"},
        "status": "SENT",
        "send_after": None,
        "sent_at": datetime.datetime(2024, 1, 15, 10, 0, tzinfo=datetime.timezone.utc),
        "read_at": None,
        "created": datetime.datetime(2024, 1, 15, 9, 0, tzinfo=datetime.timezone.utc),
        "modified": datetime.datetime(2024, 1, 15, 9, 30, tzinfo=datetime.timezone.utc),
        "adapter_used": "mailgun",
        "body_template": "emails/body.html",
        "subject_template": "emails/subject.txt",
        "preheader_template": "emails/preheader.html",
        "adapter_extra_parameters": None,
        "tenant": "tenant-1",
        "git_commit_sha": "abc123",
        "requested_template_version": 3,
        "used_template_version": 3,
        "attachments": [],
    }
    defaults.update(overrides)
    return Notification(**defaults)


def make_one_off_notification(**overrides: Any) -> OneOffNotification:
    user_notification = make_user_notification()
    defaults: dict[str, Any] = {
        key: getattr(user_notification, key)
        for key in (
            "notification_type",
            "title",
            "context_name",
            "context_kwargs",
            "context_used",
            "status",
            "send_after",
            "sent_at",
            "read_at",
            "created",
            "modified",
            "adapter_used",
            "body_template",
            "subject_template",
            "preheader_template",
            "adapter_extra_parameters",
            "tenant",
            "git_commit_sha",
            "requested_template_version",
            "used_template_version",
            "attachments",
        )
    }
    defaults.update(
        {
            "id": "oneoff-1",
            "email_or_phone": "test@example.com",
            "first_name": "John",
            "last_name": "Doe",
        }
    )
    defaults.update(overrides)
    return OneOffNotification(**defaults)


class FakeService:
    """A fully stubbed VintaSend service; each test overrides what it exercises.

    Every call is recorded in ``calls`` so tests can assert on the exact arguments the
    API passed down -- the pagination conversion and the negotiated filters are part of
    the contract, not incidental.
    """

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.capabilities: dict[str, bool] = dict(DEFAULT_CAPABILITIES)
        self.filter_results: list[Any] = []
        self.pending_results: list[Any] = []
        self.future_results: list[Any] = []
        self.notification: Notification | OneOffNotification | None = None
        self.notification_error: Exception | None = None
        self.resend_result: Notification | None = None
        self.resend_error: Exception | None = None
        self.requeue_result: Notification | OneOffNotification = make_user_notification(
            status="PENDING_SEND", sent_at=None
        )
        self.requeue_error: Exception | None = None
        self.repin_result: Notification | OneOffNotification = make_user_notification(
            status="FAILED"
        )
        self.repin_error: Exception | None = None
        self.rendered = TemplatedEmail(subject="<h1>Subject</h1>", body="<p>Body</p>")
        self.generated_context: dict[str, Any] = {"generated": True}
        for key, value in overrides.items():
            setattr(self, key, value)

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def call_args(self, name: str) -> tuple[Any, ...]:
        for call_name, args in self.calls:
            if call_name == name:
                return args
        raise AssertionError(f"{name} was never called. Calls: {[c[0] for c in self.calls]}")

    def called(self, name: str) -> bool:
        return any(call_name == name for call_name, _ in self.calls)

    # --- the service surface the API depends on ------------------------------------

    def get_backend_supported_filter_capabilities(
        self, backend_identifier: str | None = None
    ) -> dict[str, bool]:
        self._record("get_backend_supported_filter_capabilities", backend_identifier)
        return self.capabilities

    def filter_notifications(
        self,
        backend_filter: Any,
        page: int,
        page_size: int,
        order_by: Any = None,
        backend_identifier: str | None = None,
    ) -> list[Any]:
        self._record(
            "filter_notifications", backend_filter, page, page_size, order_by, backend_identifier
        )
        return self.filter_results

    def get_pending_notifications(
        self, page: int, page_size: int, backend_identifier: str | None = None
    ) -> list[Any]:
        self._record("get_pending_notifications", page, page_size, backend_identifier)
        return self.pending_results

    def get_future_notifications(
        self, page: int, page_size: int, backend_identifier: str | None = None
    ) -> list[Any]:
        self._record("get_future_notifications", page, page_size, backend_identifier)
        return self.future_results

    def get_notification(self, notification_id: Any, backend_identifier: str | None = None) -> Any:
        self._record("get_notification", notification_id, backend_identifier)
        if self.notification_error is not None:
            raise self.notification_error
        if self.notification is None:
            raise NotificationNotFoundError(f"No notification {notification_id}")
        return self.notification

    def get_notification_context(self, notification: Any) -> dict[str, Any]:
        self._record("get_notification_context", notification)
        return self.generated_context

    def resend_notification(
        self, notification_id: Any, use_stored_context_if_available: bool = False
    ) -> Notification:
        self._record("resend_notification", notification_id, use_stored_context_if_available)
        if self.resend_error is not None:
            raise self.resend_error
        if self.resend_result is None:
            raise NotificationNotFoundError(f"No notification {notification_id}")
        return self.resend_result

    def cancel_notification(self, notification_id: Any) -> None:
        self._record("cancel_notification", notification_id)

    def requeue_notification(self, notification_id: Any, **kwargs: Any) -> Any:
        """Records keyword arguments as passed, so a test can tell "left out" from ``None``."""
        self._record("requeue_notification", notification_id, kwargs)
        if self.requeue_error is not None:
            raise self.requeue_error
        return self.requeue_result

    def repin_notification(self, notification_id: Any, template_version: Any = None) -> Any:
        self._record("repin_notification", notification_id, template_version)
        if self.repin_error is not None:
            raise self.repin_error
        return self.repin_result

    def render_email_template_from_content(
        self, notification: Any, template_content: Any, context: Any
    ) -> TemplatedEmail:
        self._record("render_email_template_from_content", notification, template_content, context)
        return self.rendered


class CapableFakeService(FakeService):
    """A service on a ``vintasend`` that knows the optional notification actions.

    ``FakeService`` stands for one that predates them: it has no
    ``get_backend_supported_action_capabilities`` and no ``filter_notification_versions``.
    This one has both, and a ``repin_notification`` that takes ``expected_version``. Its
    ``requeue_notification`` is inherited, and already takes any keyword.
    """

    def __init__(self, **overrides: Any) -> None:
        self.action_capabilities: dict[str, bool] = dict(ALL_ACTION_CAPABILITIES)
        # Each call to `filter_notification_versions` answers with the next page here; once
        # they run out, with an empty one.
        self.version_pages: list[list[NotificationVersion]] = []
        super().__init__(**overrides)

    def get_backend_supported_action_capabilities(
        self, backend_identifier: str | None = None
    ) -> dict[str, bool]:
        self._record("get_backend_supported_action_capabilities", backend_identifier)
        return self.action_capabilities

    def filter_notification_versions(
        self,
        backend_filter: Any,
        page: int,
        page_size: int,
        backend_identifier: str | None = None,
    ) -> list[NotificationVersion]:
        self._record(
            "filter_notification_versions", backend_filter, page, page_size, backend_identifier
        )
        return self.version_pages.pop(0) if self.version_pages else []

    def repin_notification(  # type: ignore[override]
        self, notification_id: Any, template_version: Any = None, **kwargs: Any
    ) -> Any:
        """Records keyword arguments other than the version as passed, like requeue."""
        self._record("repin_notification", notification_id, template_version, kwargs)
        if self.repin_error is not None:
            raise self.repin_error
        return self.repin_result


class FakeTemplateClient:
    """Stands in for ``GitHubTemplateClient``."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.content = "Hello {{ name }}"
        self.main_sha = "main-sha"
        self.content_error: Exception | None = None
        for key, value in overrides.items():
            setattr(self, key, value)

    def get_template_content_by_commit(self, template_path: str, git_commit_sha: str) -> str:
        self.calls.append(("get_template_content_by_commit", (template_path, git_commit_sha)))
        if self.content_error is not None:
            raise self.content_error
        return self.content

    def get_latest_main_commit_sha(self) -> str:
        self.calls.append(("get_latest_main_commit_sha", ()))
        return self.main_sha

    def called(self, name: str) -> bool:
        return any(call_name == name for call_name, _ in self.calls)

    def call_count(self, name: str) -> int:
        return sum(1 for call_name, _ in self.calls if call_name == name)

    def call_args(self, name: str) -> tuple[Any, ...]:
        for call_name, args in self.calls:
            if call_name == name:
                return args
        raise AssertionError(f"{name} was never called")


def caller(service: FakeService, backend_identifier: str | None = None) -> ServiceCaller:
    return ServiceCaller(service, backend_identifier)
