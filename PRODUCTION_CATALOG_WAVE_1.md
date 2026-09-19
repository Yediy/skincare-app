# Production Catalog Wave 1

**Commit this document describes:** the `feat/production-catalog-wave-1` branch, base `master` at `9aabba4ee8471f0dd048725f9c24725630c3a9c5` (the merged V1 account recovery / release foundation pass, PR #11), hardened by two independent review passes that closed four merge blockers total (a publication-time backstop for `INSUFFICIENT_SOURCE_DATA` Wave 1 records; order-independent/trust-conflict-checked grouping with full merged-provenance preservation; an idempotent-reimport reporting fix; and a post-commit failure-recovery fix for the same reporting mechanism) — see "Independent review pass" below.

Classification used throughout, matching `CATALOG_INGESTION_ARCHITECTURE.md`/`PRODUCT_CATALOG_ARCHITECTURE.md`: **DESIGNED** (documented, no code), **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (explicitly out of scope for this pass, tracked in `OPEN_ENGINEERING_ITEMS.md`).

## Wave 1 purpose

`CATALOG_INGESTION_ARCHITECTURE.md`'s own "What this pass does NOT build" section left one item explicitly deferred: **real catalog population**. That pass shipped infrastructure only — every test in it uses synthetic brands/products, deliberately, and its own closing line states population is "a separate, later, controlled data pass." This document describes that pass.

Wave 1 is **not** a redesign of catalog ingestion. Every invariant `CATALOG_INGESTION_ARCHITECTURE.md` establishes — immutable raw imports, exact-match-only identity/ingredient resolution, the durable human-review queue, atomic publication, formulation provenance, the append-only audit log, reformulation-via-supersession, publication-status gating, the restricted `skincare_catalog_admin` database role, concurrency protections, and the review-identity-binding hotfix — is preserved completely unmodified. Wave 1 adds one new layer **above** that pipeline (`app.domain.catalog_source_adapter`/`catalog_wave_service`/`catalog_wave_report`) and touches **zero** lines of `catalog_ingestion_service.py`, `catalog_review_service.py`, or `catalog_validation.py`.

**One narrow, deliberate exception, added by independent review** (not part of the original pass): `catalog_publication_service.py` gained a small, additive publication-eligibility check — see "Publication eligibility backstop (the one Wave 1 addition to existing publication machinery)" below for exactly what was added, why, and the precise line-level scope of that change. Every OTHER line of every file the paragraph above names is unchanged, and every pre-existing behavior for non-Wave-1 data is provably identical to before this pass (the original, unedited `tests/domain/test_catalog_publication_service.py` suite is re-run unmodified and stays green).

No new database migration exists in this pass — every concept Wave 1 needed (content fingerprinting, verification timestamps, source provenance, an ingestion-source type) already had a home in the existing schema; see "Provenance" below for exactly where.

The goal: make the pipeline **operational** for a first controlled, auditable production skincare catalog, covering seven target categories (cleansers, moisturizers, sunscreens, acne treatments, serums, exfoliants, barrier-repair products), with real product records sourced only from explicitly documented material — never fabricated, never scraped.

## What this pass actually shipped vs. what it did not populate — IMPLEMENTED, NOT POPULATED

**Superseded — this pass's own catalog is now populated.** This document originally stated (and the paragraph below is preserved verbatim for the historical record) that this pass built the acquisition/verification/reformulation infrastructure end-to-end without importing any real commercial product data. **Production Catalog Wave 1B** (`PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md`, branch `feat/production-catalog-wave-1b-real-data`) is the follow-up pass that acquired and published the first real products through this exact, unmodified pipeline — 11 real CeraVe/The Ordinary products, sourced from official manufacturer pages, with full provenance and formulation fingerprints. See that document for the complete record (source URLs, ingredient evidence, publication counts, known source limitations for the two domains that did not yield usable data this pass).

Original text, for the historical record of this pass's own scope at the time it was written: "This pass built the acquisition/verification/reformulation infrastructure end-to-end and proved it against real Postgres. It did **not** import any real commercial product data. No manufacturer, brand, or product name anywhere in this pass's own test suite is real — every fixture uses the same obviously-synthetic convention `PRODUCT_CATALOG_ARCHITECTURE.md`'s own `synthetic_catalog` fixture already established (`Testonyx`-style names). Real product records require explicitly documented source material (a manifest file, a set of URLs, or an equivalent handed to an operator with real evidence) that was not supplied to this pass — see 'Real data acquisition' below for exactly why this pass did not attempt to acquire any on its own. Every 'REAL PRODUCTS IMPORTED'/'REAL PRODUCTS PUBLISHED' figure this pass reports is honestly `0`, per this document's own instruction never to claim otherwise."

## Source architecture — IMPLEMENTED, TESTED (`app/domain/catalog_source_adapter.py`)

```
Wave 1 curated source manifest (JSON/JSONL, bytes an operator already has in hand)
        |  CuratedManifestSourceAdapter.parse()
        v
SourceManifestRecord (Wave 1's own typed, bounded, extra="forbid" schema)
        |  manifest_record_to_raw_import_dict() -- the field-mapping transform
        v
raw import dict -- EXACTLY the shape NormalizedImportRecord accepts, nothing more
        |  (serialized to an in-memory JSONL byte stream)
        v
CatalogIngestionService.import_file()   <-- EXISTING, UNMODIFIED, unchanged since
        |                                    CATALOG_INGESTION_ARCHITECTURE.md
        v
... the rest of the existing pipeline, completely unaware Wave 1 exists ...
```

`CatalogSourceAdapter` is a narrow ABC (one method, `parse(raw_bytes) -> ManifestParseResult`) — this pass's own realization of `CATALOG_INGESTION_ARCHITECTURE.md`'s own "adapter seam" note that a future adapter is "a new function that turns ITS shape into the same list of raw dict records this one already hands downstream." One concrete adapter exists: `CuratedManifestSourceAdapter`, for a curated JSON/JSONL manifest. Nothing in this pass fetches those bytes itself — acquisition (how an operator obtained the manifest in the first place) stays a deliberately separate, out-of-scope concern, exactly as the ingestion-admin pass's own brief already established for `import_file()` itself.

### Acquisition boundary — enforced by omission

This pass contains **no HTTP client that fetches an arbitrary URL**, **no HTML parser**, **no robots.txt handling**, **no anti-bot bypass of any kind**, and **no Ulta/Sephora/retailer-specific scraping logic**. `CatalogSourceAdapter.parse()`'s only input is `bytes` — there is no code path anywhere in this pass by which those bytes could have come from a live web fetch this pass itself performed. A future manufacturer-hosted structured-data/API adapter (explicitly optional per this pass's own brief) remains genuinely **DEFERRED** — see `OPEN_ENGINEERING_ITEMS.md`'s item 18a, only partially closed by this pass (the curated-manifest half; a real acquisition adapter is still unbuilt).

### Real data acquisition — deliberately not attempted this pass

This pass's own brief requires real product records to come from "explicitly documented source material," while separately forbidding a general-purpose crawler or retailer scraping. No source material (a manifest file, a specific list of URLs, or equivalent) was supplied alongside this pass's own instructions. Independently fetching a small number of real manufacturer pages via an ad hoc script was considered and deliberately **not** done: it would have meant this pass unilaterally choosing which external sites to contact, with no per-site compliance review, no explicit authorization for those specific sources, and no way to distinguish that from exactly the scraping-adjacent behavior this pass's own exclusions warn against. The safer, explicitly-permitted path — this document's own brief states "do not claim the production catalog is populated unless real sourced records have actually been supplied and imported" — is the one this pass took: ship the infrastructure, populate it with **zero** real records, and leave real acquisition to a follow-up pass with an operator-supplied manifest or an explicitly authorized, reviewed provider adapter.

## Source manifest — IMPLEMENTED, TESTED

`SourceManifestRecord` (`app/domain/catalog_source_adapter.py`), one Pydantic model, `extra="forbid"` (an unrecognized field fails the whole record deliberately, same fail-closed posture `NormalizedImportRecord` itself uses one layer down):

| Field | Maps to |
|---|---|
| `source_evidence_id` | `external_record_id` — the one stable identity every idempotency/reformulation guarantee downstream already keys off of |
| `source_name`, `source_url`, `retrieved_at` | packed into `source_reference` (JSON) |
| `source_type` | cross-checked against the manifest's registered `catalog_sources.source_type` (see "Provenance" below) — never silently ignored |
| `jurisdiction` | `market_or_region` (defaults `"global"`) |
| `brand` | `brand_name` |
| `product_name` | `product_name` |
| `category` | `category` — Wave 1's own controlled 7-value vocabulary, see below |
| `product_url` | packed into `source_reference` |
| `formulation_version_evidence` (optional) | `formulation_version`, or a deterministic ingredient-content fingerprint when absent — see "Reformulation detection" |
| `ingredient_list_raw` | `ingredients`, **order preserved exactly** (list index -> `position`, 1-indexed) |
| `ingredient_list_complete` | passed through verbatim — never inferred, same rule the existing normalization contract already enforces |
| `ingredient_source` | mapped into `NormalizedImportRecord`'s own five-value `source_type` vocabulary (see mapping table in `catalog_source_adapter.py`) |
| `verification_date` | packed into `source_reference` |
| `upc`, `gtin`, `sku` (all optional) | `skus[0]` — a synthesized `WAVE1-<evidence-id>` placeholder is used only when the source supplies none of them, always clearly prefixed so it can never be mistaken for a real identifier |
| `size_value`, `size_unit` (optional) | `skus[0].size_value`/`size_unit` |
| `notes` | `description` |
| (all of the above not otherwise mapped) | bundled into `source_reference` as one compact, sorted-key JSON string — the existing free-text field, never a new column |

**Raw ingredient order is preserved exactly** — proven by `tests/domain/test_catalog_source_adapter.py::test_ingredient_order_is_preserved_exactly`, which round-trips a four-ingredient list through the real, unmodified `normalize_record()` and asserts the resulting `position`s and names match the source order precisely.

**Never infer ingredients absent from source material.** `ingredient_list_raw` is read verbatim; an empty list stays empty (see "Wave 1 product-quality states" below for what that means for publishability) — nothing in this pass fabricates or supplements it.

**Never silently normalize away uncertainty.** A manifest record whose provenance JSON would exceed the existing 2000-char `source_reference` bound fails deliberately (`PROVENANCE_TOO_LARGE`) rather than being silently truncated; an unrecognized `category`/`ingredient_source`/`source_type` value fails the whole record (Pydantic validation) rather than being coerced to a nearest guess.

### Wave 1's own category vocabulary — honest, not fully wired

`WAVE1_TARGET_CATEGORIES = {"cleanser", "moisturizer", "sunscreen", "acne_treatment", "serum", "exfoliant", "barrier_repair"}`. This is a **new, Wave-1-only controlled vocabulary** for `products.category` (a free-text column with no CHECK constraint) — it is deliberately **not** the same vocabulary as `app.domain.product_safety.CATEGORY_SAFETY_PROFILES`, which belongs to `PlanService`'s pre-existing category-level ranking and is out of scope for this pass. Three of Wave 1's seven values (`cleanser`, `moisturizer`, `sunscreen`) happen to already match a `CATEGORY_SAFETY_PROFILES` key exactly, so a real, `PUBLISHED`, `COMPLETE` formulation in one of those three categories is reachable by `ProductMatchingService` today. The other four (`acne_treatment`, `serum`, `exfoliant`, `barrier_repair`) import and publish successfully but are **not yet** reachable by any recommendation, because `PlanService`'s own category vocabulary has no matching key for them (it has `chemical_exfoliant`/`vitamin_c_serum`/`niacinamide_serum`/`barrier_cream` instead — near-neighbors, not exact matches). Extending `PlanService`'s vocabulary (or adding a mapping layer) to close this gap is explicitly **DEFERRED** — a separate, later pass; this document does not claim it is closed.

## Provenance — IMPLEMENTED, TESTED, reusing existing structures only

No new provenance table or column exists. Every concept this pass's own brief asked for already had a home:

| Concept | Existing structure reused |
|---|---|
| `last_verified_at` | `product_formulations.verified_at` (set by the existing, unmodified `CatalogPublicationService.publish()` at actual publish time) |
| `source_checked_at` / re-verification metadata | the manifest's own `retrieved_at`/`verification_date`, packed into `source_reference` (both `product_formulations.source_reference` and `catalog_formulation_provenance.source_reference` — the existing publish path already writes both) |
| source URL / provenance | `source_reference` (JSON, see the manifest table above) |
| formulation fingerprint/hash | `payload_sha256` (`catalog_import_records`) — the **exact same field** `CatalogPublicationService.publish()` already uses to detect an identical re-import; Wave 1 invented no second fingerprinting mechanism for this |
| "what import record, verified when, by whom" | `catalog_formulation_provenance` (`UNIQUE(formulation_id)`, permanent, `UPDATE` not granted at all — unchanged) |
| ingestion source classification | `catalog_sources.source_type = 'curated_dataset'` (already existed in the closed CHECK vocabulary before this pass — no migration needed) |

Proven end-to-end: `tests/domain/test_catalog_wave_service.py::test_source_provenance_persists_through_publish` publishes a Wave 1 record and asserts the resulting `catalog_formulation_provenance.source_reference` and `product_formulations.source_reference` both carry the manifest's own `source_url`/`source_evidence_id`.

### Source binding — real, load-bearing guards (hardened by independent review)

A manifest record's own `source_type` **and** `source_name` are both validated against the `catalog_sources` row it is actually being imported under (`--source-id`) — a mismatch on either (`SOURCE_TYPE_MISMATCH`/`SOURCE_NAME_MISMATCH`) is isolated as a manifest-level issue and never reaches the pipeline; the record is never silently imported under the wrong source. `source_name` is compared via `normalize_name()` — the same lowercase/trim/collapse-whitespace rule every other catalog identity lookup in this codebase already uses, so case/whitespace differences are not a false mismatch. Independent review found the original pass checked only `source_type` — see "Independent review pass" below for the full detail on why `source_name` binding was added.

## Wave 1 product-quality states — IMPLEMENTED, TESTED (`app/domain/catalog_wave_report.py`)

Every candidate product/import record is classified into exactly one of: `VERIFIED`, `REVIEW_REQUIRED`, `INSUFFICIENT_SOURCE_DATA`, `REJECTED` (plus three pipeline-internal states this module also reports for completeness: `MALFORMED`, `PUBLISHED`, `PENDING`). **This is a read-only classification computed from existing columns** (`catalog_import_records.status`, `.normalized_payload`) — not a new database column, not a parallel state machine. `classify_import_record_state()` is the one, canonical function; see its own docstring for the exact precedence.

The gap this classification exists to close: `app.domain.catalog_validation.compute_validation_findings()` (existing, unmodified, and correctly so — it is heavily reviewed shared logic) does **not** flag a record with `ingredient_list_complete=False, ingredients=[]` — that record normalizes and validates cleanly to `VALIDATED` today, with zero review findings. Wave 1's own, stricter bar (`manifest_record_has_sufficient_ingredient_evidence`/`normalized_payload_has_sufficient_ingredient_evidence` — one predicate, applied both pre-import and at report time, never two copies of the rule) reclassifies exactly this case as `INSUFFICIENT_SOURCE_DATA` for Wave 1's own reporting purposes, without touching `catalog_validation.py` at all.

### "Do not publish INSUFFICIENT_SOURCE_DATA" — how this is actually enforced

Four independent layers (hardened by independent review — see "Independent review pass" below for why layer 1 below was added; the original pass shipped only layers 2-4, which excluded an insufficient record from recommendations but did not stop it from becoming `PUBLISHED` at the row level):

1. **A durable, direct backstop inside `CatalogPublicationService.publish()` itself** (`app/domain/catalog_publication_service.py`, constant `WAVE1_INSUFFICIENT_SOURCE_DATA`). Any record durably identifiable as Wave 1 evidence (`is_wave1_source_reference()`, which checks for Wave 1's own schema-version marker in the record's own `source_reference` — never an in-memory flag) is checked BEFORE any brand/product/formulation write: if it fails `normalized_payload_has_sufficient_ingredient_evidence()`, `publish()` raises `PublicationError(code="WAVE1_INSUFFICIENT_SOURCE_DATA")` and creates nothing at all — no formulation row, no provenance row, no status change. This fires for **any** caller of `publish()`, not only Wave 1's own CLI — a direct, out-of-band call to the shared publication service is rejected identically. Legacy/non-Wave records (whose `source_reference` never carries the marker) are completely unaffected; `tests/domain/test_catalog_publication_service.py`'s own pre-existing suite (proving a non-Wave record with `ingredient_list_complete=False` CAN still publish with `ingredient_data_status=UNKNOWN`, per that architecture's own intentional, unchanged design) is re-run unmodified and stays green.
2. **Wave 1 never calls `publish()` automatically, for any record, ever.** `import_manifest()` only imports (`CatalogIngestionService.import_file()`, unchanged); publication stays a distinct, deliberate, per-record operator action via the pre-existing `publish` CLI command, exactly as before this pass.
3. **Reporting makes it visible.** `inspect-wave`/`report-verification-status` classify and surface `INSUFFICIENT_SOURCE_DATA` records explicitly, so an operator following this document's own documented workflow never even attempts to run `publish` against one.
4. **Defense in depth, unchanged from the original design:** even a hypothetical formulation that somehow reached `PUBLISHED` despite layer 1 could never reach `ingredient_data_status = 'COMPLETE'` for an empty ingredient list (`CatalogPublicationService._compute_ingredient_data_status`), so it would remain structurally invisible to `list_current_active_formulations_by_category`'s discovery query regardless.

Proven: `tests/domain/test_catalog_wave_service.py::test_wave1_insufficient_record_cannot_publish` (and its siblings `test_wave1_insufficient_record_publish_creates_no_formulation_row`, `test_wave1_insufficient_record_remains_non_published`, `test_wave1_insufficient_record_rejection_leaves_product_matching_unaffected`, `test_wave1_sufficient_verified_record_still_publishes_normally`).

**Never convert uncertain identity resolution into guessed catalog identity.** Two manifest records whose grouped formulation content agrees but whose declared `category` disagrees are never silently resolved one way or the other — both are excluded and reported as `CONFLICTING_PRODUCT_IDENTITY` (see "Duplicate-product handling" below), a real, tested manifest-level rejection, not a guess.

## Verification policy — IMPLEMENTED

The six criteria this pass's own brief lists for publishability are exactly the pre-existing `CatalogPublicationService.publish()` contract, criteria 1-5 completely unmodified by Wave 1 and criterion 6 carrying the one small, additive check described in "Publication eligibility backstop" immediately below:

1. product identity resolved — `resolve_or_create_product` (existing, exact-match)
2. brand identity resolved — `resolve_or_create_brand` (existing, exact-match)
3. ingredient list available — `ingredients` non-empty (existing normalization contract; Wave 1's own `INSUFFICIENT_SOURCE_DATA` classification is the earlier, honest signal that this criterion will fail)
4. ingredients pass normalization/review — `compute_validation_findings`/`reconcile_review_state` (existing, unmodified)
5. provenance recorded — `catalog_formulation_provenance`, `UNIQUE(formulation_id)` (existing, unmodified)
6. formulation marked `PUBLISHED` by the existing publication system — `CatalogPublicationService.publish()` (existing and, for every non-Wave-1 record, still completely unmodified; a Wave 1 record additionally passes through this pass's own eligibility backstop first — see "Publication eligibility backstop" below)

Wave 1 never publishes from retailer marketing copy alone — `ingredient_source` is a **closed, validated vocabulary** (`manufacturer_label_text`, `manufacturer_website_disclosure`, `manufacturer_pdf_or_sds`, `regulatory_filing`, `third_party_verified_database`, `retailer_listing_text`, `unverified`), and `retailer_listing_text`/`unverified` map to the existing pipeline's own weakest evidentiary tier (`user_submitted_unverified`), which `compute_validation_findings` already treats specially (`SOURCE_INSUFFICIENT` fires when an unverified source claims completeness — existing, unmodified logic, simply fed real Wave 1 data).

### Publication eligibility backstop — the one Wave 1 addition to existing publication machinery

Everything else in `app/domain/catalog_publication_service.py` — the transaction structure, brand/product resolution, ingredient resolution, the structural safety backstop, SKU-conflict checking, reformulation/supersession, provenance/audit writes, and the idempotent-republish short-circuit — is **completely unchanged** by this pass, for every record, Wave 1 or not. The **one** addition, added by independent review (not part of the original Wave 1 pass), is a single early check inside `_publish_within_transaction()`:

```python
if is_wave1_source_reference(normalized.get("source_reference")):
    if not normalized_payload_has_sufficient_ingredient_evidence(normalized):
        raise PublicationError(..., code=WAVE1_INSUFFICIENT_SOURCE_DATA)
```

- **What it checks**: whether `normalized_payload["source_reference"]` — a plain JSON string already stored on the row, never an in-memory flag or a second table — carries Wave 1's own schema-version marker (`is_wave1_source_reference()`, `app/domain/catalog_source_adapter.py`). Only a record this function can positively identify as Wave 1 evidence is ever affected; anything else (every record from before this pass, and any future non-Wave-1 source) takes exactly the code path it always did.
- **Where it runs**: immediately after `normalized = record["normalized_payload"]` is loaded, before any brand/product/formulation write of any kind — a rejected attempt leaves no side effect.
- **What it does not touch**: the structural safety backstop, reformulation logic, SKU handling, provenance/audit writing, and every other line of `publish()` are byte-for-byte identical to before independent review added this check.
- **Proof it doesn't affect legacy behavior**: `tests/domain/test_catalog_publication_service.py` — the original suite, not edited by this pass or its repairs — is re-run unmodified and stays green, including its own test that a non-Wave record with `ingredient_list_complete=False` publishes successfully with `ingredient_data_status=UNKNOWN` (that architecture's own intentional, still-unchanged design for data this pass has no opinion about).

This is the only functional change Wave 1 (across its original pass and all three independent-review repairs) ever made to a file that existed before it.

## Product lifecycle

```
Wave 1 manifest record
        |  validate-source-manifest (pure, no DB)
        v
schema-valid, grouped by formulation content (SKU/size variants merged)
        |  import-source-manifest  -->  CatalogIngestionService.import_file() [UNCHANGED]
        v
catalog_import_records (NORMALIZED or MALFORMED)
        |  validate-batch [UNCHANGED, existing command]
        v
   ------------------------------------------------
   |                |                              |
VALIDATED,      NEEDS_REVIEW                  VALIDATED,
sufficient      (REVIEW_REQUIRED)              insufficient
(VERIFIED)          |                        (INSUFFICIENT_SOURCE_DATA)
   |            CatalogReviewService                |
   |            [UNCHANGED, existing]           never published by
   |                |                           Wave 1 tooling -- needs
   v                v                           new/better source evidence
publish [existing CLI command, operator-invoked; CatalogPublicationService.publish()
         itself carries one small, additive Wave 1 eligibility check -- see below]
        v
PUBLISHED, provenance recorded, catalog_audit_log entries written
```

## Reformulation handling — IMPLEMENTED, TESTED, zero new logic

`CatalogPublicationService.publish()` already compares the incoming `formulation_version` against the currently-published formulation's own `version` for the same `(product_id, market_or_region)`, and already supersedes-then-publishes when they differ (superseding strictly before the new row is marked current — the existing `idx_product_formulations_one_current_per_market` partial unique index makes any other ordering fail outright). Wave 1's entire contribution to reformulation detection is **feeding that existing comparison real, deterministic version data**:

- When a manifest supplies explicit `formulation_version_evidence`, it is used verbatim.
- When it does not (the common case for a real source that doesn't publish an explicit version string), `compute_ingredient_fingerprint()` derives one: `"fp-" + sha256(normalized, newline-joined ingredient list)[:16]`. Same ingredient list (modulo whitespace/case, via the same `normalize_name()` every other identity lookup in this codebase already uses) -> same fingerprint, always. A genuine ingredient change -> a different fingerprint, always. Reordering the same ingredients is treated as a different disclosure (order is part of the fingerprint) — a deliberate, documented choice, not an oversight.

Because this fingerprint *is* `formulation_version`, a changed fingerprint for the same product/market is, to the existing unmodified `publish()`, indistinguishable from any other legitimate version bump — it takes the exact same supersede-and-publish path, with the exact same guarantees:

- **The old formulation is never silently overwritten.** `supersede_formulation()` only ever sets `is_current = false`/`publication_status = 'SUPERSEDED'` — the row, and its `formulation_ingredients`, persist forever.
- **Old provenance is preserved.** The old formulation's own `catalog_formulation_provenance` row is untouched; the new formulation gets its own.
- **The new formulation is created/reviewed according to current architecture** — it goes through the same `NORMALIZED -> VALIDATED/NEEDS_REVIEW -> publish` path as any other Wave 1 record, no shortcut.

Proven: `tests/domain/test_catalog_wave_report.py::test_formulation_changes_detected_counts_reformulations` and `::test_old_formulation_is_preserved_after_reformulation` (asserts the old formulation row, and its exact original `formulation_ingredients` rows, are byte-identical after the new one publishes), plus `tests/domain/test_catalog_wave_service.py::test_same_product_from_two_sources_second_publish_sees_existing_current_formulation` (two independent sources' evidence for the same product correctly routes through reformulation, not a spurious conflict).

## Duplicate-product handling — IMPLEMENTED, TESTED

Wave 1 groups schema-valid manifest records by `(normalized brand, normalized product name, market_or_region, formulation-content fingerprint)` **before** they ever reach the existing pipeline (`group_manifest_records()`, `app/domain/catalog_source_adapter.py`):

- **Duplicate product from the same source** (the common real-world case: a manufacturer's own size/variant SKUs for one formulation, submitted as separate manifest rows) — records sharing the group key are merged into **one** raw import record, with every member's own SKU/UPC carried into that one record's `skus` list (deduplicated by SKU value). This is what lets a real product with three sizes import as one formulation with three SKUs, rather than three formulations spuriously conflicting on `FORMULATION_VERSION_CONFLICT` (same fingerprint, "different" raw payload only because the SKU differs). Proven: `tests/domain/test_catalog_wave_service.py::test_duplicate_product_from_same_source_merges_sku_variants`.
- **Same product from two sources** — grouping only ever happens *within* one `import-source-manifest` invocation (one source, one file); two different sources' import records for the same product are never merged, and instead reach the existing publish()-level dedup/reformulation/conflict logic unmodified when both are published — see "Reformulation handling" above for the proof.
- **Conflicting product identity** — a group whose members disagree on `category` (same brand/product/formulation content, inconsistent category) is never silently resolved one way; every member is excluded and reported as `CONFLICTING_PRODUCT_IDENTITY`. Proven: `tests/domain/test_catalog_wave_service.py::test_conflicting_product_identity_within_one_manifest_is_flagged_not_guessed`.
- **Duplicate source record** (the exact same `source_evidence_id` submitted twice, byte-identical manifest content) — the existing pipeline's own whole-file idempotency (`UNIQUE(source_id, content_sha256)`) resolves it to the same batch, no duplicate. Proven: `tests/domain/test_catalog_wave_service.py::test_duplicate_source_record_same_evidence_id_is_idempotent`.

### Grouping is order-independent and preserves all merged provenance (independent review)

Independent review found the original merge logic took every trust-critical field (`ingredient_source`, `ingredient_list_complete`, `notes`, provenance identity) from `group[0]` alone — manifest ORDER could silently determine whether a merged group produced manufacturer-grade/complete output or unverified/incomplete output. Closed with two changes, neither optional:

- **`_describe_trust_conflicts()`** requires every member of a group to agree EXACTLY on every trust-critical formulation fact (`ingredient_list_raw` content — not just its fingerprint, `ingredient_list_complete`, `ingredient_source`, `formulation_version_evidence`) before they may merge at all. Disagreement on any of these is rejected as `CONFLICTING_FORMULATION_EVIDENCE` (a new, distinct reason from `CONFLICTING_PRODUCT_IDENTITY`'s category-only conflict) — never resolved by picking a "winning" record. Once a group passes this check, every member is provably identical on every field that matters, so which member ends up representative is irrelevant.
- **`manifest_record_to_raw_import_dict()` sorts a group by `source_evidence_id`** before building anything, making every field of its output — not merely the trust-critical ones — byte-identical regardless of the order members were supplied in.
- **Full provenance preservation**: `source_reference` now carries an `evidence` array with one entry (`source_evidence_id`, `source_name`, `source_url`, `product_url`, `retrieved_at`, `verification_date`, `ingredient_source`) per contributing manifest row — not merely a list of superseded ids alongside the representative member's own values. A group whose combined evidence would exceed the existing `source_reference` bound still fails closed (`PROVENANCE_TOO_LARGE`), exactly as a single record already did — evidence is never silently truncated to fit.

Proven: `tests/domain/test_catalog_source_adapter.py::test_reversing_grouped_record_order_produces_identical_output`, `::test_complete_vs_incomplete_evidence_cannot_be_first_record_wins`, `::test_manufacturer_vs_unverified_ingredient_source_cannot_be_first_record_wins`, `::test_every_merged_evidence_record_is_reconstructible_from_persisted_provenance`, plus the end-to-end `tests/domain/test_catalog_wave_service.py::test_duplicate_product_merge_is_order_independent_end_to_end`.

## Publish criteria / Safety

Unchanged, unmodified, and re-proven under real Wave 1 data:

```
plan category -> catalog candidate -> formulation safety -> routine safety -> eligible product recommendation
```

`SafetyEngine.evaluate_product_formulation()` has, and needs, no notion of Wave 1, `source_evidence_id`, or manifest provenance at all — it evaluates whatever `formulation_id` it is given, exactly as before. `ProductMatchingService`'s discovery query (`publication_status = 'PUBLISHED' AND ingredient_data_status = 'COMPLETE' AND is_current = true`) is untouched. No affiliate ranking, no sponsored ranking, and no commercial/popularity signal of any kind was added by this pass — `SourceManifestRecord` has no price, ranking, or commerce field whatsoever.

## Review workflow

Unchanged. `CatalogReviewService` (`map_ingredient`/`create_ingredient`/`reject_import_record`/`dismiss`) is the sole, authoritative way to resolve a `NEEDS_REVIEW` Wave 1 record — Wave 1 adds no second review mechanism and no bypass.

## Operational commands — IMPLEMENTED, TESTED (`app/catalog_admin/cli.py`)

Five new subcommands, same thin-argparse-adapter discipline as every existing command (no business logic in `cli.py` — everything delegates to `app.domain.catalog_wave_service`/`catalog_wave_report`), still gated behind `CATALOG_ADMIN_ENABLED` and the dedicated `skincare_catalog_admin` role, exactly like every command before them:

- `validate-source-manifest <path> [--format json|jsonl]` — pure, schema-level only, **zero database access**. Reports total/valid/invalid record counts, sufficient-vs-insufficient-source-data counts, and per-category/per-source-name breakdowns.
- `import-source-manifest <path> --source-id <id> [--format ...] [--dry-run] [--allow-test-source]` — the only DB-writing Wave 1 command; delegates every mutation to the existing, unmodified `CatalogIngestionService.import_file()`. `--dry-run` performs the exact same parsing/grouping/mapping in memory and writes nothing (same "strongest reading of no production mutation" the existing `import-file --dry-run` already provides). `--allow-test-source` is required to import into a source whose registered name looks like a test fixture — see "Seed/demo separation" below.
- `inspect-wave [--source-id <id>]` — the full deterministic Wave 1 report (see "Reporting").
- `report-review-required --source-id <id>` — open review items scoped to one source.
- `report-verification-status --source-id <id>` — per-record `VERIFIED`/`REVIEW_REQUIRED`/`INSUFFICIENT_SOURCE_DATA`/`REJECTED`/... state.

No public HTTP mutation endpoint was added, or considered — catalog mutation remains CLI/admin-only, unchanged.

## Seed/demo separation — IMPLEMENTED, TESTED

`is_reserved_test_source_name()` (`app/domain/catalog_source_adapter.py`) refuses `import-source-manifest` against any `catalog_sources` row whose registered `name` starts with `test_`, `synthetic_`, or `wave1_test_`, **unless** the caller explicitly passes `--allow-test-source`/`allow_test_source=True` — the production import path never passes it; only automated test code does. This is a real, technical guard (`RESERVED_TEST_SOURCE_NAME_PREFIXES`), not merely a naming convention an operator could accidentally violate: attempting it without the flag raises `WaveManifestRejectedError(code="RESERVED_TEST_SOURCE")` before any database write. Proven: `tests/domain/test_catalog_wave_service.py::test_reserved_test_source_name_is_refused_by_default`.

Every Wave 1 automated test uses this repository's own established synthetic-data convention (obviously-fake brand names, `example.invalid` URLs) and truncates its own state via the pre-existing `clean_catalog_ingestion` fixture — no Wave 1 test fixture is ever committed as, or could be mistaken for, real production data.

## Reporting — IMPLEMENTED, TESTED (`app/domain/catalog_wave_report.py`)

`inspect-wave` produces a fully deterministic report, reproducible from database state alone (re-running it against an unchanged database always returns byte-identical results — proven directly, `tests/domain/test_catalog_wave_report.py::test_report_is_reproducible_across_repeated_calls`):

| Field | Source |
|---|---|
| total records supplied | reconstructed from `catalog_audit_log` entries `import_manifest()` writes for every non-dry-run import **of a genuinely new batch** (see below — includes records that never became a DB row) |
| imported | `count(catalog_import_records)` for the source |
| malformed | `status = 'MALFORMED'` |
| unresolved brands / product identities | open `catalog_review_items` with reason `BRAND_IDENTITY_AMBIGUOUS`/`PRODUCT_IDENTITY_AMBIGUOUS` — a real query, honestly `0` today (exact-match identity resolution cannot itself produce ambiguity in the current architecture) |
| unresolved ingredients | open `catalog_review_items` with reason `UNKNOWN_INGREDIENT` |
| review-required | `status = 'NEEDS_REVIEW'` |
| insufficient source data | `status = 'VALIDATED'` and failing Wave 1's sufficiency predicate |
| verified | `status = 'VALIDATED'` and sufficient |
| published | `status = 'PUBLISHED'` |
| rejected | `status = 'REJECTED'` |
| formulation changes detected | `count(catalog_audit_log WHERE action = 'SUPERSEDE')` scoped to the source |
| source counts | one row per registered `catalog_sources`, with its own import-record count |
| provenance completeness | distinct published-formulation count vs. distinct formulation count actually holding a `catalog_formulation_provenance` row, both independently re-derived |

**"Total records supplied" and database reproducibility.** A manifest record that fails Wave 1's own schema/grouping checks never becomes a `catalog_import_records` row at all (by design — see "Source architecture"), so it cannot be recovered from that table alone. `import_manifest()` closes this gap by writing one additional `catalog_audit_log` entry (`action='IMPORT'`, reusing the existing closed action vocabulary — no migration), carrying `{"wave1_manifest_total_records": N, ...}` in its `after_metadata`, for every non-dry-run import that produces a **genuinely new** batch. `inspect-wave` sums these, one entry per distinct batch, to reconstruct the manifest's own original total. **Known, narrow gap, stated honestly**: a manifest attempt where *every* record fails Wave 1's own checks never creates a batch at all, so it has nowhere to attach this audit entry — that attempt's total is visible only in that single CLI invocation's own immediate output, not later via `inspect-wave`. This is a real, acknowledged limitation, not silently glossed over.

**Idempotent-reimport correctness and post-commit failure recovery (independent review, two passes).** A byte-identical reimport of the same manifest reuses the existing `catalog_import_batches` row (`content_sha256` uniqueness) rather than creating a new one. The first independent-review pass found the original implementation wrote a NEW Wave 1 audit entry on every call regardless of reuse, so repeated idempotent reimports accumulated multiple audit entries for the same batch and `inspect-wave` summed all of them uncritically — a two-record manifest imported three times could report `total_records_supplied = 6` against a database that still held exactly two import records. The first fix gated the write on `ImportOutcome.batch_is_new` — but a SECOND independent-review pass then found this introduced a durability gap of its own: the summary write happens in a separate transaction strictly after `import_file()`'s own transaction has already committed, so a transient failure at that step, followed by a retry, would find `batch_is_new=False` and skip the write forever, permanently stranding the batch without a summary.

The final design, `_ensure_wave_manifest_summary()` (`app/domain/catalog_wave_service.py`), closes both gaps at once: called unconditionally for every successful non-dry-run import with a `batch_id` — never gated on `batch_is_new` — it first checks whether a Wave 1 summary already exists for that batch and returns immediately if so (this is what keeps a sequential idempotent reimport, or a retry after a prior successful write, from creating a duplicate), and appends one only when genuinely missing (this is what makes a post-commit failure recoverable by a plain retry, not a permanent gap). `catalog_wave_report._total_records_supplied_for_source()` additionally deduplicates by `batch_id` when reading, as defense in depth against the narrow theoretical race two truly concurrent repair attempts could produce. Neither the write path nor the read path ever deletes or mutates audit history. Proven: `tests/domain/test_catalog_wave_report.py::test_idempotent_reimport_does_not_inflate_total_records_supplied`, `::test_multiple_genuinely_distinct_batches_still_sum_correctly`, `::test_post_commit_wave_summary_failure_is_recoverable_by_retry` (simulates the failure by making `repo.write_audit()` raise only for the Wave 1 summary's own metadata shape, proving the catalog import itself still commits, the failure is surfaced to the caller, a retry repairs the missing summary without creating new import records, and a third sequential attempt neither duplicates the summary nor changes either report total).

## Tests — TESTED

`tests/domain/test_catalog_source_adapter.py` (47 tests, pure, no DB), `tests/domain/test_catalog_wave_service.py` (22 tests), `tests/domain/test_catalog_wave_report.py` (18 tests), `tests/catalog_admin/test_wave1_cli.py` (9 tests) — 96 Wave 1 tests total, all against real Postgres where DB access is involved (no mocked database anywhere in this pass, same convention as every prior catalog pass). Covers: valid manifest ingestion, malformed manifest record isolation, missing ingredient list, duplicate source record, duplicate product from the same source (including full order-independence), the same product from two sources, conflicting product identity, conflicting trust-critical formulation evidence, source-name/source-type binding, unresolved ingredient, source provenance persistence (including full multi-record evidence reconstruction), ingredient ordering preservation, verification state classification, publication only after validation, the direct publication-time `WAVE1_INSUFFICIENT_SOURCE_DATA` backstop (insufficient records cannot publish, sufficient ones still do), idempotent-reimport report-total correctness, post-commit Wave-summary failure recovery by retry, formulation fingerprint stability/determinism, reformulation detection, old-formulation preservation, inactive-source rejection, restricted admin-role enforcement, the ordinary application role's inability to mutate catalog evidence through this pass's own new code path, and production data's inability to masquerade as a test fixture.

Every pre-existing catalog test — the ORIGINAL, completely unmodified `tests/domain/test_catalog_publication_service.py` (proving the Wave 1 publication backstop leaves legacy/non-Wave publish() behavior byte-for-byte unchanged), concurrency (`test_two_concurrent_publications_same_product_market_preserve_unique_current`, the 10-concurrent-publish idempotency proof), review-security (the review-identity-binding hotfix's own tests), and the full privilege/immutability suites — was re-run against this pass's own final head and remains green; see this pass's own PR description for the exact count.

## Independent review pass — four merge blockers, all closed

### 1. `INSUFFICIENT_SOURCE_DATA` could still reach `PUBLISHED` — IMPLEMENTED, TESTED

The original pass's own three enforcement layers (Wave 1 never auto-publishes, reporting surfaces it, `ingredient_data_status` can never reach `COMPLETE`) never actually stopped `CatalogPublicationService.publish()` itself from creating a `PUBLISHED` formulation with `ingredient_data_status = UNKNOWN` for an insufficient Wave 1 record — the pass's own brief's literal instruction, "Do not publish INSUFFICIENT_SOURCE_DATA," was violated at the row level even though the result was correctly invisible to `ProductMatchingService`. Fixed with a new, durable, direct backstop inside `publish()` itself (`is_wave1_source_reference()` + `normalized_payload_has_sufficient_ingredient_evidence()`, `WAVE1_INSUFFICIENT_SOURCE_DATA`) that rejects the attempt outright — no formulation, no provenance, no status change — for ANY caller, not just Wave 1's own tooling, while leaving every non-Wave record's publish() behavior provably unchanged (the original `test_catalog_publication_service.py` suite re-run unmodified). See "Do not publish INSUFFICIENT_SOURCE_DATA" above for the full four-layer detail.

### 2. Grouped SKU/size variants were order-dependent and lost provenance — IMPLEMENTED, TESTED

The original merge logic took every trust-critical field (ingredient evidence quality, completeness, source declaration) from `group[0]` alone, so manifest ordering could silently determine whether merged output looked manufacturer-grade/complete or unverified/incomplete, and only `source_type` (not `source_name`) was cross-checked against the registered source. Fixed: `_describe_trust_conflicts()` requires exact agreement across a group before merging (rejecting disagreement as `CONFLICTING_FORMULATION_EVIDENCE` rather than picking a winner), grouping/mapping sorts by `source_evidence_id` for full order-independence, `source_name` is now bound alongside `source_type` (`SOURCE_NAME_MISMATCH`), and `source_reference` now carries a per-contributing-record `evidence` array instead of collapsing to the representative member's own fields. See "Grouping is order-independent and preserves all merged provenance" above.

### 3. Idempotent reimport could double-count `total_records_supplied` — IMPLEMENTED, TESTED

A byte-identical reimport reused the existing batch but still wrote a second Wave 1 audit-summary entry for it, which `inspect-wave`'s reader summed without deduplication — a two-record manifest reimported N times could report `total_records_supplied = 2N` against a database that still held exactly two import records. Fixed with defense in depth: the audit entry is now written only for a genuinely new batch, and the reader additionally deduplicates by `batch_id`. See "Idempotent-reimport correctness" above.

### 4. A post-commit Wave-summary failure could permanently strand a batch without one — IMPLEMENTED, TESTED

Blocker 3's own fix (write the summary only when `batch_is_new`) introduced a new gap: the summary write happens in a SEPARATE transaction, strictly after `CatalogIngestionService.import_file()`'s own transaction has already committed the batch and its import records. If that separate write failed transiently (`repo.write_audit()` propagates DB failures, same as every other call in this codebase) and the caller retried the byte-identical manifest, the retry's `batch_is_new` would now read `False` (the batch already exists) — so the gated write would be skipped forever, permanently stranding that batch without its summary and silently undercounting `total_records_supplied` from then on. Fixed: the write is now `_ensure_wave_manifest_summary()`, called unconditionally for every successful non-dry-run import with a `batch_id` (not gated on `batch_is_new`), which itself checks for an existing summary first and appends one only if genuinely missing — repairable by a plain retry, and idempotent (a sequential retry after a successful summary write is a no-op). A narrow theoretical race between two truly concurrent repair attempts for the same batch could in principle still produce two entries; that is accepted deliberately (per this fix's own brief) because the existing `batch_id`-based reader deduplication (Blocker 3) already makes a duplicate entry harmless at read time. Audit history is never deleted or updated by either the write path or its repair. See "Idempotent-reimport correctness" above and `tests/domain/test_catalog_wave_report.py::test_post_commit_wave_summary_failure_is_recoverable_by_retry`.

## Remaining external/manual work

- **Superseded — real product data now exists.** Production Catalog Wave 1B (`PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md`) acquired and published 11 real products from `cerave.com`/`theordinary.com` official pages. `laroche-posay.us` (active Cloudflare bot challenge) and `paulaschoice.com` (client-side-rendered ingredient disclosure) remain unacquired — a genuine, documented limitation, not a scope choice — so a real manufacturer-hosted structured-data/API adapter, or the bot-challenge/JS-rendering gap closing on its own, remains a real follow-up for those two domains specifically.
- **`PlanService`'s category vocabulary does not yet include `acne_treatment`/`serum`/`exfoliant`/`barrier_repair`** — a real, `PUBLISHED`, `COMPLETE` Wave 1 formulation in one of those four categories will not surface in any recommendation until that vocabulary (or a mapping layer) is extended. `cleanser`/`moisturizer`/`sunscreen` are already fully wired.
- **A manufacturer-hosted structured-data/API adapter** remains unbuilt (optional, explicitly deferred this pass).
- **No CSV import adapter** — the existing seam (`_parse_raw_records`) still supports one; still unbuilt (`OPEN_ENGINEERING_ITEMS.md` item 18e, unchanged by this pass).
- **The "every record failed Wave 1 checks" total-supplied gap** described under "Reporting" above.
