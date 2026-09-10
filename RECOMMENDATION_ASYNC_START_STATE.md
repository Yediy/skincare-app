# Production Recommendation + Async Analysis — Start State

**Starting HEAD:** `b0279272d4b140ac9f98d5e7c729b8795542fe8b` (verified as
`master`'s tip at the start of this pass; working tree clean).
**Feature branch:** `feat/production-recommendation-async-analysis`, cut
from that commit.
**Starting test count:** 210 passed, 0 failed, 0 errors, 0 skipped
(`python -m pytest tests/`, real Postgres/Redis, restricted `skincare_app`
role).
**Starting CI state:** GitHub Actions green on `master` at this HEAD (run
`34434358684`): `secret-scan`/`test`/`dependency-scan`/`docker-build-and-scan`
all PASS.

Verified by direct inspection (grep across `backend/app/`), not assumed:

| Component | Exists? | Actually called from production code? |
|---|---|---|
| `SafetyEngine.evaluate_offer()` | Yes | Yes — `PlanService.generate_plan()` (`app/services/plan_service.py:45`) |
| `SafetyEngine.evaluate_product_formulation()` | Yes | **No** — zero callers outside its own module/docstrings. Fully built and tested (`tests/planning/test_formulation_safety.py`) but not on any production path. |
| `PlanService.generate_plan()` | Yes | Yes — `app/domain/analysis_service.py:142` |
| `perform_analysis()` | Yes | Yes — `app/main.py`'s `/analyze` route |
| `UsagePolicyService` | Yes | Yes — constructed per-request in `/analyze`, passed into `perform_analysis()` |
| `analysis_usage` (table) | Yes | Yes — read/written by `app/db/usage_repository.py`, RLS enabled |
| `RateLimiter` | Yes | Yes — every real route in `app/main.py` via `rate_limit_by_ip`/`rate_limit_by_user` dependencies |
| `PostgresJobQueue` | Yes | **No** — the class exists and is tested (`tests/queue/test_postgres_job_queue.py`), but nothing in `app/` ever constructs or calls it. `/analyze` still runs `FacialAnalysisPipeline` synchronously inline. |
| `ObjectStorage` / `CloudflareR2ObjectStorage` | Yes | **No** — `app/storage/base.py`/`r2.py` exist and are tested (`tests/storage/test_r2_adapter.py`), but no route or service calls them. |

**Confirmed: `PlanService` still uses category-level safety** —
`generate_plan()` calls `self.safety_engine.evaluate_offer(cat, ...)` for
every abstract product category it collects from triggered priorities; it
never touches `evaluate_product_formulation()` or any catalog table. This
is exactly the P0 this pass closes.

## Schema gaps confirmed by direct inspection (`\d` in psql against
`skincare_test`)

- `product_formulations` has no `ingredient_data_status` column — a
  formulation with one recorded ingredient is indistinguishable from one
  with a genuinely complete disclosure, beyond the existing "zero
  ingredients → `INSUFFICIENT_DATA`" check. Phase 1's target.
- `analysis_usage` has no `attempt_count` column and no schema-level
  distinction of retry semantics beyond `RESERVED`/`CONSUMED`/`RELEASED`
  status alone. Phase 14's target.
- No `user_ingredient_constraints`, `analysis_requests`, `analysis_results`,
  `analysis_measurements`, or `analysis_product_recommendations` tables
  exist yet.
- `users` has no `home_region`/`cell_id` columns yet (confirmed absent,
  consistent with `OPEN_ENGINEERING_ITEMS.md` item 16, deferred until now).

## What this pass does NOT touch (per its own explicit scope)

RevenueCat, mobile app, notifications, affiliate ranking, merchant
integrations, price optimization, Personal Baseline Engine, Outcome
Engine, adaptive routines, Digital Twin, N-of-1 experimentation, research
cohort analytics, microservices, Kafka, Kubernetes.
