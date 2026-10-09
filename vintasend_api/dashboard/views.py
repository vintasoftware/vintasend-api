"""Views that sit outside the API's routes."""

from django.http import HttpRequest, HttpResponse, JsonResponse


def envelope_404(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Serve unmatched paths in the contract's error envelope.

    A ``handler404`` for a project, not a route. Django calls it for a path no URL pattern
    matches, including one under ``api/v1/``, so the bundled project sets it and a client that
    mistypes a URL gets the same JSON shape rather than Django's HTML error page. A host that
    embeds the app may set it too, but it answers every unmatched path in the host's project,
    not only the API's, so it is optional there.
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
