/**
 * Skin goal options offered during onboarding. Every id here is a
 * literal key from the backend's own concern vocabulary
 * (backend/app/domain/priorities.py::PRIORITIES) -- never an invented
 * label -- so a self-reported goal always means something the
 * planning/recommendation system can actually name.
 *
 * These are stored (PUT /profile skin_goals) as plain self-reported
 * metadata. They do not yet influence scoring or recommendations --
 * PlanService/SafetyEngine don't read this field. Copy in the goals
 * screen must not imply otherwise.
 */
export type SkinGoalId =
  | "EVENNESS_TONE"
  | "REDNESS_CONTROL"
  | "OIL_CONTROL"
  | "TEXTURE_SMOOTHING"
  | "UNDER_EYE_SHADOWS";

export type SkinGoalOption = {
  id: SkinGoalId;
  label: string;
  description: string;
};

export const SKIN_GOAL_OPTIONS: SkinGoalOption[] = [
  {
    id: "EVENNESS_TONE",
    label: "Even out skin tone",
    description: "Discoloration, uneven pigmentation, and dullness",
  },
  {
    id: "REDNESS_CONTROL",
    label: "Reduce redness",
    description: "Facial redness and irritation-prone skin",
  },
  {
    id: "OIL_CONTROL",
    label: "Control oiliness",
    description: "Excess shine and sebum",
  },
  {
    id: "TEXTURE_SMOOTHING",
    label: "Smooth texture",
    description: "Roughness and visible texture",
  },
  {
    id: "UNDER_EYE_SHADOWS",
    label: "Brighten under-eyes",
    description: "Under-eye shadows and tired appearance",
  },
];

export const SKIN_GOAL_IDS: readonly string[] = SKIN_GOAL_OPTIONS.map((g) => g.id);
