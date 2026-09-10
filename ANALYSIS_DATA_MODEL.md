# Analysis Data Model

**Commit this document describes:** the head of `feat/production-recommendation-async-analysis` as of this continuation.

The full set of tables one analysis attempt touches, from submission through result persistence. All are user-owned data with row-level security (`app.current_user_id`-scoped), same pattern as `user_profiles`/`consent_events` — except `jobs`, which deliberately has none (see below).

## `analysis_requests` (migration `b034483cb876`)

One row per logical analysis attempt. `request_id` is `UNIQUE` — the same idempotency contract `analysis_usage`/`jobs` use.

| Column | Notes |
|---|---|
| `status` | `RECEIVED` -> `QUEUED` -> `PROCESSING` -> `COMPLETED`/`FAILED`/`CANCELLED`. A `CHECK` constraint enforces the closed set; `app/db/analysis_repository.py` module constants (`RECEIVED`/`QUEUED`/etc.) are the only place these strings should be written from. |
| `usage_reservation_id` | FK into `analysis_usage` — the reservation `AnalysisSubmissionService` created; `AnalysisExecutionService` reads this to know which reservation to consume/release, never creates a new one. |
| `image_object_key` / `image_expires_at` | A reference into the *ephemeral* object store (`RAW_IMAGE_LIFECYCLE.md`) — never the image bytes themselves. `NULL` once the object is confirmed deleted (`clear_image_reference()`). |
| `home_region` / `cell_id` | Snapshotted from `UserPlacementService.get_placement()` at submission (migration `219c52642ed4` added the matching columns on `users`) — so a later change to a user's own placement never rewrites history for a request already submitted. |
| `attempt_count` | Incremented by `increment_attempt_count()` — currently unused by any caller in this pass (the job-level `jobs.attempt_count` is what the worker's retry logic actually reads); kept for a future per-request-level retry-count surface. |
| `error_code` | Set only by `mark_failed()`, always one of the closed set of safe classifications `app/workers/analysis_worker.py`'s `classify_failure()` assigns — never an exception message. |

Grants: `SELECT, INSERT, UPDATE` (its own status/timestamp columns are genuinely mutated across the lifecycle — unlike the three result-shaped tables below).

## `analysis_results` / `analysis_measurements` / `analysis_product_recommendations` (same migration)

Write-once, append-only rows — grants are `SELECT, INSERT` only, no `UPDATE`/`DELETE`. All three are written together, exactly once, inside `app/db/analysis_repository.py`'s `commit_analysis_result()` — see "Atomic commit" below.

- **`analysis_results`**: one row per `COMPLETED` request, `UNIQUE(analysis_request_id)`. `capture_assessment`/`scores`/`plan` are the same JSONB shapes `compute_analysis()`'s `AnalysisResult` already carries — no reshaping between "what the domain layer computed" and "what gets persisted."
- **`analysis_measurements`**: one row per metric per request, `UNIQUE(analysis_request_id, metric_name)`. `value IS NULL` is a real, meaningful, preserved outcome (an `ABSTAINED` metric), not an omission — `status` records exactly why.
- **`analysis_product_recommendations`**: one row per routine step that got a specific product match, `UNIQUE(analysis_request_id, plan_step_key)` — the same provenance `app/domain/recommendation_service.py`'s `StepProductRecommendation` carries in-memory, persisted for later audit/reconstruction. A step with no concrete match (Phase 11's fallback, or this pass's P0 fail-closed backstop — `PRODUCT_RECOMMENDATION_PIPELINE.md`) simply has no row here at all, never a placeholder.

## Atomic commit (`commit_analysis_result()`)

One transaction: `analysis_results` (once, `ON CONFLICT DO NOTHING`) + every `analysis_measurements` row (`ON CONFLICT DO NOTHING`) + every `analysis_product_recommendations` row (`ON CONFLICT DO NOTHING`) + `analysis_requests.status = 'COMPLETED'` (`WHERE status != 'COMPLETED'`) + `analysis_usage.status = 'CONSUMED'` (`WHERE status = 'RESERVED'`) — all commit together or none does. The `ON CONFLICT`/`WHERE` guards are what make a retry after this transaction already committed a genuine no-op: re-running the whole function inserts nothing twice and doesn't re-consume an already-`CONSUMED` reservation. `AnalysisExecutionService.execute()` short-circuits even earlier for exactly this case — it checks `status == 'COMPLETED'` before doing any compute at all, so a worker replaying a job after a crash between commit and `acknowledge()` never re-runs the CV pipeline either.

## `analysis_usage` (migration `ee276e90a60f`, pre-existing)

The quota reservation ledger — `RESERVED`/`CONSUMED`/`RELEASED`, one row per `request_id`. Full detail in `USAGE_AND_RATE_LIMIT_ARCHITECTURE.md`. This document's own addition: `analysis_requests.usage_reservation_id` is the join key `AnalysisExecutionService` uses to find the reservation submission already created, and quota consumption for the async path happens inside `commit_analysis_result()`'s own transaction (above), not a separate call.

## `jobs` (migration `2e77bc462867`, retry/heartbeat columns added by `9db3e5856a79`)

Deliberately **no row-level security** — this is the queue's own internal bookkeeping, not user-owned data a caller ever queries directly by `user_id`; every row is addressed by `job_type`+`request_id` (idempotent enqueue) or by `id` (claim/ack/fail). `payload` carries `{"analysis_request_id": ..., "user_id": ...}` — the join back to user-owned data happens one level up, inside `AnalysisExecutionService`, which does set `app.current_user_id` before touching `analysis_requests`.

| Column | Notes |
|---|---|
| `status` | `pending` -> `claimed` -> `completed`/`failed`. |
| `claimed_until` | The heartbeat's target — `extend_visibility()` pushes this forward while a worker is still legitimately processing; `claim()`'s own query treats `status = 'claimed' AND claimed_until < now()` as reclaimable. |
| `attempt_count` / `max_attempts` / `next_attempt_at` | Retry backoff bookkeeping — `fail(retryable=True)` increments `attempt_count`, sets `next_attempt_at = now() + BACKOFF_BASE_SECONDS * 2**attempt_count`, and only actually requeues (`status = 'pending'`) while `attempt_count + 1 < max_attempts`; once exhausted it lands in the same terminal `failed` state `retryable=False` would have gone to directly. |

## `users.home_region` / `users.cell_id` (migration `219c52642ed4`)

Nullable; assigned idempotently by `UserPlacementService.get_placement()` from `settings.launch_home_region`/`launch_cell_id` the first time either is needed for a given user, never re-derived or overwritten once set. No RLS on `users` (unchanged — it never had any).
