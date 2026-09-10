# Async Analysis Architecture

**Commit this document describes:** the head of `feat/production-recommendation-async-analysis` as of this continuation. Referenced from `app/domain/analysis_submission_service.py`'s own module docstring, which is why this file needed to actually exist.

The full sequence behind `POST /api/v2/analyses` / `GET /api/v2/analyses/{analysis_id}` — from HTTP submission through worker execution to result retrieval. Gated entirely off (`settings.async_image_storage_enabled`, default `False`) until a real legal/product review signs off on transient third-party cloud processing of a face photo — see `CONSENT_ASYNC_PROCESSING_REVIEW.md`, whose conclusion this document does not revisit.

## Sequence

```text
Client                                    Postgres            R2 (ephemeral)     Worker process
  |                                            |                    |                   |
  |-- POST /api/v2/analyses ----------------->|                    |                   |
  |   (image_base64, request_id REQUIRED)     |                    |                   |
  |                                            |                    |                   |
  |   AnalysisSubmissionService.submit():     |                    |                   |
  |     1. async_image_storage_enabled? else 503                   |                   |
  |     2. has_valid_consent()? else 403      |                    |                   |
  |     3. get_request_by_request_id() -- replay? return it        |                   |
  |     4. reserve_analysis()  ---------------|                    |                   |
  |        (AnalysisInProgressError on a genuine race -> poll      |                   |
  |         get_request_by_request_id() for the winner's row       |                   |
  |         instead of surfacing a bare 409 to every loser)        |                   |
  |     5. strict base64 decode (validate=True)                    |                   |
  |     6. create_request()  -----------------|                    |                   |
  |        (failure here releases the reservation from step 4)     |                   |
  |     7. image_store.store()  -------------------------------->  |                   |
  |        (failure releases quota + marks the request FAILED)     |                   |
  |     8. ONE transaction: mark_queued() + job_queue.enqueue() ->|                    |
  |        (failure here = compensation, see below)                |                   |
  |                                            |                    |                   |
  |<-- 202 {analysis_id, request_id, QUEUED} -|                    |                   |
  |                                            |                    |                   |
  |                                            |<---- claim() ------------------------- |
  |                                            |                    |    heartbeat      |
  |                                            |                    |    (extend_       |
  |                                            |                    |     visibility    |
  |                                            |                    |     every 60s)    |
  |                                            |<-- retrieve() ---  |                   |
  |                                            |                    |  compute_analysis()
  |                                            |                    |  (same core       |
  |                                            |                    |   /analyze uses)  |
  |                                            |<-- commit_analysis_result() -----------|
  |                                            |   (COMPLETED, quota CONSUMED, one txn) |
  |                                            |<-- delete_if_confirmed() ------------->|
  |                                            |<-- acknowledge() ---------------------|
  |                                            |                    |                   |
  |-- GET /api/v2/analyses/{id} -------------->|                    |                   |
  |<-- {status: COMPLETED, result, ...} -------|                    |                   |
```

## Quota ownership -- explicit, not implicit

```text
SUBMISSION (AnalysisSubmissionService)  -> reserves, exactly once.
EXECUTION  (AnalysisExecutionService)   -> consumes (atomically, inside
                                            commit_analysis_result(), on
                                            success) or releases
                                            (mark_terminal_failure(), on
                                            a terminal outcome only).
```

`AnalysisExecutionService` never calls `UsagePolicyService.reserve_analysis()`. This is enforced by the module boundary, not a convention a future edit could silently violate unnoticed: `compute_analysis()` (`app/domain/analysis_service.py`), the function both the synchronous and async paths actually call for CV/scoring/planning, takes no `UsagePolicyService` parameter at all — there is no reservation call it *could* make even by accident. `perform_analysis()` (the synchronous wrapper) and `AnalysisExecutionService` (the async wrapper) each own their own reservation lifecycle around that same shared core.

A retryable worker failure never releases the reservation — the same slot is still valid for the next attempt. Only a genuinely terminal outcome (a non-retryable exception, or a retryable one whose `max_attempts` are exhausted) calls `mark_terminal_failure()`, which releases the reservation and marks the durable request `FAILED`, both once.

## Failure-saga compensation

Two failure windows `AnalysisSubmissionService.submit()` explicitly compensates, plus one concurrency gap it resolves without a process-local lock:

1. **`create_request()` fails after `reserve_analysis()` succeeded.** Without compensation, the reservation would be stranded `RESERVED` forever (nothing else has an `analysis_request` to key a later release off of). Fixed: any exception here releases the reservation before propagating.
2. **The image uploads successfully, but the atomic `mark_queued()` + `enqueue()` transaction fails to commit.** The object would otherwise be orphaned — no DB reference for the cleanup sweeper to find, since `mark_queued()` (the only thing that would have written `image_object_key`/`image_expires_at`) is exactly what didn't commit. Compensation (`_compensate_orphaned_upload()`): attempt immediate deletion; if that succeeds, nothing durable to clean up. If deletion itself fails (storage unavailable), `record_ephemeral_image_reference()` persists the reference **independently of the failed transaction**, so `find_overdue_ephemeral_images()` can still recover it once it expires — see `RAW_IMAGE_LIFECYCLE.md`. Either way, the reservation is released and the request marked `FAILED`, each step independently best-effort (a failure in one, e.g. the DB itself being down, does not skip the others).
3. **Two truly concurrent first-time submissions of the same `request_id`.** `usage_repository.reserve()`'s advisory-lock + `UNIQUE(request_id)` semantics correctly serialize them so exactly one gets a fresh `RESERVED` reservation — but the loser previously surfaced that as a bare `AnalysisInProgressError` instead of resolving to the winner's logical analysis, even though the winner is durably creating that exact row microseconds away. `submit()` now catches that specific case and polls `get_request_by_request_id()` (real DB state — `analysis_requests.request_id` is `UNIQUE`) for a bounded window (5s, 50ms interval) before giving up and re-raising the original error. No process-local lock anywhere in this path — it works correctly across multiple API/worker processes for exactly that reason.

Verified by `tests/domain/test_analysis_submission_service.py`, including a real 10-concurrent-submission test (`asyncio.gather` of 10 `submit()` calls for the same user+request_id) asserting exactly one `analysis_request`/`analysis_usage`/`jobs` row and every caller referencing the same logical analysis.

## Worker retry classification

`app/workers/analysis_worker.py`'s `classify_failure()` is a closed set of TERMINAL exception types (never retrying will help): `NoFaceDetectedError`, `CaptureQualityFailedError`, a `ValueError` (image decodes but isn't a valid image), `AnalysisRequestNotFoundError`, `AnalysisRequestInvalidStateError`, and an `ImageRetrievalError` wrapping `ObjectNotFoundError` (the object is genuinely gone, most likely its retention window already expired). Everything else defaults RETRYABLE, bounded by the job's own `max_attempts` — an unanticipated failure mode still dead-letters eventually rather than retrying forever, per this document's own "don't fabricate certainty" posture: this codebase cannot enumerate every possible transient-infrastructure exception type in advance, so the default has to be the safe direction (retry) rather than the convenient one (terminal).

`JobQueue.fail()` returns whether the call left the job in its terminal `failed` state (`True`) or requeued it for a future retry (`False`) — this is what the worker reads to decide whether to call `AnalysisExecutionService.mark_terminal_failure()`. A dead letter always: releases quota, marks the request `FAILED`, and best-effort deletes the raw image.

## Cell-readiness metadata

`users.home_region`/`cell_id` (migration `219c52642ed4`) and `UserPlacementService.get_placement()` assign a user the deployment's configured launch defaults (`settings.launch_home_region`/`launch_cell_id` -- never a literal hardcoded anywhere else) the first time either is needed, idempotently (`COALESCE` -- a user who already has a placement keeps it forever). `POST /api/v2/analyses` snapshots that placement onto the new `analysis_requests` row at submission time, so a later change to a user's own placement (not built) never rewrites history for requests already submitted. This is metadata/contract readiness only -- no cell-based routing, replication, or sharding exists, per `PRODUCTION_ARCHITECTURE.md`'s principle 7.

## What this does not claim

- No SLA on queue latency or worker throughput -- `EMPTY_QUEUE_POLL_INTERVAL_SECONDS` is a plain poll loop (the launch implementation `app/queue/base.py`'s own docstring describes), not a push-based dispatch mechanism.
- No autoscaling of worker replica count -- deploying more workers is an operational action (`DOKPLOY_DEPLOYMENT.md`), not something this codebase does for you.
- No image retention beyond the ephemeral transport window -- see `RAW_IMAGE_LIFECYCLE.md`. This is not a photo-history feature.
