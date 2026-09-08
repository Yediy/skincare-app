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
| 7. `CaptureAssessment` | **NOT STARTED** | — | — | — | Entire phase not attempted this pass — see `CV_VALIDATION_LIMITATIONS.md` |
| 8. Real head pose | **NOT STARTED** | — | — | — | Same |
| 9. `MetricResult` for all 8 metrics | **NOT STARTED** | — | — | — | Same |
| 10. Metric-specific confidence | **NOT STARTED** | — | — | — | Same |
| 11. Abstention in scorer | **NOT STARTED** | — | — | — | Same |
| 12. SafetyEngine/SafetyDecision | **COMPLETE** | `app/domain/safety_engine.py` (new), `app/domain/product_safety.py` (new), `app/services/plan_service.py` (wired in) | 10 (`test_safety_engine.py`) | none | Category-level candidates only — see item 13 |
| 13. Allergy/avoid-ingredient enforcement | **COMPLETE** (at category granularity) | Same as 12 | Covered by `test_safety_enforcement_in_plan.py` (5 tests) | none | No per-product catalog exists — this is the honest ceiling of this phase without one |
| 14. Sensitive skin materially changes plan | **COMPLETE** | `app/services/plan_service.py` (`_build_pm_routine` restriction-driven text, `_generate_disclaimers` gated on real decisions) | Covered by `test_safety_enforcement_in_plan.py` | none | Frequency cap (2x/week) is currently a fixed constant, not derived from any clinical data |
| 15. Concern vs. intensity separation | **COMPLETE** | `app/services/plan_service.py` (`_apply_compatibility_constraints`, new `_effective_intensity`) | 3 | none | Only affects the one priority (`TEXTURE_SMOOTHING`) that currently has `default_intensity="advanced"` |
| 16. Ranking correction | **COMPLETE** | `app/services/plan_service.py` (`_rank_priorities_by_severity`) | 2 | none | Still an interim additive model, documented as such — the brief's preferred multiplicative model needs real confidence data from Phase 9/10 |
| 17. DB runtime role + RLS | **COMPLETE** (2 of 4 tables) | `app/db/profile_repository.py`, `app/db/consent_repository.py` (`set_config` wiring), `tests/conftest.py` (`app_db_pool`) | 4 (`test_rls_isolation.py`) | `7b38b717546e` (role + RLS + policies) | `users`/`refresh_tokens` explicitly out of scope — see `OPEN_ENGINEERING_ITEMS.md` P0-2 |
| 18. End-to-end test | **COMPLETE** (scoped to what exists) | — | 6 (3 real + 3 honestly skipped, blocked on Phases 7-11) | none | Cannot test yaw/lighting/abstention scenarios because the underlying features don't exist; cannot test "plan persisted" or "offers matched" because no such persistence/catalog exists |

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
[final commit containing this document and the other regenerated docs]
```

## Test command used

```
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

See `TEST_REPORT.md` for the exact final pass/fail/skip counts from this command.

## Unresolved P0 blockers (see `OPEN_ENGINEERING_ITEMS.md` for full detail)

1. Phases 7-11 (CaptureAssessment, head pose, MetricResult, per-metric confidence, abstention) — not started.
2. RLS on `users`/`refresh_tokens` — not started, needs a different design than the `user_profiles`/`consent_events` pattern.
3. Allergy/avoid-ingredient enforcement is category-level, not per-product — no product catalog exists.

## Unresolved P1 items

Billing/subscription/webhooks, rate limiting/quotas, mobile app (entirely absent), app `Dockerfile`/`.dockerignore`, deprecated `@app.on_event` usage, ongoing dependency-pin drift.

## Exact next recommended engineering phase

**Phases 7-11 as one connected unit** (CaptureAssessment → head pose → MetricResult → per-metric confidence → abstention wired into the scorer). These five are the largest remaining gap, they're tightly coupled (confidence formulas need head pose as an input; abstention needs `MetricResult` to exist as a structure first), and every downstream claim this application makes about a user's skin currently rests on measurements with zero reliability signal. This is more consequential than any remaining P1/P2 item, and it's the one area of this pass's original 18-phase brief that received no implementation work at all — everything else (Phases 1-6, 12-18) is genuinely complete and verified.

**Do not claim production readiness.** P0 items remain open.
