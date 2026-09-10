# Recommendation / Async-Analysis Implementation Report

This document, plus `PRODUCT_RECOMMENDATION_PIPELINE.md`, `ASYNC_ANALYSIS_ARCHITECTURE.md`, `RAW_IMAGE_LIFECYCLE.md`, `ANALYSIS_DATA_MODEL.md`, `WORKER_OPERATIONS.md`, and the updates to `ARCHITECTURE_CURRENT.md`/`OPEN_ENGINEERING_ITEMS.md`/`PRODUCTION_ARCHITECTURE.md`/`SECURITY_AND_SAFETY_NOTES.md`/`FAILURE_DOMAINS.md`/`DOKPLOY_DEPLOYMENT.md`, were written across this branch's full lifetime (`feat/production-recommendation-async-analysis`) and this continuation specifically. Starting baseline for this continuation: commit `54a7d8b852e1eb1ccb05a88b2d16031c9fb6c518` (see `RECOMMENDATION_ASYNC_START_STATE.md` for the pre-continuation verification).

Status taxonomy: `COMPLETE`, `PARTIAL` (real and tested, with a stated, honest gap), `NOT BUILT` (explicitly out of scope).

## Phase-by-phase status

| Phase | Status | Files | Tests |
|---|---|---|---|
| P0 fail-closed fix on `apply_product_matching_and_routine_safety()` | **COMPLETE** | `app/domain/recommendation_service.py`, `app/domain/safety_engine.py` (`exclude_action_formulation_ids`) | `tests/domain/test_recommendation_service.py` (+5), `tests/planning/test_routine_safety.py` (+2) |
| Async submission failure-saga hardening | **COMPLETE** | `app/domain/analysis_submission_service.py`, `app/db/analysis_repository.py` (`record_ephemeral_image_reference`) | `tests/domain/test_analysis_submission_service.py` (9, new file) |
| Concurrent idempotent submit semantics | **COMPLETE**, no process-local lock | `app/domain/analysis_submission_service.py` (`_await_concurrent_replay`) | Covered in the same file: `test_concurrent_first_submissions_same_request_id_resolve_to_one_analysis` |
| Strict base64 validation | **COMPLETE** | `app/domain/analysis_submission_service.py`, `app/domain/analysis_service.py` (both now `validate=True`) | `test_invalid_base64_is_rejected_strictly_and_releases_quota` |
| Shared quota-independent compute core | **COMPLETE** | `app/domain/analysis_service.py` (`compute_analysis()` factored out of `perform_analysis()`) | Existing `tests/domain/test_analysis_service.py` suite passes unchanged (proves no synchronous-path regression); new execution-service tests below prove the async side |
| `POST`/`GET /api/v2/analyses` | **COMPLETE** | `app/api/v2/analyses.py` (new router), `app/main.py` (mounted) | `tests/api/test_analyses_v2.py` (8, new file, full real-HTTP path) |
| `AnalysisExecutionService` | **COMPLETE** | `app/domain/analysis_execution_service.py` (new) | `tests/domain/test_analysis_execution_service.py` (8, new file) |
| Real queue-consuming worker + retry classification + dead-letter | **COMPLETE** | `app/workers/analysis_worker.py` (new), `app/queue/base.py`/`postgres_queue.py` (`fail()` now returns `is_terminal`) | `tests/workers/test_analysis_worker.py` (5, new file, real heartbeat/timing test included) |
| Minimal cell readiness | **COMPLETE** (metadata only, no routing) | `app/domain/user_placement_service.py` (new), migration `219c52642ed4`, `app/config.py` (`launch_home_region`/`launch_cell_id`) | Covered in `tests/api/test_analyses_v2.py::test_home_region_and_cell_id_are_snapshotted_onto_the_request` |
| FastAPI lifespan migration | **COMPLETE** | `app/main.py` (`lifespan()` replaces `@app.on_event`) | `tests/database/test_smoke_infra.py::test_lifespan_initializes_and_closes_the_db_pool` (new) |
| Minimum structured observability | **PARTIAL** — real structured events wired into 11 real call sites; no metrics/Prometheus backend deployed to receive them, no per-request correlation ID threading every log line | `app/observability/events.py` (new) | `tests/domain/test_observability_events.py` (3, new file) |
| Documentation catch-up | **COMPLETE** | This file + the 5 other new docs + 6 updated docs | — |
| CI secret-scan permission fix | **COMPLETE** | `.github/workflows/ci.yml` (`permissions: contents: read, pull-requests: read`) | Verified by the PR's own CI run, not a local test |

**Explicitly not started this continuation**, per its own stop conditions: RevenueCat, mobile, notifications, affiliate monetization, Personal Baseline, Outcome Engine, Digital Twin, N-of-1 experiments, Kafka, Kubernetes.

## What's genuinely COMPLETE vs. PARTIAL — no phase claims more than it proves

- The P0 fix's hard backstop (clearing every step's concrete candidates when the bounded conflict-resolution heuristic can't resolve `UNSAFE`) is a real behavior, not merely a metadata flag — proven by asserting `product_recommendations == []` and every step's `product_recommendations == []` directly, not just checking `routine_safety_decision.status`.
- The worker's retry classification is a closed, explicit set of TERMINAL exception types; everything else defaults RETRYABLE. This is an honest, bounded-by-`max_attempts` default, not a claim that every retryable-classified failure is actually transient — an unanticipated permanent failure mode would retry `max_attempts` times before dead-lettering, wasted compute but not wasted correctness (no quota is consumed or released incorrectly during those retries).
- Observability is structured *log events*, deliberately not a metrics/Prometheus integration — no such backend is deployed in this repository to receive one, and standing one up was out of scope. The 11 required event types are all real and wired in; nothing about them is aggregated, alerted on, or dashboarded yet.
- `UserPlacementService`/`home_region`/`cell_id` is metadata/contract readiness only — verified by its own docstring and by the complete absence of any routing/replication/sharding code anywhere in this diff.
- The heartbeat/concurrent-claim-prevention test (`tests/workers/test_analysis_worker.py::test_heartbeat_prevents_a_concurrent_worker_from_reclaiming`) uses real Postgres timing (`asyncio.sleep`, real `claimed_until` comparisons), not a mocked clock — a real, if inherently timing-sensitive, proof rather than a simulated one.

## Tests

```text
before this continuation (commit 54a7d8b): 281 passed, 0 failed, 0 errors, 0 skipped
after this continuation:                   322 passed, 0 failed, 0 errors, 0 skipped
new tests added this continuation:         41
```

New test files: `tests/domain/test_analysis_submission_service.py` (9), `tests/domain/test_analysis_execution_service.py` (8), `tests/workers/test_analysis_worker.py` (5), `tests/api/test_analyses_v2.py` (8), `tests/domain/test_observability_events.py` (3). Extended existing files: `tests/domain/test_recommendation_service.py` (+5), `tests/planning/test_routine_safety.py` (+2), `tests/database/test_smoke_infra.py` (+1, plus one existing assertion updated for the new migration head).

## Migrations

One new migration this continuation: `219c52642ed4_add_home_region_and_cell_id_to_users.py` (revises `9db3e5856a79`). Verified `upgrade` -> `downgrade -1` -> `upgrade` round-trip against a real Postgres database; the full chain from empty database to head is verified every test session by `tests/database/test_smoke_infra.py::test_migrations_reach_head`.
