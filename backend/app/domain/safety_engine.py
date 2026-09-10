"""
Dedicated safety subsystem, replacing the safety-adjacent logic that
used to live entirely inline inside PlanService._apply_compatibility_constraints
(pregnancy/nursing exclusion) with no machine-readable reason, no
inspectable decision record, and no allergy/avoid-ingredient
enforcement at all.

Every decision this engine makes is a plain, serializable
SafetyDecision -- inspectable and persistable (as JSON) without any
special-casing.

evaluate_product_formulation() (product/usage foundation pass) is the
graduation from evaluate_offer()'s static, category-level profiles to
real per-formulation evaluation against the normalized catalog
(app/db/catalog_repository.py) -- real ingredients, real ingredient-
level rules, real pairwise interactions. It is deliberately additive,
not a replacement of evaluate_offer()/evaluate_priorities(): nothing
yet matches PlanService's abstract recommended categories to concrete
catalog products (that is ranking/matching work, explicitly out of
scope for this pass), so PlanService still calls evaluate_offer()
against product_safety.py's category profiles. This method is the
seam a future product-matching/ranking step wires into.

Critical invariant, structural rather than merely documented:
evaluate_product_formulation()'s signature takes only a formulation_id
and the user's own constraints -- it has no brand/price/popularity/
commercial-ranking input of any kind, so there is no code path by
which a commercial factor could influence its result. Proven, not just
asserted, by
tests/planning/test_formulation_safety.py::test_commercial_factors_cannot_change_safety_result.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

import asyncpg

from app.db.catalog_repository import (
    get_active_rules_for_ingredients,
    get_formulation_by_id,
    get_formulation_ingredients,
    get_interactions_within,
    resolve_ingredient,
)
from app.domain.priorities import CompatibilityRule, PriorityDef
from app.domain.product_safety import get_safety_profile

RULES_VERSION = "1.0"

# Machine-readable reason codes. Every reason a SafetyDecision can carry
# must be one of these -- never an ad hoc string invented at a call site.
ALLERGY_CONFLICT = "ALLERGY_CONFLICT"
USER_AVOID_INGREDIENT = "USER_AVOID_INGREDIENT"
SENSITIVE_SKIN_INTENSITY_LIMIT = "SENSITIVE_SKIN_INTENSITY_LIMIT"
PREGNANCY_RESTRICTION = "PREGNANCY_RESTRICTION"
NURSING_RESTRICTION = "NURSING_RESTRICTION"
ACTIVE_INTERACTION_CONFLICT = "ACTIVE_INTERACTION_CONFLICT"
BARRIER_RECOVERY_CONFLICT = "BARRIER_RECOVERY_CONFLICT"
MAX_FREQUENCY_EXCEEDED = "MAX_FREQUENCY_EXCEEDED"
# Not in the original brief list, but required by its own explicit rule:
# "Missing information must not silently mean safe." A candidate with
# no safety profile at all must fail closed with a distinct, honest
# reason code -- not be silently allowed as if it had been checked.
SAFETY_DATA_UNAVAILABLE = "SAFETY_DATA_UNAVAILABLE"
# Formulation-level equivalent of SAFETY_DATA_UNAVAILABLE: a
# formulation that doesn't exist, or exists but has zero recorded
# ingredients. Kept as its own distinct reason code (rather than
# reusing SAFETY_DATA_UNAVAILABLE) because it corresponds to its own
# distinct SafetyStatus, INSUFFICIENT_DATA -- see that status's own
# docstring for why "unknown" must never collapse into "safe".
UNKNOWN_FORMULATION = "UNKNOWN_FORMULATION"
# A formulation exists and has recorded ingredients, but the catalog
# ingestion/admin process has not marked that list COMPLETE (it is
# PARTIAL or UNKNOWN -- see migration ac641537d224). One or more
# recorded ingredients is not sufficient grounds to treat the
# disclosure as exhaustive.
INCOMPLETE_FORMULATION_DATA = "INCOMPLETE_FORMULATION_DATA"
# The user has an allergy/avoid entry (user_ingredient_constraints)
# that could not be resolved to a canonical ingredient. Specific-
# product recommendation must fail closed rather than silently
# evaluate as though the unresolved entry didn't exist -- Part I,
# Phase 4.
UNRESOLVED_ALLERGY_CONSTRAINT = "UNRESOLVED_ALLERGY_CONSTRAINT"
UNRESOLVED_AVOID_CONSTRAINT = "UNRESOLVED_AVOID_CONSTRAINT"

# Formulation-level decision status. Four states, not a boolean --
# per this pass's own explicit "Unknown Formulation Policy":
# INSUFFICIENT_DATA is NOT equivalent to SAFE, and must be
# distinguishable from it by every caller, not collapsed into a single
# allowed/disallowed bit. `SafetyDecision.allowed` is kept as a
# derived convenience for callers that only need a boolean (and for
# the pre-existing evaluate_offer()/evaluate_priorities() callers,
# which never set `status` at all), but `status` is the actual source
# of truth for any new caller of evaluate_product_formulation().
SAFE = "SAFE"
RESTRICTED = "RESTRICTED"
UNSAFE = "UNSAFE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class SafetyDecision:
    candidate_type: str  # "priority" | "product_category" | "product_formulation"
    candidate_id: str
    allowed: bool
    status: Optional[str] = None  # SAFE | RESTRICTED | UNSAFE | INSUFFICIENT_DATA; set by evaluate_product_formulation()
    restrictions: Dict[str, Any] = field(default_factory=dict)
    reason_codes: List[str] = field(default_factory=list)
    rules_version: str = RULES_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_type": self.candidate_type,
            "candidate_id": self.candidate_id,
            "allowed": self.allowed,
            "status": self.status,
            "restrictions": self.restrictions,
            "reason_codes": self.reason_codes,
            "rules_version": self.rules_version,
        }


def _mark_exclude_action_formulation(restrictions: Dict[str, Any], formulation_id: UUID) -> None:
    """Records, in a form the routine builder (recommendation_service's
    conflict-resolution pass) can act on directly, that this
    formulation -- on its own, independent of any pairwise interaction
    -- is why the routine is UNSAFE (an EXCLUDE-action MAX_FREQUENCY or
    BARRIER_RECOVERY rule). Kept as its own dedicated restriction key
    rather than overloading frequency_caps_exceeded/
    barrier_recovery_conflicts (whose existing shape several tests
    already assert on): those two stay restriction-only records of
    what was compared, regardless of action; this key is specifically
    "the routine builder must drop this formulation's concrete product
    to have any chance of resolving to non-UNSAFE."""
    ids = restrictions.setdefault("exclude_action_formulation_ids", [])
    fid = str(formulation_id)
    if fid not in ids:
        ids.append(fid)


@dataclass(frozen=True)
class ProposedRoutineEntry:
    """One formulation's place in a proposed routine, as far as
    evaluate_routine_safety() needs to know about it -- not the full
    routine-step shape PlanService/ProductMatchingService build."""
    formulation_id: UUID
    proposed_weekly_frequency: int
    daypart: str = "BOTH"  # "AM" | "PM" | "BOTH"


@dataclass(frozen=True)
class SafetyUsageContext:
    """Real, currently-known usage state that formulation-level
    evaluation must never invent (Part I, Phase 6). barrier_recovery_
    active defaults False -- there is no signal producing True yet in
    this pass (that requires a future irritation/outcome-tracking
    capability); wiring a real one in later requires no change to
    evaluate_routine_safety() itself, only to whatever constructs this
    context."""
    barrier_recovery_active: bool = False


class SafetyEngine:
    def evaluate_priorities(
        self,
        priority_ids: List[str],
        priority_defs_by_id: Dict[str, PriorityDef],
        constraints: Dict[str, Any],
    ) -> List[SafetyDecision]:
        """One SafetyDecision per candidate priority. Currently the only
        exclusion at this level is pregnancy/nursing -- other
        constraints (allergies, sensitive skin) act on product
        categories via evaluate_offer(), not on the priority/concern
        itself (Phase 15 keeps the concern visible either way)."""
        decisions = []
        for pid in priority_ids:
            priority_def = priority_defs_by_id[pid]
            reason_codes: List[str] = []

            is_pregnancy_restricted = CompatibilityRule.PREGNANCY_RESTRICTED in priority_def.compatibility_restrictions
            if constraints.get("is_pregnant") and is_pregnancy_restricted:
                reason_codes.append(PREGNANCY_RESTRICTION)
            if constraints.get("is_nursing") and is_pregnancy_restricted:
                reason_codes.append(NURSING_RESTRICTION)

            decisions.append(SafetyDecision(
                candidate_type="priority",
                candidate_id=pid,
                allowed=len(reason_codes) == 0,
                reason_codes=reason_codes,
            ))
        return decisions

    def evaluate_offer(self, category: str, constraints: Dict[str, Any]) -> SafetyDecision:
        """Evaluates one product-category candidate (see
        app/domain/product_safety.py for why this is category-level,
        not per-SKU) against the user's allergies, avoid_ingredients,
        pregnancy/nursing status, and sensitive skin."""
        profile = get_safety_profile(category)
        if profile is None:
            return SafetyDecision(
                candidate_type="product_category",
                candidate_id=category,
                allowed=False,
                reason_codes=[SAFETY_DATA_UNAVAILABLE],
            )

        reason_codes: List[str] = []
        restrictions: Dict[str, Any] = {}

        user_allergies = {a.strip().lower() for a in constraints.get("allergies", []) if a.strip()}
        user_avoid = {a.strip().lower() for a in constraints.get("avoid_ingredients", []) if a.strip()}
        profile_allergens = {a.lower() for a in profile.allergens}
        profile_ingredients = {i.lower() for i in profile.ingredient_names}

        if user_allergies & profile_allergens:
            reason_codes.append(ALLERGY_CONFLICT)
        if user_avoid & profile_ingredients:
            reason_codes.append(USER_AVOID_INGREDIENT)
        if constraints.get("is_pregnant") and profile.pregnancy_policy == "restricted":
            reason_codes.append(PREGNANCY_RESTRICTION)
        if constraints.get("is_nursing") and profile.nursing_policy == "restricted":
            reason_codes.append(NURSING_RESTRICTION)

        # Sensitive skin does not exclude the category outright -- it
        # restricts frequency instead. This is what actually makes
        # Phase 14 real: a downstream disclaimer is only allowed to
        # claim an adjustment happened if this restriction is present.
        if constraints.get("has_sensitive_skin") and (profile.retinoid_family or profile.acids):
            restrictions["maximum_weekly_frequency"] = 2
            reason_codes.append(SENSITIVE_SKIN_INTENSITY_LIMIT)

        excluding_reasons = {ALLERGY_CONFLICT, USER_AVOID_INGREDIENT, PREGNANCY_RESTRICTION, NURSING_RESTRICTION}
        allowed = not (excluding_reasons & set(reason_codes))

        return SafetyDecision(
            candidate_type="product_category",
            candidate_id=category,
            allowed=allowed,
            restrictions=restrictions,
            reason_codes=reason_codes,
        )

    async def evaluate_product_formulation(
        self,
        pool: asyncpg.Pool,
        formulation_id: UUID,
        constraints: Dict[str, Any],
    ) -> SafetyDecision:
        """Real, per-formulation evaluation against the normalized
        catalog: User constraints + product formulation + ingredient
        rules + within-formulation ingredient interactions ->
        SafetyDecision. Formulation-level, not per-category -- see this
        module's own docstring for how this composes with (not
        replaces) evaluate_offer(). Cross-*product* interactions and
        actual-schedule-dependent restrictions (MAX_FREQUENCY_EXCEEDED,
        BARRIER_RECOVERY_CONFLICT) are ROUTINE safety, not formulation
        safety -- see evaluate_routine_safety() below and Part I,
        Phase 7's FORMULATION vs ROUTINE distinction.

        Unknown Formulation Policy, tightened this pass (Phase 1/2): a
        formulation cannot return SAFE or RESTRICTED unless its safety
        data is sufficiently complete. Three independent things all
        collapse to status=INSUFFICIENT_DATA, never SAFE:
          - the formulation doesn't exist, or has zero recorded
            ingredients (UNKNOWN_FORMULATION -- unchanged from before);
          - it has ingredients but ingredient_data_status is PARTIAL or
            UNKNOWN, not COMPLETE (INCOMPLETE_FORMULATION_DATA -- new);
          - the calling user has an unresolved allergy/avoid constraint
            (UNRESOLVED_ALLERGY_CONSTRAINT/UNRESOLVED_AVOID_CONSTRAINT --
            new, Phase 4). This is a *user*-level precondition, not a
            formulation-level one, but it gates the exact same output:
            a specific product can't honestly be called safe for a
            user whose own constraints aren't fully known. Category-
            level guidance (evaluate_offer()) is unaffected -- this
            gate is specific-formulation-recommendation only, per this
            pass's own explicit instruction.

        constraints carries two new optional boolean keys this pass,
        alongside the pre-existing ones (allergies/avoid_ingredients/
        is_pregnant/is_nursing/has_sensitive_skin):
        has_unresolved_allergy_constraint / has_unresolved_avoid_constraint
        -- computed by the caller via
        app/db/user_constraint_repository.py's has_unresolved_constraints()
        (typically once per request, not per candidate formulation)."""
        formulation = await get_formulation_by_id(pool, formulation_id)
        if formulation is None:
            return SafetyDecision(
                candidate_type="product_formulation",
                candidate_id=str(formulation_id),
                allowed=False,
                status=INSUFFICIENT_DATA,
                reason_codes=[UNKNOWN_FORMULATION],
            )

        formulation_ingredients = await get_formulation_ingredients(pool, formulation_id)

        insufficient_reasons: List[str] = []
        if not formulation_ingredients:
            insufficient_reasons.append(UNKNOWN_FORMULATION)
        elif formulation.get("ingredient_data_status") != "COMPLETE":
            insufficient_reasons.append(INCOMPLETE_FORMULATION_DATA)
        if constraints.get("has_unresolved_allergy_constraint"):
            insufficient_reasons.append(UNRESOLVED_ALLERGY_CONSTRAINT)
        if constraints.get("has_unresolved_avoid_constraint"):
            insufficient_reasons.append(UNRESOLVED_AVOID_CONSTRAINT)

        if insufficient_reasons:
            return SafetyDecision(
                candidate_type="product_formulation",
                candidate_id=str(formulation_id),
                allowed=False,
                status=INSUFFICIENT_DATA,
                reason_codes=insufficient_reasons,
            )

        ingredient_ids = [fi["ingredient_id"] for fi in formulation_ingredients]
        rules = await get_active_rules_for_ingredients(pool, ingredient_ids)
        interactions = await get_interactions_within(pool, ingredient_ids)

        reason_codes: List[str] = []
        restrictions: Dict[str, Any] = {}
        unsafe = False

        # Allergy/avoid-ingredient conflicts are resolved through the
        # exact same alias-resolution path every other catalog lookup
        # uses (app/db/catalog_repository.py's resolve_ingredient) --
        # a user who typed "Vitamin C" correctly conflicts with a
        # formulation whose ingredient's canonical name is "Ascorbic
        # Acid", as long as "vitamin c" is a registered alias for it.
        # An unresolvable free-text entry matches nothing at this
        # per-candidate step -- it was already handled, globally, by
        # the has_unresolved_*_constraint gate above.
        ingredient_id_set = set(ingredient_ids)

        async def _resolve_ids(raw_names: Sequence[str]) -> set:
            resolved_ids = set()
            for raw in raw_names:
                if not raw.strip():
                    continue
                resolved = await resolve_ingredient(pool, raw)
                if resolved is not None:
                    resolved_ids.add(resolved["id"])
            return resolved_ids

        user_allergy_ids = await _resolve_ids(constraints.get("allergies", []))
        user_avoid_ids = await _resolve_ids(constraints.get("avoid_ingredients", []))

        if user_allergy_ids & ingredient_id_set:
            reason_codes.append(ALLERGY_CONFLICT)
            unsafe = True
        if user_avoid_ids & ingredient_id_set:
            reason_codes.append(USER_AVOID_INGREDIENT)
            unsafe = True

        # Ingredient-intrinsic rules (pregnancy/nursing/sensitive-skin
        # restrictions on a *specific ingredient*), as opposed to the
        # allergy/avoid checks above, which are about the *user's* own
        # declared list, not an ingredient-table fact.
        for rule in rules:
            rule_type = rule["rule_type"]
            action = rule["action"]
            if rule_type == "PREGNANCY" and constraints.get("is_pregnant"):
                reason_codes.append(PREGNANCY_RESTRICTION)
                if action == "EXCLUDE":
                    unsafe = True
            elif rule_type == "NURSING" and constraints.get("is_nursing"):
                reason_codes.append(NURSING_RESTRICTION)
                if action == "EXCLUDE":
                    unsafe = True
            elif rule_type == "SENSITIVE_SKIN" and constraints.get("has_sensitive_skin"):
                reason_codes.append(SENSITIVE_SKIN_INTENSITY_LIMIT)
                cap = rule.get("parameters", {}).get("maximum_weekly_frequency", 2)
                restrictions["maximum_weekly_frequency"] = cap
            elif rule_type == "MAX_FREQUENCY":
                # Fixed this pass (Phase 5): a rule saying "maximum N
                # uses per week" is a *restriction* to surface, not a
                # violation -- MAX_FREQUENCY_EXCEEDED is never emitted
                # here, only by evaluate_routine_safety() once an
                # actual proposed schedule has been compared against
                # this cap. No cap parameter at all means the rule
                # carries no enforceable threshold; nothing is surfaced.
                cap = rule.get("parameters", {}).get("maximum_weekly_frequency")
                if cap is not None:
                    restrictions.setdefault("ingredient_frequency_caps", []).append({
                        "ingredient_id": str(rule["ingredient_id"]),
                        "maximum_weekly_frequency": cap,
                        "action": action,
                    })
            elif rule_type == "BARRIER_RECOVERY":
                # Fixed this pass (Phase 6): never emitted here at all
                # -- whether barrier recovery is *currently active* for
                # this user is routine/usage-context state formulation-
                # level evaluation has no access to and must not
                # invent. Only surfaced as a restriction the routine
                # builder can act on if/when that context says it's
                # active (evaluate_routine_safety()).
                restrictions.setdefault("barrier_recovery_rules", []).append({
                    "ingredient_id": str(rule["ingredient_id"]),
                    "action": action,
                })
            elif rule_type in ("PHOTOSENSITIVITY", "IRRITATION"):
                # Advisory-only in this pass -- no dedicated top-level
                # reason code exists for these in the brief's list, so
                # they're recorded as a restriction, not fabricated
                # into a new unlisted reason code.
                restrictions.setdefault("advisory_rule_types", [])
                if rule_type not in restrictions["advisory_rule_types"]:
                    restrictions["advisory_rule_types"].append(rule_type)

        if interactions:
            reason_codes.append(ACTIVE_INTERACTION_CONFLICT)
            restrictions["interactions"] = [
                {
                    "ingredient_a_id": str(i["ingredient_a_id"]),
                    "ingredient_b_id": str(i["ingredient_b_id"]),
                    "interaction_type": i["interaction_type"],
                    "severity": i["severity"],
                    "reason_code": i["reason_code"],
                    "recommendation": i["recommendation"],
                    "recommended_action": i["recommended_action"],
                }
                for i in interactions
            ]
            if any(i["severity"] in ("HIGH", "CRITICAL") for i in interactions):
                unsafe = True

        if unsafe:
            status = UNSAFE
        elif reason_codes:
            status = RESTRICTED
        else:
            status = SAFE

        return SafetyDecision(
            candidate_type="product_formulation",
            candidate_id=str(formulation_id),
            allowed=status in (SAFE, RESTRICTED),
            status=status,
            restrictions=restrictions,
            reason_codes=reason_codes,
        )

    async def evaluate_routine_safety(
        self,
        pool: asyncpg.Pool,
        entries: List[ProposedRoutineEntry],
        usage_context: SafetyUsageContext,
    ) -> SafetyDecision:
        """ROUTINE safety (Part I, Phase 7/8): can these formulations be
        used together, in this proposed schedule? Distinct from
        FORMULATION safety (evaluate_product_formulation, above), which
        only answers "can this one formulation be considered for this
        user at all", with no notion of a schedule or of what else is
        in the routine.

        Three things only routine-level evaluation can honestly decide,
        each requiring information formulation-level evaluation
        deliberately doesn't have:
          - MAX_FREQUENCY_EXCEEDED: only once `entry.proposed_weekly_
            frequency` is actually compared against a real
            maximum_weekly_frequency rule parameter on one of that
            entry's own ingredients -- never merely because such a rule
            exists (that was last pass's bug, fixed here).
          - BARRIER_RECOVERY_CONFLICT: only when
            usage_context.barrier_recovery_active is True. This pass
            does not invent that state -- it is False by default, and
            wired from a real signal only once one exists (a future
            irritation/outcome-tracking pass); until then this branch
            is inert by construction, not merely by convention.
          - ACTIVE_INTERACTION_CONFLICT across *different* formulations
            in the routine (cross-product), via the union of every
            entry's own ingredient set -- app/db/catalog_repository.py's
            get_interactions_within() is reused unmodified: it was
            already written to accept any ingredient-ID set, not just
            one formulation's own.

        Interaction severity is read from `recommended_action`
        (migration 32231ea81bb5), not blindly treated as exclusion:
        EXCLUDE_COMBINATION marks the routine UNSAFE; SEPARATE_DAYPART/
        ALTERNATE_DAYS/REDUCE_FREQUENCY are RESTRICTED (the routine
        builder must adjust the schedule to satisfy them -- Phase 12);
        ADVISORY never restricts anything, only informs."""
        reason_codes: List[str] = []
        restrictions: Dict[str, Any] = {}
        unsafe = False

        formulation_ingredient_ids: Dict[str, List[UUID]] = {}
        all_ingredient_ids: List[UUID] = []
        for entry in entries:
            fi = await get_formulation_ingredients(pool, entry.formulation_id)
            ids = [x["ingredient_id"] for x in fi]
            formulation_ingredient_ids[str(entry.formulation_id)] = ids
            all_ingredient_ids.extend(ids)

        unique_ids = list({i for i in all_ingredient_ids})
        rules = await get_active_rules_for_ingredients(pool, unique_ids)

        for entry in entries:
            entry_ids = set(formulation_ingredient_ids[str(entry.formulation_id)])

            for rule in rules:
                if rule["ingredient_id"] not in entry_ids:
                    continue

                if rule["rule_type"] == "MAX_FREQUENCY":
                    cap = rule.get("parameters", {}).get("maximum_weekly_frequency")
                    if cap is not None and entry.proposed_weekly_frequency > cap:
                        if MAX_FREQUENCY_EXCEEDED not in reason_codes:
                            reason_codes.append(MAX_FREQUENCY_EXCEEDED)
                        restrictions.setdefault("frequency_caps_exceeded", []).append({
                            "formulation_id": str(entry.formulation_id),
                            "maximum_weekly_frequency": cap,
                            "proposed_weekly_frequency": entry.proposed_weekly_frequency,
                        })
                        if rule["action"] == "EXCLUDE":
                            unsafe = True
                            _mark_exclude_action_formulation(restrictions, entry.formulation_id)

                elif rule["rule_type"] == "BARRIER_RECOVERY" and usage_context.barrier_recovery_active:
                    if BARRIER_RECOVERY_CONFLICT not in reason_codes:
                        reason_codes.append(BARRIER_RECOVERY_CONFLICT)
                    restrictions.setdefault("barrier_recovery_conflicts", []).append(str(entry.formulation_id))
                    if rule["action"] == "EXCLUDE":
                        unsafe = True
                        _mark_exclude_action_formulation(restrictions, entry.formulation_id)

        interactions = await get_interactions_within(pool, unique_ids)
        if interactions:
            interaction_details = []
            for interaction in interactions:
                action = interaction["recommended_action"]
                if action == "EXCLUDE_COMBINATION":
                    unsafe = True
                elif action in ("SEPARATE_DAYPART", "ALTERNATE_DAYS", "REDUCE_FREQUENCY"):
                    pass  # restriction, not necessarily unsafe -- routine builder must satisfy it
                # ADVISORY: no schedule effect at all, informational only.
                interaction_details.append({
                    "ingredient_a_id": str(interaction["ingredient_a_id"]),
                    "ingredient_b_id": str(interaction["ingredient_b_id"]),
                    "interaction_type": interaction["interaction_type"],
                    "severity": interaction["severity"],
                    "recommended_action": action,
                    "recommendation": interaction["recommendation"],
                })
            if ACTIVE_INTERACTION_CONFLICT not in reason_codes:
                reason_codes.append(ACTIVE_INTERACTION_CONFLICT)
            restrictions["interactions"] = interaction_details

        if unsafe:
            status = UNSAFE
        elif reason_codes:
            status = RESTRICTED
        else:
            status = SAFE

        return SafetyDecision(
            candidate_type="routine",
            candidate_id="routine",
            allowed=status in (SAFE, RESTRICTED),
            status=status,
            restrictions=restrictions,
            reason_codes=reason_codes,
        )
