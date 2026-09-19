import type { CustomerInfo, RevenueCatAdapter } from "@/billing/revenuecat-adapter";
import { PAYWALL_RESULT } from "@/billing/revenuecat-adapter";

/**
 * Plain, duck-typed stand-in for RevenueCatAdapter -- no real
 * react-native-purchases/-ui call anywhere. Every mobile test in this
 * suite that needs billing behavior injects one of these directly
 * into RevenueCatProvider (never mocks the native SDK modules
 * themselves), matching this codebase's existing "define an
 * interface, inject a fake in tests" convention (see
 * src/auth/auth-client-singleton.ts's own docstring).
 */
export function createFakeCustomerInfo(overrides: Partial<CustomerInfo> = {}): CustomerInfo {
  return {
    entitlements: { active: {}, all: {}, verification: "NOT_REQUESTED" },
    activeSubscriptions: [],
    allPurchasedProductIdentifiers: [],
    latestExpirationDate: null,
    firstSeen: "2026-01-01T00:00:00Z",
    originalAppUserId: "test-user",
    requestDate: "2026-01-01T00:00:00Z",
    allExpirationDates: {},
    allPurchaseDates: {},
    originalApplicationVersion: null,
    originalPurchaseDate: null,
    managementURL: null,
    nonSubscriptionTransactions: [],
    ...overrides,
  } as CustomerInfo;
}

export type FakeAdapterState = {
  configureCalls: { apiKey: string; appUserID: string }[];
  logInCalls: string[];
  restoreCalls: number;
  presentPaywallCalls: number;
  presentCustomerCenterCalls: number;
  isConfiguredValue: boolean;
  presentPaywallResult: PAYWALL_RESULT;
  restoreResult: CustomerInfo | Error;
  /** Set to an Error to make the next logIn() call(s) reject --
   * simulates a failed account switch (independent-review Blocker 1). */
  logInError: Error | null;
  /** Set to an Error to make the next presentCustomerCenter() call
   * reject. */
  presentCustomerCenterError: Error | null;
};

export function createFakeRevenueCatAdapter(): { adapter: RevenueCatAdapter; state: FakeAdapterState } {
  const state: FakeAdapterState = {
    configureCalls: [],
    logInCalls: [],
    restoreCalls: 0,
    presentPaywallCalls: 0,
    presentCustomerCenterCalls: 0,
    isConfiguredValue: false,
    presentPaywallResult: PAYWALL_RESULT.CANCELLED,
    restoreResult: createFakeCustomerInfo(),
    logInError: null,
    presentCustomerCenterError: null,
  };

  const adapter: RevenueCatAdapter = {
    async isConfigured() {
      return state.isConfiguredValue;
    },
    configure({ apiKey, appUserID }) {
      state.configureCalls.push({ apiKey, appUserID });
      state.isConfiguredValue = true;
    },
    async logIn(appUserID) {
      state.logInCalls.push(appUserID);
      if (state.logInError) throw state.logInError;
      return createFakeCustomerInfo({ originalAppUserId: appUserID });
    },
    async restorePurchases() {
      state.restoreCalls += 1;
      if (state.restoreResult instanceof Error) throw state.restoreResult;
      return state.restoreResult;
    },
    async presentPaywall() {
      state.presentPaywallCalls += 1;
      return state.presentPaywallResult;
    },
    async presentCustomerCenter() {
      state.presentCustomerCenterCalls += 1;
      if (state.presentCustomerCenterError) throw state.presentCustomerCenterError;
    },
  };

  return { adapter, state };
}
