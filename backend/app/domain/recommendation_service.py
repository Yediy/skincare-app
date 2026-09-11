"""Part I, Phase 11-13: the actual P0 closure. Orchestrates
ProductMatchingService + SafetyEngine.evaluate_routine_safety() on top
of PlanService's abstract plan, so that every concrete product
recommendation this application ever returns has passed
evaluate_product_formulation() AND routine-level safety -- never
produced from evaluate_offer(category) alone. See
PRODUCT_RECOMMENDATION_PIPELINE.md for the full call-chain diagram.

Deliberately its own module, not folded into PlanService (which stays
synchronous and category-only) or ProductMatchingService (which knows
nothing about routines/schedules, only single-category candidate
lookup): this is the piece that actually crosses the sync/async
boundary and does cross-step reasoning, exactly once, in one place.

Fallback behavior (Phase 11's explicit requirement): if
ProductMatchingService finds no compatible product for a step, that
step's `product_recommendations` simply stays empty -- the abstract
routine/category step itself is untouched (it was already confirmed
category-level-safe by PlanService.generate_plan() before this
function ever runs). Never a generic/popular/default/affiliate
substitute.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import asyncpg

from app.db.catalog_repository import get_formulation_ingredients
from app.domain.product_matching_service import ProductMatch, ProductMatchingService
from app.observability import events as observability_events
from app.domain.safety_engine import (
    ProposedRoutineEntry,
    SafetyDecision,
    SafetyEngine,
    SafetyUsageContext,
    UNSAFE,
)

# Real, documented default weekly frequency PlanService's own existing
# text-based routine cadence implies, translated into a number routine
# safety evaluation can actually compare a rule's parameters against --
# not a fabricated clinical value. PlanService's PM-routine builder
# already writes explicit "Monday/Wednesday/Friday" (3x/week) text for
# retinoid/chemical_exfoliant categories when no sharper restriction
# applies (app/services/plan_service.py::_build_pm_routine); everything
# else in a routine is once-daily by construction (one AM step, one PM
# step).
DAILY_WEEKLY_FREQUENCY = 7
ACTIVE_INGREDIENT_DEFAULT_WEEKLY_FREQUENCY = 3
_ACTIVE_INGREDIENT_CATEGORIES = {"retinoid", "chemical_exfoliant"}


def _proposed_weekly_frequency(category: str, category_level_restrictions: Dict[str, Dict[str, Any]]) -> int:
    cap = category_level_restrictions.get(category, {}).get("maximum_weekly_frequency")
    if cap is not None:
        return cap
    if category in _ACTIVE_INGREDIENT_CATEGORIES:
        return ACTIVE_INGREDIENT_DEFAULT_WEEKLY_FREQUENCY
    return DAILY_WEEKLY_FREQUENCY


@dataclass
class StepProductRecommendation:
    """Provenance for one routine step's chosen product -- exactly the
    fields Part I, Phase 13 requires be preservable so the
    recommendation can later be reconstructed/audited. Part III, Phase
    18's analysis_product_recommendations table persists this shape
    once analysis results themselves are persisted."""
    plan_step_key: str  # e.g. "AM:2" (daypart:step_number)
    product_id: str
    formulation_id: str
    brand: str
    product_name: str
    rank_position: int
    safety_status: str
    reason_codes: List[str]
    restrictions: Dict[str, Any]
    rules_version: str
    verification_date: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_step_key": self.plan_step_key,
            "product_id": self.product_id,
            "formulation_id": self.formulation_id,
            "brand": self.brand,
            "product_name": self.product_name,
            "rank_position": self.rank_position,
            "safety_status": self.safety_status,
            "reason_codes": self.reason_codes,
            "restrictions": self.restrictions,
            "rules_version": self.rules_version,
            "verification_date": self.verification_date,
        }


@dataclass
class RecommendationResult:
    plan: Dict[str, Any]  # PlanService's plan, mutated in place with product_recommendations/enforced_weekly_frequency
    routine_safety_decision: SafetyDecision
    product_recommendations: List[StepProductRecommendation] = field(default_factory=list)


async def apply_product_matching_and_routine_safety(
    pool: asyncpg.Pool,
    plan: Dict[str, Any],
    constraints: Dict[str, Any],
    *,
    product_matching_service: ProductMatchingService,
    safety_engine: SafetyEngine,
    market_or_region: str = "global",
    usage_context: Optional[SafetyUsageContext] = None,
) -> RecommendationResult:
    usage_context = usage_context or SafetyUsageContext()
    category_level_restrictions = {
        d["candidate_id"]: d["restrictions"]
        for d in plan["metadata"]["safety_decisions"]
        if d["candidate_type"] == "product_category"
    }

    # (step dict, daypart, plan_step_key) for every real routine step.
    all_steps = (
        [(step, "AM", f"AM:{step['step_number']}") for step in plan["am_routine"]]
        + [(step, "PM", f"PM:{step['step_number']}") for step in plan["pm_routine"]]
    )

    step_matches: Dict[str, List[ProductMatch]] = {}
    for step, daypart, key in all_steps:
        step_matches[key] = await product_matching_service.find_compatible_products(
            step["product_category"], constraints, market_or_region=market_or_region, max_results=3,
        )

    def _top_choice_entries() -> Dict[str, ProposedRoutineEntry]:
        """Rebuilt every pass -- reflects whichever match is currently
        step_matches[key][0] (Phase 12's conflict-resolution loop below
        removes a step's top match on an unresolved EXCLUDE_COMBINATION
        conflict, which changes this)."""
        entries: Dict[str, ProposedRoutineEntry] = {}
        for step, daypart, key in all_steps:
            matches = step_matches[key]
            if not matches:
                continue
            freq = _proposed_weekly_frequency(step["product_category"], category_level_restrictions)
            entries[key] = ProposedRoutineEntry(
                formulation_id=matches[0].formulation_id, proposed_weekly_frequency=freq, daypart=daypart,
            )
        return entries

    # Phase 12 (P0 safety fix, this pass): enforce restrictions in the
    # actual routine. A single bounded conflict-resolution pass -- each
    # iteration drops exactly one step's top (specific-product) match
    # for each formulation this pass can attribute to the current
    # UNSAFE status, then re-evaluates, until routine safety is no
    # longer UNSAFE or nothing further can be attributed/dropped. This
    # does not chase every pathological N-way conflict graph to a
    # global optimum; it is real, terminates, and is honestly scoped,
    # not a fabricated claim of completeness -- the hard backstop below
    # is what actually guarantees the invariant regardless.
    #
    # Two independent kinds of UNSAFE contributor, both handled here:
    #   - exclude_action_formulation_ids (safety_engine's own explicit
    #     marker): a single formulation is unsafe on its own, given the
    #     proposed schedule -- an EXCLUDE-action MAX_FREQUENCY_EXCEEDED
    #     or BARRIER_RECOVERY_CONFLICT rule. Drop it unconditionally;
    #     there is no "other side" to keep.
    #   - an unresolved EXCLUDE_COMBINATION interaction: pairwise by
    #     nature -- the later-ordered step yields to the earlier one,
    #     same as before.
    routine_decision = await safety_engine.evaluate_routine_safety(pool, list(_top_choice_entries().values()), usage_context)
    guard = 0
    while routine_decision.status == UNSAFE and guard < len(all_steps) * 2:
        guard += 1
        entries_by_key = _top_choice_entries()
        if not entries_by_key:
            break  # every step has already lost its concrete product -- nothing left to drop.

        direct_exclude_formulation_ids = set(routine_decision.restrictions.get("exclude_action_formulation_ids", []))
        excluded_ingredient_ids = {
            i["ingredient_a_id"] for i in routine_decision.restrictions.get("interactions", [])
            if i["recommended_action"] == "EXCLUDE_COMBINATION"
        } | {
            i["ingredient_b_id"] for i in routine_decision.restrictions.get("interactions", [])
            if i["recommended_action"] == "EXCLUDE_COMBINATION"
        }

        direct_drop_keys: List[str] = []
        pairwise_conflict_keys: List[str] = []
        for key, entry in entries_by_key.items():
            if str(entry.formulation_id) in direct_exclude_formulation_ids:
                direct_drop_keys.append(key)
                continue
            if excluded_ingredient_ids:
                fi = await get_formulation_ingredients(pool, entry.formulation_id)
                if any(str(x["ingredient_id"]) in excluded_ingredient_ids for x in fi):
                    pairwise_conflict_keys.append(key)

        drop_keys = set(direct_drop_keys)
        if len(pairwise_conflict_keys) >= 2:
            # Keep the earliest-ordered step (AM before PM, then
            # step_number); drop the top match of every other
            # conflicting step.
            ordered = sorted(
                pairwise_conflict_keys, key=lambda k: (0 if k.startswith("AM:") else 1, int(k.split(":")[1]))
            )
            drop_keys |= set(ordered[1:])

        if not drop_keys:
            break  # UNSAFE for a reason this pass can't attribute to a specific step -- stop, don't loop forever.

        made_progress = False
        for drop_key in drop_keys:
            if step_matches[drop_key]:
                step_matches[drop_key] = step_matches[drop_key][1:]  # drop only the top choice, try the next-ranked one next pass
                made_progress = True
        if not made_progress:
            break  # every implicated step already has no candidates left to fall back to.

        routine_decision = await safety_engine.evaluate_routine_safety(pool, list(_top_choice_entries().values()), usage_context)

    # Hard backstop for the central invariant ("no concrete product
    # recommendation may survive an UNSAFE routine decision"): the
    # bounded pass above is a heuristic, not a guaranteed solver for
    # every conflict graph. If routine safety is STILL UNSAFE after it
    # gives up, no per-step attribution above can be trusted -- clear
    # every step's concrete candidates (abstract category guidance
    # untouched) rather than ever emit a recommendation on top of an
    # UNSAFE routine decision. This is the one case where matching
    # products for otherwise-uninvolved steps are also cleared, because
    # the pass above already tried and failed to attribute the UNSAFE
    # status to a smaller set.
    unresolved_unsafe_routine = routine_decision.status == UNSAFE
    if unresolved_unsafe_routine:
        for key in step_matches:
            step_matches[key] = []

    # Apply the FINAL routine decision's frequency-cap restrictions to
    # the actual routine step data -- not merely metadata prose.
    final_entries = _top_choice_entries()
    formulation_to_key = {str(e.formulation_id): key for key, e in final_entries.items()}
    key_to_step = {key: step for step, daypart, key in all_steps}

    for exceeded in routine_decision.restrictions.get("frequency_caps_exceeded", []):
        key = formulation_to_key.get(exceeded["formulation_id"])
        if key is not None:
            key_to_step[key]["enforced_weekly_frequency"] = exceeded["maximum_weekly_frequency"]

    # Attach provenance + populate product_recommendations for every
    # step whose top choice survived Phase 12's enforcement pass.
    product_recommendations: List[StepProductRecommendation] = []
    for step, daypart, key in all_steps:
        matches = step_matches[key]
        step["product_recommendations"] = [m.to_dict() for m in matches]
        if not matches:
            observability_events.no_compatible_product(analysis_id=None, category=step["product_category"])
        if matches:
            top = matches[0]
            product_recommendations.append(StepProductRecommendation(
                plan_step_key=key,
                product_id=str(top.product_id),
                formulation_id=str(top.formulation_id),
                brand=top.brand,
                product_name=top.product_name,
                rank_position=top.rank_position,
                safety_status=top.safety_status,
                reason_codes=top.safety_decision.reason_codes,
                restrictions=top.safety_decision.restrictions,
                rules_version=top.safety_decision.rules_version,
                verification_date=top.verification_date.isoformat() if top.verification_date else None,
            ))

    plan["metadata"]["routine_safety_decision"] = routine_decision.to_dict()
    # Explicit safety metadata (never silently absent) explaining why no
    # concrete product survives, whenever that's the case -- required
    # by this pass's own invariant: a caller reading only `plan` (not
    # `product_recommendations`) must still be able to tell this
    # happened, not infer it from an all-empty routine.
    plan["metadata"]["unresolved_unsafe_routine"] = unresolved_unsafe_routine
    if unresolved_unsafe_routine:
        plan["metadata"]["safety_notice"] = (
            "No compatible specific-product routine could be safely produced for this combination; "
            "abstract category guidance only. See routine_safety_decision for details."
        )

    return RecommendationResult(
        plan=plan,
        routine_safety_decision=routine_decision,
        product_recommendations=product_recommendations,
    )
