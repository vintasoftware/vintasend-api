"""WSGI entrypoint. Point gunicorn/uwsgi at ``vintasend_api.wsgi:application``."""

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vintasend_api.settings")

application = get_wsgi_application()
