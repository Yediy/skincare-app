/** Stable query keys -- every hook that reads/invalidates one of
 * these resources uses the same key, defined once here. */
export const queryKeys = {
  me: ["me"] as const,
  profile: ["profile"] as const,
  consent: ["consent"] as const,
};
