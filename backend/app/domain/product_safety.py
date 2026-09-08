"""
Adapter around the product-category strings that already exist in
domain/priorities.py (PriorityDef.product_categories), since there is
no normalized per-product ingredient catalog in this repository yet
(no `offers` table exists at all -- confirmed directly, repeatedly,
against the live repo).

This is deliberately category-level, not per-SKU: once a real product
catalog exists, ProductSafetyProfile should be looked up per product_id
against real ingredient data, not this static category table. Treat
this as the seam where that swap happens, not as a finished catalog.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

Policy = str  # "safe" | "restricted" | "unknown"


@dataclass(frozen=True)
class ProductSafetyProfile:
    candidate_id: str
    ingredient_names: List[str]
    active_ingredients: List[str]
    allergens: List[str]
    retinoid_family: bool = False
    acids: bool = False
    fragrance: bool = False
    irritation_flags: List[str] = field(default_factory=list)
    pregnancy_policy: Policy = "unknown"
    nursing_policy: Policy = "unknown"


CATEGORY_SAFETY_PROFILES: Dict[str, ProductSafetyProfile] = {
    "retinoid": ProductSafetyProfile(
        candidate_id="retinoid",
        ingredient_names=["retinol", "retinoic acid"],
        active_ingredients=["retinol"],
        allergens=[],
        retinoid_family=True,
        irritation_flags=["photosensitizing", "drying"],
        pregnancy_policy="restricted",
        nursing_policy="restricted",
    ),
    "chemical_exfoliant": ProductSafetyProfile(
        candidate_id="chemical_exfoliant",
        ingredient_names=["glycolic acid", "salicylic acid", "lactic acid"],
        active_ingredients=["glycolic acid", "salicylic acid"],
        allergens=[],
        acids=True,
        irritation_flags=["photosensitizing"],
        pregnancy_policy="restricted",
        nursing_policy="unknown",
    ),
    "vitamin_c_serum": ProductSafetyProfile(
        candidate_id="vitamin_c_serum",
        ingredient_names=["ascorbic acid"],
        active_ingredients=["ascorbic acid"],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "niacinamide_serum": ProductSafetyProfile(
        candidate_id="niacinamide_serum",
        ingredient_names=["niacinamide"],
        active_ingredients=["niacinamide"],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "barrier_cream": ProductSafetyProfile(
        candidate_id="barrier_cream",
        ingredient_names=["ceramides", "petrolatum"],
        active_ingredients=[],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "cleanser": ProductSafetyProfile(
        candidate_id="cleanser",
        ingredient_names=["surfactants", "fragrance"],
        active_ingredients=[],
        allergens=["fragrance"],
        fragrance=True,
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "light_moisturizer": ProductSafetyProfile(
        candidate_id="light_moisturizer",
        ingredient_names=["dimethicone", "glycerin", "fragrance"],
        active_ingredients=[],
        allergens=["fragrance"],
        fragrance=True,
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "moisturizer": ProductSafetyProfile(
        candidate_id="moisturizer",
        ingredient_names=["dimethicone", "glycerin", "shea butter", "fragrance"],
        active_ingredients=[],
        allergens=["fragrance"],
        fragrance=True,
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "sunscreen": ProductSafetyProfile(
        candidate_id="sunscreen",
        ingredient_names=["zinc oxide", "avobenzone"],
        active_ingredients=[],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "caffeine_eye_serum": ProductSafetyProfile(
        candidate_id="caffeine_eye_serum",
        ingredient_names=["caffeine"],
        active_ingredients=["caffeine"],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "peptide_eye_cream": ProductSafetyProfile(
        candidate_id="peptide_eye_cream",
        ingredient_names=["peptides"],
        active_ingredients=["peptides"],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "night_cream": ProductSafetyProfile(
        candidate_id="night_cream",
        ingredient_names=["shea butter", "ceramides", "fragrance"],
        active_ingredients=[],
        allergens=["fragrance"],
        fragrance=True,
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "cooling_roller": ProductSafetyProfile(
        candidate_id="cooling_roller",
        ingredient_names=[],
        active_ingredients=[],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "beard_trim": ProductSafetyProfile(
        candidate_id="beard_trim",
        ingredient_names=[],
        active_ingredients=[],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
    "brow_grooming": ProductSafetyProfile(
        candidate_id="brow_grooming",
        ingredient_names=[],
        active_ingredients=[],
        allergens=[],
        pregnancy_policy="safe",
        nursing_policy="safe",
    ),
}


def get_safety_profile(category: str) -> Optional[ProductSafetyProfile]:
    return CATEGORY_SAFETY_PROFILES.get(category)
