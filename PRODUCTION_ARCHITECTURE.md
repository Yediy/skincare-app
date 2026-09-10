# Production Architecture

**Commit this document describes:** see the commit this pass ends on — search this repo's log for "Production Platform Foundation" to find the exact range.

This describes the target production architecture and, separately and explicitly, what of it actually exists in this repository right now. Do not read "target" sections as implemented — each one says so.

## Core principles (binding on all future work in this repository)

1. **PostgreSQL is the transactional source of truth** for `users`, auth state, consent, profiles, measurements, plans, safety decisions, products, ingredients, billing state, usage. Object storage is never used as a substitute.
2. **Cloudflare R2 is object storage only** — media/exports/reports/backups/model assets. Never the source of truth for transactional state.
3. **Redis is disposable.** Cache, rate-limit state, session/revocation cache, coordination, locks, temporary job state. Permanent user data must survive complete Redis loss — already true today: `users`/`refresh_tokens`/`user_profiles`/`consent_events` all live in Postgres, and Redis's own role (revocation cache) is documented to fail closed, not silently lose correctness, if it disappears (`SECURITY_AND_SAFETY_NOTES.md`).
4. **Stateless compute scales horizontally.** No API replica may depend on local filesystem state, sticky memory, or single-process state. Adding a replica must be a deployment change, not a code change.
5. **CV compute scales independently of HTTP workers**, via a queue. **Now built and real**, not merely designed: `POST /api/v2/analyses` enqueues; `python -m app.workers.analysis_worker` is a separate process consuming that queue, running the same CV/scoring/plan/product-matching/routine-safety compute the synchronous `/analyze` path runs (`app.domain.analysis_service.compute_analysis`, shared by both). `/analyze` itself is unchanged and still runs inline — see `ASYNC_ANALYSIS_ARCHITECTURE.md`.
6. **Dokploy/VPS are launch infrastructure, not application dependencies.** The app is a standard OCI container reading standard env config and standard health endpoints — nothing in `app/` may assume a specific orchestrator.
7. **Design for regional cells now, build them later.** Minimal metadata now exists (`users.home_region`/`cell_id`, `analysis_requests.home_region`/`cell_id`, `UserPlacementService`) — see `ASYNC_ANALYSIS_ARCHITECTURE.md`'s cell-readiness section. No cell-based routing, replication, or sharding exists; this is contract readiness only, not infrastructure.

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
`app/storage/base.py` — provider-neutral `ObjectStorage` ABC (`put`/`get`/`delete`/`exists`/`create_upload_authorization`/`create_download_authorization`) plus a small provider-neutral exception hierarchy (`ObjectNotFoundError`, `ObjectStorageUnavailableError`). `app/storage/r2.py` — `CloudflareR2ObjectStorage`, the only module permitted to import `boto3`/`botocore`, mapping R2/S3 errors onto that hierarchy and never logging call arguments (which could be raw object bytes or, for the client itself, credentials). **Now genuinely called**, by `app/storage/ephemeral_image_store.py`'s `EphemeralAnalysisImageStore`, for exactly one purpose: transient raw-face-image transport between the API process and the async CV worker — see "Raw face image policy" below, which this pass's own work required rewriting, not just re-affirming. `tests/storage/test_r2_adapter.py` proves the object-storage contract itself via `botocore.stub.Stubber`, with zero real network calls.

### Postgres connection layer
`app/db/connection.py`'s `init_db_pool` now takes pool min/max size, a per-connection connect timeout, a command timeout, and sets `application_name` — all from `Settings`, not hardcoded. `close_db_pool` already used `pool.close()` (graceful — waits for checked-out connections) rather than `.terminate()`; unchanged, just documented.

### Async analysis foundation — now a complete, real pipeline
Full detail in `ASYNC_ANALYSIS_ARCHITECTURE.md`. Summary: `POST /api/v2/analyses` (`app/api/v2/analyses.py`) → `AnalysisSubmissionService` (consent check, quota reservation, ephemeral image upload, atomic durable-request-creation + job-enqueue) → `python -m app.workers.analysis_worker` (claims the job, heartbeats while `AnalysisExecutionService` runs the same `compute_analysis()` core `/analyze` uses, reusing the submission's own reservation rather than reserving a second one, commits atomically, deletes the raw image) → `GET /api/v2/analyses/{id}` for the caller to poll. `app/queue/base.py`'s `JobQueue` ABC and `app/queue/postgres_queue.py`'s `PostgresJobQueue` back this with real `SELECT ... FOR UPDATE SKIP LOCKED` claim exclusivity, exponential-backoff retry, and a visibility heartbeat (`extend_visibility`) — still Postgres-table-backed, not a new infrastructure dependency. `/analyze` itself is untouched and still runs synchronously inline; the two paths share `compute_analysis()`, not the CV/scoring/planning logic duplicated.

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

Cell-based routing/replication/sharding itself is not built. `home_region`/`cell_id` now exist as plain metadata columns (`users`, `analysis_requests`) and a `UserPlacementService` that assigns/snapshots them — contract readiness only, per this principle's own "build them later." See `ASYNC_ANALYSIS_ARCHITECTURE.md`.

## PgBouncer compatibility (Phase 12)

Already true, not newly built: every RLS-protected query in this codebase sets `app.current_user_id`/`app.current_token_hash` via `set_config(..., true)` (the `true` third argument is what makes it `SET LOCAL`-equivalent — transaction-scoped, not connection-scoped) as the first statement of its own transaction, and no code path anywhere assumes two requests share a physical connection. This was verified directly, not assumed, by `tests/database/test_rls_isolation.py::test_pooled_connection_reuse_does_not_leak_context_between_users`.

**One real, currently-undeployed caveat, stated honestly rather than glossed over**: `asyncpg` uses the extended query protocol with server-side prepared statements by default. PgBouncer's `transaction` pooling mode (the mode that actually delivers PgBouncer's benefit at scale) does not support server-side prepared statements surviving across transactions on a pooled connection, because the underlying physical connection can be handed to a different client between transactions. When PgBouncer is actually introduced in front of Postgres, `asyncpg.create_pool(..., statement_cache_size=0)` (disabling client-side prepared-statement caching) will be required, or PgBouncer must run in `session` mode instead of `transaction` mode. This is not fixed in this pass because PgBouncer is not deployed yet — noted here so it isn't rediscovered the hard way later.

## Raw face image policy

**`/analyze` (the synchronous path) is unchanged**: it does not persist raw face photographs at all. Capture -> transient in-memory processing -> derived `MetricResult` data -> raw image discarded, with no code path writing the decoded image to disk, Postgres, or R2.

**The async path (`POST /api/v2/analyses`) is genuinely different, deliberately, and this is the one place this document's prior "never persisted" claim needed correcting rather than merely re-affirming.** A raw image submitted asynchronously *is* written to Cloudflare R2 — transiently, ephemerally, and only because crossing the API-process-to-worker-process boundary requires the bytes to live somewhere in between. See `RAW_IMAGE_LIFECYCLE.md` for the full policy this implements: a fixed, non-identifying object key (no email/user_id/username in it — the user linkage lives only in `analysis_requests`, RLS-protected, DB-side), a short retention window (default one hour, not months), primary deletion by the worker immediately after terminal processing, and a DB-tracked-expiry safety-net sweeper (`app/workers/image_cleanup.py`) for whatever the primary path misses (a crash, a kill, a network failure). This capability is gated off (`settings.async_image_storage_enabled`, default `False`) until a real legal/product consent review confirms the consent language covers transient third-party cloud processing — this pass has no authority to assert that on its own; see `CONSENT_ASYNC_PROCESSING_REVIEW.md`, whose conclusion this pass did not change.

`ObjectStorage`/`CloudflareR2ObjectStorage` remain available for other honest object-storage needs (product/media assets, exports, reports, model assets) beyond this one ephemeral-transport use — their existence still is not license to start *permanently* archiving face images; any future image-retention feature requires its own consent/retention/deletion/encryption/jurisdiction design before it touches this abstraction for that purpose.
