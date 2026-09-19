import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Platform } from "react-native";

import { me } from "@/api/auth-api";
import { useSession } from "@/auth/session-context";
import { REVENUECAT_ANDROID_API_KEY, REVENUECAT_ENTITLEMENT_ID, REVENUECAT_IOS_API_KEY } from "@/constants/config";
import { logger } from "@/utils/logger";

import { classifyPurchaseError } from "./purchase-error";
import { type RevenueCatAvailability, computeAvailability } from "./revenuecat-availability";
import { createRevenueCatAdapter, type CustomerInfo, PAYWALL_RESULT, type RevenueCatAdapter } from "./revenuecat-adapter";

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
 * sign-out here only clears in-memory UI state (`configuredUserId`);
 * RevenueCat itself stays configured for whichever user it last
 * identified, and the next `AUTHENTICATED` transition either
 * recognizes the SAME user (a plain local no-op, see
 * `moduleConfiguredUserId` below) or SWITCHES to a new one via
 * `adapter.logIn()` -- never `logOut()` first.
 *
 * Fail-closed account switching (independent-review Blocker 1): this
 * provider deliberately tracks two different things. `moduleConfiguredUserId`
 * is whatever user the SDK itself last *successfully* configured/
 * logged in as -- it only ever changes on a successful adapter call,
 * so a failed `logIn()` leaves the underlying SDK on its previous
 * user (A), exactly as it should (the SDK is never logged out).
 * `configuredUserId` (React state, exposed to consumers) is instead
 * "is the SDK demonstrably configured as *this session's* intended
 * user" -- it is set to the resolved backend UUID ONLY after that
 * exact user was successfully configured/switched to, and is
 * explicitly set to `null` on any failure. A stale
 * `moduleConfiguredUserId` from a *previous* session's user must never
 * leak into a *new* session's `configuredUserId` merely because a
 * switch attempt failed -- `purchase`/`restore`/`presentCustomerCenter`
 * all gate on `configuredUserId`, so a null value here makes every
 * purchase action unreachable until identification actually succeeds
 * for the current user, without ever calling `logOut()`.
 * `retryIdentification()` lets a caller (e.g. the subscription screen)
 * re-attempt identification after such a failure.
 *
 * Central invariant (unchanged by this file): local RevenueCat state
 * is for immediate purchase UX ONLY. No consumer of this context ever
 * treats it as authorization -- see src/query/use-billing.ts /
 * app/(app)/subscription.tsx for the server-authoritative status this
 * app actually renders premium access from.
 */

type RevenueCatContextValue = {
  availability: RevenueCatAvailability;
  entitlementId: string;
  configuredUserId: string | null;
  presentPaywall: () => Promise<PAYWALL_RESULT | null>;
  restorePurchases: () => Promise<CustomerInfo | null>;
  presentCustomerCenter: () => Promise<void>;
  retryIdentification: () => void;
};

const RevenueCatContext = createContext<RevenueCatContextValue | null>(null);

// Module-level singleton coordinator (Part 9): survives component
// remounts within the same JS process, so two React effects (or a
// fast-refresh remount) can never both call Purchases.configure()/
// logIn() for the same transition -- the second one recognizes
// `moduleConfiguredUserId` already matches and does nothing. This
// value only ever advances on a SUCCESSFUL adapter call -- see the
// module docstring above for why that matters.
let moduleConfiguredUserId: string | null = null;
let moduleConfiguringPromise: Promise<void> | null = null;

/** Test-only reset -- production code never calls this. */
export function __resetRevenueCatSingletonForTests(): void {
  moduleConfiguredUserId = null;
  moduleConfiguringPromise = null;
}

// Lazily constructed, memoized: `createRevenueCatAdapter()` wraps the
// REAL `react-native-purchases`/`react-native-purchases-ui` packages,
// and merely importing/constructing it can touch native-module
// resolution. Every test in this codebase supplies its own `adapter`
// prop (a fake), so building this eagerly at module scope -- as an
// earlier version of this file did -- meant every test that merely
// IMPORTED this module (even ones that never used the default) paid
// that cost too, for no reason; an independent review's Jest-hang
// investigation found this was the source of a harmless but noisy
// "Cannot log after tests are done" warning in `tests/billing/`.
// Building it only on first actual use (i.e., only when no test
// override is supplied) avoids that entirely.
let productionAdapter: RevenueCatAdapter | null = null;
function getProductionAdapter(): RevenueCatAdapter {
  if (!productionAdapter) productionAdapter = createRevenueCatAdapter();
  return productionAdapter;
}

export function RevenueCatProvider({
  children,
  adapter = getProductionAdapter(),
}: {
  children: React.ReactNode;
  adapter?: RevenueCatAdapter;
}) {
  const { state } = useSession();
  // Platform/env-derived, never changes for the life of the process.
  const [availability] = useState<RevenueCatAvailability>(computeAvailability);
  // Deliberately starts `null`, never `moduleConfiguredUserId`: at
  // mount time this session has not yet confirmed the SDK is
  // configured as ITS user -- exposing a previous session's leftover
  // module-level value here would be exactly the cross-account leak
  // Blocker 1 describes, just at mount instead of at switch-failure.
  const [configuredUserId, setConfiguredUserId] = useState<string | null>(null);
  const [identificationAttempt, setIdentificationAttempt] = useState(0);

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
        if (!cancelled) setConfiguredUserId(null);
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
        // Falls through: the concurrent attempt configured/left the
        // SDK on a DIFFERENT user than the one this session needs --
        // this session must still attempt its own switch below rather
        // than exposing that other user's id.
      }

      const apiKey = Platform.OS === "ios" ? REVENUECAT_IOS_API_KEY : REVENUECAT_ANDROID_API_KEY;
      if (!apiKey) {
        if (!cancelled) setConfiguredUserId(null);
        return;
      }

      moduleConfiguringPromise = (async () => {
        const alreadyConfigured = await adapter.isConfigured();
        if (!alreadyConfigured) {
          // First-ever configuration in this native process.
          adapter.configure({ apiKey, appUserID: userId });
        } else if (moduleConfiguredUserId !== userId) {
          // Already configured (possibly for a different user, or a
          // prior process state) -- switch identified customers
          // WITHOUT logging out first (Part 8). If this throws, the
          // line below never runs, so `moduleConfiguredUserId` stays
          // whatever the SDK's real last-successful user was.
          await adapter.logIn(userId);
        }
        moduleConfiguredUserId = userId;
      })();

      let switchSucceeded = true;
      try {
        await moduleConfiguringPromise;
      } catch (err) {
        switchSucceeded = false;
        logger.warn("revenuecat configure/login failed", { category: classifyPurchaseError(err) });
      } finally {
        moduleConfiguringPromise = null;
      }

      if (cancelled) return;
      // Fail-closed (Blocker 1): only expose `userId` as configured
      // when the switch to THAT EXACT user just succeeded. Never fall
      // back to whatever `moduleConfiguredUserId` happens to hold --
      // on failure that could still be a previous session's user.
      setConfiguredUserId(switchSucceeded && moduleConfiguredUserId === userId ? userId : null);
    }

    configureOrSwitch();

    return () => {
      cancelled = true;
    };
  }, [state.status, availability, adapter, identificationAttempt]);

  const value = useMemo<RevenueCatContextValue>(
    () => ({
      availability,
      entitlementId: REVENUECAT_ENTITLEMENT_ID,
      configuredUserId,
      async presentPaywall() {
        if (availability !== "READY" || !configuredUserId) return null;
        return adapter.presentPaywall({ requiredEntitlementIdentifier: REVENUECAT_ENTITLEMENT_ID });
      },
      async restorePurchases() {
        if (availability !== "READY" || !configuredUserId) return null;
        return adapter.restorePurchases();
      },
      async presentCustomerCenter() {
        if (availability !== "READY" || !configuredUserId) return;
        await adapter.presentCustomerCenter();
      },
      retryIdentification() {
        setIdentificationAttempt((n) => n + 1);
      },
    }),
    [availability, configuredUserId, adapter],
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
