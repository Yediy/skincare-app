# Production Architecture

**Commit this document describes:** see the commit this pass ends on — search this repo's log for "Production Platform Foundation" to find the exact range.

This describes the target production architecture and, separately and explicitly, what of it actually exists in this repository right now. Do not read "target" sections as implemented — each one says so.

## Core principles (binding on all future work in this repository)

1. **PostgreSQL is the transactional source of truth** for `users`, auth state, consent, profiles, measurements, plans, safety decisions, products, ingredients, billing state, usage. Object storage is never used as a substitute.
2. **Cloudflare R2 is object storage only** — media/exports/reports/backups/model assets. Never the source of truth for transactional state.
3. **Redis is disposable.** Cache, rate-limit state, session/revocation cache, coordination, locks, temporary job state. Permanent user data must survive complete Redis loss — already true today: `users`/`refresh_tokens`/`user_profiles`/`consent_events` all live in Postgres, and Redis's own role (revocation cache) is documented to fail closed, not silently lose correctness, if it disappears (`SECURITY_AND_SAFETY_NOTES.md`).
4. **Stateless compute scales horizontally.** No API replica may depend on local filesystem state, sticky memory, or single-process state. Adding a replica must be a deployment change, not a code change.
5. **CV compute scales independently of HTTP workers**, eventually via a queue (target architecture below; not built this pass — see Phase 14/15 in `OPEN_ENGINEERING_ITEMS.md`).
6. **Dokploy/VPS are launch infrastructure, not application dependencies.** The app is a standard OCI container reading standard env config and standard health endpoints — nothing in `app/` may assume a specific orchestrator.
7. **Design for regional cells now, build them later.** Not started this pass (Phase 23 — deferred).

## What actually exists in this repository as of this pass

### Containerization
`backend/Dockerfile` — real multi-stage build. Builder stage (`build-essential`) resolves any wheel that needs compiling; nothing from it ships in the final image. Runtime stage installs only `libgl1`/`libglib2.0-0` (mediapipe's real, verified runtime dependency — see below) plus the installed Python packages, runs as a non-root `appuser` (uid 1000), and defines `HEALTHCHECK` against `/health/live`. `backend/.dockerignore` excludes `.git`, `.env*`, credentials, caches, and the test suite itself from the build context.

### Health and readiness
`GET /health/live` (`app/main.py`) — process-alive only, no dependency checks, so a transient Postgres/Redis outage can never cause a liveness-driven restart loop.
`GET /health/ready` — checks `SELECT 1` against Postgres and `PING` against Redis; either failing returns `503` with a per-dependency breakdown so the instance is correctly pulled out of rotation.

### Production configuration validation
`app/config.py`'s `Settings` model fails application startup outright (not a log warning) when `ENVIRONMENT=production` and any of: a blank/placeholder/short `JWT_SECRET`, a `DATABASE_URL`/`REDIS_URL` containing a known dev/CI marker (`localhost`, the dev Postgres role's own password, etc.), `ENABLE_DOCS=true`, wildcard `ALLOWED_ORIGINS`, or a placeholder-looking R2 credential. `tests/unit/test_config_validation.py` proves each rejection individually and proves development config is untouched by these rules.

### Secrets boundary
`.gitignore` already excluded `.env`/credentials before this pass (verified, not assumed). `.github/workflows/ci.yml` now runs `gitleaks` as its own job on every push/PR.

### Object storage abstraction
`app/storage/base.py` — provider-neutral `ObjectStorage` ABC (`put`/`get`/`delete`/`exists`/`create_upload_authorization`/`create_download_authorization`) plus a small provider-neutral exception hierarchy (`ObjectNotFoundError`, `ObjectStorageUnavailableError`). `app/storage/r2.py` — `CloudflareR2ObjectStorage`, the only module permitted to import `boto3`/`botocore`, mapping R2/S3 errors onto that hierarchy and never logging call arguments (which could be raw object bytes or, for the client itself, credentials). Nothing in the application calls this yet — see "Raw face image policy" below for why that's deliberate, not an oversight. `tests/storage/test_r2_adapter.py` proves the full contract via `botocore.stub.Stubber`, with zero real network calls and zero new dependencies (botocore ships with boto3, already in `requirements.txt`).

### Postgres connection layer
`app/db/connection.py`'s `init_db_pool` now takes pool min/max size, a per-connection connect timeout, a command timeout, and sets `application_name` — all from `Settings`, not hardcoded. `close_db_pool` already used `pool.close()` (graceful — waits for checked-out connections) rather than `.terminate()`; unchanged, just documented.

### Async analysis foundation (Phases 14-16, a later pass)
`app/domain/analysis_service.py`'s `perform_analysis()` is `/analyze`'s full consent → decode → CV → profile → score → plan sequence, extracted from the HTTP route handler into a plain async function with no FastAPI dependency, raising framework-agnostic exceptions and taking `pipeline`/`scorer`/`plan_service` as parameters rather than constructing them — so a future background worker can call this exact function. `/analyze` itself is now a thin adapter and its behavior is unchanged (verified by the pre-existing E2E suite passing unchanged, plus new tests calling `perform_analysis()` directly with zero HTTP involved). `app/queue/base.py`'s `JobQueue` ABC and `app/queue/postgres_queue.py`'s `PostgresJobQueue` (migration `2e77bc462867`) provide a generic, provider-neutral job queue backed by Postgres rather than a new infrastructure dependency, with real `SELECT ... FOR UPDATE SKIP LOCKED`-based claim exclusivity and idempotent enqueue-by-`request_id` (a partial unique index, not application-level check-then-insert). Nothing calls `enqueue`/`claim` from any real code path yet — same deliberate-but-unused posture as the object storage abstraction above.

## Target architecture (not built this pass unless stated above)

```text
Cloudflare
    |
Dokploy / reverse proxy
    |
API replicas (stateless, horizontally scaled)
    |
    +-- Postgres (primary + read replica later)
    +-- Redis (cache / rate limits / revocation)
    +-- R2 (object storage)

Later:
    API -> queue -> CV worker replicas -> result
```

Eventually:

```text
Global Edge
    |
Regional routing
    |
Cell
 +- API
 +- Redis
 +- Queue
 +- CV workers
 +- Postgres shard
```

Not built. `home_region`/`cell_id` are not yet fields on any table (Phase 23, `OPEN_ENGINEERING_ITEMS.md`).

## PgBouncer compatibility (Phase 12)

Already true, not newly built: every RLS-protected query in this codebase sets `app.current_user_id`/`app.current_token_hash` via `set_config(..., true)` (the `true` third argument is what makes it `SET LOCAL`-equivalent — transaction-scoped, not connection-scoped) as the first statement of its own transaction, and no code path anywhere assumes two requests share a physical connection. This was verified directly, not assumed, by `tests/database/test_rls_isolation.py::test_pooled_connection_reuse_does_not_leak_context_between_users`.

**One real, currently-undeployed caveat, stated honestly rather than glossed over**: `asyncpg` uses the extended query protocol with server-side prepared statements by default. PgBouncer's `transaction` pooling mode (the mode that actually delivers PgBouncer's benefit at scale) does not support server-side prepared statements surviving across transactions on a pooled connection, because the underlying physical connection can be handed to a different client between transactions. When PgBouncer is actually introduced in front of Postgres, `asyncpg.create_pool(..., statement_cache_size=0)` (disabling client-side prepared-statement caching) will be required, or PgBouncer must run in `session` mode instead of `transaction` mode. This is not fixed in this pass because PgBouncer is not deployed yet — noted here so it isn't rediscovered the hard way later.

## Raw face image policy (Phase 9)

Unchanged from the existing, pre-this-pass direction: `/analyze` does not persist raw face photographs. The pipeline is capture -> transient in-memory processing -> derived `MetricResult` data -> raw image discarded, with no code path writing the decoded image to disk, Postgres, or (now that it exists) R2. `ObjectStorage`/`CloudflareR2ObjectStorage` exist as infrastructure for cases that already need object storage honestly (product/media assets, exports, reports, model assets) — their existence is not license to start persisting face images. Any future image-retention feature requires its own consent/retention/deletion/encryption/jurisdiction design before it touches this abstraction.
