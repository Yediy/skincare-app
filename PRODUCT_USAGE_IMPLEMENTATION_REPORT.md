# Product + Usage Foundation Implementation Report

This document, `PRODUCT_CATALOG_ARCHITECTURE.md`, `USAGE_AND_RATE_LIMIT_ARCHITECTURE.md`,
and the updates to `ARCHITECTURE_CURRENT.md`/`OPEN_ENGINEERING_ITEMS.md`/
`SECURITY_AND_SAFETY_NOTES.md`/`PRODUCT_USAGE_START_STATE.md` were written
during this pass. Starting baseline: commit `b04fb4285fe3d5be8b7326b2669b3df3119ee4b0`
(verified as HEAD at the start; see `PRODUCT_USAGE_START_STATE.md`).

Status taxonomy: `COMPLETE`, `PARTIAL` (real and tested, with a stated,
honest gap), `NOT BUILT` (explicitly out of scope this pass).

## Phase-by-phase status

| Phase | Status | Files | Tests | Migrations |
|---|---|---|---|---|
| 1. Product data model | **COMPLETE** | `migrations/versions/d70e5fc90775_*.py`, `16b82dde6e7d_*.py` | Covered by phases 3/4/5 below | `d70e5fc90775`, `16b82dde6e7d` |
| 2. Migrations + RLS (catalog read/admin/user-owned split) | **COMPLETE** | Same two migrations + `ee276e90a60f_*.py` (`analysis_usage`) | `tests/database/test_product_usage_rls.py` (5), `tests/database/test_smoke_infra.py` (updated) | `ee276e90a60f` |
| 3. Repository layer | **COMPLETE** | `app/db/catalog_repository.py` (new) | `tests/catalog/test_catalog_repository.py` (21) | none |
| 4. Connect the existing SafetyEngine | **COMPLETE** (additive) | `app/domain/safety_engine.py` (`evaluate_product_formulation()` added; `evaluate_offer()`/`evaluate_priorities()` unchanged) | `tests/planning/test_formulation_safety.py` (14) | none |
| 5. Test catalog | **COMPLETE** | `tests/conftest.py` (`synthetic_catalog` fixture) | Consumed by phases 4/9/10's test files | none |
| 6. Atomic rate limiting | **COMPLETE**, wired into every real route | `app/middleware/rate_limiter.py` (new), `app/main.py` (every route), `app/config.py` (6 new settings + `trusted_proxies`) | `tests/middleware/test_rate_limiter.py` (10) | none |
| 7. Analysis usage ledger | **COMPLETE** | `app/db/usage_repository.py` (new) | `tests/usage/test_usage_repository.py` (10) | `ee276e90a60f` |
| 8. Entitlement abstraction | **COMPLETE**, `FreeTierEntitlementService` is the real default (not RevenueCat) | `app/domain/entitlement.py` (new) | `tests/domain/test_entitlement.py` (7) | none |
| 9. Concurrency tests | **COMPLETE** | — | Rate limiter: `test_concurrent_requests_at_exact_limit_no_overrun`. Quota: `test_concurrent_reservations_never_exceed_allowance`. Idempotency: `test_concurrent_identical_request_id_produces_exactly_one_reservation`. All three use real `asyncio.gather` races, not mocks. | none |
| 10. Safety tests | **COMPLETE** | `tests/planning/test_formulation_safety.py` | 14 tests covering every required scenario (canonical/alias ingredient conflict, allergy, avoid-ingredient, sensitive skin, pregnancy, nursing, interaction, unknown formulation, safe formulation) plus the commercial-override invariant | none |
| 11. Async CV worker | **NOT BUILT** (explicitly out of scope) | — | — | — |
| — `/analyze` quota wiring | **COMPLETE** | `app/domain/analysis_service.py` (`perform_analysis` now reserves/consumes/releases), `app/main.py` (`request_id`, `QuotaExceededError` → 429) | `tests/domain/test_analysis_service.py` (+3 new tests: releases-on-failure, consumes-on-success, quota-exceeded) | none |

## What's genuinely COMPLETE vs. PARTIAL — no phase claims more than it proves

- **Formulation-level safety evaluation is real** against real seeded
  ingredient/rule/interaction data (not a mock), through the restricted
  `skincare_app` role (not the superuser) — but it is **additive**, not
  wired into `PlanService`, because no product-matching/ranking step
  exists to connect `PlanService`'s abstract categories to concrete
  catalog formulations. That connective work is explicitly out of this
  pass's scope (affiliate ranking is a later pass).
- `MAX_FREQUENCY`/`BARRIER_RECOVERY` ingredient-rule types are supported
  end-to-end by the evaluation code but have no seeded fixture data
  exercising them in this pass's test catalog — **PARTIAL**, stated in
  `PRODUCT_CATALOG_ARCHITECTURE.md`.
- Rate limiting is a **fixed window**, not a sliding window or token
  bucket — a deliberate, documented trade-off (can allow up to ~2x the
  limit across one window boundary in the worst case), not a hidden
  limitation.
- The usage ledger's period granularity is **calendar-month only** — no
  other granularity is implemented.
- RevenueCat itself is **NOT BUILT**, per this pass's explicit
  instruction — `EntitlementService`/`UsagePolicyService` exist
  specifically so that integration is a new implementation behind an
  existing interface, not a domain-layer change.

## Tests

```text
before (starting baseline, commit b04fb428): 140 passed, 0 failed, 0 errors, 0 skipped
after this pass:                              210 passed, 0 failed, 0 errors, 0 skipped
new tests added:                              70
```

New test files/dirs: `tests/catalog/` (21), `tests/usage/` (10),
`tests/middleware/` (10), `tests/planning/test_formulation_safety.py` (14),
`tests/domain/test_entitlement.py` (7), `tests/database/
test_product_usage_rls.py` (5), plus 3 new tests in the existing
`tests/domain/test_analysis_service.py` and updates to `tests/database/
test_smoke_infra.py` and `tests/conftest.py` (truncate list, `synthetic_
catalog` fixture).

## Migrations created

```text
d70e5fc90775  create product catalog core tables
16b82dde6e7d  create ingredient rules and interactions tables
ee276e90a60f  create analysis usage reservation ledger
```

All three verified with a real `alembic upgrade head` / `alembic downgrade`
/ `alembic upgrade head` cycle against the test database, not just authored
and assumed correct.

## Commits (chronological, this pass)

See `git log` on the pushed branch for the exact sequence and messages —
this pass's commits are grouped by phase (schema/migrations, repository +
safety-engine extension, rate limiting, usage ledger + entitlement +
`/analyze` wiring, tests, documentation), each a real, buildable, tested
state.

## Final GitHub Actions result

See this document's own commit and `git log` for the exact SHA this
report ships with; the required final state is recorded in the pass's
closing commit message and PR/response, not duplicated here to avoid this
file going stale the moment a later pass adds a commit.
