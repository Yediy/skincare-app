# Billing Architecture (RevenueCat)

**Commit this document describes:** the `feat/revenuecat-entitlement-sync` branch, base `master` at `76fcaa2b454ef390a4e50e38160ca5e011448e31`. Hardened by three independent review passes. The first (migration `9815eb266923`, merged to master via PR #2) corrected four merge blockers found in the original version: the `TRANSFER` field contract, the reconciliation REST endpoint, the database privilege boundary, and `entitlement_ids` enforcement. The second (migration `4e5cda3a6bb0`, plus `app/domain/revenuecat_reconciliation_service.py`) corrected two more: `next_page` pagination trusting an absolute, potentially cross-origin URL, and the billing role's database credential/shared-queue privilege design. **The third pass corrected a mistake made *by* the second pass itself:** its first attempt fixed the hardcoded billing credential by editing `9815eb266923` in place -- which, because that migration was already merged into master, would never actually run again against any database that had already applied it. `9815eb266923` is restored verbatim to its merged-master contents (immutable from here on -- see its own note below), and a new forward-only migration, `1367b870bdcd`, performs the LOGIN -> NOLOGIN transition instead. See the sections below, each marked where it changed.

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
    |  record_event() + enqueue()        app/db/revenuecat_repository.py, one transaction,
    |                                     over the BILLING pool (skincare_billing role)
    v  HTTP 200 (no entitlement logic ran inline)
revenuecat_webhook_events (durable)     migration a1c9f3e7b2d4 / 9815eb266923
    |
    v  claimed by
revenuecat_webhook_worker                app/workers/revenuecat_webhook_worker.py, also the billing pool
    |  process_webhook_event()           app/domain/revenuecat_entitlement_processor.py
    v
user_entitlements (local projection)    migration a1c9f3e7b2d4 / 9815eb266923
    ^
    |  reads only, no RevenueCat API call, ordinary skincare_app pool
RevenueCatEntitlementService              app/domain/entitlement.py
    |
    v
UsagePolicyService (unchanged)           app/domain/entitlement.py
    ^
    |  corrective, out-of-band, the billing pool
RevenueCatReconciliationService          app/domain/revenuecat_reconciliation_service.py
    |  GET /v2/projects/{id}/customers/{customer_id}/active_entitlements
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

The application's own `users.id` UUID is the canonical RevenueCat App User ID (Section 2 of the brief) — never email/username. `app/domain/revenuecat_entitlement_processor.py::_resolve_user` enforces this: a lifecycle event's `app_user_id` must parse as a UUID *and* match a real `users.id`, or the event fails closed (`UNKNOWN_APP_USER_ID`, no entitlement row created for anyone). `TRANSFER`'s `transferred_from`/`transferred_to` entries go through the same `_resolve_user`, but an unresolvable entry there is *not* a hard failure the way an unresolvable lifecycle `app_user_id` is — see "TRANSFER handling" below and `REVENUECAT_INTEGRATION_NOTES.md` section 3. See `REVENUECAT_INTEGRATION_NOTES.md` section 4 for why this sidesteps most of RevenueCat's own alias-merge complexity.

## Database model — IMPLEMENTED, TESTED

Migrations `a1c9f3e7b2d4` and `9815eb266923`:

- **`revenuecat_webhook_events`** — durable receipt log, `revenuecat_event_id UNIQUE`. No RLS (a provider-level log, not literally user-owned — same rationale as the pre-existing `jobs` table); no route ever exposes it to a normal user. Never stores an Authorization header value, HMAC secret, or raw signature. `app_user_id`/`environment` are **nullable** (`9815eb266923`) — a real `TRANSFER` delivery carries neither/only sometimes the latter, and this table stores exactly what RevenueCat sent, never a fabricated value. `processing_status` (now `VARCHAR(30)`) has two additional terminal values beyond the original four — see `ENTITLEMENT_STATE_MACHINE.md`.
- **`user_entitlements`** — the local projection. `UNIQUE(user_id, entitlement_identifier, provider, environment)`; RLS scoped by `app.current_user_id`, same pattern as `analysis_requests`/`user_profiles`; `source_event_id` has a real foreign key into `revenuecat_webhook_events(revenuecat_event_id)` (nullable, for reconciliation-driven corrections with no single event to attribute to). No `DELETE` grant, for either role that can write it. See `ENTITLEMENT_STATE_MACHINE.md` for the status enum and transition table.

## Database privilege boundary — IMPLEMENTED, TESTED (migrations `9815eb266923`, `4e5cda3a6bb0`, `1367b870bdcd`)

**This corrects a real gap in the original pass.** The original migration granted the *ordinary* runtime role (`skincare_app` — what every HTTP request in this application connects as) direct `INSERT`/`UPDATE` on both `revenuecat_webhook_events` and `user_entitlements`, and described this as safe because RLS blocks cross-user writes. That claim was too narrow: RLS only stops a request from writing *someone else's* row — nothing stopped the ordinary role, under an ordinary request's own session context, from writing an `ACTIVE` row for **itself** (`source_event_id = NULL` satisfies the nullable FK; `WITH CHECK (user_id = current_setting('app.current_user_id'))` is satisfied because the row genuinely is the caller's own). `tests/database/test_revenuecat_billing_privilege.py::test_ordinary_role_cannot_insert_its_own_active_entitlement_even_with_null_source_event` is the test that would have caught this and previously did not exist.

Fixed by introducing a dedicated, least-privilege `skincare_billing` role (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`, same posture as `skincare_app`, still fully subject to `user_entitlements`' own RLS policy — not a bypass):

| Role | `revenuecat_webhook_events` | `user_entitlements` | `jobs` | `users` |
|---|---|---|---|---|
| `skincare_app` (ordinary runtime) | no grant at all | `SELECT` only | `SELECT`/`INSERT`/`UPDATE`, row-scoped by RLS to `job_type <> 'revenuecat_webhook'` (`4e5cda3a6bb0`) | `SELECT`/`INSERT`/`UPDATE`/`DELETE` (pre-existing) |
| `skincare_billing` (dedicated) | `SELECT`/`INSERT`/`UPDATE` | `SELECT`/`INSERT`/`UPDATE` | `SELECT`/`INSERT`/`UPDATE`, row-scoped by RLS to `job_type = 'revenuecat_webhook'` only (`4e5cda3a6bb0`) | `SELECT` only (identity resolution via `_resolve_user`, never a write) |

**Credential design, corrected in two steps.** `9815eb266923` (first review pass) shipped a known `LOGIN PASSWORD 'skincare_billing_dev_only'` hardcoded directly into this schema migration for `skincare_billing` — a real secret an application schema migration must never carry, dev-only or not. A second independent review caught this and initially fixed it by editing `9815eb266923` in place to create the role `NOLOGIN` instead — but `9815eb266923` had, by then, already merged into `master` (PR #2). Alembic never re-runs a revision a database has already recorded as applied, so that edit would have done nothing at all for any real deployment past that merge; it only ever "worked" for a database created fresh after the edit. **A third review pass caught this second mistake and closed it correctly:** `9815eb266923` is restored verbatim to its merged-master contents (it still creates `skincare_billing` `LOGIN PASSWORD 'skincare_billing_dev_only'`, and must never be edited again — see the note at the end of this section), and a new, separate, forward-only migration, `1367b870bdcd`, performs `ALTER ROLE skincare_billing NOLOGIN` instead. Every database — one that already applied `9815eb266923` months ago, or one created fresh today — passes through this same explicit step and ends up `NOLOGIN` at head. `skincare_billing` is thus a pure privilege/group role at head, never a connectable credential, and no migration embeds a password for it. The actual connectable login (this repository's dev/CI infrastructure names it `skincare_billing_runtime`, provisioned in `tests/conftest.py::_provision_test_billing_runtime_role`, *after* migrations run, never inside one) is granted membership in `skincare_billing` (`GRANT skincare_billing TO skincare_billing_runtime`) by whichever deployment-specific process manages that environment's secrets — outside of, and independent from, this application's Alembic migration chain. `REVENUECAT_BILLING_DATABASE_URL` points at that runtime login, never at `skincare_billing` directly (see `.env.example`). `app/config.py::_reject_unsafe_production_config` still rejects the literal `skincare_billing_dev_only` marker in production regardless, as defense in depth against that value being copy-pasted into a real deployment's config, even though the `NOLOGIN` design means it can never actually be `skincare_billing`'s own password.

**`9815eb266923` is immutable from here on.** It is merged into `master`; any database that has applied it will never re-execute a modified version. Any future correction to what it did must be a new, separate, forward-only migration (as `1367b870bdcd` and `4e5cda3a6bb0` both are), never an edit to `9815eb266923` itself. See `tests/database/test_billing_role_migration_lineage.py` for the regression guard.

**Shared job-queue isolation, corrected (`4e5cda3a6bb0`, second pass).** The first version of this migration granted `skincare_billing` unrestricted `SELECT`/`INSERT`/`UPDATE` on the entire `jobs` table — table-wide, even though `jobs` is shared with `analysis` (`app/workers/analysis_worker.py`). Nothing but application-layer convention (the billing code only ever passing `job_type='revenuecat_webhook'`) stopped that credential from claiming, completing, or failing someone's `analysis` job — convention is not a boundary. Fixed with row-level security scoped by `job_type`, enabled on `jobs` for the first time: `skincare_billing` may only see/write `job_type = 'revenuecat_webhook'` rows; `skincare_app` may see/write everything **except** that job_type (an exclusion, not an allowlist pinned to today's one other job_type, so a future non-billing job_type needs no migration change to keep working through the ordinary role). The two policies don't overlap, so there is no accidental table-wide restoration. See `tests/database/test_revenuecat_jobs_isolation.py`.

Webhook ingestion (`app/api/v2/webhooks.py`), the RevenueCat worker (`app/workers/revenuecat_webhook_worker.py`), and `RevenueCatReconciliationService` all connect through `app/db/connection.py::get_billing_db_pool()` (a second pool, `REVENUECAT_BILLING_DATABASE_URL`, initialized only when `REVENUECAT_BILLING_ENABLED=true`). `RevenueCatEntitlementService`'s read path — the only revenuecat-aware code on the ordinary request-serving path — keeps using the ordinary `skincare_app` pool via `get_db_pool()`, unchanged. Production config validation (`app/config.py`) refuses to start with billing enabled and no `REVENUECAT_BILLING_DATABASE_URL` configured, or one identical to `DATABASE_URL`, or one carrying a dev/CI marker (including `skincare_billing_dev_only` specifically).

The correct, now-actually-tested claim is: **the ordinary application runtime role cannot manufacture billing truth, and the billing role cannot touch anything outside billing, period** — not merely "cannot manufacture *another user's*" (RLS's own, narrower guarantee). Proven in four files:

- `tests/database/test_revenuecat_billing_privilege.py`: `skincare_app` cannot `INSERT` a webhook event row, cannot `UPDATE` one, cannot `INSERT` its own `ACTIVE` entitlement (even with `source_event_id = NULL` and its own `user_id`), cannot `UPDATE` its own entitlement to `ACTIVE`, cannot `DELETE` one — and still *can* read its own entitlement (the one thing it needs). `skincare_billing` can persist a verified webhook, project an entitlement, and correct one via reconciliation, and is confirmed `NOSUPERUSER`/`NOBYPASSRLS`/`NOLOGIN` via a direct `pg_roles` query.
- `tests/database/test_revenuecat_rls.py`: RLS itself, exercised through `skincare_billing` (the only role that can write the table at all) — cross-user writes still blocked, the `source_event_id` FK still blocks forging attribution to a never-received event, still no `DELETE` grant for anyone.
- `tests/database/test_revenuecat_jobs_isolation.py`: `skincare_billing` can enqueue/claim/acknowledge/fail a `revenuecat_webhook` job but cannot see, claim, complete, or fail an `analysis` job (proven both through `PostgresJobQueue` and via a raw `UPDATE` checked against the superuser's own view of the row); `skincare_app` symmetrically cannot see or claim a `revenuecat_webhook` job; the ordinary `analysis` queue lifecycle is unaffected (also re-proven end-to-end by the pre-existing, unmodified `tests/queue/test_postgres_job_queue.py` and `tests/workers/test_analysis_worker.py`).
- `tests/database/test_billing_role_migration_lineage.py`: proves the migration *lineage* itself, not just the end state — `9815eb266923`'s own source still creates a `LOGIN` role (never edited to do the NOLOGIN transition itself), `1367b870bdcd`'s own source contains no embedded password, and executing each migration's actual captured SQL against a real (rollback-guarded, never persisted) role reproduces the LOGIN -> NOLOGIN transition exactly as claimed — a real regression guard against ever repeating the "edit an already-merged migration" mistake.

## Webhook security — IMPLEMENTED, TESTED

`app/security/revenuecat_webhook.py`: Authorization header (constant-time compare), HMAC-SHA256 over raw request body bytes (`hmac.compare_digest`, never a re-serialized copy), timestamp tolerance (`settings.revenuecat_webhook_signature_tolerance_seconds`, default 300s). Production config validation (`app/config.py::_reject_unsafe_production_config`) fails startup outright when `REVENUECAT_BILLING_ENABLED=true` and any of `REVENUECAT_WEBHOOK_AUTH`/`REVENUECAT_WEBHOOK_SIGNING_SECRET`/`REVENUECAT_API_KEY`/`REVENUECAT_PROJECT_ID` is blank or a placeholder. See `REVENUECAT_INTEGRATION_NOTES.md` section 1 for the exact verified contract.

`POST /api/v2/webhooks/revenuecat` also now checks `settings.revenuecat_billing_enabled` first and deliberately returns `404` (not a bare, accidental 500 from calling `get_billing_db_pool()` against a pool that was never initialized) when billing is off — see `tests/api/test_revenuecat_webhook_route.py::test_webhook_route_is_a_deliberate_404_when_billing_disabled`. `GET /health/ready` (`app/main.py`) additionally checks the billing pool itself, but only when `REVENUECAT_BILLING_ENABLED=true` — an unconfigured deployment's readiness contract is unchanged. This closes a real gap: without it, an instance with billing enabled but an unreachable/uninitialized billing pool could report `ready` while every real webhook delivery was guaranteed to fail. See `tests/unit/test_smoke.py`.

## Idempotency and concurrency — IMPLEMENTED, TESTED

Two layers, deliberately: `revenuecat_webhook_events.revenuecat_event_id UNIQUE` (durable-receipt idempotency) and the pre-existing `JobQueue`'s `(job_type, request_id)` uniqueness, keyed `request_id=f"revenuecat:{event_id}"` (job idempotency) — no new queue system built, per Section 6. `tests/api/test_revenuecat_webhook_route.py::test_ten_concurrent_duplicate_deliveries_create_exactly_one_event_and_one_job` proves 10 real concurrent HTTP deliveries of the same event produce exactly one durable row and one job.

## Out-of-order protection — IMPLEMENTED, TESTED

`app/db/revenuecat_repository.py::apply_entitlement_projection` is a single `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE user_entitlements.last_provider_event_at <= EXCLUDED.last_provider_event_at` statement — a stale write is a genuine no-op at the database level, not a read-then-compare race. `tests/domain/test_revenuecat_entitlement_processor.py::test_out_of_order_stale_event_does_not_override_newer_state` proves a `CANCELLATION` timestamped before an already-processed `RENEWAL` cannot revert it, and the stale event is marked `STALE_IGNORED` (persisted for audit, not silently dropped).

## Event semantics — IMPLEMENTED, TESTED

See `ENTITLEMENT_STATE_MACHINE.md` for the full table and `REVENUECAT_INTEGRATION_NOTES.md` section 6 for which event types are deliberately unsupported and why. Highlights: `CANCELLATION` never revokes access by itself (only `EXPIRATION` does) unless `cancel_reason=CUSTOMER_SUPPORT` (a refund, revoked immediately, **without** fabricating `will_renew=false` — refund and auto-renew-off are independent facts, see `REVENUECAT_INTEGRATION_NOTES.md` section 3); `BILLING_ISSUE` enters `GRACE_PERIOD`, not revoked.

**`entitlement_ids` enforcement (migration `9815eb266923`, `app/domain/revenuecat_entitlement_processor.py`).** The original pass applied every supported lifecycle event unconditionally to `settings.revenuecat_entitlement_id`, regardless of which entitlement(s) the event's own `entitlement_ids` actually named — meaning an unrelated RevenueCat product could grant or revoke this app's premium. Fixed: every lifecycle mutation now first checks `settings.revenuecat_entitlement_id in (event.entitlement_ids or [])`; a mismatch (or `null`) is a durably-received, successfully-processed `NOT_RELEVANT` outcome — never an error, never retried, never a projection write. `tests/domain/test_revenuecat_entitlement_processor.py` proves this for `INITIAL_PURCHASE`/`RENEWAL`/`CANCELLATION`/`EXPIRATION` against an unrelated or `null` `entitlement_ids`.

**`TRANSFER` handling (migration `9815eb266923`).** The original pass modeled `TRANSFER` as if it carried `app_user_id` (the destination) — it does not; RevenueCat's real payload carries only `transferred_from`/`transferred_to` (both always present) and `environment` (sometimes present). Fixed: the webhook route's required-field validation is event-type-aware (`TRANSFER` requires non-empty `transferred_from`/`transferred_to`, never `app_user_id`); the processor resolves every `transferred_to` entry and requires exactly one distinct resolvable local destination (zero or more than one both fail closed to `RECONCILIATION_REQUIRED`, never guessing or granting multiple unrelated users access); a missing `environment` also fails closed to `RECONCILIATION_REQUIRED` rather than a fabricated SANDBOX/PRODUCTION guess. Every resolvable source is revoked before the destination is activated (fail-safe ordering), so a processed transfer can never leave both sides holding paid access, and a partial failure favors temporary denial over duplicate paid entitlement. See `REVENUECAT_INTEGRATION_NOTES.md` section 3 and `ENTITLEMENT_STATE_MACHINE.md`.

## Sandbox/production isolation — IMPLEMENTED, TESTED

`user_entitlements.environment` is part of the row's own uniqueness scope — a `SANDBOX` purchase and a `PRODUCTION` purchase for the same user/entitlement are two separate rows. `RevenueCatEntitlementService` additionally filters by the running environment. `tests/domain/test_revenuecat_entitlement_service.py::test_sandbox_entitlement_never_grants_production_allowance` proves a `SANDBOX` `ACTIVE` row never satisfies a `PRODUCTION` allowance check.

## Provider failure / staleness policy — IMPLEMENTED, TESTED

`RevenueCatEntitlementService.get_analysis_allowance` performs exactly one local `SELECT` — it never constructs an HTTP client (`tests/domain/test_revenuecat_entitlement_service.py::test_entitlement_service_never_makes_a_network_call` fails loudly if that ever changes). A RevenueCat outage therefore degrades **reconciliation freshness only** — the local projection can drift stale relative to RevenueCat's true state until a webhook or a reconciliation run corrects it, but `POST /api/v2/analyses` and the worker's own quota decisions keep working from whatever the local projection currently says. Acceptable staleness is bounded by whichever is sooner: the next real webhook event for that user, or an operator-scheduled reconciliation run (not itself scheduled by this pass — **DEFERRED**, see `OPEN_ENGINEERING_ITEMS.md`).

## Reconciliation — IMPLEMENTED, TESTED (single-user and batch); scheduling is DEFERRED

`app/domain/revenuecat_reconciliation_service.py::RevenueCatReconciliationService`. **Corrected (migration `9815eb266923`):** the original client called a nonexistent `?expand=active_entitlements` query parameter on the plain customer-lookup endpoint; it now calls the real, documented, paginated `GET /v2/projects/{project_id}/customers/{customer_id}/active_entitlements` resource, follows `next_page` up to a bounded page count, and maps `401`/`403`/`404`/`429`/`5xx`/network failures to distinct error codes rather than one generic exception. `reconcile_user` compares local projection against that call and corrects on mismatch — without fabricating `will_renew=true` (the endpoint doesn't return renewal state; a correction to `ACTIVE` preserves whatever `will_renew` was already locally known). `reconcile_batch` runs a bounded list of users with a real inter-call delay (`DEFAULT_BATCH_DELAY_SECONDS`) and never lets one user's API failure abort the rest of the batch. Neither is wired to any HTTP route or cron in this pass — Section 13's "do not reconcile every user on every request" is satisfied by there being no automatic trigger at all yet, not by a rate limit on one. See `REVENUECAT_INTEGRATION_NOTES.md` section 5 for the full corrected contract.

**Pagination trust boundary, corrected (second pass).** RevenueCat documents `next_page` as a path *relative* to the API host, not an absolute URL — the first version of this fix's own test suite got this wrong too (it fabricated an absolute, same-origin `next_page` and asserted against that fabrication). More importantly, every request here carries `Authorization: Bearer <RevenueCat API key>`; a client that followed `next_page` wherever it pointed would hand that header to any URL a malformed or compromised response named. `RevenueCatAPIClient._resolve_and_validate_next_page` resolves `next_page` against the current page's own URL and requires the result to still share the configured API base's scheme/host/port, rejecting anything else outright (`UNTRUSTED_PAGINATION_ORIGIN`) *before* issuing a request to it — so the Authorization header can never leak cross-origin. A `next_page` resolving back to an already-fetched URL is rejected the same way (`PAGINATION_LOOP_DETECTED`), independent of the pre-existing `MAX_ACTIVE_ENTITLEMENT_PAGES` bound. See `tests/domain/test_revenuecat_reconciliation_http_contract.py`.

## Observability — IMPLEMENTED

`app/observability/events.py`: `revenuecat_webhook_received`, `revenuecat_webhook_rejected` (reason code only, never secret material), `revenuecat_event_duplicate`, `revenuecat_event_stale`, `revenuecat_event_processed`, `revenuecat_event_failed`, `revenuecat_event_requires_reconciliation`, `revenuecat_event_not_relevant`, `entitlement_activated`, `entitlement_expired`, `entitlement_revoked`, `reconciliation_success`, `reconciliation_mismatch`, `reconciliation_failure`. Structural test (`tests/domain/test_observability_events.py`) enforces every event function's parameter list stays inside a closed, non-PII vocabulary.

## Test coverage — TESTED

`tests/security/test_revenuecat_webhook_verification.py`, `tests/api/test_revenuecat_webhook_route.py`, `tests/domain/test_revenuecat_entitlement_processor.py`, `tests/domain/test_revenuecat_entitlement_service.py`, `tests/domain/test_revenuecat_reconciliation_service.py`, `tests/domain/test_revenuecat_reconciliation_http_contract.py` (the real `RevenueCatAPIClient` against `httpx.MockTransport`), `tests/database/test_revenuecat_rls.py`, `tests/database/test_revenuecat_billing_privilege.py`, plus additions to `tests/unit/test_config_validation.py`. All run against real Postgres (no mocked DB, except the RevenueCat HTTP transport itself, which has no reachable real endpoint to test against), same convention as the rest of this repository.

## DEFERRED (tracked in `OPEN_ENGINEERING_ITEMS.md`)

- Scheduling reconciliation automatically (cron/worker trigger).
- Admin/support promotional-entitlement grants.
- `sentry-sdk` wiring for billing-specific error reporting (it remains unwired repository-wide, pre-existing gap).
- Mobile SDK integration itself (no mobile app exists to call `Purchases.configure`).
