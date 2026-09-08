"""
Dedicated safety subsystem, replacing the safety-adjacent logic that
used to live entirely inline inside PlanService._apply_compatibility_constraints
(pregnancy/nursing exclusion) with no machine-readable reason, no
inspectable decision record, and no allergy/avoid-ingredient
enforcement at all.

Every decision this engine makes is a plain, serializable
SafetyDecision -- inspectable and persistable (as JSON) without any
special-casing.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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


@dataclass
class SafetyDecision:
    candidate_type: str  # "priority" | "product_category"
    candidate_id: str
    allowed: bool
    restrictions: Dict[str, Any] = field(default_factory=dict)
    reason_codes: List[str] = field(default_factory=list)
    rules_version: str = RULES_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_type": self.candidate_type,
            "candidate_id": self.candidate_id,
            "allowed": self.allowed,
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
