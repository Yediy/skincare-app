# Catalog Ingestion & Administration Architecture

**Commit this document describes:** the `feat/catalog-ingestion-admin` branch, base `master` at `3189050143a0066617fa1caaf77b17f53b465c3b` (the fully-closed RevenueCat billing foundation).

Classification used throughout, matching `PRODUCT_CATALOG_ARCHITECTURE.md`/`ARCHITECTURE_CURRENT.md`: **DESIGNED** (documented, no code), **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (explicitly out of scope for this pass, tracked in `OPEN_ENGINEERING_ITEMS.md`).

## Scope of this pass

A controlled, auditable pipeline that safely feeds the *existing* normalized product catalog (`PRODUCT_CATALOG_ARCHITECTURE.md`) without touching its core schema, its safety boundary, or `SafetyEngine`/`ProductMatchingService` in any way beyond one new, narrow discovery-time filter. Explicitly **DEFERRED**: a web crawler or any provider-specific *acquisition* adapter (this pass ingests bytes the operator already has in hand — see "What this pass does NOT build" below), an HTTP admin surface, ingredient safety-rule (`ingredient_rules`/`ingredient_interactions`) authoring, fuzzy-match-based automatic resolution.

## Core invariant

```
INGESTED != VERIFIED != PUBLISHED != SAFE FOR EVERY USER
```

Only `SafetyEngine.evaluate_product_formulation()` ever determines user-specific safety — nothing in this pass touches that determination. "Published" means *trusted enough to be evaluated*, never *safe for everyone*.

## Pipeline — IMPLEMENTED, TESTED

```
External/curated source file (JSON or JSONL)
        |  CatalogIngestionService.import_file()
        v
catalog_import_batches / catalog_import_records   (immutable raw_payload)
        |  normalize_record() -- typed, bounded, fail-closed contract
        v
normalized_payload stored on the same, still-immutable-raw_payload record
        |  CatalogIngestionService.validate_batch()
        |    - identity resolution (read-only: does this brand/product already exist?)
        |    - ingredient resolution (exact canonical/alias match ONLY -- app/db/catalog_repository.resolve_ingredient)
        |    - structural checks (market present, SKU/UPC conflicts, source-type plausibility)
        v
   ------------------------------
   |                            |
NEEDS_REVIEW                 VALIDATED
   |  CatalogReviewService        |
   |  (map/create ingredient,     |
   |   confirm/reject identity)   |
   v                              |
 VALIDATED  <----------------------
        |  CatalogPublicationService.publish()  -- ONE transaction
        v
brands / products / product_formulations / product_skus / formulation_ingredients
        |
catalog_formulation_provenance + catalog_audit_log
        |
product_formulations.publication_status = 'PUBLISHED'
        v
ProductMatchingService (only PUBLISHED + COMPLETE + is_current formulations are ever candidates)
        v
SafetyEngine.evaluate_product_formulation()  -- unchanged, still the only source of user-specific safety
```

Every arrow above is real code exercised by a real test (see "Test coverage" below), against real Postgres, through the real restricted database roles — not a design sketch.

## What this pass deliberately preserves, unmodified

- The existing safety boundary: `product_formulations` (not `products`) is still the only place an ingredient list lives; formulation history is never flattened; `declared_concentration` is never fabricated.
- `SafetyEngine` and its four-state `SafetyDecision` (`SAFE`/`RESTRICTED`/`UNSAFE`/`INSUFFICIENT_DATA`) — zero changes.
- `app/db/catalog_repository.py`'s existing read paths (`resolve_ingredient`, `get_formulation_by_id`, etc.) — reused directly by this pass's own services, never duplicated.
- `ingredient_data_status`'s existing three-value, fail-closed vocabulary (`COMPLETE`/`PARTIAL`/`UNKNOWN`, migration `ac641537d224`) — this pass is the first thing that can actually *promote* a formulation to `COMPLETE` outside a raw migration/fixture, and it enforces the exact same "never inferred from count" rule that column's own migration established.
- The runtime API's `SELECT`-only posture on every catalog table (migrations `d70e5fc90775`/`16b82dde6e7d`) — unchanged; catalog administration remains entirely outside the HTTP surface (see "Admin surface" below).

## What this pass adds

### Publication lifecycle — IMPLEMENTED, TESTED (migration `37c88143a9ed`)

`product_formulations.publication_status`, six states, `NOT NULL DEFAULT 'DRAFT'` (fail-closed, same posture as `ingredient_data_status`'s own default): `DRAFT` -> `NEEDS_REVIEW` -> `VERIFIED`\* -> `PUBLISHED` -> `SUPERSEDED`, or `REJECTED` at any point before publication. (\*This pass's `CatalogPublicationService` publishes directly from a `VALIDATED` staging record and does not itself write an intermediate `VERIFIED` formulation-table state — the state exists in the vocabulary for a future human-verification-gate-before-publish workflow, `DEFERRED`, see `OPEN_ENGINEERING_ITEMS.md`.)

This is a *distinct* field from `is_current`/`ingredient_data_status` on purpose — the brief's own explicit instruction: `is_current=true` must never be overloaded to mean verified+published+safe+current+complete all at once. A formulation participates in `ProductMatchingService`'s recommendation candidates only when **all three** hold simultaneously:

```sql
publication_status = 'PUBLISHED' AND ingredient_data_status = 'COMPLETE' AND is_current = true
```

— enforced in exactly one place, `app/db/catalog_repository.py::list_current_active_formulations_by_category` (the only query `ProductMatchingService` uses for discovery). `SafetyEngine.evaluate_product_formulation()` itself has, and needs, no notion of `publication_status` at all — the gate is entirely a *discovery-time* filter, never folded into safety evaluation itself, keeping the two concerns structurally separate.

### Staging domain — IMPLEMENTED, TESTED (migration `2de8380d3618`)

Six new tables, all global/operational data (no `user_id`, no RLS — same posture as the pre-existing catalog tables and `jobs`):

| Table | Purpose |
|---|---|
| `catalog_sources` | Registered ingestion sources. `source_type` reuses `product_formulations`' own five-value vocabulary verbatim, plus one ingestion-specific addition (`curated_dataset`) — no credentials column exists on this table at all. |
| `catalog_import_batches` | One row per ingestion run of one file. `UNIQUE(source_id, content_sha256)` — whole-file idempotency. |
| `catalog_import_records` | One row per source record. `raw_payload` is immutable after receipt — no code path in this pass ever overwrites it, only `normalized_payload`/`status`/`validation_errors`/`review_reason_codes`/`formulation_id` change after insert. `UNIQUE(batch_id, external_record_id)`. |
| `catalog_review_items` | Durable human-review queue, ten explicit reason codes (never bare prose as the only record of why). |
| `catalog_formulation_provenance` | `UNIQUE(formulation_id)` — every formulation this pipeline ever publishes gets exactly one permanent provenance row, answering "what import record, verified when, by whom, superseding what" even after a later reformulation. |
| `catalog_audit_log` | Append-only. Records small structured before/after summaries, never a duplicated copy of the (already-immutable) raw payload. |

### Database privilege boundary — IMPLEMENTED, TESTED (migration `13c1fff1867e`)

A dedicated, least-privilege `skincare_catalog_admin` role, following the corrected billing architecture's own pattern *from day one* rather than repeating its history: created `NOLOGIN` immediately (no hardcoded password ever shipped in any migration, and no LOGIN phase to later transition away from — see `BILLING_ARCHITECTURE.md`'s own credential-design section for the mistake this deliberately avoids repeating). The actual connectable login (`skincare_catalog_runtime` in this repository's dev/CI infrastructure — `tests/conftest.py`) is provisioned outside Alembic entirely and granted membership.

| Role | Staging tables | `brands`/`products`/`product_formulations`/`product_skus`/`ingredients`/`ingredient_aliases`/`formulation_ingredients` | `ingredient_rules`/`ingredient_interactions` | users/billing/analysis tables |
|---|---|---|---|---|
| `skincare_app` (ordinary runtime) | no grant | `SELECT` only (unchanged) | `SELECT` only (unchanged) | as before, unaffected |
| `skincare_catalog_admin` (dedicated) | `SELECT`/`INSERT`/`UPDATE` | `SELECT`/`INSERT`/`UPDATE` | **no grant** (deliberately out of scope this pass) | **no grant at all** |

No `DELETE` grant anywhere for `skincare_catalog_admin` — a superseded formulation is superseded, never erased; `catalog_import_records.raw_payload` is durable evidence, never removed.

`CATALOG_ADMIN_ENABLED` / `CATALOG_ADMIN_DATABASE_URL` (`app/config.py`) gate the CLI exactly like `REVENUECAT_BILLING_ENABLED`/`REVENUECAT_BILLING_DATABASE_URL` gate billing — production config validation refuses a missing DSN, a DSN identical to `DATABASE_URL`, or one carrying a known dev/CI marker (`skincare_catalog_dev_only` included).

Proven in `tests/database/test_catalog_admin_privilege.py`: `skincare_catalog_admin` can write every staging and production catalog table this pipeline needs, and gets an `InsufficientPrivilegeError` attempting to write `ingredient_rules`/`ingredient_interactions`/`users`/`refresh_tokens`/`consent_events`/`user_entitlements`/`revenuecat_webhook_events`; `skincare_app` gets the same error attempting to write any of the six new staging tables.

### Normalization contract — IMPLEMENTED, TESTED (`app/domain/catalog_normalization.py`)

A typed Pydantic model (`NormalizedImportRecord`), `extra="forbid"`, with real bounds (255/2000-char strings, 200 ingredients, 50 SKUs per record) — a malformed or hostile record fails `normalize_record()` deliberately (`MalformedRecordError`, carrying Pydantic's own structured `.errors()`), never silently accepted or accepted-then-resource-exhausting. `ingredient_list_complete` is read verbatim from the source — never inferred from ingredient count or "the last entry looks minor" (Section 5's explicit prohibition).

### Ingredient resolution — IMPLEMENTED, TESTED

No new resolver exists. `CatalogIngestionService.validate_batch()` and `CatalogPublicationService.publish()` both call the *existing*, unmodified `app/db/catalog_repository.py::resolve_ingredient` — exact canonical-name match, then exact alias match, never fuzzy. An unresolved raw name becomes a `catalog_review_items` row (`UNKNOWN_INGREDIENT`), never an automatic best-guess. Human review (`app/domain/catalog_review_service.py`) may:

- **map** the raw string to an existing canonical ingredient (creates a durable alias, so future imports resolve it automatically);
- **create** a new canonical ingredient (and, if the raw string differs from the new canonical name, an alias too);
- **reject** the source value (the whole import record becomes `REJECTED`, terminal).

Every one of these three actions is audited (`catalog_audit_log`, actions `CREATE_INGREDIENT`/`ADD_ALIAS`/`REJECT`). Creating an alias fails closed (`AliasConflictError`) if the normalized string already means something else — canonical name or existing alias — never silently overwriting an existing mapping (Section 17).

### Identity resolution — IMPLEMENTED, TESTED

Brand: exact `normalized_name` match, creating a new brand only when none exists. Product: exact `(brand_id, normalized_name)` match. Both are structurally incapable of ambiguity in this pass (the underlying columns are `UNIQUE`) — `PRODUCT_IDENTITY_AMBIGUOUS`/`BRAND_IDENTITY_AMBIGUOUS` exist in the review-reason-code vocabulary for a future fuzzy-suggestion admin tool (`DEFERRED`) but are not automatically triggerable by this pass's own exact-match code. Never silently merges two plausible existing products — there is no code path here that could, since exact match returns at most one row by construction.

### Publication — IMPLEMENTED, TESTED (`app/domain/catalog_publication_service.py`)

`CatalogPublicationService.publish(import_record_id, actor, dry_run=False)`. One transaction per call — commits atomically or rolls back with zero effect (`dry_run=True` always rolls back, after doing the real work, so a preview is provably equivalent to a real publish minus persistence). Steps: re-verify the record is `VALIDATED`; resolve/create brand and product deterministically; re-resolve every ingredient fresh (never trusting a possibly-stale validate-time result); run the structural safety backstop (below); check for a currently-published formulation on the same product/market (`SELECT ... FOR UPDATE`, serializing concurrent publications); either publish fresh, reuse an identical-content republish as a no-op, or supersede-and-publish a genuine reformulation; write provenance and an audit trail; mark the staging record `PUBLISHED`.

**Idempotency, extended through publication (Section 9):** the same record content, re-imported through an entirely new file/batch, resolves (at publish time, by comparing `payload_sha256` against the currently-published formulation's own provenance) to the *same* formulation — no duplicate ever created. Genuinely different content for the same `(product, market, formulation_version)` is rejected outright (`FORMULATION_VERSION_CONFLICT`) rather than silently overwritten.

### Reformulation — IMPLEMENTED, TESTED

```
Formulation A (published, current)
        |  new source data differs materially
        v
Formulation B created (DRAFT internally, never visible mid-transaction)
        |  same publish() transaction:
        |    A.is_current = false, A.publication_status = SUPERSEDED   (ordered FIRST)
        |    B.is_current = true,  B.publication_status = PUBLISHED    (ordered SECOND)
        v
Both rows persist forever -- A is never deleted (historical analyses may reference it)
```

The ordering above is not incidental: `product_formulations`' own pre-existing partial unique index (`idx_product_formulations_one_current_per_market`, migration `d70e5fc90775`) permits at most one `is_current = true` row per `(product_id, market_or_region)` — superseding the old row *before* the new one is ever marked current means the database itself would reject any attempt to violate that invariant, not just careful code ordering. `tests/domain/test_catalog_publication_service.py::test_two_concurrent_publications_same_product_market_preserve_unique_current` proves this under real concurrent load (`asyncio.gather`), not merely by reading the code.

### Structural safety backstop — IMPLEMENTED, TESTED

Immediately before a formulation may become `PUBLISHED` (`app/domain/catalog_publication_service.py::_run_structural_backstop`), independent of everything `validate_batch` already checked (defense in depth against staging state changing between validate and publish): ingredient positions contiguous and unique, no duplicate ingredient (checked both by raw name at normalization time and by *resolved ingredient_id* at publish time — two differently-spelled raw names resolving to the same canonical ingredient is exactly the duplicate normalization-time checks alone cannot catch), every ingredient resolves, a `COMPLETE` claim is only honored when ingredients actually exist, market present, source type valid. A violation blocks publication outright — it never partially commits, and (Section 15's own "if any required step fails: ROLLBACK") it never reaches outside its own transaction to leave any other side effect either; the whole attempt rolls back as one unit.

### Admin surface: CLI first — IMPLEMENTED, TESTED (`app/catalog_admin`)

`python -m app.catalog_admin <command> ...` — refuses to run at all unless `CATALOG_ADMIN_ENABLED` is set, then connects through the dedicated `skincare_catalog_admin` role. No HTTP route in this pass exposes any catalog mutation; this CLI is the only way to mutate the catalog. Thin argparse adapter (`app/catalog_admin/cli.py`) over the three domain services — no business logic in an argparse handler.

Commands: `create-source`, `import-file` (`--dry-run` supported, performs zero database writes), `validate-batch` (`--dry-run` supported, transactional rollback), `batch-status`, `review-list`, `review-show`, `resolve-ingredient` (`--map-to` / `--create-canonical`), `approve` (generic non-ingredient review dismissal), `reject`, `publish` (`--dry-run` supported, transactional rollback).

## What this pass does NOT build

- **No web crawler, no provider-specific acquisition adapter, no arbitrary-URL fetching.** `import_file()`'s only input is bytes the caller already has (read from a controlled local file) — acquiring those bytes from a manufacturer/provider is a separate, deliberately out-of-scope concern (Section 8's own instruction). The adapter seam (`_parse_raw_records`) is designed so a future CSV or provider-specific adapter is a new function producing the same list of raw `dict` records this one already hands downstream — nothing below that seam would need to change.
- **No HTTP admin route.** CLI only, per the brief's own explicit instruction.
- **No ingredient safety-rule authoring** (`ingredient_rules`/`ingredient_interactions`) — `skincare_catalog_admin` has no grant on either table.
- **No fuzzy matching anywhere in the automatic path.** `BRAND_IDENTITY_AMBIGUOUS`/`PRODUCT_IDENTITY_AMBIGUOUS`/`DUPLICATE_ALIAS_CONFLICT` exist in the review vocabulary for a *future* admin-suggestion tool, `DEFERRED` — not implemented, not triggerable automatically by anything in this pass.
- **Real catalog population.** This pass ships infrastructure only; populating it with real commercial brand/product data is a separate, later, controlled data pass (Section 23) — every test in this pass uses synthetic brands/products, same convention `PRODUCT_CATALOG_ARCHITECTURE.md`'s own `synthetic_catalog` fixture already established.

## Untrusted input handling — IMPLEMENTED, TESTED

Every SQL statement is parameterized (no dynamic SQL construction from source strings, anywhere in `app/db/catalog_admin_repository.py`). Bounded: 50MB whole-file ceiling, 2MB per-record ceiling (an oversized record is never persisted — even its `raw_payload` becomes a small honest placeholder, `{"_rejected": "PAYLOAD_TOO_LARGE", ...}`, never the dangerous content itself), 5000 records per batch, 200 ingredients / 50 SKUs per record, 255/2000-character string fields. Malformed overall JSON fails the whole batch outright before any database write; one malformed record within an otherwise-valid JSONL/JSON-array file never aborts its siblings. No shell execution, no template evaluation, no HTML rendering assumption anywhere in this pipeline.

## Test coverage — TESTED

`tests/domain/test_catalog_normalization.py`, `tests/domain/test_catalog_ingestion_service.py`, `tests/domain/test_catalog_publication_service.py`, `tests/domain/test_catalog_review_service.py`, `tests/domain/test_product_matching_publication_gate.py`, `tests/catalog_admin/test_cli.py`, `tests/catalog_admin/test_main_gating.py`, `tests/database/test_catalog_admin_privilege.py`, plus additions to `tests/unit/test_config_validation.py`. All run against real Postgres (no mocked DB), through the real restricted `skincare_catalog_admin`/`skincare_app` roles for every privilege-relevant assertion — same convention as every other pass in this repository.

## DEFERRED (tracked in `OPEN_ENGINEERING_ITEMS.md`)

- Web crawler / provider-specific source acquisition.
- HTTP admin route for catalog mutation.
- Ingredient safety-rule (`ingredient_rules`/`ingredient_interactions`) authoring.
- Fuzzy-match-based admin *suggestions* (never automatic decisions).
- A CSV import adapter (the seam exists; the adapter itself does not yet).
- An explicit `VERIFIED` formulation-table state gate between `validate_batch` and `publish` (the vocabulary exists; this pass's own publish path goes `VALIDATED` -> `PUBLISHED` directly).
- Real commercial catalog population (a separate, later, controlled data pass).
