"""A host project's URLconf, for the tests that mount the app under a prefix of its own.

Only ever used through ``override_settings(ROOT_URLCONF=...)``. It is what the README tells a
host to write, and nothing from the bundled project's ``urls.py``.
"""

from django.urls import include, path


urlpatterns = [
    path("notifications-api/", include("vintasend_api.dashboard.urls")),
]
