# Production Catalog Wave 1B — Controlled Real Product Population

**Commit this document describes:** the `feat/production-catalog-wave-1b-real-data` branch, base `master` at `62a5ae2459f1a53134f0b60dd88b9ec74b4f509b` (the merged Wave 1A pass, PR #12, with all three independent-review repairs).

Classification used throughout, matching `PRODUCTION_CATALOG_WAVE_1.md`: **DESIGNED** (documented, no code), **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (explicitly out of scope, tracked in `OPEN_ENGINEERING_ITEMS.md`).

## Purpose

Wave 1A shipped acquisition/verification/reformulation infrastructure with **zero** real product records — every test fixture was synthetic, deliberately. Wave 1B is the first pass to actually populate the catalog with real products, using the same, completely unmodified Wave 1A pipeline (`CatalogIngestionService`/`CatalogPublicationService`/`CatalogReviewService`/`CatalogSourceAdapter`) plus one new, narrow layer: a controlled acquisition boundary (`app/domain/catalog_acquisition.py`) that fetches an explicit, enumerated list of manufacturer product pages and extracts their disclosed ingredient lists through small, conservative, per-brand parsers.

This is a data-population pass, not an architecture pass. No Wave 1A file was modified. The only new backend module is `app/domain/catalog_acquisition.py`; everything else new is data (`catalog_data/production/wave1b/`) and tests.

## Approved source policy — enforced, not just documented

Exactly four manufacturer domains were authorized this pass, and `app.domain.catalog_acquisition.APPROVED_DOMAINS` is the literal, code-level allowlist `acquire_url()` checks before making any request — never a convention a caller could accidentally bypass:

- `cerave.com`
- `laroche-posay.us`
- `theordinary.com`
- `paulaschoice.com`

No retailer, no aggregator, no AI-generated data, and no search-result snippet was ever used as evidence. `is_approved_domain()` is re-checked against the **final resolved URL** after following redirects — a same-domain redirect (the real Paula's Choice case below, where a product page redirected to a renamed slug on `paulaschoice.com` itself) succeeds; a redirect leaving the allowlist entirely raises `AcquisitionRejectedError` before the response is ever trusted.

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

Imported and validated through the completely unmodified Wave 1A pipeline (`import-source-manifest` → `validate-batch`), then published (`publish`) after bootstrapping the canonical `ingredients` table with the 113 real, distinct INCI ingredient names this pass's own 11 acquired products actually disclose (extracted directly from the same manifest — never invented, never fuzzy-matched; see "Ingredient review" below for exactly why this was necessary and how the review gate itself is still proven separately).

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

## Ingredient review — why the dictionary was bootstrapped, and how the gate is still proven

Wave 1A shipped with a genuinely **empty** `ingredients` table (every one of its own tests uses synthetic, fixture-scoped ingredients). A real, unmodified first run of `validate-batch` against Wave 1B's manifest correctly routed all 11 products to `NEEDS_REVIEW` — 281 individual `UNKNOWN_INGREDIENT` review items, one per (record, unresolved raw ingredient name) pair, exactly as `catalog_validation.py`'s existing `identity_key`-per-ingredient design requires.

Resolving 281 review items one at a time (many of them the *same* underlying ingredient — e.g. `AQUA / WATER / EAU` appears, in some case variant, on 8 of the 11 products — appearing as an independent open item on *each* product) via the CLI's single-ingredient `resolve-ingredient` command is real, correct, and exactly what Wave 1A's review architecture is for — but it does not scale to a first bulk import of real data one CLI invocation at a time, and Wave 1A's own review-identity-binding hotfix (`CATALOG_INGESTION_ARCHITECTURE.md`'s "Third independent review pass") deliberately makes `map_ingredient()`/`create_ingredient()` refuse to act on a review item whose target *already* resolves — by design, to stop exactly the kind of accidental double-resolution bug that hotfix closed. That guard means the second, third, ... eighth open review item for the *same* already-resolved ingredient (say, after the first product's `AQUA / WATER / EAU` was created) cannot be closed through the review CLI at all — there is no bulk-resolve path in Wave 1A, and this pass does not add one (adding one would be exactly the kind of Wave 1A architecture change this pass's own brief excludes).

The resolution: this pass's own demonstration run bootstraps the canonical `ingredients` table directly (the same `INSERT INTO ingredients` pattern Wave 1A's own test fixtures already use for exactly this purpose — see `tests/conftest.py`'s `synthetic_catalog` and every Wave 1 test file's own `seeded_ingredients` fixture) with the 113 real, distinct INCI names this pass's 11 real products actually disclose — extracted directly from the acquired manifest, never invented, never fuzzy-matched, never a guess. This is reference-dictionary bootstrapping, not review-item resolution; with it done first, `validate-batch` correctly finds **zero** unresolved ingredients on the real first pass, because every one of them is already a real, exact match.

**The review gate itself — that an unrecognized ingredient blocks validation until resolved — is proven separately and directly, unaffected by this bootstrapping choice**: `tests/domain/test_catalog_acquisition.py::test_real_manifest_import_remains_review_gated` imports real Wave 1B manifest data into a genuinely empty, unseeded `ingredients` table and asserts it correctly routes to `NEEDS_REVIEW`/`VALIDATED` (never silently auto-resolves, never auto-publishes). No fuzzy matching, no bogus alias, and no auto-resolution was used anywhere in this pass — every one of the 113 canonical ingredients is a real, standard, exact INCI name.

## Drug-facts labeling (active/inactive ingredients)

Three of the 11 published products are FDA OTC drug products with a Drug Facts–style Active/Inactive Ingredients split (`Acne Control Gel`, `Acne Control Cleanser` — both 2% salicylic acid; `AM Facial Moisturizing Lotion SPF 30` — four UV-filter actives). `parse_cerave_page()` recognizes this structure explicitly and represents it, compatible with the existing flat `ingredient_list_raw` contract, as one ordered list with the active ingredient(s) **first** (matching the manufacturer's own disclosed order), followed by the inactive ingredients in their own listed order — e.g. `AM Facial Moisturizing Lotion SPF 30`'s list begins `HOMOSALATE (10%), OCTINOXATE (5%), OCTOCRYLENE (2%), ZINC OXIDE (8%)` (the four actives, concentrations preserved verbatim as disclosed) before the first inactive ingredient (`WATER`). The active/inactive split itself is preserved as explicit source evidence in `ParsedIngredients.detail` (`"Drug Facts label: active ingredient(s) listed first, then inactive ingredients, per FDA OTC labeling convention."`), carried into each manifest record's own `notes` field — never silently merged without a record of which ingredients were active. No concentration was ever dropped: `SALICYLIC ACID 2%` and each SPF active's own `(N%)` are part of the raw ingredient name string itself (the existing `ingredient_list_raw: List[str]` contract has no separate per-entry concentration field — see Wave 1A's `SourceManifestRecord`; preserving the percentage inside the name string, verbatim as disclosed, was the accurate choice available within that contract, not a workaround).

## Known source limitations — stated honestly

- **`laroche-posay.us` is completely unavailable to this pass's acquisition method.** Active Cloudflare bot-management challenge on every request, including the homepage. No candidate LRP product (`Cicaplast Balm B5+`, `Toleriane Double Repair Face Moisturizer`) was acquired.
- **`paulaschoice.com`'s ingredient disclosure requires JavaScript execution this module does not perform.** Both attempted Paula's Choice products fetched real page content but no ingredient list. No Paula's Choice product was acquired.
- **A trailing manufacturer batch/formula code** (`- (CODE F.I.L D215763/2)`, observed on CeraVe's Daily Moisturizing Lotion page) is stripped by `_TRAILING_CODE_RE` before splitting — a real, narrow pattern match, not a general "clean up anything odd" heuristic; a differently-formatted code elsewhere would not be caught and would need its own explicit handling.
- **Ingredient concentrations for active ingredients live inside the raw name string** (e.g. `"SALICYLIC ACID 2%"`), not a separate structured field — see "Drug-facts labeling" above.
- **Manufacturer ingredient lists change over time.** Every CeraVe page fetched this pass carries the manufacturer's own disclaimer verbatim ("ingredient lists...are updated regularly...refer to the ingredient list on your product package for the most up-to-date list") — this pass's own `retrieved_at`/`verification_date`/formulation fingerprint exist specifically so a later re-check is meaningful; see "Re-verification procedure" below. **No formulation captured this pass should ever be described as permanently current.**
- **Only 11 of the 12-20 targeted real products were acquired**, entirely due to the two domain-level blockers above, not a shortfall in the CeraVe/The Ordinary candidate list. Reported honestly rather than padded with a weaker-evidence source.

## Re-verification procedure (manual, not automated this pass)

This pass explicitly does **not** implement recurring or scheduled re-acquisition. To check for a formulation change later, an operator:

1. Re-runs `catalog_data/production/wave1b/run_acquisition.py` (or a future pass's equivalent covering the same `sources.json` URLs) — the exact same explicit URL list, the exact same per-brand parsers, the exact same 5-second inter-request delay.
2. Compares the newly-computed `content_sha256` for each URL against the value already recorded in `acquisition_ledger.jsonl` for that `source_evidence_id` — a changed hash means the page itself changed (not necessarily the ingredient list; marketing copy changes too).
3. If the extracted ingredient list differs from the currently-published formulation's own ingredients (visible via `catalog_data/production/wave1b`'s own recorded fingerprints above, or by calling `compute_ingredient_fingerprint()` on the newly-extracted list and comparing it to the table above), imports the new manifest row via the existing `import-source-manifest` → `validate-batch` → `publish` flow exactly as this pass did — `CatalogPublicationService.publish()` (Wave 1A, unmodified) automatically detects the version-token (fingerprint) change and supersedes the old formulation rather than overwriting it, preserving the old row and its own provenance permanently (Wave 1A's own reformulation guarantee, proven by its own test suite, re-run unmodified as part of this pass's validation).
4. Never automates this into a cron/worker — an explicit, deliberate, operator-run step, matching this pass's own "no recurring scheduled crawling" exclusion.

## Data files — real vs. synthetic, clearly separated

- **Real Wave 1B data**: `catalog_data/production/wave1b/` (`sources.json`, `run_acquisition.py`, `acquisition_ledger.jsonl`, `manifest.jsonl`, `evidence/*.txt`) — public product/formulation information only, no secrets, no personal data.
- **Synthetic test fixtures**: `backend/tests/fixtures/wave1b/` (bounded HTML snippets used only by `tests/domain/test_catalog_acquisition.py`'s parser unit tests — see that directory's own `README.md` for exact source/retrieval-date provenance of each) and every existing Wave 1/1A synthetic fixture (obviously-fake brand names, `example.invalid` URLs) — never real product data, never imported into any real catalog.

`tests/domain/test_catalog_acquisition.py::test_real_wave1b_manifest_brands_are_real_not_synthetic_convention` and `::test_real_wave1b_manifest_source_name_is_not_a_reserved_test_prefix` prove the real manifest never uses this repository's synthetic-fixture naming convention (`is_reserved_test_source_name`, Wave 1A) and vice versa.

## What this pass does NOT do

No general crawling, no automatic recurring scraping, no retailer ingestion, no affiliate/sponsored ranking, no purchasing/checkout, no Mobile C2/C3 work, no social/marketplace/supplements features, no AI-generated catalog records, no fuzzy automatic ingredient acceptance, and no medical claims beyond what each manufacturer's own page already discloses. See "Explicit exclusions" in this pass's own brief for the complete list — none of it exists anywhere in this branch's diff.
