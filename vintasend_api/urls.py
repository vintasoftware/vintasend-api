"""URL routing.

``/health`` sits at the root and unauthenticated; everything else lives under
``/api/v1`` and requires the bearer token.
"""

from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import path

from vintasend_api.dashboard.api import api, health_api


urlpatterns = [
    path("", health_api.urls),
    path("api/v1/", api.urls),
]


def envelope_404(request: HttpRequest, exception: Any = None) -> HttpResponse:
    """Serve unmatched paths in the contract's error envelope.

    Paths under ``/api/v1`` are handled by Ninja's own 404 handler, which already uses
    the envelope. This covers everything else, so a client that mistypes a URL gets the
    same JSON shape rather than Django's HTML error page.
    """
    return JsonResponse(
        {
            "error": {
                "code": "NOT_FOUND",
                "message": f"No route matches {request.method} {request.path}.",
            }
        },
        status=404,
    )


handler404 = "vintasend_api.urls.envelope_404"
