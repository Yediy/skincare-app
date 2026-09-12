# Product Catalog Architecture

What the normalized product catalog (product/usage foundation pass) actually
is, how it composes with the existing category-level safety path, and what
remains partial. Status taxonomy matches `ARCHITECTURE_CURRENT.md`:
`VERIFIED_IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, `NOT_IMPLEMENTED`.

## Why formulation-level, not product-level

A product name is not a stable safety boundary: the same product name can
carry different formulas by market/region, by reformulation date, or by
revision, and a manufacturer disclosing "contains retinol" today can
reformulate without it tomorrow under the identical product name. This
catalog puts the actual ingredient list on `product_formulations`, not on
`products` or `brands` — a product can have many formulations (different
`version`/`market_or_region`/`effective_from`/`effective_to`), and safety
evaluation always operates on one specific `formulation_id`, never on a
product or brand alone.

## Schema — `VERIFIED_IMPLEMENTED`

Migrations `d70e5fc90775` (core tables) and `16b82dde6e7d` (rules/
interactions):

```text
brands
  └─ products (brand_id)
       └─ product_formulations (product_id) -- the safety boundary
            ├─ product_skus (product_id, formulation_id)
            └─ formulation_ingredients (formulation_id, ingredient_id, position)
                    └─ ingredients (canonical, resolved via ingredient_aliases)

ingredients
  ├─ ingredient_aliases (normalized_alias UNIQUE -- resolves to exactly one ingredient)
  ├─ ingredient_rules (per-ingredient restriction: rule_type/severity/action)
  └─ ingredient_interactions (pairwise, canonically ordered ingredient_a_id < ingredient_b_id)
```

Key invariants, enforced at the database level, not just in application code:

- At most one `is_current = true` formulation per `(product_id,
  market_or_region)` — a partial unique index, since a plain `UNIQUE` can't
  be conditional.
- `ingredient_aliases.normalized_alias` is globally `UNIQUE` — every alias
  resolves to exactly one canonical ingredient, never an ambiguous set.
- `ingredient_interactions` canonicalizes pair ordering with a `CHECK
  (ingredient_a_id < ingredient_b_id)` constraint plus `UNIQUE
  (ingredient_a_id, ingredient_b_id, interaction_type)` — `(A, B)` and `(B,
  A)` can never exist as two separate, potentially-contradictory rows.
  Every caller canonicalizes before querying/inserting
  (`app/db/catalog_repository.py::canonical_ingredient_pair`), matching the
  DB's own ordering exactly (verified: Postgres's `uuid` `<` operator and
  Python string comparison of two canonically-formatted UUIDs agree).
- `formulation_ingredients.declared_concentration` is nullable and stays
  `NULL` when a manufacturer doesn't disclose it — never fabricated. It has
  no surrogate `id`; its primary key is `(formulation_id, ingredient_id)`.

## Access control — `VERIFIED_IMPLEMENTED`

Every catalog table above is global reference data, not user-owned data:
no row has a `user_id` to scope by, so none of them have row-level
security. What they do have is a hard read/write split, enforced by
`GRANT`, not merely by convention:

- `skincare_app` (the runtime API role every request goes through) has
  **`SELECT`-only** grants on every catalog table — verified directly by a
  real `InsufficientPrivilegeError` when the restricted role attempts to
  `INSERT` (`tests/database/test_product_usage_rls.py`).
- Nothing in this pass adds a catalog-administration HTTP route, so there
  is currently no code path by which the API can write catalog data at
  all — population happens through migrations and, for tests, the
  superuser test-fixture path (`tests/conftest.py`'s `synthetic_catalog`
  fixture, seeded via `db_pool`), the same test-owner-seeds/
  runtime-role-reads split every other RLS-protected table in this
  repository already uses.
- **Superseded by the catalog-ingestion-admin pass:** a real write path
  now exists — a dedicated `skincare_catalog_admin` database role
  (still no HTTP route; CLI-only, `python -m app.catalog_admin`) can
  `INSERT`/`UPDATE` these same tables, plus `product_formulations`'
  new `publication_status` column. `skincare_app` itself is
  unaffected — still `SELECT`-only on every table listed here. See
  `CATALOG_INGESTION_ARCHITECTURE.md` for the full ingestion/
  publication pipeline and its own privilege-boundary tests.

## Ingredient resolution — `VERIFIED_IMPLEMENTED`

`app/db/catalog_repository.py::resolve_ingredient(pool, name)`: exact match
against `ingredients.normalized_name` first, then `ingredient_aliases.
normalized_alias` — never substring/`LIKE` matching. `normalize_name()` is
the single normalization rule every lookup in this module uses (lowercase,
trimmed, internal whitespace collapsed), so two different callers can never
disagree about whether two strings are "the same" ingredient name.
Deliberately does not strip punctuation/accents or attempt fuzzy matching —
a real, separate data-quality decision this pass does not make casually;
adding it later only makes matching more permissive, never less, so
deferring it is safe. An unresolvable name returns `None`, never a guessed
match.

Deterministic lookup is provided by three independent paths (Phase 3's
explicit requirement), each tested directly
(`tests/catalog/test_catalog_repository.py`):

- `get_formulation_by_id(pool, formulation_id)`
- `get_current_formulation_for_product(pool, product_id, market_or_region)`
- `get_formulation_by_sku(pool, sku, product_id=None)`

## Safety evaluation — `VERIFIED_IMPLEMENTED`, additive not a replacement

`SafetyEngine.evaluate_product_formulation(pool, formulation_id,
constraints)` (`app/domain/safety_engine.py`) is the graduation from
`evaluate_offer()`'s static, hand-authored `CATEGORY_SAFETY_PROFILES`
(`app/domain/product_safety.py`) to real per-formulation evaluation:

```text
User constraints + product formulation + ingredient rules + ingredient interactions
        ↓
SafetyDecision(status, allowed, reason_codes, restrictions)
```

- **Allergy/avoid-ingredient conflicts** are resolved by matching the
  user's own declared list (via the same alias-resolution path above —
  "Vitamin A1" correctly conflicts with a formulation containing
  "Retinol") against the formulation's actual ingredient set. This is
  *not* driven by `ingredient_rules` rows — allergy/avoidance is
  inherently about what a specific user declared, not an ingredient-table
  fact.
- **Pregnancy/nursing/sensitive-skin/max-frequency/barrier-recovery**
  restrictions *are* ingredient-intrinsic facts, so they come from
  `ingredient_rules` rows (`rule_type` + `action`), filtered against the
  user's own constraints (`is_pregnant`, `has_sensitive_skin`, etc.).
  `action = 'EXCLUDE'` marks the formulation `UNSAFE`; `action = 'RESTRICT'`
  (e.g. sensitive-skin frequency capping) never does on its own.
- **Ingredient interactions**: any interaction found within the
  formulation's own ingredient set adds `ACTIVE_INTERACTION_CONFLICT`;
  `HIGH`/`CRITICAL` severity marks the formulation `UNSAFE`.

Reason codes implemented and actually triggerable against real data this
pass: `ALLERGY_CONFLICT`, `USER_AVOID_INGREDIENT`, `PREGNANCY_RESTRICTION`,
`NURSING_RESTRICTION`, `SENSITIVE_SKIN_INTENSITY_LIMIT`,
`ACTIVE_INTERACTION_CONFLICT`, `UNKNOWN_FORMULATION`. `MAX_FREQUENCY_EXCEEDED`
and `BARRIER_RECOVERY_CONFLICT` are supported end-to-end in the evaluation
logic (an `ingredient_rules` row of that `rule_type` will trigger them) but
have no seeded test data exercising them yet — `PARTIALLY_IMPLEMENTED`,
stated honestly rather than claimed complete.

**This is additive, not a replacement**: `PlanService` (`app/services/
plan_service.py`) still calls the original `evaluate_offer()` against
category strings — nothing in this pass matches `PlanService`'s abstract
recommended categories (`"retinoid"`, `"cleanser"`, etc.) to concrete
catalog products, because that is ranking/product-matching work, explicitly
out of scope for this pass. `evaluate_product_formulation()` is the seam a
future ranking/matching step wires into.

## Unknown Formulation Policy — `VERIFIED_IMPLEMENTED`

A formulation that doesn't exist, or exists with zero recorded ingredients,
returns `status = INSUFFICIENT_DATA` (`allowed = False`) — never `SAFE`.
Missing safety data is a distinct, honest outcome (see
`tests/planning/test_formulation_safety.py::
test_incomplete_formulation_is_insufficient_data_not_safe` and
`::test_unknown_formulation_id_is_insufficient_data`), not a silent pass.
`SafetyDecision` carries a proper four-state `status` (`SAFE` / `RESTRICTED`
/ `UNSAFE` / `INSUFFICIENT_DATA`), not just a boolean — `allowed` is kept as
a derived convenience for callers that only need a bit.

## The commercial-override invariant — `VERIFIED_IMPLEMENTED`

`evaluate_product_formulation()`'s signature takes only a `formulation_id`
and the user's own constraints — no brand, price, popularity, or
commercial-ranking input of any kind, so there is structurally no code path
by which a commercial factor could change its result (there is no
commercial ranking system yet at all, but this boundary is encoded now,
before one exists, per this pass's own explicit instruction). Proven, not
just asserted, by `tests/planning/test_formulation_safety.py::
test_commercial_factors_cannot_change_safety_result`: two different
brands/products/SKUs deliberately pointing at the *same* underlying
formulation (a real white-label/rebrand pattern) produce byte-identical
`SafetyDecision`s.

## Test catalog — `VERIFIED_IMPLEMENTED`

`tests/conftest.py`'s `synthetic_catalog` fixture: one fictional brand
("Testonyx") plus one rebrand ("Rebrand Labs") for the commercial-override
test, covering every required scenario — basic moisturizer (SAFE),
fragrance/allergen conflict, retinoid/pregnancy+nursing restriction,
exfoliating-acid/sensitive-skin restriction, canonical-name and alias-based
avoid-ingredient conflicts, an ingredient-interaction conflict
(retinol + glycolic acid), and an incomplete (zero-ingredient) formulation.
No real brand or product names are used anywhere in this catalog or its
tests.

## What remains partial or not built

- `MAX_FREQUENCY`/`BARRIER_RECOVERY` `ingredient_rules` are supported by
  the evaluation code but have no seeded fixture data exercising them —
  `PARTIALLY_IMPLEMENTED`.
- No catalog-administration route (create/update a brand/product/
  formulation/ingredient/rule via HTTP) exists — population is
  migration/fixture-only this pass, deliberately (see "Access control"
  above).
- No product-matching/ranking step connects `PlanService`'s recommended
  categories to real catalog products/formulations — `NOT_IMPLEMENTED`,
  explicitly out of scope for this pass (affiliate ranking is a later,
  separate pass per the brief).
- `PHOTOSENSITIVITY`/`IRRITATION` `ingredient_rules` are recorded as an
  advisory `restrictions["advisory_rule_types"]` list rather than a
  dedicated top-level reason code — there is no such code in this pass's
  brief, so one was not fabricated.
