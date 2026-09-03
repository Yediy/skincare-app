from dataclasses import dataclass
from typing import Literal, List, Dict
from enum import Enum

Pillar = Literal["skin_health", "facial_harmony", "feature_definition", "vitality"]
Direction = Literal["higher_worse", "lower_worse"]
Intensity = Literal["beginner", "intermediate", "advanced"]

class CompatibilityRule(Enum):
    RETINOID_VS_EXFOLIANT = "retinoid_vs_chemical_exfoliant"
    VITAMIN_C_TIMING = "vitamin_c_timing"
    PREGNANCY_RESTRICTED = "pregnancy_restricted"
    SENSITIVITY_RESTRICTED = "sensitivity_restricted"

@dataclass(frozen=True)
class PriorityDef:
    id: str
    pillar: Pillar
    label: str
    description: str
    metric_key: str
    direction: Direction
    severity_threshold: float
    display_order: int
    default_intensity: Intensity
    product_categories: List[str]
    exercise_tags: List[str]
    lifestyle_tags: List[str]
    compatibility_restrictions: List[CompatibilityRule]
    persistence_bonus: float = 0.0

PRIORITIES: Dict[str, PriorityDef] = {
    "EVENNESS_TONE": PriorityDef(
        id="EVENNESS_TONE",
        pillar="skin_health",
        label="Improve skin tone evenness",
        description="Targets discoloration, uneven pigmentation, and dullness",
        metric_key="evenness_score",
        direction="lower_worse",
        severity_threshold=0.65,
        display_order=1,
        default_intensity="intermediate",
        product_categories=["vitamin_c_serum", "niacinamide_serum", "sunscreen"],
        exercise_tags=[],
        lifestyle_tags=["hydration", "antioxidants", "sleep"],
        compatibility_restrictions=[CompatibilityRule.VITAMIN_C_TIMING],
        persistence_bonus=0.1
    ),
    "REDNESS_CONTROL": PriorityDef(
        id="REDNESS_CONTROL",
        pillar="skin_health",
        label="Reduce redness",
        description="Targets facial redness and irritation signals",
        metric_key="redness_score",
        direction="higher_worse",
        severity_threshold=0.45,
        display_order=2,
        default_intensity="beginner",
        product_categories=["niacinamide_serum", "barrier_cream", "sunscreen"],
        exercise_tags=[],
        lifestyle_tags=["reduce_alcohol", "reduce_dairy_test", "stress"],
        compatibility_restrictions=[CompatibilityRule.SENSITIVITY_RESTRICTED],
        persistence_bonus=0.15
    ),
    "OIL_CONTROL": PriorityDef(
        id="OIL_CONTROL",
        pillar="skin_health",
        label="Control oiliness",
        description="Targets excess sebum and shine",
        metric_key="oiliness_score",
        direction="higher_worse",
        severity_threshold=0.65,
        display_order=3,
        default_intensity="intermediate",
        product_categories=["cleanser", "niacinamide_serum", "light_moisturizer"],
        exercise_tags=[],
        lifestyle_tags=["reduce_fried_foods", "hydration"],
        compatibility_restrictions=[],
        persistence_bonus=0.05
    ),
    "TEXTURE_SMOOTHING": PriorityDef(
        id="TEXTURE_SMOOTHING",
        pillar="skin_health",
        label="Smooth texture",
        description="Targets roughness and visible texture irregularities",
        metric_key="texture_score",
        direction="higher_worse",
        severity_threshold=0.55,
        display_order=4,
        default_intensity="advanced",
        product_categories=["retinoid", "chemical_exfoliant", "night_cream"],
        exercise_tags=[],
        lifestyle_tags=["sleep", "protein"],
        compatibility_restrictions=[CompatibilityRule.RETINOID_VS_EXFOLIANT, CompatibilityRule.PREGNANCY_RESTRICTED],
        persistence_bonus=0.2
    ),
    "UNDER_EYE_SHADOWS": PriorityDef(
        id="UNDER_EYE_SHADOWS",
        pillar="vitality",
        label="Improve under-eye shadows",
        description="Targets dark shadows and tired appearance under eyes",
        metric_key="under_eye_darkness",
        direction="higher_worse",
        severity_threshold=0.50,
        display_order=1,
        default_intensity="intermediate",
        product_categories=["caffeine_eye_serum", "peptide_eye_cream", "sunscreen"],
        exercise_tags=["reduce_puffiness"],
        lifestyle_tags=["sleep", "hydration", "reduce_salt_evening"],
        compatibility_restrictions=[],
        persistence_bonus=0.1
    ),
    "PUFFINESS_REDUCTION": PriorityDef(
        id="PUFFINESS_REDUCTION",
        pillar="vitality",
        label="Reduce facial puffiness",
        description="Targets morning puffiness and water retention",
        metric_key="puffiness_score",
        direction="higher_worse",
        severity_threshold=0.55,
        display_order=2,
        default_intensity="beginner",
        product_categories=["caffeine_eye_serum", "cooling_roller"],
        exercise_tags=["reduce_puffiness", "facial_massage"],
        lifestyle_tags=["reduce_salt_evening", "hydration", "sleep_elevation"],
        compatibility_restrictions=[],
        persistence_bonus=0.05
    ),
    "FEATURE_DEFINITION": PriorityDef(
        id="FEATURE_DEFINITION",
        pillar="feature_definition",
        label="Enhance feature definition",
        description="Targets jawline/cheek definition via grooming, posture, and body composition",
        metric_key="feature_definition_score",
        direction="lower_worse",
        severity_threshold=0.60,
        display_order=1,
        default_intensity="intermediate",
        product_categories=["beard_trim", "brow_grooming"],
        exercise_tags=["definition", "posture"],
        lifestyle_tags=["strength_training", "protein", "reduce_ultra_processed"],
        compatibility_restrictions=[],
        persistence_bonus=0.0
    ),
    "SYMMETRY_IMPROVEMENT": PriorityDef(
        id="SYMMETRY_IMPROVEMENT",
        pillar="facial_harmony",
        label="Improve facial symmetry",
        description="Addresses asymmetries through exercises and awareness",
        metric_key="symmetry_score",
        direction="lower_worse",
        severity_threshold=0.70,
        display_order=1,
        default_intensity="beginner",
        product_categories=[],
        exercise_tags=["symmetry", "posture"],
        lifestyle_tags=["chewing_awareness", "sleep_position"],
        compatibility_restrictions=[],
        persistence_bonus=0.0
    ),
}

PRIORITY_SCHEMA_VERSION = "2.1"
PLANNER_VERSION = "2.1.0"
