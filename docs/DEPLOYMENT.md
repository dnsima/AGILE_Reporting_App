# Deployment

## Requirements

* Python 3.11 or newer
* PostgreSQL 14+ for production (SQLite is fine for evaluation and pilots)
* A reverse proxy terminating TLS

## The quickest safe path: Docker Compose

Point a DNS A record at the server, then:

```bash
git clone -b claude/agile-reporting-platform-yecfje \
    https://github.com/dnsima/AGILE_Reporting_App.git
cd AGILE_Reporting_App

cp .env.production.example .env     # fill in AGILE_DOMAIN and the secrets
docker compose up -d
docker compose run --rm app python -m scripts.seed
```

That brings up three containers: the application, PostgreSQL, and Caddy as the
reverse proxy. Caddy obtains and renews the HTTPS certificate from Let's
Encrypt on its own, so there is no certbot step and no renewal cron to forget.
The database and the storage volume are not published to the host; only ports
80 and 443 are.

Then sign in at `https://your-domain/dashboard` as the bootstrap
administrator, change that password, and create the real NPCU and state
accounts.

To load the real returns rather than start empty, copy the workbooks onto the
server and:

```bash
docker compose run --rm -v /path/to/workbooks:/data app \
    python -m scripts.load_npcu_models \
        --q2 /data/AGILE_Q2_2026_Analysis_Model_Flagged.xlsx \
        --q1 /data/AGILE_Q1_2026_Analysis_Model_v3_5.xlsx \
        --crosswalk /data/AGILE_Q1_vs_Q2_2026_Model_Comparison.xlsx
```

## Before serving real traffic

With `ENVIRONMENT=production` the app **refuses to start** if any of these is
left at its default. Setting `production` is the operator saying "this is
live", and a warning in a container log scrolls past while the server signs
real sessions with a key published in this repository.

| Setting | Do this | Refuses to start? |
|---|---|---|
| `SECRET_KEY` | A long random value — it signs every session token. `python -c "import secrets; print(secrets.token_urlsafe(48))"` | Yes, if default or under 32 characters |
| `BOOTSTRAP_ADMIN_PASSWORD` | Set one, then change it again from inside the app after first sign-in | Yes, if left at `ChangeMe!2024` |
| `DEBUG` | `false`, so internal exception text is not returned to callers | Yes, if on |
| `CORS_ORIGINS` | Leave empty for same-origin only — what the bundled dashboard needs. List exact origins only if something else calls the API from a browser | Yes, if `*` |
| `ENVIRONMENT` | `production` | — |
| `DATABASE_URL` | PostgreSQL. SQLite serialises writes and will block under concurrent state uploads | No, but do it |

`CORS_ORIGINS=*` is refused because the API accepts a session cookie: with
credentials enabled, `*` makes the server echo back whatever origin asks.
`SameSite=Lax` on that cookie stops the obvious cross-site attack, but the
configuration should not depend on it.

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

Nothing is overwritten: re-uploads supersede rather than replace, a restated
figure keeps its original, and the audit trail is append-only. That makes this
the reporting record, so back it up as one.

The database and the storage volume must be backed up **together** — a
submission row points at a file on disk, and a database restored without its
evidence files is a record with holes in it.

```bash
# Database
docker compose exec -T db pg_dump -U agile agile | gzip > agile-$(date +%F).sql.gz

# Uploads, evidence and published reports
docker run --rm -v agile_reporting_app_app-storage:/storage -v "$PWD":/backup \
    alpine tar czf /backup/agile-storage-$(date +%F).tar.gz -C /storage .
```

Run both from a cron job and keep the pair. Check the volume name with
`docker volume ls` — Compose prefixes it with the project directory.

## Upgrading

```bash
git pull
docker compose build app
docker compose up -d
docker compose run --rm app python -m scripts.seed   # if the catalogue changed
```

Missing tables and columns are added on startup by `init_db()`. It only ever
adds — never drops, renames or retypes — and skips any column it cannot add
without inventing a value for the rows already there, so a schema change never
arrives as "no such column". Re-seeding retires indicators and states the seed
files no longer carry rather than leaving them active beside the new ones.

That covers everything this project has needed so far. A change that has to
*alter* an existing column — a type change, a new NOT NULL without a default —
is beyond it, and is the point at which to add Alembic:

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
