import type { CustomerInfo, CustomerInfoListener, RevenueCatAdapter } from "@/billing/revenuecat-adapter";
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
  logOutCalls: number;
  restoreCalls: number;
  presentPaywallCalls: number;
  presentCustomerCenterCalls: number;
  isConfiguredValue: boolean;
  customerInfo: CustomerInfo;
  listeners: CustomerInfoListener[];
  presentPaywallResult: PAYWALL_RESULT;
  restoreResult: CustomerInfo | Error;
};

export function createFakeRevenueCatAdapter(): { adapter: RevenueCatAdapter; state: FakeAdapterState } {
  const state: FakeAdapterState = {
    configureCalls: [],
    logInCalls: [],
    logOutCalls: 0,
    restoreCalls: 0,
    presentPaywallCalls: 0,
    presentCustomerCenterCalls: 0,
    isConfiguredValue: false,
    customerInfo: createFakeCustomerInfo(),
    listeners: [],
    presentPaywallResult: PAYWALL_RESULT.CANCELLED,
    restoreResult: createFakeCustomerInfo(),
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
      return state.customerInfo;
    },
    async getCustomerInfo() {
      return state.customerInfo;
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
    },
    addCustomerInfoUpdateListener(listener) {
      state.listeners.push(listener);
    },
    removeCustomerInfoUpdateListener(listener) {
      state.listeners = state.listeners.filter((l) => l !== listener);
    },
  };

  return { adapter, state };
}
