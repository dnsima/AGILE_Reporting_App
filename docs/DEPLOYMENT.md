# Deployment

## Requirements

* Python 3.11 or newer
* PostgreSQL 14+ for production (SQLite is fine for evaluation and pilots)
* A reverse proxy terminating TLS

## Before serving real traffic

| Setting | Do this |
|---|---|
| `SECRET_KEY` | Replace the default with a long random value — it signs every session token. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BOOTSTRAP_ADMIN_PASSWORD` | Change it, then change the password again from inside the app after first sign-in |
| `ENVIRONMENT` | Set to `production`. The app logs an error at startup if the default `SECRET_KEY` is still in place |
| `DEBUG` | `false`, so internal exception text is not returned to callers |
| `DATABASE_URL` | Point at PostgreSQL |
| `CORS_ORIGINS` | List the dashboards that may call the API, instead of `*` |

## PostgreSQL

```bash
pip install "psycopg[binary]"
export DATABASE_URL="postgresql+psycopg://agile:secret@db:5432/agile"
python -m scripts.seed
```

The engine switches to a pooled connection (`pool_size=10`, `max_overflow=20`,
`pool_pre_ping=True`) for any non-SQLite URL. No model changes are needed —
JSON columns map to `json` on PostgreSQL and to serialised text on SQLite.

## Running

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Tables are created and the validation rule catalogue is synchronised at startup,
so a fresh deployment only needs `python -m scripts.seed` for reference data.

### A note on workers

The dashboard's live updates use an **in-process** event bus. With more than one
worker, a browser connected to worker A will not receive a server-sent event for
an upload handled by worker B. The dashboard degrades gracefully — it also polls
`/api/v1/dashboard/version` every 30 seconds and refreshes on a version change —
so multi-worker deployments still update, just on the poll interval rather than
instantly. For instant updates across workers, either run a single worker with
sticky sessions at the proxy, or replace `app/core/events.py` with a Redis
pub/sub implementation behind the same `EventBus` interface.

### Reverse proxy

Server-sent events need buffering disabled and a long read timeout on
`/api/v1/dashboard/stream`:

```nginx
location /api/v1/dashboard/stream {
    proxy_pass              http://app:8000;
    proxy_http_version      1.1;
    proxy_set_header        Connection "";
    proxy_buffering         off;
    proxy_cache             off;
    proxy_read_timeout      1h;
}

location / {
    proxy_pass       http://app:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 50m;   # match MAX_UPLOAD_MB
}
```

`X-Forwarded-For` is read for the audit trail's IP address, and
`client_max_body_size` must be at least `MAX_UPLOAD_MB` or large uploads fail at
the proxy before reaching the app.

## Storage

Two directories need to persist and be backed up alongside the database:

* `UPLOAD_DIR` — the original submitted files, retained for audit
* `REPORT_DIR` — generated report artifacts

In a container deployment these must be volumes, not layer storage.

## Health checks

`GET /api/v1/health` is public and returns `200` with:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "production",
  "database": "ok",
  "data_version": 42,
  "checks": { "states": 37, "indicators": 70, "periods": 57, "submissions": 148, "seeded": true }
}
```

`status` is `degraded` when the database is unreachable or reference data has
not been seeded. Use it as both the liveness and readiness probe.

## Logging

Logs are JSON lines by default (`LOG_JSON=true`), one object per line, each
carrying `request_id` and `actor`. The same `request_id` is returned in the
`X-Request-ID` header and in every error envelope, so a user-reported error can
be traced to its log lines directly.

Set `LOG_JSON=false` for readable console output in development.

## Backup and retention

The platform is designed to hold history indefinitely for longitudinal
tracking — nothing is overwritten. Re-uploads supersede rather than replace, and
the audit trail is append-only. Back up the database and both storage
directories together, since submissions reference files on disk.

## Upgrading

`Base.metadata.create_all()` creates missing tables but does not alter existing
ones. For schema changes after the first release, add Alembic:

```bash
pip install alembic
alembic init migrations          # point env.py at app.db.base:Base.metadata
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Security notes

* Passwords are bcrypt-hashed; anything over bcrypt's 72-byte limit is
  pre-hashed rather than silently truncated.
* Session tokens are HS256 JWTs signed with `SECRET_KEY`; rotating the key
  invalidates every outstanding session.
* API keys are stored only as SHA-256 hashes and cannot carry `ADMIN` or `NPCU`
  rights.
* `STATE_PIU` accounts are scoped to a single state on read, upload and report.
* The login endpoint returns an identical message for an unknown account and a
  wrong password, so it cannot be used to enumerate users.
* The session cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` whenever
  `ENVIRONMENT` is production.
