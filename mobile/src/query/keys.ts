/** Stable query keys -- every hook that reads/invalidates one of
 * these resources uses the same key, defined once here. */
export const queryKeys = {
  me: ["me"] as const,
  profile: ["profile"] as const,
  consent: ["consent"] as const,
  /** Parameterized -- one analysis attempt, one key, so two different
   * analysisIds are never confused in the cache and switching between
   * them (e.g. a fresh "Analyze again") never shows the previous
   * attempt's stale result. */
  analysis: (analysisId: string) => ["analysis", analysisId] as const,
};
