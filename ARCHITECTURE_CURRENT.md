# Architecture — Current State

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA (this file and that one were written/committed together, at the end of the same pass).

This describes the application as it **actually exists right now**, verified by direct inspection and, where noted, real test execution — not what was planned or described in any prior conversation. Status taxonomy used throughout: `VERIFIED_IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, `NOT_IMPLEMENTED`, `IMPLEMENTED_BUT_UNTESTED`, `IMPLEMENTED_INCORRECTLY`.

## What this repository is

A FastAPI backend (`backend/app/main.py`) with real authentication, a real Postgres database (behind a restricted, non-superuser runtime role with row-level security on two tables), a real CV pipeline (MediaPipe landmarks → real head pose → 8 skin/face metrics, each with real per-metric confidence and abstention), a real domain-driven planning/safety layer, and a real pytest suite with CI. No mobile app exists (the `mobile/` directory is empty scaffolding). No offer/product catalog exists.

## HTTP / API layer — `VERIFIED_IMPLEMENTED`

Real FastAPI app (`app/main.py`), 15 routes: `/health`, `/signup`, `/login`, `/refresh`, `/logout`, `/logout-all`, `/me` (GET + DELETE), `/analyze`, `/profile` (GET + PUT), `/consent`, `/consent/withdraw`, `/db-check`. Startup wires a real Postgres pool and Redis client (`init_db_pool`/`init_redis`); `FacialAnalysisPipeline`, `FacialScorer`, `PlanService` are constructed once at import time and genuinely called from `/analyze` — this call chain is proven by a real end-to-end test running a real photo through it (`tests/integration/test_end_to_end_analysis.py::test_full_chain_good_capture_succeeds`).

## Authentication — `VERIFIED_IMPLEMENTED`

JWT access tokens + opaque, SHA-256-hashed refresh tokens with family IDs (`app/security/tokens.py`). Refresh rotation is transactional (`app/main.py`'s `/refresh`): old-token consumption and successor creation happen in one explicit Postgres transaction, proven to roll back coherently on a real forced `UniqueViolationError`, not a mock. Replay of a consumed token revokes the whole family via Redis (`revoked_family:{family_id}`), verified by minting a second token from the same family after a replay and confirming it's also rejected. `get_current_user` (`app/security/auth.py`) checks Redis revocation *and* `users.is_active`/`deleted_at` directly against Postgres on every request — a disabled/deleted account's outstanding access tokens stop working immediately, not just at natural JWT expiry, verified with a real test disabling an account mid-session. Redis unreachability fails closed (`503`), distinct from `401`, both tested.

## Consent — `VERIFIED_IMPLEMENTED`

`consent_events` (append-only; `withdrawn_at` on the active row is the one narrow, deliberate exception) gates `/analyze` on a valid, current-policy-version grant (`app/db/consent_repository.py`). An old policy version or a withdrawn grant both correctly deny analysis; re-consenting to the current version restores access. Verified with 5 real tests plus the E2E test.

## User profile persistence — `VERIFIED_IMPLEMENTED`

`user_profiles` table + `app/db/profile_repository.py` replaces the old hardcoded placeholder profile in `/analyze` (`is_pregnant=False, has_sensitive_skin=False, experience_level="beginner"` for literally every user, regardless of who they were). A documented `DEFAULT_PROFILE` is used only when a user has never set one — never a silent, unlabeled guess.

## Database — `VERIFIED_IMPLEMENTED` (schema/migrations), `PARTIALLY_IMPLEMENTED` (isolation)

7 migrations, clean chain from empty database to head, verified by a real Alembic run in CI and in this session. Tables: `users`, `refresh_tokens`, `user_profiles`, `consent_events`. The application's runtime connection now uses a restricted `skincare_app` role (`rolsuper=false, rolcreatedb=false, rolcreaterole=false, rolbypassrls=false`, owns no tables — confirmed by direct `pg_roles` query), not the `postgres` superuser. Row-level security is enabled on `user_profiles` and `consent_events` only, verified with real cross-user Postgres integration tests (User A cannot read/update/delete User B's rows; a reused pooled connection does not leak context between transactions). **`users` and `refresh_tokens` are explicitly not RLS-scoped** — see `SECURITY_AND_SAFETY_NOTES.md` for why, and `OPEN_ENGINEERING_ITEMS.md` for this as a tracked follow-up.

## Computer vision — `VERIFIED_IMPLEMENTED`

MediaPipe FaceMesh landmarks → real head pose (`app/cv/head_pose.py`, `cv2.solvePnP` against a generic 3D face model) → structured `CaptureAssessment` (`app/cv/capture_assessment.py`: `quality_status` PASS/BORDERLINE/FAIL, `overall_quality`, yaw/pitch/roll, blur/exposure/lighting_balance/face_size/resolution/occlusion sub-scores, `failure_reasons`) → 8 metrics, each returned as a `MetricResult` (`app/cv/metric_result.py`: `value`, `confidence`, `status` VALID/BORDERLINE/ABSTAINED, `uncertainty_reasons`) with a real, metric-specific confidence formula (`app/cv/metric_confidence.py`) derived from the actual corrupting factors for that metric, not a single blanket score applied to all 8 identically.

`FacialAnalysisPipeline.analyze()` genuinely gates on capture quality: `FAIL` raises before any metric is computed at all; `BORDERLINE` still computes metrics but marks the result `eligible_for_longitudinal_comparison: false`; `PASS` proceeds normally. `FacialScorer` (`app/ml/scorer.py`) genuinely respects abstention — an `ABSTAINED` metric is excluded from priority triggering outright, proven with a real test asserting a priority cannot trigger from an abstained metric regardless of what its withheld value would have implied.

**None of this is clinically validated** — every `MetricResult.calibration_version` is literally `"uncalibrated-1.0"`, and the confidence formulas, pose thresholds, and abstain/borderline cutoffs are documented, internally-consistent, hand-picked starting points, not values derived from a validation study. See `CV_VALIDATION_LIMITATIONS.md` for the full, honest breakdown of what each metric actually measures, what corrupts it, and what remains unsolved (notably the texture/blur circular dependency, unchanged from before this pass).

## Planning and safety — `VERIFIED_IMPLEMENTED`

`SafetyEngine`/`SafetyDecision` (`app/domain/safety_engine.py`) is real and wired into `PlanService.generate_plan()` — replacing the old undocumented inline pregnancy/nursing exclusion. Every priority and product-category candidate gets a `SafetyDecision` recorded in `plan.metadata.safety_decisions`. Allergy/avoid-ingredient enforcement is real, operating against a category-level `ProductSafetyProfile` adapter (`app/domain/product_safety.py`) — **not per-product**, since no product catalog exists in this repository; a missing safety profile fails closed (`SAFETY_DATA_UNAVAILABLE`), never silently "safe". Sensitive skin genuinely caps active-ingredient frequency (routine text reflects the real cap) rather than only appending a disclaimer. A beginner's advanced-default concern stays visible with a capped `effective_intensity` instead of being dropped outright. Ranking's `display_order` bonus was reduced from a dominant factor (up to 0.45) to a true tie-break (0.001/step). Real per-metric confidence (Phase 10) now flows into severity via `PriorityTrigger.should_trigger()`'s `severity × confidence` multiplication, but `_rank_priorities_by_severity()` itself is still the original additive model (`severity + pillar_weight + tiebreak`), not the brief's preferred `severity × confidence × persistence × intervention-value` multiplicative one — that would need a validated intervention-value weighting this codebase doesn't have, so it wasn't fabricated.

## What does not exist, at all (confirmed by direct inspection this pass, same as before)

- No billing/subscription/webhook code of any kind.
- No rate limiting or quota system of any kind.
- No offer/product catalog (`offers` table or equivalent).
- No mobile app source (`mobile/` is empty directory scaffolding).
- No notification system.
- No `Dockerfile`/`.dockerignore` for the application itself (only `docker-compose.yml`'s Postgres/Redis infra services).

## Test foundation and CI — `VERIFIED_IMPLEMENTED`

`backend/tests/{unit,integration,auth,cv,planning,database}/`, isolated from dev infra (separate `skincare_test` database, Redis index 1). GitHub Actions (`.github/workflows/ci.yml`) runs the full suite against real Postgres/Redis services on every push/PR. See `TEST_REPORT.md` for the exact test count and pass/fail breakdown from the final run of this pass.
