# Raw Image Lifecycle

**Commit this document describes:** the head of `feat/production-recommendation-async-analysis` as of this continuation. Referenced from `app/storage/ephemeral_image_store.py`'s own module docstring, which is why this file needed to actually exist.

The policy `EphemeralAnalysisImageStore` (`app/storage/ephemeral_image_store.py`) implements for exactly one purpose: transient raw-face-image transport between the API process (`AnalysisSubmissionService`) and the async CV worker (`AnalysisExecutionService`). **This is not a photo-archive feature.** `/analyze` (the synchronous path) never touches this at all — see `PRODUCTION_ARCHITECTURE.md`'s Raw Face Image Policy.

## The four properties this store guarantees

1. **Non-identifying object keys.** `ephemeral-analysis/<region>/<year>/<month>/<day>/<uuid>` — no email, username, or `user_id` anywhere in the key or the bucket. The link back to a user lives only in `analysis_requests` (RLS-protected, DB-side, never derivable from the object key itself).
2. **Never a public URL.** `store()`/`retrieve()`/`delete()` only ever call `put`/`get`/`delete` — this store deliberately never calls `create_upload_authorization`/`create_download_authorization`, the two `ObjectStorage` methods that would produce a presigned, client-reachable URL.
3. **Short retention, tracked in the database, not just in the object store.** `DEFAULT_RETENTION = timedelta(hours=1)` — "minutes/hours, not months." Every stored object's expiry is written to `analysis_requests.image_expires_at` at the moment it's referenced (`mark_queued()`, or the failure-saga's `record_ephemeral_image_reference()`), so the expiry is enforceable even if the in-process code path that would have deleted it never runs again.
4. **Two independent deletion paths, not one.** See below.

## Deletion: primary path + safety net

**Primary**: the worker deletes the object itself, immediately after terminal processing — success (`AnalysisExecutionService.execute()`, right after `commit_analysis_result()` commits) or terminal failure (`mark_terminal_failure()`). Both call `delete_if_confirmed()`, which treats `ObjectNotFoundError` (already gone) and a genuinely confirmed delete identically — `True` — and only clears the DB-side `image_object_key`/`image_expires_at` reference once deletion is *confirmed*, never before. If deletion is not confirmed (`ObjectStorageUnavailableError` — `False`), the DB reference is deliberately left in place.

**Never roll back a valid result over a failed deletion.** A `COMPLETED` analysis result stays `COMPLETED` even if the immediately-following image deletion fails — `AnalysisExecutionService.execute()`'s delete call is genuinely the last statement in the function, after the atomic commit, and its failure is swallowed (logged as a leftover reference for the sweeper), not propagated as if the whole operation failed.

**Safety net**: `app/workers/image_cleanup.py`'s `run_cleanup_sweep()`, intended to run on a recurring schedule (`python -m app.workers.image_cleanup`, `WORKER_OPERATIONS.md`) independent of any single worker's own control flow. It finds every request across every user whose `image_expires_at` has passed and `image_object_key` is still set (`find_overdue_ephemeral_images()`, a narrow `SECURITY DEFINER` SQL function — migration `071fab81f0ac` — exactly because this cross-user query cannot go through the ordinary RLS-scoped path), attempts `delete_if_confirmed()` for each, and only clears the reference on confirmed success. A storage-unavailable failure here is tracked explicitly (`CleanupSweepResult.failed`/`failed_request_ids`) and logged (`image_cleanup_failure` structured event, `app/observability/events.py`) — never silently dropped, left for the next sweep to retry.

This two-path design is why an application-level delete call alone was never sufficient: a worker crash, a killed process, a network blip between "processing finished" and "delete call completed" defeats the primary path completely, and only a second, independent mechanism driven by DB-tracked expiry (not the worker's own control flow) covers that gap.

## The failure-saga case: upload succeeds, enqueue fails

Covered in detail in `ASYNC_ANALYSIS_ARCHITECTURE.md`'s failure-saga section. The short version, from this document's own angle: when the atomic `mark_queued()`+`enqueue()` transaction fails after a successful upload, `mark_queued()` — the only thing that would have written the DB-side image reference — never committed. Compensation (`AnalysisSubmissionService._compensate_orphaned_upload()`) either deletes the object immediately, or, if that itself fails, writes the reference directly via `record_ephemeral_image_reference()` (a plain `UPDATE`, independent of the failed transaction) so the same sweeper above can still find and recover it later. The one orphan case this cannot help is if *that* write also fails (e.g. the database is entirely down) — logged at `ERROR` as "object may be unrecoverable by the sweeper," an honest worst case rather than a silently swallowed one.

## What this does not claim

- No encryption-at-rest configuration is specified here — that's an R2 bucket-level setting, out of this application's code.
- No jurisdiction/data-residency handling beyond the `region` parameter already threading through `store()`'s key generation — there is no cross-region replication or region-pinning enforcement.
- Retention is a fixed constant (`DEFAULT_RETENTION`), not per-user or per-request configurable.
