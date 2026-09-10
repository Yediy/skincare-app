# Failure Domains

**Commit this document describes:** see the commit this pass ends on.

For each component this application depends on: what happens if it disappears, right now, with no warning.

## API node

**What happens if this disappears?** With one replica: total outage. With multiple stateless replicas behind a load balancer: the failed replica is removed from rotation once its `/health/ready` (or the platform's own liveness probe) stops responding; remaining replicas keep serving. No API node holds state that would be lost — `app/main.py` constructs its Postgres pool, Redis client, and CV pipeline fresh on every process start (`startup_event`), and nothing is written to local disk that matters (Phase 9: raw face images are never persisted anywhere, including locally).

**Can users still log in?** Yes, as long as at least one replica and Postgres/Redis are up.
**Can analysis continue?** Yes, same condition.
**Can data be lost?** No — an in-flight request that loses its node fails that one request; nothing durable was ever held only in that process.
**How is it restored?** Orchestrator restarts/replaces the container; `/health/ready` gates it back into rotation only once Postgres and Redis are both reachable from it.
**Blast radius:** One request's worth of in-flight work, scaled down to zero once more than one replica is running.

## Postgres primary

**What happens if this disappears?** Total outage of every stateful operation: no login (`users`), no token refresh (`refresh_tokens`), no profile/consent read or write, no `/analyze` (it depends on a valid consent record). `/health/ready` correctly reports `503` and takes every replica out of rotation rather than serving requests doomed to fail.

**Can users still log in?** No.
**Can analysis continue?** No — `/analyze` requires a live consent check against Postgres before any CV work runs.
**Can data be lost?** Only data written since the last successful WAL archive/backup (see `POSTGRES_OPERATIONS.md`) if the underlying storage itself is destroyed, not just the process. A process crash/restart with intact storage loses nothing (Postgres's own WAL-based crash recovery).
**How is it restored?** Process/instance restart if storage is intact; full restore-from-backup procedure (`POSTGRES_OPERATIONS.md`) if storage is destroyed.
**Blast radius:** Total, for as long as it's down. This is the single largest failure domain in the current architecture — there is no replica to fail over to (see `SCALING_TRIGGERS.md` for when that changes).

## Redis

**What happens if this disappears?** `get_current_user` (`app/security/auth.py`) fails closed: a `RedisError` during the revocation check raises `503`, not a silent "not revoked." Every authenticated request fails with `503` until Redis is back. `/login` and `/signup` are unaffected (they don't touch Redis).

**Can users still log in?** Yes.
**Can analysis continue?** No — `/analyze` requires `get_current_user`.
**Can data be lost?** No permanent data lives in Redis by design (`PRODUCTION_ARCHITECTURE.md`, principle 3) — only revocation-cache entries, which are reconstructable (a lost `revoked_family:*`/`user_tokens_invalid_before:*` key just means that specific revocation is no longer enforced from cache; the underlying `refresh_tokens.revoked_at` row in Postgres is the actual source of truth for whether a *refresh* succeeds, so a refresh-token-family revocation still holds even if its Redis cache entry is gone — only the access-token-level fast-revocation window is affected).
**How is it restored?** Restart; no data migration needed, cache rebuilds itself from normal traffic.
**Blast radius:** All authenticated endpoints, until restored. Unauthenticated endpoints (`/signup`, `/login`, `/health/*`) are unaffected.

## Cloudflare R2

**What happens if this disappears?** Now a real dependency of the async path only (`POST /api/v2/analyses`), not of `/analyze`, which never touches it. `EphemeralAnalysisImageStore.store()` failing at submission time releases the quota reservation and marks the request `FAILED` (`IMAGE_STORAGE_UNAVAILABLE`) rather than leaving it stuck; `retrieve()` failing at execution time is caught and classified by the worker as `ImageRetrievalError` (retryable if it's `ObjectStorageUnavailableError`, terminal if the object is genuinely gone — `ObjectNotFoundError`, most likely because its retention window already expired).

**Can users still log in?** Yes.
**Can analysis continue?** Synchronous `/analyze`: yes, unaffected. Async submission/execution: no new submissions can complete, and in-flight async jobs retry (bounded by `max_attempts`) until R2 is back or they dead-letter.
**Can data be lost?** No transactional data is ever stored here (`PRODUCTION_ARCHITECTURE.md` principle 2) — only the transient raw image, which was always meant to be short-lived (`RAW_IMAGE_LIFECYCLE.md`). A dead-lettered job releases quota and marks the request `FAILED`; nothing is silently lost, the user simply needs to resubmit.
**How is it restored?** Provider-side; nothing this application controls. The cleanup sweeper (`app/workers/image_cleanup.py`) recovers any object whose deletion failed mid-outage once R2 is back.
**Blast radius:** Async submission/execution only. Grows as more features start depending on it for non-ephemeral use (product/media assets, exports, reports).

## CV worker (`app/workers/analysis_worker.py`)

Now real, not target-architecture-only. `python -m app.workers.analysis_worker` is a separate process/deployment consuming `PostgresJobQueue`; `/analyze` still also runs CV work synchronously inline on the API process (unchanged, and remains available for callers that want an immediate result).

**What happens if a worker process disappears (crashes, is killed, hangs on a pathological image)?** The job it was processing stops receiving heartbeats (`extend_visibility`); once `claimed_until` passes, `claim()` makes it reclaimable by another worker replica — proven by a real test (`tests/workers/test_analysis_worker.py::test_heartbeat_prevents_a_concurrent_worker_from_reclaiming`, and the queue's own `test_expired_claim_becomes_reclaimable`). The synchronous `/analyze` path's own known risk is unchanged: a pathological image hanging `pipeline.analyze()` on that path still holds one API replica's event loop hostage, since mediapipe/opencv calls there aren't `await`ed — the async path is exactly the mitigation for that risk when a caller uses it, not a retrofit onto `/analyze` itself.
**Can users still log in?** Yes.
**Can analysis continue?** Yes, via `/analyze`, and via async submission once at least one worker replica is up.
**Can data be lost?** No — a job reclaimed after a crash re-runs `AnalysisExecutionService.execute()` from scratch; `commit_analysis_result()`'s idempotency means a retry after a result already committed does nothing further (no duplicate compute, no double quota consumption).
**How is it restored?** Orchestrator restarts/replaces the worker container/process; no state was held only there.
**Blast radius:** Async analysis throughput only, scaled down to zero once more than one worker replica is running (same shape as the API node's own failure domain above).

## Queue (`PostgresJobQueue`)

Now real, not unused. `app/queue/` (migration `2e77bc462867`, plus `9db3e5856a79`'s retry/heartbeat columns) backs `POST /api/v2/analyses`'s enqueue and the worker's `claim`/`acknowledge`/`fail`/`extend_visibility`.

**What happens if Postgres (and therefore this queue) disappears?** No separate failure domain from "Postgres primary" above — a Postgres outage takes down enqueue/claim exactly as it takes down login. A stuck/crashed worker does not lose work: an unacknowledged claimed job becomes reclaimable again once its `claimed_until` visibility timeout passes. A job that fails retryably backs off exponentially (`BACKOFF_BASE_SECONDS * 2**attempt`) before becoming claimable again; once `max_attempts` is exhausted it dead-letters (`fail()` returns `is_terminal=True`), and the worker releases quota + marks the durable request `FAILED` at that point, not before.

## Dokploy control plane

**What happens if this disappears?** Already-running containers keep running (Dokploy is a deployment/orchestration control plane, not a request-path dependency — this is exactly principle 6 in `PRODUCTION_ARCHITECTURE.md`: the application must not depend on it at runtime). New deploys/restarts/scaling actions are blocked until it's restored.

**Can users still log in?** Yes.
**Can analysis continue?** Yes.
**Can data be lost?** No.
**How is it restored?** Provider/operator action; out of this application's scope.
**Blast radius:** Deployment operations only, not live traffic — as long as the principle above actually holds, which is why it's a binding architectural rule and not just a preference.

## Cloudflare (edge/DNS/proxy, if used in front of the API)

**What happens if this disappears?** Total outage from the public internet's perspective, regardless of how healthy every component behind it is — this is the single point of failure the whole topology funnels through (`PRODUCTION_ARCHITECTURE.md`'s target topology diagram). Mitigations (multi-provider DNS failover, etc.) are a launch-topology decision, not an application-code one, and are out of scope for this document.
