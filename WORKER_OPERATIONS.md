# Worker Operations

**Commit this document describes:** the head of `feat/production-recommendation-async-analysis` as of this continuation. Referenced from `app/workers/image_cleanup.py`'s own module docstring, which is why this file needed to actually exist.

Two independent worker entry points. Neither is a long-running orchestrator of the other — each is a plain process/script, deployed and scaled independently.

## `python -m app.workers.analysis_worker`

The real queue-consuming CV worker. Runs `run_forever()`: claim -> heartbeat alongside execution -> acknowledge/classify-and-fail, looping with a plain poll (`EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 2.0`) when the queue is empty — not a push-based dispatch mechanism, per `app/queue/base.py`'s own "don't prematurely deploy Kafka/Pulsar" posture.

**Deployment**: its own Dokploy service/container, same image as the API (`backend/Dockerfile`), different entrypoint/`CMD`. Requires `DATABASE_URL` and, since `async_image_storage_enabled` gates whether any job exists to claim, the same `R2_*` credentials as the API whenever that flag is on. See `DOKPLOY_DEPLOYMENT.md`.

**Scaling**: horizontally, by running more replicas — `claim()`'s `SELECT ... FOR UPDATE SKIP LOCKED` guarantees two replicas never receive the same job (proven by a real concurrent-claim test, `tests/queue/test_postgres_job_queue.py::test_concurrent_claims_never_return_the_same_job`). Scale independently of API replica count per `SCALING_TRIGGERS.md` — CV compute cost is per-analysis, not per-HTTP-request.

**Visibility timeout and heartbeat**: `CLAIM_VISIBILITY_TIMEOUT_SECONDS = 300` is the window a claim survives without a heartbeat before another replica may reclaim it. `HEARTBEAT_INTERVAL_SECONDS = 60` / `HEARTBEAT_EXTENSION_SECONDS = 300` — a background `asyncio.Task` extends `claimed_until` every 60s while `AnalysisExecutionService.execute()` is still running, so a CV run that genuinely takes longer than 300s (a slow image, a loaded host) doesn't lose its claim mid-processing. Proven against real Postgres timing, not a mocked clock (`tests/workers/test_analysis_worker.py::test_heartbeat_prevents_a_concurrent_worker_from_reclaiming`). The heartbeat task is cancelled (not awaited to natural completion) once the main task finishes either way — `CancelledError` there is the expected clean-shutdown path, not an error.

**No Postgres transaction is held open across the CV pipeline call.** `claim()`/`acknowledge()`/`fail()`/`extend_visibility()` are each their own short-lived transaction; `AnalysisExecutionService` opens its own transactions only around discrete DB operations (`mark_processing()`, `commit_analysis_result()`), never around `compute_analysis()` itself.

**Retry classification and dead-lettering**: see `ASYNC_ANALYSIS_ARCHITECTURE.md`'s dedicated section. Operationally: a job stuck retrying (visible via `jobs.attempt_count` approaching `max_attempts`, or the `analysis_processing` structured event with `outcome=RETRY` recurring for the same `job_id`) usually means either a genuinely flaky dependency (R2, Postgres) or a failure mode `classify_failure()` doesn't yet recognize as terminal and should. A job that reaches `outcome=DEAD_LETTER` has already had quota released and the request marked `FAILED` — no further operator action is required for that specific request; recurring dead letters across many requests are the signal worth investigating.

**No `/health/*` endpoint exists for this process.** Monitor it via:
- The structured events it emits (`app/observability/events.py`): `queue_claim` (is it claiming anything at all?), `queue_wait_seconds` (is the queue backing up?), `analysis_processing` (`SUCCESS`/`RETRY`/`DEAD_LETTER` with `duration_seconds`).
- The `jobs` table's own row counts by `status` — a real, directly queryable operational signal (`pending` count growing without bound means no worker is claiming; `claimed` rows with `claimed_until` far in the past mean a worker died without cleanly failing its job, which the next `claim()` call will still correctly reclaim).

## `python -m app.workers.image_cleanup`

The safety-net sweeper for raw images the primary deletion path (the analysis worker itself, on both success and terminal failure) didn't clean up — a crash, a kill, a network failure between "processing finished" and "delete call completed." See `RAW_IMAGE_LIFECYCLE.md` for the two-path design this is half of.

**Not a long-running process** — one sweep (`run_cleanup_sweep()`) per invocation, up to `batch_limit=100` overdue images. **Deployment**: a scheduled/cron-triggered job (Dokploy's own scheduling mechanism, or any external scheduler that can run a container command on an interval), not a Dokploy "service." How the scheduling itself is wired up is a deployment concern this document intentionally leaves to the operator, not something this module manages.

**Sizing the schedule's cadence**: against `EphemeralAnalysisImageStore.DEFAULT_RETENTION` (one hour, currently a fixed constant). A sweep interval materially shorter than the retention window (e.g. every 10-15 minutes) keeps the real exposure window for an orphaned object close to the retention window itself, not retention-plus-however-long-until-the-next-sweep-happens-to-run.

**Failure tracking**: `CleanupSweepResult.failed`/`failed_request_ids` — a storage-unavailable failure during a sweep is logged (`image_cleanup_failure` structured event) and left for the next sweep to retry, never silently dropped. A sweep that reports a nonzero `failed` count is not itself an emergency (the reference is still safely tracked in the database) but a recurring nonzero count across multiple sweeps means R2 itself is likely degraded, worth escalating the same way any other R2 outage would be (`FAILURE_DOMAINS.md`'s Cloudflare R2 entry).

## What this does not claim

- No alerting/paging is wired to any of the signals above — they are real, queryable/observable data, not a configured alert.
- No autoscaling policy for worker replica count exists in this repository — see `SCALING_TRIGGERS.md` for when to consider one.
- No dead-letter-queue *inspection* UI/route exists — a dead-lettered job's terminal state and the associated `FAILED` analysis request are both queryable directly (via `jobs`/`analysis_requests`, or `GET /api/v2/analyses/{id}` for the request's own client-safe view), not exposed through any dedicated operational tooling yet.
