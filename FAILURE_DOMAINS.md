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

**What happens if this disappears?** Nothing in the current request path depends on it — no route calls `ObjectStorage`/`CloudflareR2ObjectStorage` yet (Phase 6/7 built the abstraction; nothing consumes it yet, deliberately — see `PRODUCTION_ARCHITECTURE.md`'s Raw Face Image Policy). Once a real feature (product/media assets, exports, reports) depends on it, an R2 outage would degrade that feature specifically; `ObjectStorageUnavailableError` is the typed exception a caller would catch to degrade gracefully rather than 500.

**Can users still log in?** Yes.
**Can analysis continue?** Yes.
**Can data be lost?** N/A currently — no transactional data is ever stored here (`PRODUCTION_ARCHITECTURE.md` principle 2).
**How is it restored?** Provider-side; nothing this application controls.
**Blast radius:** Currently zero. Grows only as real features start depending on it.

## CV worker (not built yet — target architecture)

Currently, CV work (`FacialAnalysisPipeline`) runs synchronously inside the API process handling `/analyze`. There is no separate CV worker yet (Phase 14/15, deferred).

**What happens if this disappears (i.e., today, if CV compute itself hangs or crashes)?** It takes down the one API replica handling that request; other replicas are unaffected. A pathological image that hangs `pipeline.analyze()` indefinitely would hold that replica's event loop hostage for the duration (mediapipe/opencv calls are synchronous, not `await`ed) — a real, currently-unmitigated risk worth flagging honestly rather than glossing over, since it's the reason Phase 14/15's queue-based extraction is on the roadmap at all, not just a scale optimization.

## Queue (not built yet)

N/A — doesn't exist. See `OPEN_ENGINEERING_ITEMS.md` Phase 15.

## Dokploy control plane

**What happens if this disappears?** Already-running containers keep running (Dokploy is a deployment/orchestration control plane, not a request-path dependency — this is exactly principle 6 in `PRODUCTION_ARCHITECTURE.md`: the application must not depend on it at runtime). New deploys/restarts/scaling actions are blocked until it's restored.

**Can users still log in?** Yes.
**Can analysis continue?** Yes.
**Can data be lost?** No.
**How is it restored?** Provider/operator action; out of this application's scope.
**Blast radius:** Deployment operations only, not live traffic — as long as the principle above actually holds, which is why it's a binding architectural rule and not just a preference.

## Cloudflare (edge/DNS/proxy, if used in front of the API)

**What happens if this disappears?** Total outage from the public internet's perspective, regardless of how healthy every component behind it is — this is the single point of failure the whole topology funnels through (`PRODUCTION_ARCHITECTURE.md`'s target topology diagram). Mitigations (multi-provider DNS failover, etc.) are a launch-topology decision, not an application-code one, and are out of scope for this document.
