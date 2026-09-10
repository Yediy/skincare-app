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
        rules + ingredient interactions -> SafetyDecision. Formulation-
        level, not per-category -- see this module's own docstring for
        how this composes with (not replaces) evaluate_offer().

        Unknown Formulation Policy: a formulation that doesn't exist,
        or exists with zero recorded ingredients, returns
        status=INSUFFICIENT_DATA (allowed=False) -- never SAFE. Missing
        data is a distinct, honest outcome, not a silent pass."""
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
        if not formulation_ingredients:
            return SafetyDecision(
                candidate_type="product_formulation",
                candidate_id=str(formulation_id),
                allowed=False,
                status=INSUFFICIENT_DATA,
                reason_codes=[UNKNOWN_FORMULATION],
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
        # An unresolvable free-text entry matches nothing -- it is
        # never silently treated as a match or a non-match by guessing.
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

        # Ingredient-intrinsic rules (pregnancy/nursing/sensitive-skin/
        # etc. restrictions on a *specific ingredient*), as opposed to
        # the allergy/avoid checks above, which are about the *user's*
        # own declared list, not an ingredient-table fact.
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
                restrictions["maximum_weekly_frequency"] = 2
            elif rule_type == "MAX_FREQUENCY":
                reason_codes.append(MAX_FREQUENCY_EXCEEDED)
                if action == "EXCLUDE":
                    unsafe = True
            elif rule_type == "BARRIER_RECOVERY":
                reason_codes.append(BARRIER_RECOVERY_CONFLICT)
                if action == "EXCLUDE":
                    unsafe = True
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
