import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Platform } from "react-native";

import { me } from "@/api/auth-api";
import { useSession } from "@/auth/session-context";
import { REVENUECAT_ANDROID_API_KEY, REVENUECAT_ENTITLEMENT_ID, REVENUECAT_IOS_API_KEY } from "@/constants/config";
import { logger } from "@/utils/logger";

import { classifyPurchaseError } from "./purchase-error";
import { type RevenueCatAvailability, computeAvailability } from "./revenuecat-availability";
import {
  createRevenueCatAdapter,
  type CustomerInfo,
  PAYWALL_RESULT,
  type RevenueCatAdapter,
} from "./revenuecat-adapter";

export type { RevenueCatAvailability };

/**
 * Application-level RevenueCat lifecycle provider (Mobile C2 Part 9).
 * Nests beneath SessionProvider (see app/_layout.tsx) and reacts to
 * `useSession().state` only -- it never drives session state itself.
 *
 * Identified-user lifecycle (Part 7/8): this app's ONLY RevenueCat App
 * User ID is the backend's own `users.id` UUID, obtained via `/me`
 * only after `state.status === "AUTHENTICATED"`. RevenueCat is never
 * configured while signed out (never an anonymous customer created by
 * this code), and the SDK's `logOut()` is never called on ordinary
 * sign-out -- this app's custom-ID-only model means `logOut()` would
 * immediately create an unwanted `$RCAnonymousID` customer. Ordinary
 * sign-out here only clears in-memory UI state
 * (`customerInfo`/`configuredUserId`); RevenueCat itself stays
 * configured for whichever user it last identified, and the next
 * `AUTHENTICATED` transition either recognizes the SAME user (a plain
 * local no-op, see `moduleConfiguredUserId` below) or SWITCHES to a
 * new one via `adapter.logIn()` -- never `logOut()` first.
 *
 * Central invariant (unchanged by this file): CustomerInfo here is
 * for immediate purchase UX ONLY. No consumer of this context ever
 * treats `customerInfo.entitlements.active[...]` as authorization --
 * see src/query/use-billing.ts / app/(app)/subscription.tsx for the
 * server-authoritative status this app actually renders premium
 * access from.
 */


type RevenueCatContextValue = {
  availability: RevenueCatAvailability;
  entitlementId: string;
  configuredUserId: string | null;
  customerInfo: CustomerInfo | null;
  refreshCustomerInfo: () => Promise<void>;
  presentPaywall: () => Promise<PAYWALL_RESULT | null>;
  restorePurchases: () => Promise<CustomerInfo | null>;
  presentCustomerCenter: () => Promise<void>;
};

const RevenueCatContext = createContext<RevenueCatContextValue | null>(null);

// Module-level singleton coordinator (Part 9): survives component
// remounts within the same JS process, so two React effects (or a
// fast-refresh remount) can never both call Purchases.configure()/
// logIn() for the same transition -- the second one recognizes
// `moduleConfiguredUserId` already matches and does nothing.
let moduleConfiguredUserId: string | null = null;
let moduleConfiguringPromise: Promise<void> | null = null;

/** Test-only reset -- production code never calls this. */
export function __resetRevenueCatSingletonForTests(): void {
  moduleConfiguredUserId = null;
  moduleConfiguringPromise = null;
}

const productionAdapter = createRevenueCatAdapter();

export function RevenueCatProvider({
  children,
  adapter = productionAdapter,
}: {
  children: React.ReactNode;
  adapter?: RevenueCatAdapter;
}) {
  const { state } = useSession();
  // Platform/env-derived, never changes for the life of the process.
  const [availability] = useState<RevenueCatAvailability>(computeAvailability);
  const [configuredUserId, setConfiguredUserId] = useState<string | null>(moduleConfiguredUserId);
  const [customerInfo, setCustomerInfo] = useState<CustomerInfo | null>(null);

  useEffect(() => {
    if (availability !== "READY") return;

    if (state.status !== "AUTHENTICATED") {
      // SIGNED_OUT (or UNKNOWN, where Part 9 says "do nothing" --
      // handled by the outer !== "AUTHENTICATED" check covering both):
      // clear in-memory UI state only. Never call adapter.logOut().
      // Deferred to a microtask (react-hooks/set-state-in-effect):
      // this is genuinely synchronizing local state with the external
      // session, not a value derivable during render.
      if (state.status === "SIGNED_OUT") {
        Promise.resolve().then(() => {
          setCustomerInfo(null);
          setConfiguredUserId(null);
        });
      }
      return;
    }

    let cancelled = false;

    async function configureOrSwitch() {
      let userId: string;
      try {
        ({ user_id: userId } = await me());
      } catch (err) {
        logger.warn("revenuecat: could not resolve /me before configuring", {
          category: classifyPurchaseError(err),
        });
        return;
      }
      if (cancelled) return;

      if (moduleConfiguredUserId === userId) {
        setConfiguredUserId(userId);
        return;
      }

      if (moduleConfiguringPromise) {
        await moduleConfiguringPromise;
        if (cancelled) return;
        if (moduleConfiguredUserId === userId) {
          setConfiguredUserId(userId);
          return;
        }
      }

      const apiKey = Platform.OS === "ios" ? REVENUECAT_IOS_API_KEY : REVENUECAT_ANDROID_API_KEY;
      if (!apiKey) return;

      moduleConfiguringPromise = (async () => {
        const alreadyConfigured = await adapter.isConfigured();
        if (!alreadyConfigured) {
          // First-ever configuration in this native process.
          adapter.configure({ apiKey, appUserID: userId });
        } else if (moduleConfiguredUserId !== userId) {
          // Already configured (possibly for a different user, or a
          // prior process state) -- switch identified customers
          // WITHOUT logging out first (Part 8).
          await adapter.logIn(userId);
        }
        moduleConfiguredUserId = userId;
      })();

      try {
        await moduleConfiguringPromise;
      } catch (err) {
        logger.warn("revenuecat configure/login failed", { category: classifyPurchaseError(err) });
      } finally {
        moduleConfiguringPromise = null;
      }

      if (!cancelled) setConfiguredUserId(moduleConfiguredUserId);
    }

    configureOrSwitch();

    return () => {
      cancelled = true;
    };
  }, [state.status, availability, adapter]);

  useEffect(() => {
    if (availability !== "READY" || !configuredUserId) return;

    const listener = (info: CustomerInfo) => {
      // Reason to refresh/sync SERVER status -- never authorization
      // by itself. See app/(app)/subscription.tsx for the actual
      // sync trigger this listener feeds.
      setCustomerInfo(info);
    };
    adapter.addCustomerInfoUpdateListener(listener);
    adapter
      .getCustomerInfo()
      .then(setCustomerInfo)
      .catch((err) => {
        logger.warn("revenuecat getCustomerInfo failed", { category: classifyPurchaseError(err) });
      });

    return () => {
      adapter.removeCustomerInfoUpdateListener(listener);
    };
  }, [availability, configuredUserId, adapter]);

  const value = useMemo<RevenueCatContextValue>(
    () => ({
      availability,
      entitlementId: REVENUECAT_ENTITLEMENT_ID,
      configuredUserId,
      customerInfo,
      async refreshCustomerInfo() {
        if (availability !== "READY" || !configuredUserId) return;
        const info = await adapter.getCustomerInfo();
        setCustomerInfo(info);
      },
      async presentPaywall() {
        if (availability !== "READY" || !configuredUserId) return null;
        return adapter.presentPaywall({ requiredEntitlementIdentifier: REVENUECAT_ENTITLEMENT_ID });
      },
      async restorePurchases() {
        if (availability !== "READY" || !configuredUserId) return null;
        const info = await adapter.restorePurchases();
        setCustomerInfo(info);
        return info;
      },
      async presentCustomerCenter() {
        if (availability !== "READY" || !configuredUserId) return;
        await adapter.presentCustomerCenter();
      },
    }),
    [availability, configuredUserId, customerInfo, adapter],
  );

  return <RevenueCatContext.Provider value={value}>{children}</RevenueCatContext.Provider>;
}

export function useRevenueCat(): RevenueCatContextValue {
  const ctx = useContext(RevenueCatContext);
  if (!ctx) {
    throw new Error("useRevenueCat() must be used within a RevenueCatProvider");
  }
  return ctx;
}
