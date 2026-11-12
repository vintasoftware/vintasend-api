"""Loads the operator-provided VintaSend service and adapts it to what the API needs.

The API ships no backend of its own: which database, adapters and template renderers to
use is a deployment decision. ``NOTIFICATION_SERVICE_FACTORY`` names a callable that
returns a configured service -- the same setting the VintaSend background-send worker
reads, so one factory serves the worker and this API.

Two things this module absorbs so the routes stay simple:

1. **Sync and AsyncIO services both work.** ``vintasend`` ships ``NotificationService``
   and ``AsyncIONotificationService`` with matching method names, and an operator may
   configure either. Every call goes through :func:`_resolve`, which awaits a coroutine
   result via ``async_to_sync`` and passes a plain value straight through. The view
   layer stays synchronous, which is what Django ORM backends such as
   ``vintasend-django`` need.

2. **One-off listing has no library method.** See :meth:`ServiceCaller.get_one_off_notifications`.

3. **A service may predate the optional notification actions.** ``vintasend`` 3.8.0 added
   ``expected_version``, ``template_version`` and ``send`` to requeue and repin, the version
   search and the action capability report. A service built on an older release, or a custom
   one, may lack any of them. See :meth:`ServiceCaller.action_capabilities` and
   :meth:`ServiceCaller.options_it_cannot_take`.
"""

import datetime
import inspect
import logging
import threading
from typing import Any

from asgiref.sync import async_to_sync
from vintasend.constants import UNSET, UnsetType
from vintasend.exceptions import NotificationNotFoundError, NotificationResendError
from vintasend.services.dataclasses import Notification, NotificationVersion, OneOffNotification
from vintasend.services.helpers import _import_class
from vintasend.services.notification_backends.filters import (
    NotificationFilter,
    NotificationFilterFields,
    NotificationOrderBy,
)
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
    TemplatedEmail,
)

from . import conf
from .capabilities import backend_page_number, to_wire_capabilities


logger = logging.getLogger(__name__)

AnyNotification = Notification | OneOffNotification

# How many backend pages the one-off fallback will scan before giving up on filling a
# page. Bounded so a store dominated by user notifications cannot turn one request into
# an unbounded walk of the whole table.
ONE_OFF_SCAN_PAGE_LIMIT = 50


# The optional notification actions, as `/capabilities` reports them: always all four and always
# explicit, because for these a missing key means "unsupported".
ACTION_CAPABILITY_KEYS = (
    "repin.expectedVersion",
    "requeue.expectedVersion",
    "requeue.templateVersion",
    "versions.filter",
)

# Wire option -> the keyword argument the service's requeue / repin takes it as.
ACTION_OPTION_KEYWORDS = {
    "templateVersion": "template_version",
    "expectedVersion": "expected_version",
    "send": "send",
}


def _accepts_keyword(method: Any, keyword: str) -> bool:
    """Whether ``method`` can be called with ``keyword=...``.

    A method whose signature cannot be read is treated as not accepting it: the cost of a
    wrong "no" is a 409 the caller can see, and of a wrong "yes" a ``TypeError`` that reaches
    the client as a 500.
    """
    if not callable(method):
        return False
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == keyword
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


class ServiceConfigurationError(RuntimeError):
    """Raised when ``NOTIFICATION_SERVICE_FACTORY`` cannot be resolved into a service."""


def _resolve(value: Any) -> Any:
    """Await an AsyncIO service's result, or pass a sync service's result through."""
    if inspect.isawaitable(value):
        return async_to_sync(_await)(value)
    return value


async def _await(awaitable: Any) -> Any:
    return await awaitable


def load_notification_service(factory_path: str) -> Any:
    """Import and call the configured factory, returning the service it builds."""
    if not factory_path:
        raise ServiceConfigurationError(
            "NOTIFICATION_SERVICE_FACTORY is not set, so this API cannot build a "
            "notification service to read from. Point it at a callable that returns a "
            "configured NotificationService or AsyncIONotificationService -- see "
            "vintasend_config.example.py."
        )

    try:
        factory = _import_class(factory_path)
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as error:
        raise ServiceConfigurationError(
            f"Could not import the VintaSend service factory {factory_path!r}."
        ) from error

    if not callable(factory):
        raise ServiceConfigurationError(
            f"The VintaSend service factory {factory_path!r} is not callable."
        )

    try:
        service = _resolve(factory())
    except Exception as error:
        raise ServiceConfigurationError(
            f"Calling the VintaSend service factory {factory_path!r} failed."
        ) from error

    if service is None:
        raise ServiceConfigurationError(
            f"The VintaSend service factory {factory_path!r} did not return a service."
        )

    return service


class ServiceCaller:
    """The slice of a VintaSend service this API depends on, with sync/AsyncIO and
    one-off differences absorbed.

    ``backend_identifier`` selects a non-primary backend registered in the service; the
    primary one is used when it is ``None``.
    """

    def __init__(self, service: Any, backend_identifier: str | None = None) -> None:
        self.service = service
        self.backend_identifier = backend_identifier
        self._backend_capabilities_cache: dict[str, Any] | None = None
        self._action_capabilities_cache: dict[str, bool] | None = None

    # --- capabilities --------------------------------------------------------------

    def backend_capabilities(self) -> dict[str, Any]:
        """The backend's own capability report, as the library returns it.

        Cached for the life of this caller -- which is the life of the process. A
        backend's capabilities are a static property of its implementation (the library
        merges a literal from ``get_filter_capabilities`` over a constant default), so
        re-asking on every request would buy nothing. Caching also means the pagination
        lookup below costs no extra round trip on the routes that need it.
        """
        if self._backend_capabilities_cache is None:
            raw = _resolve(
                self.service.get_backend_supported_filter_capabilities(self.backend_identifier)
            )
            self._backend_capabilities_cache = dict(raw or {})
        return self._backend_capabilities_cache

    def get_capabilities(self) -> dict[str, bool]:
        """Return the backend's capability report in wire form."""
        return to_wire_capabilities(self.backend_capabilities())

    def _knows_action_options(self) -> bool:
        """Whether the service is one that knows the optional notification actions.

        ``get_backend_supported_action_capabilities`` arrived together with them, so a service
        without it predates them -- and would drop ``template_version`` and
        ``expected_version`` silently, and send despite ``send=False``.
        """
        return callable(getattr(self.service, "get_backend_supported_action_capabilities", None))

    def action_capabilities(self) -> dict[str, bool]:
        """Which optional notification actions this API can run, every key explicit.

        What the backend reports, as the service merges it, and only ``True`` when the service
        can also be called with the option: a service without
        ``get_backend_supported_action_capabilities`` reports all four ``False``, a
        ``requeue_notification`` / ``repin_notification`` that does not take the keyword turns
        its key ``False``, and so does a missing ``filter_notification_versions``.

        Cached for the life of this caller, like the filter capabilities, for the same reason.
        """
        if self._action_capabilities_cache is None:
            reported: dict[str, Any] = {}
            if self._knows_action_options():
                reported = dict(
                    _resolve(
                        self.service.get_backend_supported_action_capabilities(
                            self.backend_identifier
                        )
                    )
                    or {}
                )
            requeue = getattr(self.service, "requeue_notification", None)
            repin = getattr(self.service, "repin_notification", None)
            callable_with = {
                "repin.expectedVersion": _accepts_keyword(repin, "expected_version"),
                "requeue.expectedVersion": _accepts_keyword(requeue, "expected_version"),
                "requeue.templateVersion": _accepts_keyword(requeue, "template_version"),
                "versions.filter": callable(
                    getattr(self.service, "filter_notification_versions", None)
                ),
            }
            self._action_capabilities_cache = {
                key: reported.get(key) is True and callable_with[key]
                for key in ACTION_CAPABILITY_KEYS
            }
        return self._action_capabilities_cache

    def options_it_cannot_take(self, method_name: str, options: list[str]) -> list[str]:
        """The wire options in ``options`` the service's ``method_name`` cannot be given.

        All of them for a service that predates the optional actions, whatever its signature
        says; otherwise the ones whose keyword the method does not accept. A service that can
        take an option but whose backend cannot honour it is not caught here: the service
        refuses that itself, with ``NotificationActionError`` reason ``UNSUPPORTED``.
        """
        if not options:
            return []
        if not self._knows_action_options():
            return list(options)
        method = getattr(self.service, method_name, None)
        return [
            option
            for option in options
            if not _accepts_keyword(method, ACTION_OPTION_KEYWORDS[option])
        ]

    def _page(self, wire_page: int) -> int:
        """Convert a contract page number into the backend's own numbering.

        Every paginated method below goes through this, so the routes only ever deal in
        the contract's 1-indexed pages and no caller has to remember which convention the
        configured backend follows.
        """
        return backend_page_number(wire_page, self.backend_capabilities())

    # --- reads ---------------------------------------------------------------------

    def filter_notifications(
        self,
        backend_filter: NotificationFilterFields,
        page: int,
        page_size: int,
        order_by: NotificationOrderBy | None,
    ) -> list[AnyNotification]:
        return list(
            _resolve(
                self.service.filter_notifications(
                    backend_filter,
                    self._page(page),
                    page_size,
                    order_by,
                    self.backend_identifier,
                )
            )
        )

    def get_pending_notifications(self, page: int, page_size: int) -> list[AnyNotification]:
        return list(
            _resolve(
                self.service.get_pending_notifications(
                    self._page(page), page_size, self.backend_identifier
                )
            )
        )

    def get_future_notifications(self, page: int, page_size: int) -> list[AnyNotification]:
        return list(
            _resolve(
                self.service.get_future_notifications(
                    self._page(page), page_size, self.backend_identifier
                )
            )
        )

    def get_one_off_notifications(self, page: int, page_size: int) -> list[OneOffNotification]:
        """Return a page of one-off notifications.

        The Python ``vintasend`` package has no one-off listing method -- unlike its
        TypeScript sibling, whose backends expose ``getOneOffNotifications`` -- and the
        composable filter vocabulary has no field that discriminates the two variants.

        So: a service that *does* expose the method (a custom one, or a future library
        release) is used directly. Otherwise this walks the unfiltered notification
        stream, keeping only one-offs, until it has skipped past the requested page and
        filled it. That costs more backend round trips than a native query and is
        bounded by ``ONE_OFF_SCAN_PAGE_LIMIT``; a truncated scan is logged rather than
        silently reported as the end of the data.
        """
        native = getattr(self.service, "get_one_off_notifications", None)
        if callable(native):
            return list(_resolve(native(self._page(page), page_size, self.backend_identifier)))

        # `page` is the contract's, so the offset arithmetic is 1-indexed regardless of
        # what the backend uses. `scan_page` is likewise a contract page number, and
        # `filter_notifications` converts it -- which is why the scan never touches the
        # backend's convention itself.
        wanted_from = (page - 1) * page_size
        wanted_to = wanted_from + page_size
        collected: list[OneOffNotification] = []
        seen = 0
        scan_page = 1

        while len(collected) < page_size and scan_page <= ONE_OFF_SCAN_PAGE_LIMIT:
            batch = self.filter_notifications({}, scan_page, page_size, None)
            if not batch:
                return collected

            for notification in batch:
                if not isinstance(notification, OneOffNotification):
                    continue
                if seen >= wanted_from and len(collected) < page_size:
                    collected.append(notification)
                seen += 1
                if seen >= wanted_to:
                    return collected

            scan_page += 1

        if scan_page > ONE_OFF_SCAN_PAGE_LIMIT:
            logger.warning(
                "One-off notification scan hit its %s-page limit before filling page %s. "
                "The response may be short. Configure a service exposing "
                "get_one_off_notifications to avoid the scan.",
                ONE_OFF_SCAN_PAGE_LIMIT,
                page,
            )

        return collected

    def filter_notification_versions(
        self, backend_filter: NotificationFilter, page: int, page_size: int
    ) -> list[NotificationVersion]:
        """IDs and version tokens of the notifications matching ``backend_filter``.

        Only call this when ``action_capabilities()["versions.filter"]`` is ``True``.
        """
        return list(
            _resolve(
                self.service.filter_notification_versions(
                    backend_filter, self._page(page), page_size, self.backend_identifier
                )
            )
        )

    def get_notification(self, notification_id: str) -> AnyNotification | None:
        """Look one notification up, returning ``None`` when it does not exist.

        ``get_notification`` returns whichever variant matches the id, so the contract's
        "user notification first, then one-off" lookup is a single call here. The
        library signals absence by raising, which this converts into ``None`` so callers
        can decide what a miss means.
        """
        try:
            return _resolve(self.service.get_notification(notification_id, self.backend_identifier))
        except NotificationNotFoundError:
            return None

    def get_notification_context(self, notification: AnyNotification) -> dict[str, Any]:
        return _resolve(self.service.get_notification_context(notification))

    # --- writes --------------------------------------------------------------------

    def resend_notification(
        self, notification_id: str, use_stored_context: bool
    ) -> Notification | None:
        """Resend a notification, returning ``None`` when the service refuses.

        The library raises for every refusal -- ``NotificationNotFoundError`` for a
        missing notification, ``NotificationResendError`` for a one-off or one still
        scheduled in the future -- and the contract collapses all three into a single
        409, so they are collapsed to ``None`` here.

        Failures *during* the send that follows (a dead SMTP host, a context generator
        that raises) are deliberately not caught: those are not "this cannot be resent",
        and reporting them as a 409 would tell the dashboard the wrong thing.
        """
        try:
            return _resolve(self.service.resend_notification(notification_id, use_stored_context))
        except (NotificationNotFoundError, NotificationResendError):
            return None

    def cancel_notification(self, notification_id: str) -> None:
        _resolve(self.service.cancel_notification(notification_id))

    def requeue_notification(
        self,
        notification_id: str,
        send_after: datetime.datetime | UnsetType | None = UNSET,
        *,
        template_version: int | None = None,
        expected_version: str | None = None,
        send: bool | None = None,
    ) -> AnyNotification:
        """Requeue a failed or cancelled notification on the primary backend.

        Each option is passed only when the caller gave one, so a service that predates
        ``template_version``, ``expected_version`` and ``send`` never sees them -- the route
        refuses those first, see :meth:`options_it_cannot_take`. ``send_after`` likewise,
        ``None`` included: left out, the notification keeps its own. The service's
        ``NotificationActionError`` refusals propagate, and the route maps them onto 404 and
        409.
        """
        options: dict[str, Any] = {}
        if send_after is not UNSET:
            options["send_after"] = send_after
        if template_version is not None:
            options["template_version"] = template_version
        if expected_version is not None:
            options["expected_version"] = expected_version
        if send is not None:
            options["send"] = send
        return _resolve(self.service.requeue_notification(notification_id, **options))

    def repin_notification(
        self,
        notification_id: str,
        template_version: int | None = None,
        *,
        expected_version: str | None = None,
    ) -> AnyNotification:
        """Pin a notification to a template version, by default the current one.

        ``expected_version`` is passed only when given, as for requeue. The service's
        ``NotificationActionError`` refusals propagate, as for requeue.
        """
        options: dict[str, Any] = {}
        if expected_version is not None:
            options["expected_version"] = expected_version
        return _resolve(
            self.service.repin_notification(
                notification_id, template_version=template_version, **options
            )
        )

    # --- rendering -----------------------------------------------------------------

    def render_email_template_from_content(
        self,
        notification: AnyNotification,
        template_content: EmailTemplateContent,
        context: dict[str, Any],
    ) -> TemplatedEmail:
        return _resolve(
            self.service.render_email_template_from_content(notification, template_content, context)
        )


_cache_lock = threading.Lock()
_cached_caller: ServiceCaller | None = None


def get_service_caller() -> ServiceCaller:
    """Build the service once per process and reuse it for every request.

    Failures are not cached: a transient misconfiguration (an unreachable database at
    boot, say) should be retried on the next request rather than poisoning the process.
    """
    global _cached_caller  # noqa: PLW0603

    if _cached_caller is not None:
        return _cached_caller

    with _cache_lock:
        if _cached_caller is not None:
            return _cached_caller

        service = load_notification_service(conf.service_factory())
        _cached_caller = ServiceCaller(service, conf.backend_identifier())
        return _cached_caller


def set_service_caller(caller: ServiceCaller | None) -> None:
    """Replace the cached service. The seam tests inject a fake service through."""
    global _cached_caller  # noqa: PLW0603

    with _cache_lock:
        _cached_caller = caller
