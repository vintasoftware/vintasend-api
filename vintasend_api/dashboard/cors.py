"""Minimal CORS support for the ``/api/v1`` prefix.

The dashboard calls this API from its own server side, so CORS is off by default and
most deployments never turn it on. When ``VINTASEND_API_CORS_ORIGINS`` is set, only the
listed origins are echoed back -- never ``*`` -- because every request carries a bearer
token and a wildcard would let any page on the internet spend it.

A dedicated middleware rather than ``django-cors-headers``: the whole policy is the
three headers below, and a browser-facing deployment needs a per-user auth layer in
front of this API anyway.
"""

from typing import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from .contract import API_BASE_PATH


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
        if not request.path.startswith(API_BASE_PATH):
            return None

        origin = request.META.get("HTTP_ORIGIN")
        if origin and origin in settings.VINTASEND_API_CORS_ORIGINS:
            return origin

        return None
