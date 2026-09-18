# Wave 1B parser test fixtures

Bounded HTML snippets (~2KB each, never a full page) captured from real,
publicly-accessible manufacturer product pages during Production Catalog
Wave 1B's own acquisition run, retained here **only** to give
`tests/domain/test_catalog_acquisition.py` a stable, offline input for
each per-brand parser's real page structure. These are test fixtures,
not production data — see `catalog_data/production/wave1b/` for the
actual Wave 1B manifest, acquisition ledger, and evidence this pass
produced from the same real pages.

| File | Source URL | Retrieved | Demonstrates |
|---|---|---|---|
| `cerave_cosmetic_product_snippet.html` | https://www.cerave.com/skincare/cleansers/foaming-facial-cleanser | 2026-09-18 | Plain cosmetic-product ingredient disclosure (one `<p>`, comma-separated, ends with `.`) |
| `cerave_drug_active_inactive_snippet.html` | https://www.cerave.com/skincare/acne/acne-control-gel | 2026-09-18 | OTC drug product: separate "Active Ingredients:" / "Inactive Ingredients:" paragraphs |
| `cerave_nbsp_artifacts_snippet.html` | https://www.cerave.com/skincare/moisturizers/am-facial-moisturizing-lotion-with-sunscreen | 2026-09-18 | `&nbsp;`-wrapped hyperlinked ingredient names (entity-decoding regression) |
| `cerave_trailing_code_snippet.html` | https://www.cerave.com/skincare/moisturizers/daily-moisturizing-lotion | 2026-09-18 | Trailing manufacturer batch/formula code (`- (CODE ...)`) after the last ingredient |
| `ordinary_product_snippet.html` | https://theordinary.com/en-us/glycolic-acid-7-exfoliating-toner-100418.html | 2026-09-18 | The Ordinary's `data-original-ingredients="..."` attribute, including a numeric-locant ingredient name (`1,2-Hexanediol`) that a naive comma-split would corrupt |
| `paulaschoice_no_ingredients_snippet.html` | https://www.paulaschoice.com/resist-perfectly-balanced-foaming-cleanser/783.html | 2026-09-18 | Real page content (JSON-LD `Product` block) with no ingredient disclosure present — proves the parser fails closed (`INGREDIENTS_NOT_FOUND`) rather than guessing |

No `laroche-posay.us` fixture exists: every request to that domain
during acquisition returned an active Cloudflare bot challenge
(HTTP 403, `cf-mitigated: challenge`) before any real page content was
ever received — there was no page structure to capture. See
`PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md`'s "known source limitations"
section.

These snippets contain only public product-page markup (brand name,
product name, ingredient disclosure, standard template HTML) — no
secrets, no personal data, no pricing/account/session content.
