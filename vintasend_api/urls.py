"""URL routing for the bundled project.

The app's own URLconf, included at the root: ``/health`` unauthenticated, everything else
under ``/api/v1`` behind the bearer token. A host project that embeds the app includes the
same URLconf under a prefix of its own instead.
"""

from django.urls import include, path

# Re-exported so the view keeps its old dotted path, which a deployment may reference.
from vintasend_api.dashboard.views import envelope_404


__all__ = ["envelope_404", "handler404", "urlpatterns"]

urlpatterns = [
    path("", include("vintasend_api.dashboard.urls")),
]

# Unmatched paths answer in the contract's error envelope rather than Django's HTML page.
handler404 = "vintasend_api.dashboard.views.envelope_404"
