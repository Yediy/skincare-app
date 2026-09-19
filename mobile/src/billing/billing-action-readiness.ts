import type { BillingStatus } from "@/api/billing-api";

import type { RevenueCatAvailability } from "./revenuecat-availability";

/**
 * Independent-review Blocker 2/"BILLING ACTION READINESS": a single,
 * pure, exhaustively-testable gate deciding whether native purchase
 * actions (paywall/restore/Customer Center) may run at all. Replaces
 * hand-written boolean checks scattered across the subscription
 * screen -- every reason a purchase action must stay unavailable is
 * named here, in one place.
 *
 * Deliberately does NOT gate whether existing premium access is
 * DISPLAYED -- an existing server entitlement must always render
 * regardless of this function's result (see app/(app)/subscription.tsx).
 * This only gates whether the user may attempt a NEW native purchase
 * action right now.
 */
export type BillingActionReadiness =
  | "READY"
  | "MOBILE_UNAVAILABLE"
  | "SERVER_STATUS_UNAVAILABLE"
  | "BACKEND_DISABLED"
  | "IDENTITY_NOT_CONFIGURED"
  | "IDENTITY_MISMATCH"
  | "ENTITLEMENT_MISMATCH";

export function computeBillingActionReadiness(params: {
  mobileAvailability: RevenueCatAvailability;
  configuredUserId: string | null;
  mobileEntitlementId: string;
  serverStatus: BillingStatus | null | undefined;
}): BillingActionReadiness {
  const { mobileAvailability, configuredUserId, mobileEntitlementId, serverStatus } = params;

  if (mobileAvailability !== "READY") return "MOBILE_UNAVAILABLE";
  if (!serverStatus) return "SERVER_STATUS_UNAVAILABLE";
  if (!serverStatus.billing_enabled) return "BACKEND_DISABLED";
  if (!configuredUserId) return "IDENTITY_NOT_CONFIGURED";
  if (configuredUserId !== serverStatus.app_user_id) return "IDENTITY_MISMATCH";
  if (mobileEntitlementId !== serverStatus.entitlement_identifier) return "ENTITLEMENT_MISMATCH";
  return "READY";
}
