# Billing Architecture (RevenueCat)

**Commit this document describes:** the `feat/revenuecat-entitlement-sync` branch, base `master` at `76fcaa2b454ef390a4e50e38160ca5e011448e31`.

Classification used throughout: **DESIGNED** (documented, no code), **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (explicitly out of scope for this pass, tracked in `OPEN_ENGINEERING_ITEMS.md`).

## Scope of this pass

RevenueCat billing synchronization only. See `REVENUECAT_INTEGRATION_NOTES.md` for the verified external contract this implementation is built against. Explicitly **DEFERRED**: mobile app, notifications, affiliate ranking, Personal Baseline/Outcome Engine/Digital Twin/N-of-1, admin/support promotional-grant tooling, scheduling reconciliation automatically.

## End-to-end flow — IMPLEMENTED, TESTED

```
RevenueCat
    |  POST (Authorization header + X-RevenueCat-Webhook-Signature)
    v
POST /api/v2/webhooks/revenuecat        app/api/v2/webhooks.py
    |  verify_webhook_request()          app/security/revenuecat_webhook.py
    |  record_event() + enqueue()        app/db/revenuecat_repository.py, one transaction
    v  HTTP 200 (no entitlement logic ran inline)
revenuecat_webhook_events (durable)     migration a1c9f3e7b2d4
    |
    v  claimed by
revenuecat_webhook_worker                app/workers/revenuecat_webhook_worker.py
    |  process_webhook_event()           app/domain/revenuecat_entitlement_processor.py
    v
user_entitlements (local projection)    migration a1c9f3e7b2d4
    ^
    |  reads only, no RevenueCat API call
RevenueCatEntitlementService              app/domain/entitlement.py
    |
    v
UsagePolicyService (unchanged)           app/domain/entitlement.py
    ^
    |  corrective, out-of-band
RevenueCatReconciliationService          app/domain/revenuecat_reconciliation_service.py
    |  GET /v2/projects/{id}/customers/{app_user_id}
    v
RevenueCat REST API
```

Every arrow above is real code exercised by a real test (see "Test coverage" below) — this is not a design sketch.

## What this pass deliberately does NOT touch — IMPLEMENTED (by omission)

Per the brief's architectural boundary, no RevenueCat-specific logic exists inside:

- `app/domain/analysis_service.py::perform_analysis`
- `app/domain/analysis_execution_service.py::compute_analysis`
- `app/domain/analysis_submission_service.py::AnalysisSubmissionService`
- `app/domain/analysis_execution_service.py::AnalysisExecutionService`
- `app/domain/product_matching_service.py::ProductMatchingService`
- `app/domain/safety_engine.py::SafetyEngine`

Those five/six modules import nothing from this pass. The only change any of them (or their callers) sees is that `app/main.py`, `app/api/v2/analyses.py`, and `app/workers/analysis_worker.py` now obtain their `EntitlementService` from `app.domain.entitlement.build_entitlement_service(pool)` instead of constructing `FreeTierEntitlementService()` directly — a composition-root change, not a change to the provider-neutral interface those modules consume. `settings.revenuecat_billing_enabled` defaults to `False`, so `build_entitlement_service` returns `FreeTierEntitlementService` unchanged until explicitly turned on; behavior for every existing test and every existing deployment is unaffected unless someone flips the flag.

## Identity — IMPLEMENTED, TESTED

The application's own `users.id` UUID is the canonical RevenueCat App User ID (Section 2 of the brief) — never email/username. `app/domain/revenuecat_entitlement_processor.py::_resolve_user` enforces this: `app_user_id` (and, for `TRANSFER`, `transferred_from`/`transferred_to`) must parse as a UUID *and* match a real `users.id`, or the event fails closed (`UNKNOWN_APP_USER_ID`, no entitlement row created for anyone). See `REVENUECAT_INTEGRATION_NOTES.md` section 4 for why this sidesteps most of RevenueCat's own alias-merge complexity.

## Database model — IMPLEMENTED, TESTED

Migration `a1c9f3e7b2d4`:

- **`revenuecat_webhook_events`** — durable receipt log, `revenuecat_event_id UNIQUE`. No RLS (a provider-level log, not literally user-owned — same rationale as the pre-existing `jobs` table); no route ever exposes it to a normal user. Never stores an Authorization header value, HMAC secret, or raw signature.
- **`user_entitlements`** — the local projection. `UNIQUE(user_id, entitlement_identifier, provider, environment)`; RLS scoped by `app.current_user_id`, same pattern as `analysis_requests`/`user_profiles`; `source_event_id` has a real foreign key into `revenuecat_webhook_events(revenuecat_event_id)`. No `DELETE` grant. See `ENTITLEMENT_STATE_MACHINE.md` for the status enum and transition table.

"Runtime role cannot forge arbitrary premium access through an unrestricted write path" concretely means, and is tested (`tests/database/test_revenuecat_rls.py`):

1. No HTTP route accepts a client-supplied entitlement status.
2. RLS blocks a write targeting another user's row regardless of the query's own `WHERE` clause (`InsufficientPrivilegeError`).
3. A write claiming `source_event_id` must reference a row that already passed webhook verification and durable insertion — fabricating one requires first getting past HMAC + Authorization (`ForeignKeyViolationError` otherwise).
4. No `DELETE` grant — a status transition supersedes a row, never erases the audit trail.

## Webhook security — IMPLEMENTED, TESTED

`app/security/revenuecat_webhook.py`: Authorization header (constant-time compare), HMAC-SHA256 over raw request body bytes (`hmac.compare_digest`, never a re-serialized copy), timestamp tolerance (`settings.revenuecat_webhook_signature_tolerance_seconds`, default 300s). Production config validation (`app/config.py::_reject_unsafe_production_config`) fails startup outright when `REVENUECAT_BILLING_ENABLED=true` and any of `REVENUECAT_WEBHOOK_AUTH`/`REVENUECAT_WEBHOOK_SIGNING_SECRET`/`REVENUECAT_API_KEY`/`REVENUECAT_PROJECT_ID` is blank or a placeholder. See `REVENUECAT_INTEGRATION_NOTES.md` section 1 for the exact verified contract.

## Idempotency and concurrency — IMPLEMENTED, TESTED

Two layers, deliberately: `revenuecat_webhook_events.revenuecat_event_id UNIQUE` (durable-receipt idempotency) and the pre-existing `JobQueue`'s `(job_type, request_id)` uniqueness, keyed `request_id=f"revenuecat:{event_id}"` (job idempotency) — no new queue system built, per Section 6. `tests/api/test_revenuecat_webhook_route.py::test_ten_concurrent_duplicate_deliveries_create_exactly_one_event_and_one_job` proves 10 real concurrent HTTP deliveries of the same event produce exactly one durable row and one job.

## Out-of-order protection — IMPLEMENTED, TESTED

`app/db/revenuecat_repository.py::apply_entitlement_projection` is a single `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE user_entitlements.last_provider_event_at <= EXCLUDED.last_provider_event_at` statement — a stale write is a genuine no-op at the database level, not a read-then-compare race. `tests/domain/test_revenuecat_entitlement_processor.py::test_out_of_order_stale_event_does_not_override_newer_state` proves a `CANCELLATION` timestamped before an already-processed `RENEWAL` cannot revert it, and the stale event is marked `STALE_IGNORED` (persisted for audit, not silently dropped).

## Event semantics — IMPLEMENTED, TESTED

See `ENTITLEMENT_STATE_MACHINE.md` for the full table. Highlights: `CANCELLATION` never revokes access by itself (only `EXPIRATION` does) unless `cancel_reason=CUSTOMER_SUPPORT` (a refund, revoked immediately); `BILLING_ISSUE` enters `GRACE_PERIOD`, not revoked; `TRANSFER` revokes every resolvable source and activates the destination in the same processing pass, so a processed transfer can never leave both sides holding paid access.

## Sandbox/production isolation — IMPLEMENTED, TESTED

`user_entitlements.environment` is part of the row's own uniqueness scope — a `SANDBOX` purchase and a `PRODUCTION` purchase for the same user/entitlement are two separate rows. `RevenueCatEntitlementService` additionally filters by the running environment. `tests/domain/test_revenuecat_entitlement_service.py::test_sandbox_entitlement_never_grants_production_allowance` proves a `SANDBOX` `ACTIVE` row never satisfies a `PRODUCTION` allowance check.

## Provider failure / staleness policy — IMPLEMENTED, TESTED

`RevenueCatEntitlementService.get_analysis_allowance` performs exactly one local `SELECT` — it never constructs an HTTP client (`tests/domain/test_revenuecat_entitlement_service.py::test_entitlement_service_never_makes_a_network_call` fails loudly if that ever changes). A RevenueCat outage therefore degrades **reconciliation freshness only** — the local projection can drift stale relative to RevenueCat's true state until a webhook or a reconciliation run corrects it, but `POST /api/v2/analyses` and the worker's own quota decisions keep working from whatever the local projection currently says. Acceptable staleness is bounded by whichever is sooner: the next real webhook event for that user, or an operator-scheduled reconciliation run (not itself scheduled by this pass — **DEFERRED**, see `OPEN_ENGINEERING_ITEMS.md`).

## Reconciliation — IMPLEMENTED, TESTED (single-user and batch); scheduling is DEFERRED

`app/domain/revenuecat_reconciliation_service.py::RevenueCatReconciliationService`. `reconcile_user` compares local projection against one live `GET /v2/projects/{project_id}/customers/{app_user_id}` call and corrects on mismatch; `reconcile_batch` runs a bounded list of users with a real inter-call delay (`DEFAULT_BATCH_DELAY_SECONDS`) and never lets one user's API failure abort the rest of the batch. Neither is wired to any HTTP route or cron in this pass — Section 13's "do not reconcile every user on every request" is satisfied by there being no automatic trigger at all yet, not by a rate limit on one.

## Observability — IMPLEMENTED

`app/observability/events.py`: `revenuecat_webhook_received`, `revenuecat_webhook_rejected` (reason code only, never secret material), `revenuecat_event_duplicate`, `revenuecat_event_stale`, `revenuecat_event_processed`, `revenuecat_event_failed`, `entitlement_activated`, `entitlement_expired`, `entitlement_revoked`, `reconciliation_success`, `reconciliation_mismatch`, `reconciliation_failure`. Structural test (`tests/domain/test_observability_events.py`) enforces every event function's parameter list stays inside a closed, non-PII vocabulary.

## Test coverage — TESTED

`tests/security/test_revenuecat_webhook_verification.py`, `tests/api/test_revenuecat_webhook_route.py`, `tests/domain/test_revenuecat_entitlement_processor.py`, `tests/domain/test_revenuecat_entitlement_service.py`, `tests/domain/test_revenuecat_reconciliation_service.py`, `tests/database/test_revenuecat_rls.py`, plus additions to `tests/unit/test_config_validation.py` and `tests/domain/test_observability_events.py`. All run against real Postgres (no mocked DB), same convention as the rest of this repository.

## DEFERRED (tracked in `OPEN_ENGINEERING_ITEMS.md`)

- Scheduling reconciliation automatically (cron/worker trigger).
- Admin/support promotional-entitlement grants.
- `sentry-sdk` wiring for billing-specific error reporting (it remains unwired repository-wide, pre-existing gap).
- Mobile SDK integration itself (no mobile app exists to call `Purchases.configure`).
