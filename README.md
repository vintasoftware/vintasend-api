# VintaSend API (Python)

REST API that exposes a [VintaSend](https://github.com/vintasoftware/vintasend)
notification service over HTTP, built with Django and
[django-ninja](https://django-ninja.dev/).

It exists so the [VintaSend dashboard](https://github.com/vintasoftware/vintasend-dashboard)
no longer has to embed a notification service: the dashboard is a pure API client, and
any implementation of this contract can serve it.

**[`openapi.yaml`](https://github.com/vintasoftware/vintasend-api/blob/main/openapi.yaml) is the contract.** It is shipped here byte-identical
to the copy in [`vintasend-ts-api`](https://github.com/vintasoftware/vintasend-ts-api),
the TypeScript reference implementation. This project is the Python implementation of the
same document, so one dashboard consumes either without knowing which is behind it.

This repository is developed and released on its own, and the
[`vintasend`](https://github.com/vintasoftware/vintasend) library repository tracks it as a
git submodule under `tools/vintasend-api` — the same arrangement its `implementations/`
packages use. Contribute here; the parent repo only records which commit it points at.

## Why Django, for a library with no web framework

The notification store is the deciding factor. `vintasend-django` persists notifications
through the Django ORM, and reading them needs a Django app registry and connection
handling — a FastAPI process would have to boot a half-configured Django anyway to use
it. Serving from Django removes that.

Nothing is lost for non-Django deployments. The backend is a pluggable seam, so a
FastAPI application storing notifications through `vintasend-sqlalchemy` is served by
this same API: point `NOTIFICATION_SERVICE_FACTORY` at a factory that builds a
SQLAlchemy-backed service and the HTTP layer neither knows nor cares.

```
┌─────────────────────┐   HTTPS + API key    ┌──────────────────┐
│  Dashboard (Next)   │ ───────────────────▶ │  vintasend-api   │
│  server-side only   │ ◀─────────────────── │  (this project)  │
└─────────────────────┘     JSON contract    └────────┬─────────┘
                                                      │
                                       ┌──────────────┴──────────────┐
                                       │  Your VintaSend service     │
                                       │  backend + adapters +       │
                                       │  template renderer          │
                                       └─────────────────────────────┘
```

The API owns everything that needs backend credentials — database access, template
rendering, GitHub template lookups. The UI owns presentation and user authentication.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness probe (unauthenticated) |
| GET | `/api/v1/capabilities` | Filter/order capabilities of the configured backend |
| GET | `/api/v1/notifications` | List notifications with filters, ordering and pagination |
| GET | `/api/v1/notifications/pending` | Notifications awaiting send |
| GET | `/api/v1/notifications/future` | Notifications scheduled for the future |
| GET | `/api/v1/notifications/one-off` | One-off notifications |
| GET | `/api/v1/notifications/{id}` | One notification, including context payloads |
| GET | `/api/v1/notifications/{id}/preview` | Templates rendered at the notification's commit |
| POST | `/api/v1/notifications/{id}/resend` | Resend a notification |
| POST | `/api/v1/notifications/{id}/cancel` | Cancel a pending notification |

A browsable version of the generated schema is served at `/api/v1/docs`.

Conventions the dashboard depends on:

- `page` is **1-indexed** on the wire.
- `hasMore` is `true` when the next page has at least one row, so a list that exactly
  fills its last page never offers an empty one. Backends are not required to produce a
  total count: after a full page, the API reads the one row that would follow it.
- List rows carry a `kind` field (`user` or `one-off`) so clients can discriminate
  without sniffing for the presence of fields.
- Timestamps are ISO-8601 UTC strings, `null` when unset — never absent.
- Every notification payload carries `requestedTemplateVersion` and `usedTemplateVersion`,
  which are `null` for a service whose template renderer has no versions. See
  [Template versions](#template-versions).
- Errors always use the envelope `{ "error": { "code", "message", "details"? } }`, and
  every 400 carries `details.issues: [{ path, message }]` — `path` empty for the body as a
  whole.
- Request bodies are JSON. A request declaring `application/json` (or any
  `application/*+json`) must carry a valid JSON object. One declaring no media type, or
  another one, counts as an omitted body when it is empty and is a 400 otherwise:
  `curl -d` sends form encoding unless told otherwise, and reading that body as `{}` would
  resend with a regenerated context instead of the stored one.
- `FORBIDDEN` (403) is declared on every route, for a host that authenticates callers
  itself and refuses one it knows. The API key alone never produces it.

## Template versions

A notification records which version of its template it renders, for services whose
template renderer versions templates at all — a store-backed one such as
[`vintasend-managed-templates`](https://github.com/vintasoftware/vintasend-managed-templates).
Two fields on every notification payload say it:

| Field | Written | Means |
| --- | --- | --- |
| `requestedTemplateVersion` | at create/update | The version this notification is pinned to. `null` is not "unknown": it means the notification was deliberately left unpinned and renders whatever version is current when it sends. |
| `usedTemplateVersion` | after the send | What the renderer reported it actually used. `null` until the notification has been sent. |

On a pinned notification the two read the same, and an edit to the template cannot change
what it renders — that is what pinning is for. On an unpinned one they differ, and
`usedTemplateVersion` is the only record of which version went out: by the time anyone
asks, the template has moved on.

Both are `null` for a file-based renderer, which has no versions to report, and for any
notification created before pinning existed. A dashboard should read `null` as *not
applicable* rather than as a missing value, and hide the field rather than showing a zero.

This is independent of `gitCommitSha`, which pins the same idea for the other kind of
renderer: templates as files in a repository. A notification uses one mechanism or the
other, never both, so a payload with a `gitCommitSha` normally has null versions and one
with versions normally has a null SHA.

Both are filterable on the listing:

```
GET /api/v1/notifications?requestedTemplateVersion=3   # pinned to v3
GET /api/v1/notifications?usedTemplateVersion=3        # actually rendered v3 — the audit query
```

They match exactly, and they never match a notification whose version is `null` — the
library's NULL semantics, the same as every other filter field. So there is no way to ask
for "the unpinned ones" positively; `not` is what includes them, and this API exposes no
`not`. A negative version is a 400, but the floor is 0 rather than 1: version numbering
belongs to the template renderer, and this API has no basis for assuming it is 1-based.

A backend that cannot evaluate either filter reports `fields.requestedTemplateVersion` /
`fields.usedTemplateVersion` as `false` in `/capabilities`, and the dashboard greys the
control out. Unlike ordering, an unsupported *field* filter is not dropped server-side —
that has always been this API's split, and these two follow it.

## Authentication

By default, every `/api/v1` request must carry the shared secret:

```
Authorization: Bearer $VINTASEND_API_KEY
```

The dashboard calls this API only from its own server side, so the key never reaches a
browser. If you do need to call the API from a browser, set `VINTASEND_API_CORS_ORIGINS`
to the allowed origins — and put a per-user auth layer in front of it first.

A host that authenticates callers itself sets `VINTASEND_API_AUTHENTICATOR` instead. See
[Authenticating callers yourself](#authenticating-callers-yourself).

## Installing and embedding

```bash
pip install vintasend-api
```

The package is a Django app, `vintasend_api.dashboard`, plus a project that runs it on its
own (see [Running it on its own](#running-it-on-its-own)). A Django project of yours can
serve the API itself: install the app and include its URLs under a prefix.

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "vintasend_api.dashboard",
]

NOTIFICATION_SERVICE_FACTORY = "myproject.notifications.create_notification_service"
VINTASEND_API_AUTHENTICATOR = "myproject.notifications_auth.authenticate"
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    # ...
    path("notifications-api/", include("vintasend_api.dashboard.urls")),
]
```

That serves `/notifications-api/health` and everything under `/notifications-api/api/v1/`.
The app's own namespace is `vintasend_api`, so `reverse("vintasend_api:api-root")` finds the
API wherever it is mounted, and a project can mount
[`vintasend-templates-management-api`](https://github.com/vintasoftware/vintasend-templates-management-api)
beside it without the two colliding. Include the URLconf once per project: a second
include registers the same namespaces again, and `reverse` only ever finds one of them. The
app has no models and no migrations. It needs nothing from the bundled project: not its
settings module, its `handler404` or its middleware.

The app reads these settings, each at the moment it is used, so `override_settings` works
on them. Every one the host leaves out falls back to the default shown.

| Setting | Required | Default | Description |
| --- | --- | --- | --- |
| `NOTIFICATION_SERVICE_FACTORY` | yes | — | Dotted path to the callable building your VintaSend service. See [Configuring your VintaSend service](#configuring-your-vintasend-service). |
| `VINTASEND_API_AUTHENTICATOR` | this or the key | unset | Callable `(request) -> None`, or its dotted path, that authenticates callers. When set, the key is not checked. |
| `VINTASEND_API_KEY` | this or the authenticator | `""` | Shared secret callers send as a bearer token. Only read when no authenticator is set. |
| `VINTASEND_BACKEND_IDENTIFIER` | no | `None` | Read from a non-primary backend registered in your service. |
| `VINTASEND_UNHANDLED_ERROR_HANDLER` | no | unset | Callable `(exc, request, request_id)`, or its dotted path, receiving every unexpected error. See [Unexpected errors](#unexpected-errors). |
| `VINTASEND_API_CORS_ORIGINS` | no | `()` | Browser origins allowed to call the API. Only read by the optional CORS middleware. |
| `GITHUB_REPO` / `GITHUB_API_KEY` | preview only | `""` | Repository holding the templates, and a token with read access to it. |
| `GITHUB_API_BASE_URL` | no | `https://api.github.com` | GitHub API root. |
| `GITHUB_TEMPLATES_BASE_PATH` | no | `""` | Prefix added to template paths before the GitHub lookup. |
| `GITHUB_TEMPLATE_CACHE_MAX_ENTRIES` | no | `100` | Template files kept in memory per process. |
| `GITHUB_TEMPLATE_TIMEOUT_SECONDS` | no | `10` | Timeout of each GitHub request. |

`manage.py check` reports a missing service factory, a missing key when no authenticator is
set, and an authenticator or error handler that cannot be imported. See
[Environment variables](#environment-variables) for the check IDs.

### Authenticating callers yourself

`VINTASEND_API_AUTHENTICATOR` runs before every `/api/v1` route, in place of the shared key.
It refuses a caller by raising `ApiError.unauthorized(...)` when no valid credential was
presented, and `ApiError.forbidden(...)` when it knows who is calling and refuses them: a
401 would tell a signed-in user to sign in again. Both answer in the contract's error
envelope. Returning lets the request through. It may be `async`. It mirrors the TypeScript
reference's `authenticate` option.

`bearer_token(request)` reads the token of an `Authorization: Bearer` header, matching the
scheme in any case, and is `None` when the request carries none:

```python
# myproject/notifications_auth.py
from django.http import HttpRequest

from vintasend_api.dashboard.auth import ApiError, bearer_token

from myproject.identity import verify_access_token  # your own


def authenticate(request: HttpRequest) -> None:
    token = bearer_token(request)
    claims = verify_access_token(token) if token else None
    if claims is None:
        raise ApiError.unauthorized("Sign in first.")
    if "notifications:manage" not in claims.scopes:
        raise ApiError.forbidden("Not allowed.")
```

Give the setting as a dotted path. Assigning the function itself also works, but importing
it into `settings.py` imports django-ninja while the settings are still being defined, and
django-ninja reads its own `NINJA_*` settings at import: any defined further down are missed.

Raise `ApiError`, which `vintasend_api.dashboard.auth` re-exports. The setting has the same
name and shape in `vintasend-templates-management-api`, so a project mounting both can point
them at one function, and that function may raise either package's `ApiError`: a refusal is
recognised by its class name and its code, as the TypeScript packages do, not by its class.
Only `UNAUTHORIZED` and `FORBIDDEN` count as a refusal; an `ApiError` with any other code, like
any other exception, is an unexpected error and answers 500.

`check_api_key(request)` in the same module is the shared-key check the app runs when no
authenticator is set, for an authenticator that still accepts the key, such as from a
server-side caller.

An authenticator that cannot be imported does not fall back to the key: every request is
answered with a 500 until it is fixed, and `manage.py check` reports it as
`vintasend_api.E004`.

Prefer a credential the caller sends explicitly, like the bearer token above. The API's views
are CSRF-exempt, as a bearer-token API's are, so an authenticator that trusts the session
cookie leaves the resend and cancel routes open to cross-site request forgery.

### Optional extras

Two things the bundled project sets up that a host may want too:

- **CORS.** Add `"vintasend_api.dashboard.cors.CorsMiddleware"` to `MIDDLEWARE` and list the
  origins in `VINTASEND_API_CORS_ORIGINS` to let browsers call the API. It applies to the
  API's routes only, under whatever prefix they are mounted. Without it, the host's own CORS
  handling applies, or none.
- **The 404 envelope.** Within the API's routes, errors use the contract's envelope. A path
  that matches no route at all is answered by the project's `handler404`, which in the
  bundled project is `vintasend_api.dashboard.views.envelope_404`. A host can set it too,
  but it then answers every unmatched path in the host's project in that shape.

## Running it on its own

The package also carries a complete Django project for a deployment with no Django project of
its own. It is configured from environment variables, read from a `.env` file in the working
directory when there is one.

```bash
pip install vintasend-api gunicorn
```

Write the factory building your service in a module of your own, say `vintasend_config.py`
in the working directory, and point `NOTIFICATION_SERVICE_FACTORY` at it (see
[Configuring your VintaSend service](#configuring-your-vintasend-service)). Then check the
configuration and serve:

```bash
export DJANGO_SETTINGS_MODULE=vintasend_api.settings
export NOTIFICATION_SERVICE_FACTORY=vintasend_config.create_notification_service
export VINTASEND_API_KEY=...   # or VINTASEND_API_AUTHENTICATOR

python -m django check
gunicorn vintasend_api.wsgi:application --bind 0.0.0.0:3333
```

gunicorn is not a dependency of the package, so install it, or another WSGI server, yourself.
An ASGI server can serve `vintasend_api.asgi:application` instead. The routes are at the root:
`/health` and `/api/v1/`. See [Environment variables](#environment-variables) for the rest of
the configuration.

## Getting started

From a checkout, for development:

```bash
poetry install
cp .env.example .env
```

Then configure the service the API should read from (below), and run:

```bash
poetry run python manage.py runserver 0.0.0.0:3333
```

## Configuring your VintaSend service

The API ships no backend of its own: which database, adapters and template renderer to
use is a deployment decision. Point `NOTIFICATION_SERVICE_FACTORY` at a callable that
returns a configured service:

```python
# vintasend_api/vintasend_config.py
from vintasend.services.notification_service import NotificationService


def create_notification_service():
    backend = ...  # your backend
    renderer = ...  # your template renderer
    adapter = ...  # your notification adapter

    return NotificationService(
        notification_adapters=[adapter],
        notification_backend=backend,
    )
```

In a checkout, start from [`vintasend_api/vintasend_config.example.py`](https://github.com/vintasoftware/vintasend-api/blob/main/vintasend_api/vintasend_config.example.py),
copying it to `vintasend_api/vintasend_config.py` (gitignored). Installed, put it in a module
of your own anywhere on the Python path. The factory is called
once per process and its result reused, so it must be safe to call once and the service
it returns must be safe to share across requests.

Either service class works. `NotificationService` and `AsyncIONotificationService` have
matching method names, and every call the API makes is awaited when it comes back as a
coroutine. The view layer stays synchronous either way, which is what Django ORM
backends need.

This is the same setting VintaSend's background-send worker reads, so one factory can
serve the worker and this API — and pointing both at it guarantees they agree about
which backend holds the notifications.

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `VINTASEND_API_KEY` | unless an authenticator is set | Shared secret clients must send as a bearer token. |
| `VINTASEND_API_AUTHENTICATOR` | no | Dotted path to a callable `(request)` authenticating callers in place of the key. See [Authenticating callers yourself](#authenticating-callers-yourself). |
| `NOTIFICATION_SERVICE_FACTORY` | yes | Dotted path to the callable building your VintaSend service. |
| `VINTASEND_BACKEND_IDENTIFIER` | no | Read from a non-primary backend registered in your service. |
| `VINTASEND_UNHANDLED_ERROR_HANDLER` | no | Dotted path to a callable `(exc, request, request_id)` receiving every unexpected error. See [Unexpected errors](#unexpected-errors). |
| `VINTASEND_API_CORS_ORIGINS` | no | Comma-separated browser origins allowed to call the API. |
| `DJANGO_SECRET_KEY` | no | Django requires one; this API signs nothing. |
| `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS` / `DJANGO_LOG_LEVEL` | no | Standard Django knobs. |
| `DJANGO_DB_*` | no | Only needed by backends that resolve their model through Django. |
| `GITHUB_REPO` | preview only | Repository holding the templates, as `owner/repo` or a full URL. |
| `GITHUB_API_KEY` | preview only | Token with read access to that repository. |
| `GITHUB_API_BASE_URL` | no | Defaults to `https://api.github.com`. |
| `GITHUB_TEMPLATES_BASE_PATH` | no | Prefix added to template paths before the GitHub lookup. |

The `GITHUB_*` variables are only read when `/preview` is called, so the API runs fine
without them if you do not use template previews.

The key and the service factory are enforced by a Django system check, so a deployment
missing either fails on `manage.py check` and on `runserver` rather than on the first
request: `vintasend_api.E001` for the key, which is only required when no authenticator is
set, and `vintasend_api.E002` for the factory. So is a setting naming something that cannot
be imported or called: `vintasend_api.E003` for `VINTASEND_UNHANDLED_ERROR_HANDLER` and
`vintasend_api.E004` for `VINTASEND_API_AUTHENTICATOR`. Run `manage.py check` in your release
step if you serve with gunicorn.

## Unexpected errors

An error the API does not map to a contract error answers a generic 500
`INTERNAL_ERROR` with an `X-Request-Id` header, and is logged as one line: the error's
class, the request id, the method and the route pattern (`api/v1/notifications/<id>`, not
the path). Never its message, its traceback, the request body or the notification id: an
error from the notification store, a provider or a context generator can quote
notification content, recipients and context values, which in the applications this API
serves can be health data. Django's own `django.request` record for the 500 is suppressed
too, since it would repeat the concrete path and attach the request.

The request id is the client's `X-Request-Id` when it matches `[A-Za-z0-9._-]{1,128}`, and
a fresh UUID otherwise, so a client cannot forge a log line through it.

Set `VINTASEND_UNHANDLED_ERROR_HANDLER` to send errors to a tracker with its own scrubbing
instead; keeping health data out of it is then your call. It may be `async`. If it raises,
the redacted line is logged in its place, and what it raised is not.

## Development

```bash
poetry run python manage.py runserver   # dev server
poetry run pytest                       # tests
poetry run ruff check .                 # lint
poetry run ruff format .                # format
poetry run mypy                         # type-check
```

Tests drive the real Django application through the test client with an injected fake
service, so they cover routing, auth, validation, filter negotiation and serialization
without needing a database, a mail provider or GitHub. They mirror the TypeScript
reference's suite case for case, which is what keeps the two implementations honest.

## Notes for anyone comparing the two implementations

The wire contract is identical. These are the places where getting there took different
code, and each is worth knowing if you are implementing the contract a third time.

**Pagination.** Both APIs are 1-indexed on the wire. Underneath, the two ecosystems
genuinely differ: `vintasend-ts` backends page from 0, VintaSend's Python backends page
from 1. So a literal port of either implementation's page arithmetic is wrong in the other.

Neither API hardcodes it any more. Backends report `pagination.oneIndexed` in their
capability map — defaulting to `True` in `vintasend` and `false` in `vintasend-ts`, matching
what their backends actually do — and each API converts from that. Here it lives in
`ServiceCaller`, so the routes only ever deal in contract pages and none of them can forget.
That also covers a custom backend that disagrees with its ecosystem's default, which no
hardcoded rule would.

The whole `pagination.*` namespace is withheld from `/api/v1/capabilities`, in both
implementations: the wire is unconditionally 1-indexed and the conversion happens
server-side, so telling the dashboard about the backend's convention could only lead it to
convert a second time.

Worth knowing because this failure mode is silent — an off-by-one in page numbering raises
nothing, it just serves the wrong page or an empty first one. `vintasend-ts` documented its
backends as 1-indexed when they page from 0; the docs were wrong for long enough that anyone
writing a backend from them would have shipped the bug without a failing test.

**Capability keys.** Every key both libraries define is spelled identically, on purpose,
so the filter negotiation here reads `stringLookups.caseInsensitive` with no translation.

Watch out for one trap if you implement this yourself: both libraries also define
`stringLookups.caseSensitive`, and the pair are **independent capabilities, not a flag and
its negation**. A backend on a case-insensitive collation reports `caseSensitive: False`
and can match only case-insensitively; a backend with no case folding reports
`caseInsensitive: False` and can match only case-sensitively. Deriving either from the
other inverts the answer for exactly the backends that had something to report, and you'd
end up declining the one lookup they support. Read the key you actually mean.

**Filter vocabulary.** The wire is camelCase throughout. The Python filter vocabulary is
snake_case (`notification_type`, `sent_at_range`, `case_sensitive`), so
`dashboard/filters.py` translates. The TypeScript implementation needs no such layer.

**One-off listing.** `vintasend-ts` backends expose `getOneOffNotifications`. The Python
package has no equivalent, and the filter vocabulary has no field discriminating the two
variants, so `GET /notifications/one-off` scans the notification stream and keeps the
one-offs. A service that does expose `get_one_off_notifications` is used directly
instead. The scan is bounded and logs when it truncates.

**Notification lookup.** The contract describes looking an id up as a user notification
first, then as a one-off. Python's `get_notification` returns whichever variant matches,
so that is one call here rather than two. Same outcome.

**Preview context.** `render_email_template_from_content` takes a materialised context in
Python, so this API resolves which context to render with — the stored one, or a
regenerated one — before calling it. The TypeScript version passes either shape down into
its service. Same outcome.

**Template versions.** `requestedTemplateVersion` and `usedTemplateVersion` — both the
payload fields and the two query parameters that filter on them — come from `vintasend`
additions that `vintasend-ts` has not made, so the TypeScript reference serves the fields
as `null` at best and omits them at worst, and ignores the filters, until it catches up.
They are additive and nullable, so a dashboard reading them tolerantly works against
either — but this is the one place where `openapi.yaml` is currently ahead of the
TypeScript implementation rather than describing both.

**`UPSTREAM_ERROR`.** `openapi.yaml` documents a 502 when the template source cannot be
reached. This implementation emits it. The TypeScript reference declares the code but
lets template-source failures fall through to a 500, so that one status differs today —
the normative document is what this follows.

## License

MIT
