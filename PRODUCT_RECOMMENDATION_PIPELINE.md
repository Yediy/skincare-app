# Product Recommendation Pipeline

**Commit this document describes:** the head of `feat/production-recommendation-async-analysis` as of this continuation. Referenced from `app/domain/recommendation_service.py`'s own module docstring, which is why this file needed to actually exist.

This is the real call-chain diagram for how a category recommended by `PlanService.generate_plan()` becomes (or doesn't become) a concrete product recommendation the client sees. Every claim below is either code-referenced or backed by a named test.

## The chain

```text
PlanService.generate_plan()
    -> abstract plan: am_routine/pm_routine steps, each with a
       product_category string (e.g. "retinoid"), evaluated only
       against app/domain/product_safety.py's category-level profiles
       (evaluate_offer()) -- unchanged by this pass.
        |
        v
apply_product_matching_and_routine_safety(pool, plan, constraints, ...)
    (app/domain/recommendation_service.py -- the actual P0 closure)
        |
        +-- ProductMatchingService.find_compatible_products(category, constraints)
        |       for every step -- each candidate formulation evaluated
        |       through SafetyEngine.evaluate_product_formulation(),
        |       never through evaluate_offer() alone. Returns up to 3
        |       ranked SAFE/RESTRICTED matches, or an empty list
        |       (Phase 11's required fallback: never a generic/
        |       popular/default/affiliate substitute).
        |
        +-- SafetyEngine.evaluate_routine_safety(pool, entries, usage_context)
                cross-checks the *proposed schedule* -- MAX_FREQUENCY_
                EXCEEDED, BARRIER_RECOVERY_CONFLICT, cross-product
                ACTIVE_INTERACTION_CONFLICT -- none of which
                evaluate_product_formulation() alone can see (it has
                no notion of a schedule or of what else is in the
                routine).
        |
        v
    bounded conflict-resolution pass (guard-limited, not a global
    solver): drops a step's top candidate for an EXCLUDE-action
    formulation, or the later-ordered side of an unresolved
    EXCLUDE_COMBINATION pair, and re-evaluates.
        |
        v
    hard backstop: if routine_decision.status is STILL UNSAFE after
    the pass gives up, EVERY step's concrete candidates are cleared
    (abstract category guidance untouched) -- see "The P0 invariant"
    below.
        |
        v
    plan (mutated in place, each step's product_recommendations
    populated or left empty) + RecommendationResult
    (routine_safety_decision, product_recommendations: List[
    StepProductRecommendation])
```

`compute_analysis()` (`app/domain/analysis_service.py`) is the one place this whole chain is invoked — called by both the synchronous `/analyze` path and the async `AnalysisExecutionService`, so neither path can silently diverge from the other's safety behavior.

## The central invariant

> **No concrete product recommendation may survive an UNSAFE routine decision.**

This is a P0 safety property, not a style preference, and it is enforced structurally, not merely documented:

- `SafetyEngine.evaluate_routine_safety()` records `restrictions["exclude_action_formulation_ids"]` whenever an EXCLUDE-action `MAX_FREQUENCY`/`BARRIER_RECOVERY` rule is what made the routine `UNSAFE` — a formulation-level attribution the conflict-resolution loop reads directly, rather than having to reverse-engineer which of possibly several restriction entries actually caused `unsafe=True`.
- The conflict-resolution loop in `apply_product_matching_and_routine_safety()` drops every formulation attributed this way unconditionally (there is no "other side" to keep — it's unsafe on its own), and drops the later-ordered side of an unresolved `EXCLUDE_COMBINATION` pair, re-evaluating after each pass.
- **The hard backstop** closes the gap the bounded heuristic itself cannot: if `routine_decision.status` is still `UNSAFE` once the loop gives up (an interaction the pairwise heuristic can't reduce to a droppable pair — e.g. a single formulation whose own two ingredients carry a MODERATE-severity `EXCLUDE_COMBINATION`, which passes *formulation*-level safety since only HIGH/CRITICAL trips that, but still makes the *routine* unsafe with no second step to yield to), every step's `product_recommendations` is cleared, not just the implicated one. `plan.metadata.unresolved_unsafe_routine` and `plan.metadata.safety_notice` record that this happened and why — a caller reading only `plan` (not the separate `product_recommendations` list) can still tell.

Verified by `tests/domain/test_recommendation_service.py`:

- `test_max_frequency_exclude_action_removes_unsafe_product` / `test_barrier_recovery_exclude_action_removes_unsafe_product` — a single-candidate EXCLUDE-action rule is dropped entirely, not emitted.
- `test_max_frequency_exclude_exhausts_multiple_alternatives` — two EXCLUDE-flagged alternatives in the same category are both dropped in turn; the step ends empty, and the routine still resolves to non-`UNSAFE`.
- `test_unresolved_exclude_combination_clears_entire_routine` — the backstop case: an otherwise-unrelated, otherwise-fine step (a plain moisturizer) also has its concrete recommendation cleared, because the pass above already tried and failed to attribute the `UNSAFE` status to a smaller set.
- `test_unsafe_routine_status_never_coexists_with_concrete_recommendations` — the invariant itself, asserted generically rather than against one specific cause, so a future change to the resolution heuristic can't silently reintroduce the bug this pass fixed.

Plus the pre-existing `test_cross_product_exclude_combination_drops_the_later_step` and `tests/planning/test_routine_safety.py`'s own suite (including this continuation's `test_max_frequency_exclude_action_marks_routine_unsafe_and_attributes_formulation` / `test_barrier_recovery_exclude_action_marks_routine_unsafe_and_attributes_formulation`, which prove the attribution mechanism itself, independent of the routine builder that consumes it).

## What this does not claim

- **Not a global optimizer.** The conflict-resolution pass is a real, terminating, bounded heuristic (`guard < len(all_steps) * 2`), not a solver for every pathological N-way conflict graph. The backstop exists precisely because the heuristic is honestly scoped, not because it's expected to be exercised often.
- **Not clinical efficacy ranking.** `ProductMatchingService`'s "compatibility ordering" (SAFE before RESTRICTED, exact-market before global fallback, freshest verification, then a stable tie-break) is deliberately never called ranking by evidence or outcome — no such data exists in this catalog.
- **No commercial input of any kind.** `ProductMatchingService.find_compatible_products()`'s only inputs are a category string, market/region, and the user's own constraints — structurally, not by omission, there is no price/brand-preference/commission field anywhere in its signature or in `ProductMatch`. Proven by `tests/planning/test_formulation_safety.py::test_commercial_factors_cannot_change_safety_result`.

## Client-facing projection (Mobile V1 Phase C1) — IMPLEMENTED, TESTED

Everything above ends at `analysis_product_recommendations` (persistence). This section covers the one new read path Phase C1 added on top: `GET /api/v2/analyses/{id}`'s `product_recommendations` field, the first client ever exposed to this pipeline's output.

**The firewall this pipeline maintains is preserved all the way to the client, not just to the database.** Mobile never re-runs `ProductMatchingService`, never calls `SafetyEngine` itself, and never substitutes a different formulation for one the backend already excluded — it renders exactly the rows `commit_analysis_result()` wrote, nothing computed or chosen client-side. `test_unsafe_routine_status_never_coexists_with_concrete_recommendations` (above) is what makes "mobile never receives an UNSAFE concrete recommendation" true in the first place; Phase C1 adds no new safety logic that could weaken it, only a read/display layer on top.

**Projection** (`app/db/analysis_repository.py::get_product_recommendations()`, `app/api/v2/analyses.py::ProductRecommendationOut`): the client-facing shape is `plan_step_key`/`product_id`/`formulation_id`/`brand`/`product_name`/`safety_status`/`reason_codes`/`restrictions`/`rules_version`/`verification_date`/`rank_position` — never `id`/`user_id`/`analysis_request_id`/`created_at` (internal bookkeeping, excluded by the SQL projection itself AND, independently, by `ProductRecommendationOut`'s own field allowlist — the same belt-and-suspenders pattern `get_measurements()` already used for `analysis_measurements`).

**`rank_position` is transport-only provenance, never a ranking claim.** It reflects `ProductMatchingService`'s "compatibility ordering" (see "What this does not claim" above) — mobile is documented and tested to never render it as "#1 product"/"best product"/"top ranked product" (`product-recommendation-presenter.ts`'s own module docstring; no such copy exists anywhere in `ProductRecommendationCard`).

**Display metadata provenance**: `brand`/`product_name`/`verification_date` prefer the row's own historical snapshot (`brand_name_snapshot`/`product_name_snapshot`/`verification_date_snapshot`, migration `366861ec262d` — see `ANALYSIS_DATA_MODEL.md`) and fall back to a read-only join against the *current* catalog only when the snapshot is `NULL` (pre-migration rows). This is a read-time display convenience, never a write, and never a fabricated verification date or invented display name — an unresolvable field is `null`, and the client (`getProductDisplayLabel()`) renders "Product details unavailable" rather than inventing one. Proven by `tests/analysis/test_analysis_repository.py::test_get_product_recommendations_persists_and_prefers_display_snapshot` (snapshot wins even after the current catalog is mutated) and `::test_get_product_recommendations_falls_back_to_current_catalog_when_snapshot_is_null`.

**No-match stays a valid state, never an error.** An empty `product_recommendations` list alongside a non-empty `am_routine`/`pm_routine` is Phase 11's own documented fallback behavior, unchanged by this section — the mobile client renders the abstract routine step normally and a "No specific product match is available for this step yet." notice, never a client-chosen substitute (`getRecommendationForRoutineStep()` returning `null`, `NoProductMatchNotice`).
