"""The app's URLconf: ``health`` and ``api/v1/``, relative to wherever it is included.

A host mounts it under a prefix of its choosing::

    path("notifications-api/", include("vintasend_api.dashboard.urls"))

which serves ``/notifications-api/health`` and ``/notifications-api/api/v1/...``. The bundled
project includes it at the root. ``health`` is unauthenticated, for load balancers and
container health checks; everything under ``api/v1/`` is behind ``ApiKeyAuth``.

No ``app_name``: each ``NinjaAPI`` brings its own namespace, ``vintasend_api`` and
``vintasend_api_health``, so ``reverse("vintasend_api:api-root")`` works wherever this is
included. Include it once per project: a second include registers the same namespaces again,
and ``reverse`` can only ever find one of them.
"""

from django.urls import path

from .api import api, health_api


urlpatterns = [
    path("", health_api.urls),
    path("api/v1/", api.urls),
]
