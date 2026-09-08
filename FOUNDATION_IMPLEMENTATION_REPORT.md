# Foundation Implementation Report

This document, `ARCHITECTURE_CURRENT.md`, `REPOSITORY_VERIFICATION_REPORT.md`, `OPEN_ENGINEERING_ITEMS.md`, `SECURITY_AND_SAFETY_NOTES.md`, `TEST_REPORT.md`, and `CV_VALIDATION_LIMITATIONS.md` were all written and committed together, at the end of this foundation pass, against the commit that finalizes it. Run `git log -1` on `master` to see that exact SHA — it's not embedded as a literal hash here because this file is itself part of that commit (the hash doesn't exist until the commit is made).

## Phase-by-phase status

| Phase | Status | Files changed | Tests added | Migrations | Remaining limitation |
|---|---|---|---|---|---|
| 1. Test foundation + CI | **COMPLETE** | `backend/pytest.ini`, `backend/tests/conftest.py` + 6 dirs, `.github/workflows/ci.yml`, `app/config.py` (+`database_url`), `app/db/connection.py` (read from settings, not a hardcoded constant) | 6 smoke tests | none | CI has not been observed running on real GitHub Actions infrastructure (only reasoned about and locally equivalent-tested) |
| 2. Transactional refresh rotation | **COMPLETE** | `app/main.py` (`/refresh` rewritten) | 6 (concurrent, replay, successor-failure-rollback, expired, revoked, logout-revokes-family) | none | none known |
| 3. Account state invalidation | **COMPLETE** | `app/security/auth.py`, `app/main.py` (`DELETE /me`) | 3 | `e5f9be879f83` (`is_active`, `deleted_at`) | none known |
| 4. User profile persistence | **COMPLETE** | `app/db/profile_repository.py` (new), `app/main.py` (`/profile` routes, `/analyze` wiring) | 3 | `f6862f2cbc66` (`user_profiles`) | none known |
| 5. Consent ledger | **COMPLETE** | `app/db/consent_repository.py` (new), `app/main.py` (`/consent` routes, `/analyze` gate) | 5 | `59ebd09d437d` (`consent_events`) | none known |
| 6. Remove supplement recommendations | **COMPLETE** | `app/services/plan_service.py` (method + call site + output key deleted) | 2 | none | none |
| 7. `CaptureAssessment` (PASS/BORDERLINE/FAIL) | **COMPLETE** | `app/cv/capture_assessment.py` (new, replaces deleted `capture_quality.py`), `app/cv/pipeline.py` (`CaptureQualityFailedError`, FAIL blocks metric computation entirely) | 4 (`test_capture_assessment.py`, real photo + pixel-degraded copies) | none | Lighting-balance and resolution sub-scores are documented proxies, not calibrated measurements — see `CV_VALIDATION_LIMITATIONS.md` |
| 8. Real head pose (`cv2.solvePnP`) | **COMPLETE** | `app/cv/head_pose.py` (new) | 3 (`test_head_pose.py`, synthetic/controlled landmark configurations) | none | Monocular, uncalibrated-camera estimate against a generic 3D face model — adequate for gating, not precision metrology |
| 9. `MetricResult` for all 8 metrics | **COMPLETE** | `app/cv/metric_result.py` (new), `app/cv/metric_extractors.py` (`compute_all_metrics()` deleted, replaced by `compute_all_metric_results()`) | Covered by `test_metric_confidence.py` and `test_scorer_abstention.py` | none | none known |
| 10. Metric-specific confidence | **COMPLETE** | `app/cv/metric_confidence.py` (new, 8 distinct formulas) | 13 (`test_metric_confidence.py`, incl. per-metric abstain cases, a boundary case, and undetermined-pose-treated-as-max-severity) | none | `calibration_version="uncalibrated-1.0"` on every result, deliberately — no calibration dataset exists |
| 11. Abstention in scorer | **COMPLETE** | `app/ml/scorer.py` (rewritten: `compute_scores()` skips triggering for any `ABSTAINED` metric), `app/main.py` (`/analyze` wiring) | 5 (`test_scorer_abstention.py`) + 2 real E2E scenarios (`test_moderate_yaw_marks_capture_borderline_not_pass`, `test_poor_lighting_causes_a_color_metric_to_abstain`) | none | Aggregate fallback (`_ABSTAINED_AGGREGATE_FALLBACK = 0.5`) is coarse-display-only, documented as never used for triggering |
| 12. SafetyEngine/SafetyDecision | **COMPLETE** | `app/domain/safety_engine.py` (new), `app/domain/product_safety.py` (new), `app/services/plan_service.py` (wired in) | 10 (`test_safety_engine.py`) | none | Category-level candidates only — see item 13 |
| 13. Allergy/avoid-ingredient enforcement | **COMPLETE** (at category granularity) | Same as 12 | Covered by `test_safety_enforcement_in_plan.py` (5 tests) | none | No per-product catalog exists — this is the honest ceiling of this phase without one |
| 14. Sensitive skin materially changes plan | **COMPLETE** | `app/services/plan_service.py` (`_build_pm_routine` restriction-driven text, `_generate_disclaimers` gated on real decisions) | Covered by `test_safety_enforcement_in_plan.py` | none | Frequency cap (2x/week) is currently a fixed constant, not derived from any clinical data |
| 15. Concern vs. intensity separation | **COMPLETE** | `app/services/plan_service.py` (`_apply_compatibility_constraints`, new `_effective_intensity`) | 3 | none | Only affects the one priority (`TEXTURE_SMOOTHING`) that currently has `default_intensity="advanced"` |
| 16. Ranking correction | **COMPLETE** | `app/services/plan_service.py` (`_rank_priorities_by_severity`) | 2 | none | Still an interim additive model, documented as such — the brief's preferred multiplicative model needs real confidence data from Phase 9/10 |
| 17. DB runtime role + RLS | **COMPLETE** (2 of 4 tables) | `app/db/profile_repository.py`, `app/db/consent_repository.py` (`set_config` wiring), `tests/conftest.py` (`app_db_pool`) | 4 (`test_rls_isolation.py`) | `7b38b717546e` (role + RLS + policies) | `users`/`refresh_tokens` explicitly out of scope — see `OPEN_ENGINEERING_ITEMS.md` P0-2 |
| 18. End-to-end test | **COMPLETE** | `tests/integration/test_end_to_end_analysis.py` (previously-skipped scenarios un-skipped + 1 new added) | 7 (good capture, no-consent, no-face, excessive yaw → FAIL, moderate yaw → BORDERLINE, poor lighting → metric abstains, cross-reference to scorer-level abstention proof) | none | Cannot test "plan persisted" or "offers matched" because no such persistence/catalog exists; yaw/lighting scenarios use targeted `monkeypatch` on `estimate_head_pose`/`CaptureAssessor.assess` (real photo, real detection, real confidence math, real scorer, real HTTP path all unmocked) rather than fighting non-deterministic real-pixel thresholds — documented in the tests themselves |

## Commits (chronological, this pass)

```
test: establish backend test and CI foundation
fix: transactional refresh rotation and account-state invalidation
fix: remove photo-derived supplement recommendations
chore: reconcile requirements.txt pins, drop unused celery dependency
docs: add repository audit status tracking
feat: persist real user profiles and an append-only consent ledger
feat: SafetyEngine, allergy/pregnancy enforcement, and planner correctness fixes
security: restricted runtime DB role and row-level security
docs: add the security/CV docs and E2E test omitted from the prior commit
feat: real head pose, structured capture assessment, per-metric confidence, and abstention (Phases 7-11)
docs: regenerate architecture/verification/open-items docs for Phases 7-11
```

## Test command used

```
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

See `TEST_REPORT.md` for the exact final pass/fail/skip counts from this command.

## Unresolved P0 blockers (see `OPEN_ENGINEERING_ITEMS.md` for full detail)

1. RLS on `users`/`refresh_tokens` — not started, needs a different design than the `user_profiles`/`consent_events` pattern.
2. Allergy/avoid-ingredient enforcement is category-level, not per-product — no product catalog exists.

## Unresolved P1 items

Billing/subscription/webhooks, rate limiting/quotas, mobile app (entirely absent), app `Dockerfile`/`.dockerignore`, deprecated `@app.on_event` usage, ongoing dependency-pin drift.

## Exact next recommended engineering phase

All 18 phases of this pass's brief are now complete and verified. The largest remaining gap is **RLS on `users` and `refresh_tokens`** (`OPEN_ENGINEERING_ITEMS.md` P0-1): both tables currently rely entirely on application-layer query correctness rather than a database-enforced isolation boundary, and both need a genuinely different design from the `user_profiles`/`consent_events` `SET LOCAL`-scoped pattern (`users` needs a pre-authentication, pre-session-identity lookup path by email; `refresh_tokens` is looked up by an unguessable per-row secret, not session identity). This is a real design problem, not a mechanical extension of the existing pattern, and it's the single remaining item that touches the core authentication path.

**Do not claim production readiness.** P0 items remain open (RLS gap above; category-level-only allergy/ingredient enforcement, since no product catalog exists).
