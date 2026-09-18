# Production Catalog Wave 1B — Controlled Real Product Population

**Commit this document describes:** the `feat/production-catalog-wave-1b-real-data` branch, base `master` at `62a5ae2459f1a53134f0b60dd88b9ec74b4f509b` (the merged Wave 1A pass, PR #12, with all three independent-review repairs).

## Independent-review repair (this revision)

An independent review of this branch's original head found four merge blockers, all now closed:

1. **Redirect domain check ran after the request, not before it.** `acquire_url()` followed redirects automatically (`httpx`'s own `follow_redirects=True`), so an off-domain destination could already have been contacted before the result was ever inspected. Rewritten to follow redirects manually, one hop at a time, validating every hop's domain (against the *original* request's own registrable domain, not merely "any approved domain") before that hop is ever sent, bounded to 5 hops. Proven with tests that assert the prohibited host's handler branch is never invoked.
2. **A Drug Facts page with only one of Active/Inactive Ingredients present could still be reported `ingredient_list_complete=True`.** `parse_cerave_page()` now requires BOTH sections before completeness may be true; either alone is `PARSE_FAILED`.
3. **Concentration was baked into canonical ingredient identity** (`"SALICYLIC ACID 2%"` as `raw_name`), which would have fragmented one real ingredient into a separate canonical row per formulation's own concentration. `app.domain.catalog_source_adapter.IngredientDetail` — an optional, backward-compatible companion to `ingredient_list_raw`, keyed by position — now separates the clean chemical identity from `declared_concentration`/`concentration_unit`, using `NormalizedIngredient`'s own pre-existing fields. The verbatim disclosure and the FDA active/inactive section boundary survive durably as `formulation_ingredients.notes` (an existing column).
4. **The demonstrated "11 published" was not reproducible from the repository** — it depended on a one-off `INSERT INTO ingredients` bootstrap of every raw manifest string, bypassing the real review queue. `app/domain/catalog_wave1b_population.py` (`populate-wave1b-real-data` CLI command) is now the single, deterministic, idempotent, dry-run-capable operator workflow that reproduces the published state from a genuinely clean database, resolving ingredients only against a committed, reviewable `ingredient_dictionary.json` via the existing, unmodified `CatalogReviewService` — never a raw-SQL shortcut.

Provenance hardening (raised alongside the four blockers): every published Wave 1B formulation's durable `source_reference` now also carries the acquisition's `final_url` (post-redirect) and `content_sha256`, not just `source_url`.

## Second independent-review repair (this revision)

A follow-up review found two further merge blockers on the repair above, both now closed:

1. **Dictionary validation ran after database mutation, and a dry run never loaded the dictionary at all.** `populate_wave1b()` called the mutating `import_manifest()` before ever loading/validating `ingredient_dictionary.json`, so a broken dictionary would only be discovered after a batch may already have committed — and `dry_run=True` returned before the dictionary was ever touched, so a broken dictionary could silently "pass" a dry run. Fixed: `load_ingredient_dictionary()` (now hardened against every malformed-input shape — non-object top level, missing/non-list `entries`, empty `entries`, non-object entry, missing/blank `canonical_name`, invalid `aliases`/`ingredient_type`/`inci_name` shape, duplicate/conflicting normalized identities — each a structured `PopulationRejectedError`, never a raw `AttributeError`/`KeyError`) and a new read-only preflight coverage check (`_run_preflight()`, checking every manifest ingredient identity against the dictionary AND the current catalog via `resolve_ingredient()`) both run **before** `import_manifest()` is ever called. `dry_run=True` now returns immediately after preflight — the database is provably untouched (no batch, import record, ingredient, alias, product, formulation, review item, or audit row), and the report (`dictionary_entry_count`, `identities_total`, `identities_already_in_catalog`, `identities_covered_by_dictionary`, `unresolved_dictionary_gaps`, `preflight_ready`) reflects the exact same check a real run would perform.
2. **CI was red on the exact reviewed head.** `expo-doctor`'s "packages match versions required by installed Expo SDK" check failed — `expo`/`expo-constants`/`expo-image-manipulator`/`expo-router` had newer compatible patch releases upstream than pinned. Fixed with Expo's own tooling (`npx expo install --fix`): `expo` 57.0.23→57.0.24, `expo-constants` (lockfile) 57.0.18→57.0.19, `expo-image-manipulator` 57.0.18→57.0.19, `expo-router` 57.0.21→57.0.22 — patch-level only, no SDK/React-Native minor or major change, no `expo.install.exclude` suppression. `expo-doctor` returns 21/21 again.

## Third independent-review repair (this revision)

One final blocker: `populate_wave1b()` computed `preflight.ready` correctly but never actually ENFORCED it for a real (non-dry-run) invocation — it called the mutating `import_manifest()` unconditionally once dry-run was ruled out, so a real population attempt against a pack with unresolved dictionary gaps or schema-invalid records could still create a batch, import records, and review items before ever reaching (and failing at) publication. Fixed: immediately after the dry-run check, `if not preflight.ready: raise PopulationRejectedError(code="PREFLIGHT_NOT_READY", ...)` — summarizing `manifest_total_records`/`manifest_schema_valid`/the manifest issue count/the unresolved-gap count, never a giant payload dump. Wave 1B's own dedicated production-population command is now a genuine all-pack preflight operation: either the WHOLE pack is ready and gets imported/resolved/published, or NOTHING is mutated at all. This is a policy this command alone enforces — the underlying, shared `CatalogIngestionService`/`import_manifest()` are unmodified and still support a partially-valid import for every other caller. A dry run is never subject to this check (`dry_run=True` still returns the full coverage report, `preflight_ready=False` included, without raising). The prior test asserting a real run with an unresolved ingredient lands in `NEEDS_REVIEW` was replaced with one asserting the real run now raises `PREFLIGHT_NOT_READY` and mutates nothing; the underlying review-gate behavior itself (an unresolved ingredient routes to human review, never auto-published) remains proven, unweakened, via the generic `CatalogIngestionService` path in `tests/domain/test_catalog_acquisition.py::test_real_manifest_import_remains_review_gated`.

Classification used throughout, matching `PRODUCTION_CATALOG_WAVE_1.md`: **DESIGNED** (documented, no code), **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (explicitly out of scope, tracked in `OPEN_ENGINEERING_ITEMS.md`).

## Purpose

Wave 1A shipped acquisition/verification/reformulation infrastructure with **zero** real product records — every test fixture was synthetic, deliberately. Wave 1B is the first pass to actually populate the catalog with real products, using the same, completely unmodified Wave 1A pipeline (`CatalogIngestionService`/`CatalogPublicationService`/`CatalogReviewService`/`CatalogSourceAdapter`) plus two new, narrow layers: a controlled acquisition boundary (`app/domain/catalog_acquisition.py`) that fetches an explicit, enumerated list of manufacturer product pages and extracts their disclosed ingredient lists through small, conservative, per-brand parsers; and a reproducible population orchestrator (`app/domain/catalog_wave1b_population.py`) that drives the existing import/review/publish pipeline deterministically and idempotently against a committed ingredient dictionary.

This is a data-population pass, not an architecture pass. No Wave 1A file was modified (Wave 1A's own `NormalizedIngredient`/`SourceManifestRecord` gained new, optional, backward-compatible fields — never a behavior change for any record that doesn't use them). The two new backend modules are `app/domain/catalog_acquisition.py` and `app/domain/catalog_wave1b_population.py`; everything else new is data (`catalog_data/production/wave1b/`) and tests.

## Approved source policy — enforced, not just documented

Exactly four manufacturer domains were authorized this pass, and `app.domain.catalog_acquisition.APPROVED_DOMAINS` is the literal, code-level allowlist `acquire_url()` checks before making any request — never a convention a caller could accidentally bypass:

- `cerave.com`
- `laroche-posay.us`
- `theordinary.com`
- `paulaschoice.com`

No retailer, no aggregator, no AI-generated data, and no search-result snippet was ever used as evidence. `acquire_url()` follows redirects **manually, one hop at a time** (`follow_redirects=False` on every request it issues), validating each hop's destination — against both the domain allowlist and the *original* request's own registrable domain — **before** that hop is ever sent, bounded to 5 hops. A same-domain redirect (the real Paula's Choice case below, where a product page redirected to a renamed slug on `paulaschoice.com` itself) succeeds; a redirect leaving the allowlist, or hopping to a *different* independently-approved manufacturer domain, raises `AcquisitionRejectedError` and that destination is never contacted at all — not merely "the result is discarded afterward."

## Acquisition method

`app/domain/catalog_acquisition.py::acquire_url()` fetches exactly one operator-supplied URL — no discovery, no sitemap-driven enumeration at request time, no recursive link traversal, no authentication, no anti-bot circumvention. The declared, exact URL list this pass was authorized to fetch lives in `catalog_data/production/wave1b/sources.json` (15 entries — see "Source URLs acquired" below); `run_acquisition.py` reads that file, fetches each URL once (with a 5-second delay between requests, honoring `laroche-posay.us`'s own published `Crawl-delay: 5` unconditionally, even for the domain that never actually returned real content), and writes three outputs:

- `acquisition_ledger.jsonl` — one record per URL, success or not (15 rows)
- `evidence/<source_evidence_id>.txt` — the bounded, extracted ingredient text only, for every successfully-extracted product (never the raw HTML page — the largest evidence file is 741 bytes)
- `manifest.jsonl` — one Wave 1 `SourceManifestRecord`-shaped row per product whose extraction was genuinely complete (11 rows)

Real robots.txt policies for all four domains were read before any product page was fetched (none of the four disallow plain product-detail pages; `laroche-posay.us` additionally publishes `Crawl-delay: 5`, honored). No robots.txt directive was ever violated.

## What actually happened, honestly — two domains yielded real data, two did not

### `cerave.com` — 9/9 pages acquired, full ingredient lists extracted

Every CeraVe product page embeds its complete, manufacturer-disclosed ingredient list **server-rendered**, inside a `<div class="richtext keyIngredients-details__content">` block following a "Full Ingredient List" toggle — no JavaScript execution required. Two page shapes exist and both are handled explicitly (`parse_cerave_page`): plain cosmetic products (one comma-separated `<p>`) and OTC drug products with separate "Active Ingredients:"/"Inactive Ingredients:" paragraphs (two CeraVe products acquired this pass — Acne Control Gel and Acne Control Cleanser — are OTC salicylic-acid products with this exact structure).

### `theordinary.com` — 2/2 pages acquired, full ingredient lists extracted

Every The Ordinary product page embeds its complete ingredient list in a `data-original-ingredients="..."` HTML attribute — plain text, server-rendered, no markup to strip.

### `laroche-posay.us` — 0/2 pages acquired: active Cloudflare bot challenge

Every request to `laroche-posay.us` during this pass — including the plain homepage, with a standard browser `User-Agent` and no unusual headers — returned `HTTP 403` with `cf-mitigated: challenge` and a `"Just a moment..."` interstitial body. This is Cloudflare's bot-management challenge, not a wrong URL or a transient error. Per this pass's own explicit "no anti-bot bypass" instruction, **no further attempt was made** — no header rotation, no challenge-solving, no headless browser. `laroche-posay.us` is excluded from this pass's real data entirely. `Cicaplast Balm B5+` and `Toleriane Double Repair Face Moisturizer` remain unacquired candidates for a future pass, contingent on either the block lifting or a separately-reviewed, explicitly-authorized acquisition method.

### `paulaschoice.com` — 2/2 pages acquired (HTTP 200, real content), but no ingredient disclosure found

Both Paula's Choice product pages fetched successfully — real HTML, a real `application/ld+json` `Product` block (name, SKU, reviews) — but the ingredient list itself is not present anywhere in the server-rendered HTML on either page checked. It is loaded by client-side JavaScript this module does not execute (no headless browser, no JS engine, by design — see `catalog_acquisition.py`'s own module docstring on why). `parse_paulaschoice_page()` genuinely looks for the disclosure and returns `INGREDIENTS_NOT_FOUND` — a real, verified, current limitation, not a guess. Neither Paula's Choice product was written to `manifest.jsonl`; both remain `INSUFFICIENT_SOURCE_DATA` in the ledger, honestly.

**Consequence**: Wave 1B's real product count (11) is below the 12-20 target range. Per this pass's own explicit instruction — "Do not force category counts if authoritative ingredient evidence is not available. Quality beats quota." — this was not compensated for with weaker evidence, a different source tier, or an AI-generated fallback. Two of the four approved domains yielded zero usable products this pass; that is reported honestly, not hidden.

## Source URLs acquired — full record

15 URLs enumerated in `catalog_data/production/wave1b/sources.json`; see `acquisition_ledger.jsonl` for the complete, real record of every one (HTTP status, content SHA-256, extraction status). Summary:

| # | source_evidence_id | Brand | Product | Category | Outcome |
|---|---|---|---|---|---|
| 1 | `cerave-foaming-facial-cleanser` | CeraVe | Foaming Facial Cleanser | cleanser | ✅ acquired, 24 ingredients |
| 2 | `cerave-hydrating-facial-cleanser` | CeraVe | Hydrating Facial Cleanser | cleanser | ✅ acquired, 24 ingredients |
| 3 | `cerave-pm-facial-moisturizing-lotion` | CeraVe | PM Facial Moisturizing Lotion | moisturizer | ✅ acquired, 24 ingredients |
| 4 | `cerave-daily-moisturizing-lotion` | CeraVe | Daily Moisturizing Lotion | moisturizer | ✅ acquired, 25 ingredients |
| 5 | `cerave-moisturizing-cream` | CeraVe | Moisturizing Cream | barrier_repair | ✅ acquired, 24 ingredients |
| 6 | `cerave-am-facial-moisturizing-lotion-spf30` | CeraVe | AM Facial Moisturizing Lotion SPF 30 | sunscreen | ✅ acquired, 30 ingredients (4 active + 26 inactive) |
| 7 | `cerave-acne-control-gel` | CeraVe | Acne Control Gel | acne_treatment | ✅ acquired, 24 ingredients (1 active + 23 inactive) |
| 8 | `cerave-acne-control-cleanser` | CeraVe | Acne Control Cleanser | acne_treatment | ✅ acquired, 27 ingredients (1 active + 26 inactive) |
| 9 | `cerave-skin-renewing-vitamin-c-serum` | CeraVe | Skin Renewing Vitamin C Serum | serum | ✅ acquired, 26 ingredients |
| 10 | `ordinary-niacinamide-10-zinc-1` | The Ordinary | Niacinamide 10% + Zinc 1% Oil Control Serum | serum | ✅ acquired, 11 ingredients |
| 11 | `ordinary-glycolic-acid-7-toner` | The Ordinary | Glycolic Acid 7% Exfoliating Toner | exfoliant | ✅ acquired, 42 ingredients |
| 12 | `lrp-cicaplast-balm-b5` | La Roche-Posay | Cicaplast Balm B5+ | barrier_repair | ❌ HTTP 403, Cloudflare bot challenge |
| 13 | `lrp-toleriane-double-repair` | La Roche-Posay | Toleriane Double Repair Face Moisturizer | moisturizer | ❌ HTTP 403, Cloudflare bot challenge |
| 14 | `pc-clear-bha-exfoliant` | Paula's Choice | Skin Perfecting 2% BHA Liquid Exfoliant → redirected to *Clear Regular Strength Anti-Redness Exfoliating Solution with 2% Salicylic Acid* | exfoliant | ⚠️ HTTP 200, ingredients not in static HTML |
| 15 | `pc-perfectly-balanced-cleanser` | Paula's Choice | RESIST Perfectly Balanced Foaming Cleanser | cleanser | ⚠️ HTTP 200, ingredients not in static HTML |

Acquisition date for every URL: **2026-09-18**. Every content hash, HTTP status, and final resolved URL is in `acquisition_ledger.jsonl` verbatim — not reproduced here to avoid a second, driftable copy of the same facts.

## Verification and publication result

Imported, validated, ingredient-resolved, and published entirely through the `populate-wave1b-real-data` CLI command (`python -m app.catalog_admin populate-wave1b-real-data catalog_data/production/wave1b/manifest.jsonl --source-id <id> --dictionary-path catalog_data/production/wave1b/ingredient_dictionary.json`) — a thin, reproducible, idempotent, dry-run-capable orchestration (`app/domain/catalog_wave1b_population.py`) of the completely unmodified Wave 1A pipeline (`import_manifest` → `CatalogIngestionService.validate_batch` → `CatalogReviewService` → `CatalogPublicationService.publish`). See "Reproducible population procedure" below for exactly how, and "Ingredient review" for why a naive bulk approach doesn't work against this architecture and what this command does instead.

| Metric | Count |
|---|---|
| Real products acquired (complete ingredient list extracted) | 11 |
| Real products imported (`catalog_import_records` rows created) | 11 |
| Manifest-level issues | 0 |
| Verified (ready to publish) | 11 |
| Review required | 0 (after ingredient-dictionary bootstrap — see below) |
| Insufficient source data (within the manifest) | 0 |
| Rejected | 0 |
| **Published** | **11** |

Category breakdown of the 11 published formulations:

| Category | Published | Target | Note |
|---|---|---|---|
| Cleansers | 2 | 2-3 | ✅ |
| Moisturizers | 2 | 2-3 | ✅ |
| Sunscreens | 1 | 2 | LRP sunscreen candidate blocked (Cloudflare) |
| Acne treatments | 2 | 2 | ✅ |
| Serums | 2 | 2 | ✅ |
| Exfoliants | 1 | 1-2 | ✅ (LRP not applicable; The Ordinary's second candidate was the acid toner used here) |
| Barrier repair | 1 | 1-2 | ✅ |

## Formulation fingerprints — the reformulation baseline

`compute_ingredient_fingerprint()` (Wave 1A, `app/domain/catalog_source_adapter.py`, unmodified) derived each published formulation's `version` from its own disclosed ingredient list, since none of these sources publish an explicit version string. These are the exact fingerprints recorded as each formulation's `product_formulations.version` at publish time — the baseline against which a future re-acquisition of the same exact URL detects a real formulation change:

| Product | Formulation fingerprint |
|---|---|
| Foaming Facial Cleanser | `fp-68a1db073812d1a1` |
| Hydrating Facial Cleanser | `fp-5a1148ae7ac87412` |
| PM Facial Moisturizing Lotion | `fp-52415dd27919ea50` |
| Daily Moisturizing Lotion | `fp-c3920cfe165e9c9f` |
| Moisturizing Cream | `fp-42fca30bc1ac59f8` |
| AM Facial Moisturizing Lotion SPF 30 | `fp-55ed7ff162f25462` |
| Acne Control Gel | `fp-122bf67b4650f30e` |
| Acne Control Cleanser | `fp-7d5f3dd9ed71728c` |
| Skin Renewing Vitamin C Serum | `fp-2df1254bcae3a611` |
| Niacinamide 10% + Zinc 1% Oil Control Serum | `fp-6ab2552be21ffd63` |
| Glycolic Acid 7% Exfoliating Toner | `fp-44e80d959f0cd54c` |

## Ingredient review — the reproducible resolution procedure, and how the gate is still proven

Wave 1A shipped with a genuinely **empty** `ingredients` table (every one of its own tests uses synthetic, fixture-scoped ingredients). A naive, unmodified `validate-batch` run against all 11 Wave 1B records **at once** (before anything is resolved) opens one `UNKNOWN_INGREDIENT` review item per (record, unresolved raw ingredient) pair — many of them the *same* underlying ingredient (e.g. plain Water appears, under one spelling or another, on 8 of the 11 products) opened independently on *each* product. Resolving the first one is correct and exactly what Wave 1A's review architecture is for — but Wave 1A's own review-identity-binding hotfix (`CATALOG_INGESTION_ARCHITECTURE.md`'s "Third independent review pass") deliberately, and correctly, makes `map_ingredient()`/`create_ingredient()` refuse to act on a review item whose target *already* resolves (`ALREADY_RESOLVED` — a hardened, explicitly-tested guarantee in `tests/domain/test_catalog_review_service.py`, never weakened by this pass). Once the first product's Water resolves, every *other* product's now-stale Water review item becomes permanently stuck behind that same guard if resolved the naive way.

**The fix is procedural, not a change to any hardened review-service code**: `populate_wave1b()` (`app/domain/catalog_wave1b_population.py`) validates and resolves records **one at a time, in a deterministic order**, resolving each record's own newly-opened review items — via a committed, reviewable ingredient dictionary (`catalog_data/production/wave1b/ingredient_dictionary.json`, 111 entries: one per distinct chemical identity actually disclosed across the 11 real products, plus the explicit alias list for the 3 raw spellings of plain Water) — **before the next record is even validated**. By the time product 2 is validated, Water already resolves (product 1 resolved it moments earlier in the same run), so `reconcile_review_state` — the exact same canonical function `validate_batch()` itself calls — never opens a review item for it on product 2 at all. No review-service code was modified; this is the existing, unmodified `CatalogReviewService.map_ingredient()`/`create_ingredient()`, called correctly.

Every dictionary lookup is exact, normalized-string matching only — an ingredient identity with no dictionary entry is left as an open, unresolved review item and reported (`review_item_outcomes` with `action=UNRESOLVED_DICTIONARY_GAP`), never guessed, never fuzzy-matched, never given a bogus alias.

**The review gate itself — that an unrecognized ingredient blocks validation until resolved — is proven separately and directly**: `tests/domain/test_catalog_acquisition.py::test_real_manifest_import_remains_review_gated` imports real Wave 1B manifest data into a genuinely empty, unseeded `ingredients` table and asserts it correctly routes to `NEEDS_REVIEW`/`VALIDATED` (never silently auto-resolves, never auto-publishes); `tests/domain/test_catalog_wave1b_population.py::test_unresolved_ingredient_prevents_publication` and `::test_dictionary_matching_is_exact_never_fuzzy` prove an ingredient absent from the dictionary blocks publication and is never guessed.

## Drug-facts labeling (active/inactive ingredients)

Three of the 11 published products are FDA OTC drug products with a Drug Facts–style Active/Inactive Ingredients split (`Acne Control Gel`, `Acne Control Cleanser` — both 2% salicylic acid; `AM Facial Moisturizing Lotion SPF 30` — four UV-filter actives). `parse_cerave_page()` recognizes this structure explicitly and **requires both sections to be present** before reporting the list as complete — a page carrying only one (a layout this parser doesn't fully recognize) is `PARSE_FAILED`, never silently treated as a full disclosure (independent-review Blocker 2).

`ingredient_list_raw` still carries the active ingredient(s) **first** (matching the manufacturer's own disclosed order) followed by the inactive ingredients, verbatim as disclosed (e.g. `AM Facial Moisturizing Lotion SPF 30`'s list begins `HOMOSALATE (10%), OCTINOXATE (5%), OCTOCRYLENE (2%), ZINC OXIDE (8%)`) — this is the immutable evidence, never lost. Separately, `ingredient_details` (independent-review Blocker 3) carries the same ingredients with concentration **separated from identity** — `raw_name="HOMOSALATE"`, `declared_concentration=10`, `concentration_unit="%"`, `section="active"` — which is what actually becomes each ingredient's canonical identity at publish time, so `resolve_ingredient()` treats "Salicylic Acid" the same regardless of which product's formulation disclosed it at 2% (never a separate canonical ingredient per concentration). The verbatim disclosure text and the active/inactive section label both survive durably per-ingredient on the published row itself, in `formulation_ingredients.notes` (an existing column, e.g. `"disclosed as 'SALICYLIC ACID 2%'; FDA Drug Facts active ingredient"`) — not merely reconstructable from a generic prose note.

## Known source limitations — stated honestly

- **`laroche-posay.us` is completely unavailable to this pass's acquisition method.** Active Cloudflare bot-management challenge on every request, including the homepage. No candidate LRP product (`Cicaplast Balm B5+`, `Toleriane Double Repair Face Moisturizer`) was acquired.
- **`paulaschoice.com`'s ingredient disclosure requires JavaScript execution this module does not perform.** Both attempted Paula's Choice products fetched real page content but no ingredient list. No Paula's Choice product was acquired.
- **A trailing manufacturer batch/formula code** (`- (CODE F.I.L D215763/2)`, observed on CeraVe's Daily Moisturizing Lotion page) is stripped by `_TRAILING_CODE_RE` before splitting — a real, narrow pattern match, not a general "clean up anything odd" heuristic; a differently-formatted code elsewhere would not be caught and would need its own explicit handling.
- **Concentration extraction is a narrow pattern match, not general chemistry parsing.** `_extract_concentration()` recognizes exactly two disclosed shapes (`"NAME N%"` and `"NAME (N%)"`, both observed on real CeraVe pages) — a concentration disclosed in some other format would pass through with `declared_concentration=None`, never a wrong guess.
- **Manufacturer ingredient lists change over time.** Every CeraVe page fetched this pass carries the manufacturer's own disclaimer verbatim ("ingredient lists...are updated regularly...refer to the ingredient list on your product package for the most up-to-date list") — this pass's own `retrieved_at`/`verification_date`/formulation fingerprint exist specifically so a later re-check is meaningful; see "Re-verification procedure" below. **No formulation captured this pass should ever be described as permanently current.**
- **Only 11 of the 12-20 targeted real products were acquired**, entirely due to the two domain-level blockers above, not a shortfall in the CeraVe/The Ordinary candidate list. Reported honestly rather than padded with a weaker-evidence source.

## Reproducible population procedure

`app/domain/catalog_wave1b_population.py::populate_wave1b()`, exposed as `python -m app.catalog_admin populate-wave1b-real-data`, is the single operator-invoked entry point that reproduces the published state above from a genuinely clean database:

```
python -m app.catalog_admin populate-wave1b-real-data \
    catalog_data/production/wave1b/manifest.jsonl \
    --source-id <registered catalog_sources.id> \
    --dictionary-path catalog_data/production/wave1b/ingredient_dictionary.json
```

Steps 0 and 1 below run for EVERY invocation, including `--dry-run`; steps 2-4 are skipped entirely for a dry run:

0. **Preflight (read-only).** `load_ingredient_dictionary()` loads and fully validates `ingredient_dictionary.json` (fails closed, structured `PopulationRejectedError`, on any malformed shape). `_run_preflight()` then schema-validates the manifest and checks every distinct ingredient identity it discloses against the dictionary AND the current catalog (`resolve_ingredient()`), reporting `identities_total`/`identities_already_in_catalog`/`identities_covered_by_dictionary`/`unresolved_dictionary_gaps`/`preflight_ready`. Neither step ever creates a batch, import record, ingredient, alias, product, formulation, review item, or audit row — this is what makes `--dry-run` provably a no-op, and what surfaces a broken dictionary or a coverage gap BEFORE any mutation, not after.
1. `import_manifest()` (idempotent by content SHA-256 — re-running resolves to the same batch, never a duplicate).
2. Each record is validated (`reconcile_review_state`) and its own newly-opened `UNKNOWN_INGREDIENT` review items resolved against the SAME dictionary already loaded in step 0, one record at a time, in deterministic (`source_evidence_id`) order — see "Ingredient review" above for exactly why this ordering matters.
3. Every record reaching `VALIDATED` (or already `PUBLISHED` from a prior run) is published (`CatalogPublicationService.publish()`, itself an idempotent no-op for an already-published record).
4. Reports, honestly: records imported vs. reused, every review-item resolution (`CREATED_INGREDIENT`/`MAPPED_TO_EXISTING`/`UNRESOLVED_DICTIONARY_GAP`), validated/needing-review/rejected counts, and per-record publish outcomes.

The canonical ingredient dictionary itself is committed, reviewable structured data (`catalog_data/production/wave1b/ingredient_dictionary.json`) generated once, deterministically, from the manifest's own distinct normalized ingredient identities — never reconstructed ad hoc at import time; its own header comment documents exactly how it was built and why only plain Water needed an explicit alias group. Tested end-to-end in `tests/domain/test_catalog_wave1b_population.py` (clean-database reproducibility, second-run idempotency, dry-run loads/validates the dictionary and writes zero rows across every table this workflow could touch, a missing/malformed dictionary fails before any batch is created, unresolved-ingredient-blocks-publication, non-fuzzy dictionary matching) and `tests/catalog_admin/test_wave1b_population_cli.py` (the CLI command itself).

## Re-verification procedure (manual, not automated this pass)

This pass explicitly does **not** implement recurring or scheduled re-acquisition. To check for a formulation change later, an operator:

1. Re-runs `catalog_data/production/wave1b/run_acquisition.py` (or a future pass's equivalent covering the same `sources.json` URLs) — the exact same explicit URL list, the exact same per-brand parsers, the exact same 5-second inter-request delay.
2. Compares the newly-computed `content_sha256` for each URL against the value already recorded in `acquisition_ledger.jsonl` for that `source_evidence_id` — a changed hash means the page itself changed (not necessarily the ingredient list; marketing copy changes too).
3. If the extracted ingredient list differs from the currently-published formulation's own ingredients (visible via `catalog_data/production/wave1b`'s own recorded fingerprints above, or by calling `compute_ingredient_fingerprint()` on the newly-extracted list and comparing it to the table above), imports the new manifest row via the existing `import-source-manifest` → `validate-batch` → `publish` flow exactly as this pass did — `CatalogPublicationService.publish()` (Wave 1A, unmodified) automatically detects the version-token (fingerprint) change and supersedes the old formulation rather than overwriting it, preserving the old row and its own provenance permanently (Wave 1A's own reformulation guarantee, proven by its own test suite, re-run unmodified as part of this pass's validation).
4. Never automates this into a cron/worker — an explicit, deliberate, operator-run step, matching this pass's own "no recurring scheduled crawling" exclusion.

## Data files — real vs. synthetic, clearly separated

- **Real Wave 1B data**: `catalog_data/production/wave1b/` (`sources.json`, `run_acquisition.py`, `acquisition_ledger.jsonl`, `manifest.jsonl`, `evidence/*.txt`, `ingredient_dictionary.json`) — public product/formulation information only, no secrets, no personal data.
- **Synthetic test fixtures**: `backend/tests/fixtures/wave1b/` (bounded HTML snippets used only by `tests/domain/test_catalog_acquisition.py`'s parser unit tests — see that directory's own `README.md` for exact source/retrieval-date provenance of each) and every existing Wave 1/1A synthetic fixture (obviously-fake brand names, `example.invalid` URLs) — never real product data, never imported into any real catalog.

`tests/domain/test_catalog_acquisition.py::test_real_wave1b_manifest_brands_are_real_not_synthetic_convention` and `::test_real_wave1b_manifest_source_name_is_not_a_reserved_test_prefix` prove the real manifest never uses this repository's synthetic-fixture naming convention (`is_reserved_test_source_name`, Wave 1A) and vice versa.

## What this pass does NOT do

No general crawling, no automatic recurring scraping, no retailer ingestion, no affiliate/sponsored ranking, no purchasing/checkout, no Mobile C2/C3 work, no social/marketplace/supplements features, no AI-generated catalog records, no fuzzy automatic ingredient acceptance, and no medical claims beyond what each manufacturer's own page already discloses. See "Explicit exclusions" in this pass's own brief for the complete list — none of it exists anywhere in this branch's diff.
