# Architecture — Current State

**Commit this document describes:** see `FOUNDATION_IMPLEMENTATION_REPORT.md` for the exact SHA (this file and that one were written/committed together, at the end of the same pass).

This describes the application as it **actually exists right now**, verified by direct inspection and, where noted, real test execution — not what was planned or described in any prior conversation. Status taxonomy used throughout: `VERIFIED_IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, `NOT_IMPLEMENTED`, `IMPLEMENTED_BUT_UNTESTED`, `IMPLEMENTED_INCORRECTLY`.

## What this repository is

A FastAPI backend (`backend/app/main.py`) with real authentication, a real Postgres database (behind a restricted, non-superuser runtime role with row-level security on every user-owned application table), a real CV pipeline (MediaPipe landmarks → real head pose → 8 skin/face metrics, each with real per-metric confidence and abstention), a real domain-driven planning/safety layer, a normalized product/ingredient catalog with formulation-level safety evaluation, atomic Redis rate limiting and an atomic usage-quota reservation ledger, a real pytest suite with CI, and a real production container image, health/readiness endpoints, fail-closed production config validation, and a provider-neutral object storage abstraction. A real RevenueCat webhook-driven billing synchronization foundation now exists (`BILLING_ARCHITECTURE.md`) — off by default (`REVENUECAT_BILLING_ENABLED=false`). No mobile app exists (the `mobile/` directory is empty scaffolding). No ranking or affiliate monetization exist yet.

## HTTP / API layer — `VERIFIED_IMPLEMENTED`

Real FastAPI app (`app/main.py`), 17 routes: `/health/live`, `/health/ready`, `/signup`, `/login`, `/refresh`, `/logout`, `/logout-all`, `/me` (GET + DELETE), `/analyze`, `/profile` (GET + PUT), `/consent`, `/consent/withdraw`, plus v2's `POST /api/v2/analyses` / `GET /api/v2/analyses/{analysis_id}` (`app/api/v2/analyses.py`, an `APIRouter` — the first router mounted outside `main.py` itself). (The unauthenticated `/db-check` debug route was removed alongside the RLS work below — it would have silently returned 0 once `users` gained row-level security, since it never set any session identity.) Startup/shutdown now use FastAPI's `lifespan` context manager (`app/main.py`'s `lifespan()`), replacing the deprecated `@app.on_event` handlers, wiring the same real Postgres pool and Redis client (`init_db_pool`/`init_redis`) — verified directly, not just by absence of the deprecation warning (`tests/database/test_smoke_infra.py::test_lifespan_initializes_and_closes_the_db_pool`). `FacialAnalysisPipeline`, `FacialScorer`, `PlanService` are constructed once at import time and genuinely called from `/analyze` — this call chain is proven by a real end-to-end test running a real photo through it (`tests/integration/test_end_to_end_analysis.py::test_full_chain_good_capture_succeeds`).

## Authentication — `VERIFIED_IMPLEMENTED`

JWT access tokens + opaque, SHA-256-hashed refresh tokens with family IDs (`app/security/tokens.py`). Refresh rotation is transactional (`app/main.py`'s `/refresh`): old-token consumption and successor creation happen in one explicit Postgres transaction, proven to roll back coherently on a real forced `UniqueViolationError`, not a mock. Replay of a consumed token revokes the whole family via Redis (`revoked_family:{family_id}`), verified by minting a second token from the same family after a replay and confirming it's also rejected. `get_current_user` (`app/security/auth.py`) checks Redis revocation *and* `users.is_active`/`deleted_at` directly against Postgres on every request — a disabled/deleted account's outstanding access tokens stop working immediately, not just at natural JWT expiry, verified with a real test disabling an account mid-session. Redis unreachability fails closed (`503`), distinct from `401`, both tested.

## Consent — `VERIFIED_IMPLEMENTED`

`consent_events` (append-only; `withdrawn_at` on the active row is the one narrow, deliberate exception) gates `/analyze` on a valid, current-policy-version grant (`app/db/consent_repository.py`). An old policy version or a withdrawn grant both correctly deny analysis; re-consenting to the current version restores access. Verified with 5 real tests plus the E2E test.

## User profile persistence — `VERIFIED_IMPLEMENTED`

`user_profiles` table + `app/db/profile_repository.py` replaces the old hardcoded placeholder profile in `/analyze` (`is_pregnant=False, has_sensitive_skin=False, experience_level="beginner"` for literally every user, regardless of who they were). A documented `DEFAULT_PROFILE` is used only when a user has never set one — never a silent, unlabeled guess.

## Database — `VERIFIED_IMPLEMENTED` (schema/migrations and isolation)

8 migrations, clean chain from empty database to head, verified by a real Alembic run in CI and in this session. Tables: `users`, `refresh_tokens`, `user_profiles`, `consent_events`. The application's runtime connection uses a restricted `skincare_app` role (`rolsuper=false, rolcreatedb=false, rolcreaterole=false, rolbypassrls=false`, owns no tables — confirmed by direct `pg_roles` query), not the `postgres` superuser. Row-level security is now enabled on **all four** tables (P0-1, migration `feb038fd05bd`), each using the mechanism that actually fits its access pattern rather than one pattern forced onto all of them:

- `user_profiles` / `consent_events`: `user_id = current_setting('app.current_user_id')`, set as the first statement of every transaction that touches them.
- `users`: the same identity-scoped policy for everything post-auth (self-lookup, self soft-delete), plus a permissive `WITH CHECK (true)` INSERT policy for self-registration. The one genuinely different case — `/login`'s pre-auth lookup by email, before any session identity exists — goes through `login_lookup_by_email`, a narrow `SECURITY DEFINER` function (owned by the migration's superuser, so it transparently bypasses RLS) returning only `id, password_hash` for one exact email. A direct `SELECT ... WHERE email = $1` against the table itself is now provably blocked pre-auth (tested).
- `refresh_tokens`: the existing `user_id`-scoped policy for everything where the caller's identity is already known (successor-token insert, family-wide revocation, logout-all, account-deletion cleanup), OR'd with a second policy scoped by a `app.current_token_hash` GUC for the token-hash lookups in `/refresh`/`/logout` that are genuinely pre-identity — the raw refresh token itself (a 48-byte random secret) is the only credential presented at that point, and proof of possession of its hash is a valid access boundary on its own. Tested to confirm possessing one token's hash does *not* expose the rest of its family.

Verified with 14 real cross-user Postgres integration tests run through the restricted role, not the superuser and not mocked (`tests/database/test_rls_isolation.py`): User A cannot read/update/delete User B's rows on any of the four tables even when explicitly querying by User B's ID; a reused pooled connection does not leak context between transactions; the login-lookup function correctly bypasses RLS for exactly one email while the raw table stays closed.

## Computer vision — `VERIFIED_IMPLEMENTED`

MediaPipe FaceMesh landmarks → real head pose (`app/cv/head_pose.py`, `cv2.solvePnP` against a generic 3D face model) → structured `CaptureAssessment` (`app/cv/capture_assessment.py`: `quality_status` PASS/BORDERLINE/FAIL, `overall_quality`, yaw/pitch/roll, blur/exposure/lighting_balance/face_size/resolution/occlusion sub-scores, `failure_reasons`) → 8 metrics, each returned as a `MetricResult` (`app/cv/metric_result.py`: `value`, `confidence`, `status` VALID/BORDERLINE/ABSTAINED, `uncertainty_reasons`) with a real, metric-specific confidence formula (`app/cv/metric_confidence.py`) derived from the actual corrupting factors for that metric, not a single blanket score applied to all 8 identically.

`FacialAnalysisPipeline.analyze()` genuinely gates on capture quality: `FAIL` raises before any metric is computed at all; `BORDERLINE` still computes metrics but marks the result `eligible_for_longitudinal_comparison: false`; `PASS` proceeds normally. `FacialScorer` (`app/ml/scorer.py`) genuinely respects abstention — an `ABSTAINED` metric is excluded from priority triggering outright, proven with a real test asserting a priority cannot trigger from an abstained metric regardless of what its withheld value would have implied.

**None of this is clinically validated** — every `MetricResult.calibration_version` is literally `"uncalibrated-1.0"`, and the confidence formulas, pose thresholds, and abstain/borderline cutoffs are documented, internally-consistent, hand-picked starting points, not values derived from a validation study. See `CV_VALIDATION_LIMITATIONS.md` for the full, honest breakdown of what each metric actually measures, what corrupts it, and what remains unsolved (notably the texture/blur circular dependency, unchanged from before this pass).

## Planning and safety — `VERIFIED_IMPLEMENTED`

`SafetyEngine`/`SafetyDecision` (`app/domain/safety_engine.py`) is real and wired into `PlanService.generate_plan()` — replacing the old undocumented inline pregnancy/nursing exclusion. Every priority and product-category candidate gets a `SafetyDecision` recorded in `plan.metadata.safety_decisions`. Allergy/avoid-ingredient enforcement here is real, operating against a category-level `ProductSafetyProfile` adapter (`app/domain/product_safety.py`) — **not per-product** — because `PlanService` has no product-matching/ranking step connecting its recommended categories to the normalized catalog that now exists (see `PRODUCT_CATALOG_ARCHITECTURE.md`); a missing safety profile fails closed (`SAFETY_DATA_UNAVAILABLE`), never silently "safe". `SafetyEngine.evaluate_product_formulation()`, new this pass, is the real, formulation-level, per-ingredient evaluation path — additive, not yet wired into `PlanService` (see `PRODUCT_CATALOG_ARCHITECTURE.md`). Sensitive skin genuinely caps active-ingredient frequency (routine text reflects the real cap) rather than only appending a disclaimer. A beginner's advanced-default concern stays visible with a capped `effective_intensity` instead of being dropped outright. Ranking's `display_order` bonus was reduced from a dominant factor (up to 0.45) to a true tie-break (0.001/step). Real per-metric confidence (Phase 10) now flows into severity via `PriorityTrigger.should_trigger()`'s `severity × confidence` multiplication, but `_rank_priorities_by_severity()` itself is still the original additive model (`severity + pillar_weight + tiebreak`), not the brief's preferred `severity × confidence × persistence × intervention-value` multiplicative one — that would need a validated intervention-value weighting this codebase doesn't have, so it wasn't fabricated.

## Platform foundation — `VERIFIED_IMPLEMENTED` (this pass)

- **Containerization**: `backend/Dockerfile`, a real multi-stage build (builder with `build-essential`; runtime with only `libgl1`/`libglib2.0-0` — mediapipe's actual, confirmed runtime dependency — plus the installed packages), non-root `appuser` (uid 1000), `HEALTHCHECK` against `/health/live`. `backend/.dockerignore` excludes secrets/caches/tests from the build context. **Built and run for real in this session** (`sudo docker build`/`docker run`, not just authored) against the real dev Postgres/Redis containers: `/health/live` → `200`, `/health/ready` → `200` with both dependency checks `ok`, a real `/signup` request round-tripped through the RLS-protected `users` table successfully, and the process was confirmed running as `appuser`, not root.
- **Health/readiness**: `GET /health/live` (no dependency checks — never restart-loops on a transient outage) and `GET /health/ready` (`503` with a per-dependency breakdown if Postgres or Redis is unreachable). 4 tests in `tests/unit/test_smoke.py`, including a real simulated database outage proving readiness fails closed while liveness stays unaffected.
- **Production config validation**: `app/config.py`'s `Settings` fails startup outright when `ENVIRONMENT=production` and any of a blank/placeholder/short `JWT_SECRET`, a dev/CI-marker `DATABASE_URL`/`REDIS_URL`, `ENABLE_DOCS=true`, wildcard `ALLOWED_ORIGINS`, or a placeholder R2 credential is present. **Verified against the real built container**, not just unit tests: a `docker run` with `ENVIRONMENT=production` and a short JWT secret/dev DB password/wildcard origins crashed at startup with an itemized `ValidationError`, while the same image with safe config started and served traffic normally. 10 unit tests in `tests/unit/test_config_validation.py`.
- **Object storage abstraction**: `app/storage/base.py`'s provider-neutral `ObjectStorage` ABC (`put`/`get`/`delete`/`exists`/`create_upload_authorization`/`create_download_authorization`) and `app/storage/r2.py`'s `CloudflareR2ObjectStorage`, the only module permitted to import boto3/botocore. 12 tests in `tests/storage/test_r2_adapter.py`, via `botocore.stub.Stubber` — no real R2 connectivity, no new dependency (botocore ships with boto3, already pinned in `requirements.txt`). **Nothing in the application calls this yet**, deliberately — see `PRODUCTION_ARCHITECTURE.md`'s Raw Face Image Policy.
- **Postgres connection layer**: `app/db/connection.py`'s pool now takes configurable min/max size, connect/command timeouts, and sets `application_name`, all from `Settings` rather than hardcoded.
- **Secrets/CI hardening**: `gitleaks` now runs as its own CI job; `pip-audit` (dependency CVE scan) and a Trivy container scan (against the real built image) run as two more.

Full detail, including the target architecture this is a first slice of, in `PRODUCTION_ARCHITECTURE.md`, `POSTGRES_OPERATIONS.md`, `DOKPLOY_DEPLOYMENT.md`, `FAILURE_DOMAINS.md`, `SCALING_TRIGGERS.md`.

## Async analysis foundation — `VERIFIED_IMPLEMENTED`, now a complete pipeline

Full detail in `ASYNC_ANALYSIS_ARCHITECTURE.md`, `RAW_IMAGE_LIFECYCLE.md`, `ANALYSIS_DATA_MODEL.md`, `WORKER_OPERATIONS.md`. What used to be a domain boundary and a job queue with nothing consuming either is now an actual end-to-end async path, real from HTTP submission through worker execution to result retrieval:

- **Shared compute core**: `app/domain/analysis_service.py`'s `compute_analysis()` is the quota-independent CV → scoring → abstract-plan → product-matching → routine-safety sequence, called by both `perform_analysis()` (the synchronous `/analyze` path, which wraps it with its own reserve/consume/release lifecycle) and `AnalysisExecutionService` (the async worker path, which reuses a reservation `AnalysisSubmissionService` already created). Neither path duplicates the CV/planning logic; only the quota-ownership wrapper differs.
- **Submission** (`app/domain/analysis_submission_service.py`'s `AnalysisSubmissionService`, behind `POST /api/v2/analyses`): consent check → quota reservation → raw image upload to ephemeral object storage → one atomic transaction marking the durable `analysis_requests` row `QUEUED` and enqueuing the worker job. Idempotent by client-supplied `request_id` (required, never manufactured — a retry-oriented async endpoint can't honestly manufacture one). Two failure-saga gaps closed this pass: a `create_request()` failure after a successful reservation now releases it (previously stranded `RESERVED` forever); an upload-succeeded-but-enqueue-failed window now compensates (delete the orphaned object, or if that itself fails, persist its reference independently so the cleanup sweeper can still recover it — see `RAW_IMAGE_LIFECYCLE.md`). Truly concurrent first-time submissions of the same `request_id` resolve to one logical analysis via real DB uniqueness (`analysis_usage`/`analysis_requests`/`jobs`), not a process-local lock.
- **Execution** (`app/domain/analysis_execution_service.py`'s `AnalysisExecutionService`): loads the durable request, retrieves the raw image, calls `compute_analysis()`, then one atomic transaction (`app/db/analysis_repository.py`'s `commit_analysis_result()`, against `analysis_results`/`analysis_measurements`/`analysis_product_recommendations` — migration `b034483cb876`, a table set this document previously, and incorrectly, described as not existing) writes the result/measurements/product recommendations, marks the request `COMPLETED`, and consumes the reservation — followed by best-effort raw-image deletion. Replaying `execute()` on an already-`COMPLETED` request is a genuine no-op (no recompute, no double consumption), proven directly, not just claimed.
- **Worker** (`python -m app.workers.analysis_worker`): claims a job (`SELECT ... FOR UPDATE SKIP LOCKED`), runs a background heartbeat (`extend_visibility`) alongside `execute()` so a long CV run doesn't lose its claim to another worker — proven with a real timing test, not a mocked clock. Classifies failures into a closed set of terminal (bad image, no face, invalid state) vs. everything-else-retryable (bounded by the job's own `max_attempts`), and only releases quota / marks the request `FAILED` on a genuinely terminal outcome (`fail()` now returns whether it left the job terminal, precisely so the worker can tell a requeue apart from a dead letter).
- **Retrieval** (`GET /api/v2/analyses/{analysis_id}`): owner-only (RLS-enforced — another user's analysis 404s identically to a nonexistent one), returns the persisted result once `COMPLETED`, and a closed set of safe error-code classifications (never a raw exception message) once `FAILED`.
- **Cell-readiness metadata**: `users.home_region`/`cell_id` (migration `219c52642ed4`) + `UserPlacementService`, snapshotted onto each `analysis_requests` row at submission — metadata only, no routing/replication built.

Test coverage: `tests/domain/test_analysis_submission_service.py` (9), `tests/domain/test_analysis_execution_service.py` (8), `tests/workers/test_analysis_worker.py` (5, including the real heartbeat/concurrent-claim test), `tests/api/test_analyses_v2.py` (8, full HTTP path with real signup/login/consent).

16 pre-existing job-queue tests (`tests/queue/test_postgres_job_queue.py`) plus new retry-backoff/heartbeat coverage, all run through the real restricted `skincare_app` role, not mocked.

## Product catalog — `VERIFIED_IMPLEMENTED` (this pass)

A normalized `brands → products → product_formulations → product_skus`
catalog, with ingredients resolved through `ingredient_aliases`, and the
safety boundary at the formulation level (not product/brand) — full detail
in `PRODUCT_CATALOG_ARCHITECTURE.md`. `SafetyEngine.
evaluate_product_formulation()` is the new, additive, real-ingredient-data
evaluation path (`ALLERGY_CONFLICT`, `USER_AVOID_INGREDIENT`,
`PREGNANCY_RESTRICTION`, `NURSING_RESTRICTION`,
`SENSITIVE_SKIN_INTENSITY_LIMIT`, `ACTIVE_INTERACTION_CONFLICT`,
`UNKNOWN_FORMULATION`, all actually triggered against real seeded data, not
just defined) alongside — not replacing — the pre-existing category-level
`evaluate_offer()` `PlanService` still uses (nothing yet matches
`PlanService`'s recommended categories to real catalog products; that is
ranking/matching work, out of scope this pass). Catalog tables are global
reference data with `SELECT`-only grants to `skincare_app` (no write access
at all — verified with a real `InsufficientPrivilegeError`), not RLS
(nothing in them has a `user_id`).

## Usage/rate-limit foundation — `VERIFIED_IMPLEMENTED` (this pass)

Full detail in `USAGE_AND_RATE_LIMIT_ARCHITECTURE.md`. Summary:

- **Rate limiting** (`app/middleware/rate_limiter.py`): atomic (single Lua
  script), fixed-window, three policies (`auth`/`analysis`/`general`),
  wired into every real route via FastAPI dependencies — not left unused.
  Per-policy fail-open/fail-closed Redis-failure behavior. Trusted-proxy-
  gated `X-Forwarded-For` handling.
- **Usage/quota reservation** (`app/db/usage_repository.py`,
  `app/domain/entitlement.py`, `analysis_usage` table, migration
  `ee276e90a60f`): atomic reserve/consume/release via a
  `pg_advisory_xact_lock`-serialized critical section, proven under real
  concurrent load to never oversubscribe a fixed allowance and to never
  double-reserve an idempotent retry. `EntitlementService`/
  `UsagePolicyService` is the RevenueCat-independent boundary (Phase 8);
  `FreeTierEntitlementService` is a real, working default policy, not a
  stub. Wired into `perform_analysis()`: reservation happens after the
  consent check and before CV compute; any failure past that point
  releases the slot, only full success consumes it.
- `analysis_usage` has row-level security (user-owned data, unlike the
  catalog tables above).

## What does not exist, at all (confirmed by direct inspection this pass, same as before except where noted)

- No offer/product catalog with pricing, availability, or affiliate/
  commission data. **What changed**: a real product-matching step now
  connects `PlanService`'s abstract categories to concrete catalog
  formulations (`ProductMatchingService` + `SafetyEngine.
  evaluate_product_formulation()`, via `apply_product_matching_and_
  routine_safety()`) — but its ordering ("compatibility ordering") is
  explicitly SAFE-before-RESTRICTED / market-exactness / verification
  freshness / a stable tie-break, never called "clinical efficacy" or
  "commercial ranking", because no such data or evidence exists to
  rank by. See `PRODUCT_RECOMMENDATION_PIPELINE.md`.
- No mobile app source (`mobile/` is empty directory scaffolding).
- No notification system, no transactional outbox / domain events.
- No cell-based routing, replication, or sharding — `home_region`/
  `cell_id` are metadata-readiness columns + `UserPlacementService`
  only (see "Async analysis foundation" above).
- No metrics/Prometheus backend deployed. `app/observability/events.py`
  now provides real, wired-in structured log events (analysis
  submission outcomes, queue claim/wait, processing success/retry/
  dead-letter, image cleanup failures, capture quality, metric
  abstention, no-compatible-product, quota/rate-limit denial) — a
  genuine step past "nothing but stdlib `logging`", but still not a
  time-series metrics/alerting system.

## Billing (RevenueCat) — `VERIFIED_IMPLEMENTED` (this pass, hardened by independent review), off by default

Full detail in `BILLING_ARCHITECTURE.md` and `ENTITLEMENT_STATE_MACHINE.md`. Summary: a webhook endpoint (`POST /api/v2/webhooks/revenuecat`, HMAC + Authorization-header verified against the raw request body, event-type-aware required-field validation) durably records every RevenueCat event (`revenuecat_webhook_events`, migrations `a1c9f3e7b2d4`/`9815eb266923`) and enqueues it on the existing `JobQueue` — no entitlement logic runs inline in the webhook request. A dedicated worker (`app/workers/revenuecat_webhook_worker.py`) processes events idempotently into a local, provider-neutral projection (`user_entitlements`), respecting RevenueCat's real cancellation-vs-expiration-vs-refund semantics (without fabricating a renewal-preference change on refund), out-of-order delivery, `entitlement_ids` gating (an unrelated RevenueCat product cannot touch this app's premium), and the real `TRANSFER` field contract (`transferred_from`/`transferred_to`, never `app_user_id`; ambiguous/unresolvable/no-environment cases fail closed to `RECONCILIATION_REQUIRED`) — `app/domain/revenuecat_entitlement_processor.py`. `RevenueCatEntitlementService` (`app/domain/entitlement.py`) reads only this local projection, through the ordinary runtime pool — never RevenueCat's API — behind the same `EntitlementService` interface `FreeTierEntitlementService` has always implemented, so a RevenueCat outage cannot make analysis unavailable. `RevenueCatReconciliationService` corrects drift against RevenueCat's real, documented, paginated `active_entitlements` REST resource (not the nonexistent `?expand=active_entitlements` the original pass assumed), single-user or bounded-batch, not wired to any automatic trigger yet. A dedicated, least-privilege `skincare_billing` database role (migration `9815eb266923`) is now the *only* role that can write `revenuecat_webhook_events`/`user_entitlements` at all — the ordinary runtime role (`skincare_app`) has no write grant on either table, closing a gap where RLS alone would have let an ordinary request write itself an entitlement row. `REVENUECAT_BILLING_ENABLED` defaults `false`; production startup refuses to enable it without all four required secrets *and* a distinct `REVENUECAT_BILLING_DATABASE_URL` configured. 100+ tests across the original and hardening passes, all against real Postgres (the RevenueCat HTTP transport itself is tested via `httpx.MockTransport`, since there is no reachable real endpoint to test against), no DB mocking.

## Catalog ingestion & administration — `VERIFIED_IMPLEMENTED`, hardened by independent review, CLI-only

Full detail in `CATALOG_INGESTION_ARCHITECTURE.md`. Summary: a controlled pipeline (`app/domain/catalog_ingestion_service.py`, `catalog_publication_service.py`, `catalog_review_service.py`, `catalog_validation.py`) that safely feeds the *existing* normalized catalog (`PRODUCT_CATALOG_ARCHITECTURE.md`) without touching its schema or `SafetyEngine`. JSON/JSONL source files import into durable, immutable staging rows (`catalog_import_batches`/`catalog_import_records`, migration `2de8380d3618`; column-level `UPDATE` privileges since migration `e5277cf2f0ee` make the immutable fields database-enforced, not just unwritten-to by convention), normalize against a typed, bounded Pydantic contract (a non-object JSON value at record position is its own `MALFORMED` outcome, never an exception that aborts sibling records), resolve ingredients through the existing exact canonical/alias resolver only (never fuzzy — an unresolved name becomes an audited human-review item, `catalog_review_items`, one row per distinct unresolved ingredient since migration `154080b29153`, reconciled by one canonical validation function shared between the initial pass and post-review revalidation), and publish atomically (`CatalogPublicationService`, one transaction, row-locked before any status decision so concurrent publication of the same record is idempotent, `--dry-run` supported via rollback) into `brands`/`products`/`product_formulations`/`product_skus`/`formulation_ingredients`, with permanent provenance (`catalog_formulation_provenance`) and an append-only audit trail (`catalog_audit_log`, both database-enforced immutable since `e5277cf2f0ee`). A new `product_formulations.publication_status` column (migration `37c88143a9ed`, six states, `DRAFT` default) is a genuinely distinct concept from `is_current`/`ingredient_data_status` — `ProductMatchingService`'s discovery query now requires `PUBLISHED` + `COMPLETE` + `is_current` simultaneously, closing the gap where any of those three alone would have been enough. A reformulation supersedes the old formulation and publishes the new one atomically, never leaving two `is_current` rows for the same product/market (database-enforced via the pre-existing partial unique index, proven under real concurrency, including two different records racing to introduce the same previously-unseen brand/product). Database writes go through a dedicated, `NOLOGIN`-from-creation `skincare_catalog_admin` role (migration `13c1fff1867e`) — `skincare_app` retains its pre-existing `SELECT`-only posture on every catalog table, unchanged. No HTTP route; `python -m app.catalog_admin` (gated by `CATALOG_ADMIN_ENABLED`) is the only admin surface this pass ships. No web crawler or provider-specific acquisition adapter — this pass ingests bytes an operator already has in hand.

## Test foundation and CI — `VERIFIED_IMPLEMENTED`

`backend/tests/{unit,integration,auth,cv,planning,database,queue,storage,domain,catalog,catalog_admin,usage,middleware}/`, isolated from dev infra (separate `skincare_test` database, Redis index 1). GitHub Actions (`.github/workflows/ci.yml`) runs the full suite against real Postgres/Redis services on every push/PR. See `PRODUCT_USAGE_IMPLEMENTATION_REPORT.md` for this pass's exact test count and pass/fail breakdown.
