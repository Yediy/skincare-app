# Product + Usage Foundation — Start State

**Baseline commit:** `b04fb4285fe3d5be8b7326b2669b3df3119ee4b0` (verified as HEAD at the start of this pass; `git status --short` clean).

**Starting test count:** 140 passed, 0 failed, 0 errors, 0 skipped (`python -m pytest tests/`, real Postgres/Redis, restricted `skincare_app` role).

Plain statement of what genuinely exists and doesn't, verified by direct inspection of the live repository — not by re-reading prior reports uncritically.

## Product catalog — NOT PRESENT

Confirmed by direct inspection: no `brands`/`products`/`product_formulations`/`product_skus`/`ingredients`/`ingredient_aliases`/`formulation_ingredients`/`ingredient_rules`/`ingredient_interactions` tables, migrations, or repositories exist anywhere in this repository. `app/domain/product_safety.py` is a static, hand-authored `CATEGORY_SAFETY_PROFILES` dict keyed by product-category strings (`"retinoid"`, `"cleanser"`, etc.) — a deliberate category-level placeholder, documented as such in its own docstring and in `SECURITY_AND_SAFETY_NOTES.md`/`OPEN_ENGINEERING_ITEMS.md` (P0-2). This pass adds the normalized catalog underneath it; it does not remove or break the existing category-level path, which `PlanService` still uses (nothing yet matches an abstract recommended category to a real catalog product — that's ranking/matching work, out of scope here per this pass's own instructions).

## Safety engine — PARTIALLY PRESENT

`app/domain/safety_engine.py`'s `SafetyEngine`/`SafetyDecision` are real: `evaluate_priorities()` (pregnancy/nursing exclusion on concerns) and `evaluate_offer()` (category-level allergy/avoid-ingredient/pregnancy/nursing/sensitive-skin checks against `product_safety.py`'s static profiles) are both wired into `PlanService.generate_plan()` and covered by existing tests. Reason codes already defined: `ALLERGY_CONFLICT`, `USER_AVOID_INGREDIENT`, `SENSITIVE_SKIN_INTENSITY_LIMIT`, `PREGNANCY_RESTRICTION`, `NURSING_RESTRICTION`, `ACTIVE_INTERACTION_CONFLICT`, `BARRIER_RECOVERY_CONFLICT`, `MAX_FREQUENCY_EXCEEDED`, `SAFETY_DATA_UNAVAILABLE` — some of these (`ACTIVE_INTERACTION_CONFLICT`, `BARRIER_RECOVERY_CONFLICT`, `MAX_FREQUENCY_EXCEEDED`) are already defined as constants but never actually triggered by any code path yet (no interaction/frequency data exists to trigger them against). This pass adds a genuinely new, additional evaluation path — formulation-level, against real ingredient/rule/interaction data — rather than modifying the category-level one `PlanService` depends on today.

## Rate limiting — NOT PRESENT

Confirmed: `app/middleware/__init__.py` is empty (just the package marker). No rate limiting or quota code of any kind exists anywhere in `app/`. No route in `app/main.py` is rate-limited.

## Usage/quota ledger — NOT PRESENT

Confirmed: no `analysis_usage` table, migration, or repository exists. No `EntitlementService`/`UsagePolicyService` interface exists. `/analyze` has no concept of a request_id, quota, or allowance today — it runs unconditionally for any authenticated user with valid consent, exactly once per HTTP call, with no idempotency protection at the usage-tracking level (job-queue-level idempotency exists — `app/queue/postgres_queue.py`'s `request_id` dedup — but nothing enqueues onto that queue from any real code path, and it is not a usage/quota concept regardless).

## Job queue / async CV worker — PARTIALLY PRESENT, EXPLICITLY OUT OF SCOPE THIS PASS

`app/queue/base.py` (`JobQueue` ABC) and `app/queue/postgres_queue.py` (`PostgresJobQueue`, migration `2e77bc462867`) exist and are tested (claim exclusivity via `FOR UPDATE SKIP LOCKED`, idempotent enqueue-by-`request_id`). Nothing calls `enqueue`/`claim` from any real code path — `/analyze` still runs the CV pipeline synchronously inline. Per this pass's own explicit instruction (Phase 11), this is not touched or combined with the schema/safety/quota work below.

## Database / RLS — PRESENT, being extended, not modified

8 migrations, clean chain, restricted `skincare_app` runtime role (`NOSUPERUSER`/`NOCREATEDB`/`NOCREATEROLE`/`NOBYPASSRLS`, owns no tables) with RLS on all four existing application tables (`users`, `refresh_tokens`, `user_profiles`, `consent_events`). This pass adds new tables following the same conventions (raw SQL via `op.execute`, explicit per-table `GRANT`s to `skincare_app`, RLS only where a table is genuinely user-owned) — catalog tables are global reference data (no RLS, `SELECT`-only grant, no write grant at all, since nothing in this pass builds a catalog-administration route); the new usage ledger is user-owned (RLS, same `app.current_user_id` pattern as `user_profiles`/`consent_events`).

## What this pass will NOT touch or recreate

Existing auth, consent, profile, CV pipeline, job queue, container/CI/security posture (all verified `VERIFIED_IMPLEMENTED` in `ARCHITECTURE_CURRENT.md` and confirmed still green by the starting test run above) — none of it is recreated or altered except where a new table/route genuinely needs to compose with it (e.g. `/analyze` gaining a quota-reservation step around the existing `perform_analysis()` call).
