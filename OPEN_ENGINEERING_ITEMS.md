# Open Engineering Items

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA.

Classified into P0 (release blockers) / P1 (pre-launch) / P2 (post-launch) / P3 (research/moat). Nothing here is marked complete because scaffolding exists — every item below reflects real, verified absence or partiality, not assumption.

## P0 — Release blockers

1. **CaptureAssessment, real head pose, MetricResult, per-metric confidence, and abstention (Phases 7-11) are entirely unimplemented.** Every plan this app generates is built from bare-float metrics with no confidence signal and no pose/lighting sanity check — a badly-lit or badly-angled photo produces a full plan with the same apparent authority as a well-captured one. See `CV_VALIDATION_LIMITATIONS.md` for the complete breakdown. This is the single largest gap remaining, and the clear next engineering phase.
2. **`users` and `refresh_tokens` have no row-level security.** Only `user_profiles` and `consent_events` are RLS-scoped in this pass. `users` needs a pre-authentication lookup-by-email design that doesn't yet have an RLS-compatible answer; `refresh_tokens` is looked up by an unguessable per-row secret rather than session identity. Both need a real design pass, not a rushed extension of the current `user_id = current_setting(...)` pattern.
3. **Allergy/avoid-ingredient enforcement is category-level, not per-product**, because no product/offer catalog exists in this repository at all. `app/domain/product_safety.py` is a deliberate, static, hand-authored adapter — real enforcement against real per-SKU ingredient data requires that catalog to exist first.

## P1 — Pre-launch

4. **No billing/subscription/webhook code exists.** RevenueCat lifecycle correctness, out-of-order webhook protection, and duplicate-webhook idempotency are all moot until a billing integration exists at all.
5. **No rate limiting or quota system exists.** `app/middleware/` is still empty.
6. **No mobile app exists.** `mobile/` is empty directory scaffolding — raw-photo `AsyncStorage` persistence, client-side consent versioning, and all mobile-specific security items are unverifiable because there is no mobile codebase to check.
7. **No `Dockerfile`/`.dockerignore` for the application itself** — only the Postgres/Redis infra services in `docker-compose.yml` are containerized; the app has no deployable image.
8. **The `@app.on_event("startup"/"shutdown")` pattern in `app/main.py` is deprecated** by FastAPI in favor of lifespan context managers — still functional (confirmed working throughout this entire pass), but flagged in every test run's warnings. Low urgency, but will eventually need migrating.
9. **Dependency version drift** between `requirements.txt` and what's actually installed/tested has recurred multiple times this session (fastapi, pytest, redis, python-jose, pydantic-settings, and — most seriously — an actual `celery[redis]`/`redis` version conflict that silently went undetected because packages were being installed individually rather than via `pip install -r requirements.txt` as a whole). `celery` itself has zero imports anywhere in the codebase and was removed. Recommend a CI step that fails if `pip install -r requirements.txt` can't resolve cleanly (already partially covered by `ci.yml`'s install step, but worth an explicit fresh-venv check in the release process too).

## P2 — Post-launch

10. **No notification system exists** of any kind (no outbox, no dedup).
11. **`allergies`/`avoid_ingredients` are stored as plain text arrays**, not normalized ingredient entities. Documented explicitly as a placeholder in `app/db/profile_repository.py` and `app/domain/product_safety.py`; migrating to a real ingredient/allergen entity model is a real, larger project once a product catalog exists to normalize against.
12. **Ranking is still an interim additive model** (`_rank_priorities_by_severity`), not the `severity × confidence × persistence × intervention-value` multiplicative model this pass's brief describes as preferred. That model needs real per-metric confidence data (Phase 9/10) as an input before it can be built honestly — building it now would mean fabricating the confidence term.
13. **No professional review mode, B2B analytics, or affiliate product ranking** — none of these have any code, and per the brief's own Section 22, none should be started before the P0/P1 items above are resolved.

## P3 — Research / moat (explicitly not started, per the brief's own Section 22)

14. Personal Baseline Engine, Digital Twin, Outcome Engine, adaptive N-of-1 experiments, full ingredient knowledge graph, microservices/large module reorganization. None of these exist, and none should be started until the P0/P1 items above are resolved.

## What was closed out in this pass (for contrast — not to be re-litigated as still-open)

Test foundation + CI, transactional refresh rotation, account-state invalidation, real user profile persistence, append-only consent ledger gating `/analyze`, removal of automatic photo-derived supplement recommendations, `SafetyEngine`/`SafetyDecision` with real allergy/pregnancy/sensitive-skin enforcement, beginner concern-vs-intensity separation, ranking's display-order dominance, and a restricted DB runtime role with RLS on two tables. See `FOUNDATION_IMPLEMENTATION_REPORT.md` for the full phase-by-phase detail, including what remains partial within each.
