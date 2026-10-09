"""Minimal CORS support for the ``/api/v1`` routes, wherever the host mounts them.

The dashboard calls this API from its own server side, so CORS is off by default and
most deployments never turn it on. When ``VINTASEND_API_CORS_ORIGINS`` is set, only the
listed origins are echoed back -- never ``*`` -- because every request carries a bearer
token and a wildcard would let any page on the internet spend it.

A dedicated middleware rather than ``django-cors-headers``: the whole policy is the
three headers below, and a browser-facing deployment needs a per-user auth layer in
front of this API anyway.

Optional when the app is embedded: a host that serves the API to browsers adds
``vintasend_api.dashboard.cors.CorsMiddleware`` to its ``MIDDLEWARE``, or uses its own CORS
handling. The middleware follows the API to whatever prefix the host includes it under.
"""

from typing import Callable

from django.http import HttpRequest, HttpResponse
from django.urls import Resolver404, resolve

from . import conf
from .contract import API_BASE_PATH, URLS_NAMESPACE


ALLOWED_HEADERS = "Authorization, Content-Type"
ALLOWED_METHODS = "GET, POST, OPTIONS"


class CorsMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        origin = self._allowed_origin(request)

        if (
            request.method == "OPTIONS"
            and origin
            and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META
        ):
            response: HttpResponse = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if origin:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
            response["Access-Control-Allow-Methods"] = ALLOWED_METHODS
            # The allowed origin varies by request, so caches must not reuse one
            # origin's response for another.
            response["Vary"] = "Origin"

        return response

    def _allowed_origin(self, request: HttpRequest) -> str | None:
        # Checked before the path, so a request no allowed origin sent -- nearly all of them --
        # is never resolved here.
        origin = request.META.get("HTTP_ORIGIN")
        if not isinstance(origin, str) or origin not in conf.cors_origins():
            return None

        if not _is_api_request(request):
            return None

        return origin


def _is_api_request(request: HttpRequest) -> bool:
    """Whether the request is for one of the API's routes, under whatever prefix it is mounted.

    Middleware runs before URL resolution, so the path is resolved here. A route in the API's
    namespace is the API's, wherever a host included it. A path nothing matches is the API's
    only under the standalone project's ``/api/v1``, so a mistyped URL there still answers a
    browser with headers it can read, as it always has.
    """
    try:
        match = resolve(request.path_info, getattr(request, "urlconf", None))
    except Resolver404:
        return request.path_info.startswith(API_BASE_PATH)
    return URLS_NAMESPACE in match.namespaces
