"""ASGI entrypoint. Point uvicorn/daphne at ``vintasend_api.asgi:application``.

The API's own view layer is synchronous (see ``dashboard/service.py`` for why), so
running under ASGI buys nothing on its own. It is here for deployments that already
standardise on an ASGI server.
"""

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vintasend_api.settings")

application = get_asgi_application()
