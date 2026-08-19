"""Django settings for the VintaSend dashboard API.

This is a deliberately thin Django project. It has no models, no migrations, no admin
and no user accounts: every notification it serves comes from whichever VintaSend
service the operator configures, and the dashboard handles its own user auth in front
of the shared API key checked here.

Everything is read from the environment and validated at import time, so a
misconfigured deployment fails on startup rather than on the first request.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent

# Development convenience only. Production deployments set real environment variables,
# and a missing .env is not an error.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is an optional convenience
    pass
else:
    load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str) -> list[str]:
    return [entry.strip() for entry in _env(name).split(",") if entry.strip()]


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from error


# --- Django plumbing ---------------------------------------------------------------

# Django refuses to start without a SECRET_KEY, but this project signs nothing: it has
# no sessions, no cookies, no password reset and no CSRF-protected forms. The fallback
# keeps `manage.py` usable in development without pretending the value is a secret.
SECRET_KEY = _env("DJANGO_SECRET_KEY") or "not-a-secret-this-api-signs-nothing"

DEBUG = _env("DJANGO_DEBUG").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS") or ["*"]

ROOT_URLCONF = "vintasend_api.urls"

WSGI_APPLICATION = "vintasend_api.wsgi.application"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "vintasend_api.dashboard.apps.DashboardConfig",
]

# No sessions, no auth middleware, no CSRF: this API is authenticated by a bearer token
# and serves JSON only. CommonMiddleware is kept for its standard header handling.
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "vintasend_api.dashboard.cors.CorsMiddleware",
]

# The API itself never touches the ORM. A database is only configured because
# `django.contrib.auth` is installed, which some VintaSend backends
# (notably `vintasend-django`) need in order to resolve their notification model.
DATABASES = {
    "default": {
        "ENGINE": _env("DJANGO_DB_ENGINE") or "django.db.backends.sqlite3",
        "NAME": _env("DJANGO_DB_NAME") or str(BASE_DIR / "db.sqlite3"),
        "USER": _env("DJANGO_DB_USER"),
        "PASSWORD": _env("DJANGO_DB_PASSWORD"),
        "HOST": _env("DJANGO_DB_HOST"),
        "PORT": _env("DJANGO_DB_PORT"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "vintasend_api": {"handlers": ["console"], "level": _env("DJANGO_LOG_LEVEL") or "INFO"},
    },
}


# --- API configuration -------------------------------------------------------------

# Shared secret every /api/v1 request must present as `Authorization: Bearer <key>`.
VINTASEND_API_KEY = _env("VINTASEND_API_KEY")

# Browser origins allowed to call the API. Empty means "server-side clients only",
# which is how the dashboard uses it.
VINTASEND_API_CORS_ORIGINS = _env_list("VINTASEND_API_CORS_ORIGINS")

# Dotted path to a callable returning a configured NotificationService or
# AsyncIONotificationService. This is the same setting the VintaSend background-send
# worker reads, so one factory serves the worker and this API.
NOTIFICATION_SERVICE_FACTORY = _env("NOTIFICATION_SERVICE_FACTORY")

# Read from a non-primary backend registered in that service. Empty means the primary.
VINTASEND_BACKEND_IDENTIFIER = _env("VINTASEND_BACKEND_IDENTIFIER") or None

# --- Template preview (GitHub) -----------------------------------------------------
# Only read when /preview is called, so the API runs fine without them.

GITHUB_REPO = _env("GITHUB_REPO")
GITHUB_API_KEY = _env("GITHUB_API_KEY")
GITHUB_API_BASE_URL = _env("GITHUB_API_BASE_URL") or "https://api.github.com"
GITHUB_TEMPLATES_BASE_PATH = _env("GITHUB_TEMPLATES_BASE_PATH")
GITHUB_TEMPLATE_CACHE_MAX_ENTRIES = _env_int("GITHUB_TEMPLATE_CACHE_MAX_ENTRIES", 100)
GITHUB_TEMPLATE_TIMEOUT_SECONDS = _env_int("GITHUB_TEMPLATE_TIMEOUT_SECONDS", 10)
