import Purchases, { type CustomerInfo, PURCHASES_ERROR_CODE } from "react-native-purchases";
import RevenueCatUI, { PAYWALL_RESULT } from "react-native-purchases-ui";

/**
 * Mobile C2's narrow, testable boundary around the native RevenueCat
 * SDK (react-native-purchases / react-native-purchases-ui) -- no
 * screen or component in this app imports either package directly;
 * everything goes through `RevenueCatAdapter`. Covers only what C2
 * actually needs (identify/switch a customer, read CustomerInfo,
 * present the paywall/Customer Center, restore purchases, subscribe
 * to CustomerInfo updates) -- nothing else on the SDK's much larger
 * surface is exposed here.
 *
 * Deliberately has NO `logOut` method -- see
 * src/billing/revenuecat-context.tsx's own docstring for why this
 * app's custom-App-User-ID model never calls the SDK's `logOut()`
 * during ordinary application sign-out (it would create an unwanted
 * anonymous `$RCAnonymousID` customer).
 *
 * Also deliberately has no CustomerInfo-update-listener surface: an
 * independent review of this file found a prior version registered
 * `addCustomerInfoUpdateListener` but nothing ever consumed the
 * CustomerInfo it captured (the subscription screen renders
 * exclusively from server billing status, never from CustomerInfo --
 * see app/(app)/subscription.tsx). Purchase, restore, and Customer
 * Center already explicitly trigger a backend sync on their own; a
 * passive listener added no real behavior, only a misleading
 * pseudo-integration. If a genuine need for CustomerInfo push updates
 * arises later, add it back with an explicit, bounded,
 * coalesced-sync consumer -- not as a dangling side effect.
 *
 * Every method signature here is exactly what the installed SDK's own
 * TypeScript definitions declare (node_modules/react-native-purchases
 * /dist/purchases.d.ts, node_modules/react-native-purchases-ui/src/
 * index.tsx) -- inspected before writing this file, never invented
 * from memory.
 */
export type { CustomerInfo };
export { PAYWALL_RESULT, PURCHASES_ERROR_CODE };

export interface RevenueCatAdapter {
  /** Whether Purchases.configure() has already been called in this
   * native process -- the SDK's own official facility (Purchases.
   * isConfigured(), confirmed present in the installed SDK's own
   * typings), used as an extra guard alongside this app's own
   * singleton coordinator (see revenuecat-context.tsx). */
  isConfigured(): Promise<boolean>;
  /** First-ever configuration in this native process. Must only ever
   * be called with a real, authenticated backend user UUID as
   * `appUserID` -- never while signed out, never with an email/
   * display name/device id. */
  configure(params: { apiKey: string; appUserID: string }): void;
  /** Switches the identified customer WITHOUT logging out first --
   * the supported way to move from user A to user B in this app's
   * custom-ID-only model (Part 8). Returns the new user's
   * CustomerInfo. */
  logIn(appUserID: string): Promise<CustomerInfo>;
  restorePurchases(): Promise<CustomerInfo>;
  /** Presents RevenueCat's dashboard-configured paywall for the
   * CURRENT/default Offering (no `offering` override passed here --
   * this app never hardcodes a package/offering identifier so the
   * dashboard stays free to change presentation without a client
   * release) only if the given entitlement isn't already active. */
  presentPaywall(params: { requiredEntitlementIdentifier: string }): Promise<PAYWALL_RESULT>;
  /** Resolves once the user dismisses Customer Center -- the caller
   * (revenuecat-context.tsx) treats that resolution as a reason to
   * re-sync backend billing status, never as proof of any specific
   * outcome. */
  presentCustomerCenter(): Promise<void>;
}

export function createRevenueCatAdapter(): RevenueCatAdapter {
  return {
    isConfigured: () => Purchases.isConfigured(),
    configure: ({ apiKey, appUserID }) => {
      Purchases.configure({ apiKey, appUserID });
    },
    logIn: async (appUserID) => {
      const result = await Purchases.logIn(appUserID);
      return result.customerInfo;
    },
    restorePurchases: () => Purchases.restorePurchases(),
    presentPaywall: ({ requiredEntitlementIdentifier }) =>
      RevenueCatUI.presentPaywallIfNeeded({ requiredEntitlementIdentifier }),
    presentCustomerCenter: () => RevenueCatUI.presentCustomerCenter(),
  };
}
