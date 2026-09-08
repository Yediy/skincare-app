# Repository Verification Report

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

Regenerated against the current HEAD, superseding the much earlier version of this document (written when the repository had no HTTP layer, no auth, no database access code at all). Status taxonomy: `VERIFIED_IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, `NOT_IMPLEMENTED`, `IMPLEMENTED_BUT_UNTESTED`, `IMPLEMENTED_INCORRECTLY`.

## Auth / Authorization

| Item | Status | Evidence |
|---|---|---|
| Server derives ownership from authenticated credentials only | `VERIFIED_IMPLEMENTED` | `get_current_user` (`app/security/auth.py`) decodes the JWT and derives `user_id` from it; no route accepts a client-supplied `user_id`. |
| Refresh-token families exist, populated | `VERIFIED_IMPLEMENTED` | `refresh_tokens.family_id`, set on every insert (`app/main.py` `/login`, `/refresh`). |
| Refresh consumption is atomic | `VERIFIED_IMPLEMENTED` | Single `UPDATE ... WHERE used_at IS NULL ... RETURNING`, wrapped in an explicit transaction with successor insertion (Phase 2). Proven race-safe with real concurrent-request tests. |
| Refresh-token replay is detected | `VERIFIED_IMPLEMENTED` | Proven with a real replay: a second, freshly-issued access token from the same family is also rejected after a replay, not just the replayed token. |
| Token-family revocation on replay | `VERIFIED_IMPLEMENTED` | Same test as above. |
| Session invalidation after account disable/delete | `VERIFIED_IMPLEMENTED` | `get_current_user` checks `users.is_active`/`deleted_at` directly against Postgres on every request — real DB read, not a Redis TTL. `DELETE /me` is a real, reachable route. Verified with a real disable-mid-session test. |

## Database / RLS

| Item | Status | Evidence |
|---|---|---|
| Runtime DB role is non-superuser | `VERIFIED_IMPLEMENTED` | `skincare_app` role, confirmed via direct `pg_roles` query: `rolsuper=false`. |
| Runtime role doesn't own protected tables | `VERIFIED_IMPLEMENTED` | Tables owned by `postgres` (the migration-running owner); `skincare_app` only has CRUD grants. |
| No BYPASSRLS on runtime role | `VERIFIED_IMPLEMENTED` | `rolbypassrls=false`, confirmed via `pg_roles`. |
| `SET LOCAL app.current_user_id` used in real query paths | `VERIFIED_IMPLEMENTED` (2 of 4 tables) | `app/db/profile_repository.py`, `app/db/consent_repository.py` — every query sets it via `set_config(..., true)` as the first statement of its transaction. `users`/`refresh_tokens` do not use this pattern (see below). |
| Connection-pool reuse cannot leak user context | `VERIFIED_IMPLEMENTED` | Real test: a single reused pooled connection, context set to user A in transaction 1, proven to see nothing (not A, not a leak) in transaction 2 without a new context, then correctly scoped to B in transaction 3. |
| Cross-user RLS tests exist and pass against real PostgreSQL | `VERIFIED_IMPLEMENTED` | `tests/database/test_rls_isolation.py`, 4 tests, run through the actual restricted role, not the superuser and not mocked. |
| RLS on `users`/`refresh_tokens` | `NOT_IMPLEMENTED` | Deliberately out of scope this pass — see `SECURITY_AND_SAFETY_NOTES.md` and `OPEN_ENGINEERING_ITEMS.md` P0-2 for why and what's needed. |

## Encryption / Privacy

| Item | Status | Evidence |
|---|---|---|
| Legacy sensitive-field encryption | `NOT_IMPLEMENTED` | No encrypted fields exist; not attempted this pass (out of scope per the brief's phase ordering). |
| Raw photos not written to client storage / not logged | `PARTIALLY_IMPLEMENTED`, unchanged | No mobile app exists to check client storage. Backend logging remains derived-metrics-only (`app/cv/pipeline.py`), not raw bytes — unchanged from before, not modified this pass. |
| Sentry / `before_send` scrubbing | `NOT_IMPLEMENTED` | `sentry-sdk` remains in `requirements.txt`, unused, unchanged from before. |

## Billing / Quotas

| Item | Status | Evidence |
|---|---|---|
| Everything billing/webhook/quota/rate-limit related | `NOT_IMPLEMENTED` | Confirmed again this pass: zero matches for `revenuecat`, `webhook`, `subscription`, `quota`, `rate_limit` anywhere in `backend/app`. No code exists to evaluate correctness of. |

## Recommendation Safety

| Item | Status | Evidence |
|---|---|---|
| Allergies enforced | `VERIFIED_IMPLEMENTED` (category-level) | `SafetyEngine.evaluate_offer()` excludes a category on allergen match, real tests proving exclusion from the actual routine output. **Category-level, not per-product** — no product catalog exists (see `OPEN_ENGINEERING_ITEMS.md` P0-3). |
| `avoid_ingredients` enforced | `VERIFIED_IMPLEMENTED` (category-level) | Same mechanism, same scope caveat. |
| Sensitive-skin rules materially alter interventions | `VERIFIED_IMPLEMENTED` | Real frequency cap (`SENSITIVE_SKIN_INTENSITY_LIMIT`) reflected in generated routine text, not just a disclaimer; disclaimer only fires when a restriction actually applied. |
| Pregnancy/nursing restrictions | `VERIFIED_IMPLEMENTED` | Moved from ad hoc inline logic into `SafetyEngine.evaluate_priorities()`, real exclusion, real reason codes, real test. |
| Automatic supplement recommendations removed | `VERIFIED_IMPLEMENTED` | `_build_supplement_recommendations` deleted entirely — method, call site, and output key. Verified: `hasattr(PlanService, "_build_supplement_recommendations")` is `False`; a real generated plan has no `supplement_recommendations` key. |
| Affiliate commission cannot override safety ranking | `NOT_APPLICABLE` | No affiliate/commission concept exists anywhere in the repository — nothing to override, since no product catalog exists. |

## Capture / CV (Phases 7-11 of this pass's brief)

| Item | Status | Evidence |
|---|---|---|
| `CaptureAssessment`, PASS/BORDERLINE/FAIL | `NOT_IMPLEMENTED` | `CaptureQualityAssessor` still returns a single bare float; zero matches for `CaptureAssessment` anywhere. |
| Head pose (`cv2.solvePnP` or equivalent) | `NOT_IMPLEMENTED` | Zero matches for `solvePnP`/`head_pose` anywhere. |
| `MetricResult` for all 8 metrics | `NOT_IMPLEMENTED` | All 8 metrics still return bare floats; zero matches for `MetricResult` anywhere. |
| Per-metric confidence | `NOT_IMPLEMENTED` | No confidence value of any kind is computed per-metric. |
| Abstention wired into scorer | `NOT_IMPLEMENTED` | No abstention concept exists. |

See `CV_VALIDATION_LIMITATIONS.md` for the full detail on what each of the 8 metrics actually measures and what corrupts it — documented now even though not yet enforced in code.

## Safety Decisions (Phase 12 of this pass's brief)

| Item | Status | Evidence |
|---|---|---|
| `SafetyDecision` exists with the claimed fields | `VERIFIED_IMPLEMENTED` | `app/domain/safety_engine.py` — `candidate_type`, `candidate_id`, `allowed`, `restrictions`, `reason_codes`, `rules_version`, `to_dict()`. |
| `SafetyEngine` called by `PlanService`, old inline logic removed | `VERIFIED_IMPLEMENTED` | Pregnancy/nursing exclusion moved out of `_apply_compatibility_constraints` entirely; `PlanService.generate_plan()` calls both `evaluate_priorities()` and `evaluate_offer()`. |
| Both `evaluate_priorities()` and `evaluate_offer()` actually called | `VERIFIED_IMPLEMENTED` | Confirmed by direct code read and by tests exercising both paths independently and through the full plan. |
| Rejected candidates excluded from final plan, not just recorded | `VERIFIED_IMPLEMENTED` | Real test: an avoid-ingredient-conflicting category is absent from the generated `pm_routine`, while the decision is still recorded in `plan.metadata.safety_decisions`. |
| Reason codes are consistent, machine-readable | `VERIFIED_IMPLEMENTED` | Fixed set of module-level constants (`ALLERGY_CONFLICT`, `USER_AVOID_INGREDIENT`, etc.), no ad hoc strings. |

## Deployment

| Item | Status | Evidence |
|---|---|---|
| CI | `VERIFIED_IMPLEMENTED` | `.github/workflows/ci.yml` — real Postgres/Redis services, migrations, full test suite. New this pass; did not exist before. |
| `Dockerfile`/`.dockerignore` for the app | `NOT_IMPLEMENTED` | Still only `docker-compose.yml`'s infra services; not attempted this pass. |
| Startup config validation | `NOT_IMPLEMENTED`, unchanged | `Settings` fails closed on missing `jwt_secret`/`redis_url`/`database_url` (no default), which is real config validation — but there's no explicit rejection of placeholder/wildcard values like `ALLOWED_ORIGINS=["*"]`. |

## Test foundation (new this pass, did not exist before at all)

| Item | Status | Evidence |
|---|---|---|
| pytest structure, isolated test infra | `VERIFIED_IMPLEMENTED` | `backend/tests/{unit,integration,auth,cv,planning,database}/`, dedicated `skincare_test` database + Redis index 1, never touching dev/prod infra. |
| Smoke tests (import, health, DB, Redis, migrations) | `VERIFIED_IMPLEMENTED` | All passing — see `TEST_REPORT.md` for exact counts. |
| CI running the suite | `VERIFIED_IMPLEMENTED` | `.github/workflows/ci.yml`. |
