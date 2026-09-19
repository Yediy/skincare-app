import { authorizedRequest } from "@/auth/auth-client-singleton";

/**
 * Mirrors backend/app/api/v2/billing.py's BillingStatusResponse/
 * BillingSyncResponse exactly. The server is the source of truth for
 * every one of these fields -- this app never derives `hasPremiumAccess`,
 * `analysisAllowance`, or `analysesRemaining` from local RevenueCat
 * CustomerInfo (see src/billing/revenuecat-context.tsx).
 */
export type BillingStatus = {
  billing_enabled: boolean;
  app_user_id: string;
  entitlement_identifier: string;
  environment: string | null;
  projection_status: string | null;
  has_premium_access: boolean;
  will_renew: boolean | null;
  expires_at: string | null;
  analysis_allowance: number;
  period_key: string;
  analyses_used_or_reserved: number;
  analyses_remaining: number;
};

export type BillingSyncResult = BillingStatus & {
  mismatch_found: boolean;
  corrected: boolean;
};

export function getBillingStatus(): Promise<BillingStatus> {
  return authorizedRequest<BillingStatus>("/api/v2/billing/status");
}

/** POST /api/v2/billing/sync -- no request body: the user is derived
 * from the bearer token server-side, never supplied by this client. */
export function syncBillingStatus(): Promise<BillingSyncResult> {
  return authorizedRequest<BillingSyncResult>("/api/v2/billing/sync", { method: "POST" });
}
