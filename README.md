# VintaSend API (Python)

REST API that exposes a [VintaSend](https://github.com/vintasoftware/vintasend)
notification service over HTTP, built with Django and
[django-ninja](https://django-ninja.dev/).

It exists so the [VintaSend dashboard](https://github.com/vintasoftware/vintasend-dashboard)
no longer has to embed a notification service: the dashboard is a pure API client, and
any implementation of this contract can serve it.

**[`openapi.yaml`](./openapi.yaml) is the contract.** It is shipped here byte-identical
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
- `hasMore` is `true` when a page comes back full. Backends are not required to produce
  a total count.
- List rows carry a `kind` field (`user` or `one-off`) so clients can discriminate
  without sniffing for the presence of fields.
- Timestamps are ISO-8601 UTC strings, `null` when unset — never absent.
- Errors always use the envelope `{ "error": { "code", "message", "details"? } }`.

## Authentication

Every `/api/v1` request must carry the shared secret:

```
Authorization: Bearer $VINTASEND_API_KEY
```

The dashboard calls this API only from its own server side, so the key never reaches a
browser. If you do need to call the API from a browser, set `VINTASEND_API_CORS_ORIGINS`
to the allowed origins — and put a per-user auth layer in front of it first.

## Getting started

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

Start from [`vintasend_api/vintasend_config.example.py`](./vintasend_api/vintasend_config.example.py),
copying it to `vintasend_api/vintasend_config.py` (gitignored). The factory is called
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
| `VINTASEND_API_KEY` | yes | Shared secret clients must send as a bearer token. |
| `NOTIFICATION_SERVICE_FACTORY` | yes | Dotted path to the callable building your VintaSend service. |
| `VINTASEND_BACKEND_IDENTIFIER` | no | Read from a non-primary backend registered in your service. |
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

The first two are enforced by a Django system check, so a deployment missing either
fails on `manage.py check` and on `runserver` rather than on the first request. Run
`manage.py check` in your release step if you serve with gunicorn.

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

**`UPSTREAM_ERROR`.** `openapi.yaml` documents a 502 when the template source cannot be
reached. This implementation emits it. The TypeScript reference declares the code but
lets template-source failures fall through to a 500, so that one status differs today —
the normative document is what this follows.

## License

MIT
