import { Platform } from "react-native";

import {
  REVENUECAT_ANDROID_API_KEY,
  REVENUECAT_ENABLED,
  REVENUECAT_IOS_API_KEY,
} from "@/constants/config";

/**
 * Deliberately its own tiny module with NO other dependency beyond
 * `react-native`'s `Platform` and the public config constants -- kept
 * separate from revenuecat-context.tsx (which transitively pulls in
 * the auth/session layer and the native RevenueCat SDK) specifically
 * so this pure derivation is unit-testable without mocking any of
 * that (see tests/billing/compute-availability.test.ts).
 */
export type RevenueCatAvailability =
  | "DISABLED" // EXPO_PUBLIC_REVENUECAT_ENABLED is not "true"
  | "WEB_UNSUPPORTED" // Platform.OS === "web" -- no native purchasing in C2
  | "MISSING_KEY" // enabled, native, but no public SDK key configured for this platform
  | "READY";

export function computeAvailability(): RevenueCatAvailability {
  if (!REVENUECAT_ENABLED) return "DISABLED";
  if (Platform.OS === "web") return "WEB_UNSUPPORTED";
  const key = Platform.OS === "ios" ? REVENUECAT_IOS_API_KEY : REVENUECAT_ANDROID_API_KEY;
  if (!key) return "MISSING_KEY";
  return "READY";
}
