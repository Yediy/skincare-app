import React, { useState } from "react";
import { Platform, Text, View } from "react-native";

import { PAYWALL_RESULT } from "@/billing/revenuecat-adapter";
import { computeBillingActionReadiness } from "@/billing/billing-action-readiness";
import { useRevenueCat } from "@/billing/revenuecat-context";
import { classifyPurchaseError, purchaseErrorMessage, type PurchaseErrorCategory } from "@/billing/purchase-error";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { Screen } from "@/components/screen";
import { useBillingStatusQuery, useBillingSyncMutation } from "@/query/use-billing";
import { useTheme } from "@/theme/theme-provider";
import { logger } from "@/utils/logger";

/**
 * Mobile C2 Part 11. Server billing status (`useBillingStatusQuery`)
 * is the ONLY source of truth this screen renders premium/allowance
 * state from -- RevenueCat local state is used only to decide when to
 * present a paywall/trigger a sync, never to flip `hasPremium`
 * directly (Part 12).
 *
 * Purchase-action readiness (independent-review Blocker 2):
 * `computeBillingActionReadiness` additionally requires the SERVER to
 * agree that billing is enabled for this exact identified user and
 * entitlement before any native purchase action becomes reachable --
 * native availability (`revenueCat.availability === "READY"`) alone
 * is not sufficient, since the mobile SDK can be configured while the
 * server has billing disabled, or while server/mobile identity or
 * entitlement configuration disagree. An existing server-granted
 * entitlement is always displayed regardless of this gate -- only the
 * ability to start a NEW purchase action is gated.
 */
export default function Subscription() {
  const theme = useTheme();
  const revenueCat = useRevenueCat();
  const statusQuery = useBillingStatusQuery({ enabled: true });
  const syncMutation = useBillingSyncMutation();

  const [purchaseSyncing, setPurchaseSyncing] = useState(false);
  const [purchaseErrorCategory, setPurchaseErrorCategory] = useState<PurchaseErrorCategory | null>(null);
  const [restoreErrorCategory, setRestoreErrorCategory] = useState<PurchaseErrorCategory | null>(null);
  const [customerCenterErrorCategory, setCustomerCenterErrorCategory] = useState<PurchaseErrorCategory | null>(null);

  async function afterLocalPurchaseSignal() {
    setPurchaseSyncing(true);
    setPurchaseErrorCategory(null);
    try {
      await syncMutation.mutateAsync();
    } catch {
      // The mutation's own isError/error already drives the UI below;
      // logged here only as a bounded category, never the raw error.
      logger.warn("billing sync after purchase signal failed", { category: "SYNC_FAILED" });
    } finally {
      setPurchaseSyncing(false);
    }
  }

  async function handleUpgrade() {
    setPurchaseErrorCategory(null);
    try {
      const result = await revenueCat.presentPaywall();
      if (result === PAYWALL_RESULT.CANCELLED || result === null) {
        return; // true user cancellation -- not an error, no sync.
      }
      if (result === PAYWALL_RESULT.NOT_PRESENTED) {
        // The server already told this screen the user is not
        // premium (this button is only reachable in that state), yet
        // the SDK suppressed the paywall -- meaning ITS local
        // entitlement cache already disagrees with the server. That
        // disagreement is exactly what /billing/sync exists to
        // resolve; NOT_PRESENTED must never be treated like
        // cancellation (independent-review Blocker 3).
        await afterLocalPurchaseSignal();
        return;
      }
      if (result === PAYWALL_RESULT.ERROR) {
        setPurchaseErrorCategory("GENERIC_FAILURE");
        return;
      }
      // PURCHASED or RESTORED
      await afterLocalPurchaseSignal();
    } catch (err) {
      setPurchaseErrorCategory(classifyPurchaseError(err));
    }
  }

  async function handleRestore() {
    setRestoreErrorCategory(null);
    try {
      await revenueCat.restorePurchases();
      await afterLocalPurchaseSignal();
    } catch (err) {
      setRestoreErrorCategory(classifyPurchaseError(err));
    }
  }

  async function handleManageSubscription() {
    setCustomerCenterErrorCategory(null);
    try {
      await revenueCat.presentCustomerCenter();
    } catch (err) {
      // Customer Center never successfully opened/closed -- do not
      // sync (nothing could have changed) and never render/log the
      // raw RevenueCat error object.
      setCustomerCenterErrorCategory(classifyPurchaseError(err));
      return;
    }
    // Part 14: after Customer Center closes, trigger backend
    // reconciliation and refresh -- never assume anything changed,
    // just re-check with the server.
    await afterLocalPurchaseSignal();
  }

  if (Platform.OS === "web") {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Subscription</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          Subscription management isn&apos;t available on web yet. Use the mobile app to upgrade or manage your
          subscription.
        </Text>
      </Screen>
    );
  }

  if (statusQuery.isPending) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Subscription</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>Loading your subscription status…</Text>
      </Screen>
    );
  }

  if (statusQuery.isError) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Subscription</Text>
        <ErrorState error={statusQuery.error} onRetry={() => statusQuery.refetch()} />
      </Screen>
    );
  }

  const status = statusQuery.data;
  const isPremium = status.has_premium_access;
  const isGracePeriod = status.projection_status === "GRACE_PERIOD";

  const readiness = computeBillingActionReadiness({
    mobileAvailability: revenueCat.availability,
    configuredUserId: revenueCat.configuredUserId,
    mobileEntitlementId: revenueCat.entitlementId,
    serverStatus: status,
  });
  const actionsReady = readiness === "READY";

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Subscription</Text>

      {isPremium ? (
        <Section title={isGracePeriod ? "Premium (renewal issue)" : "Premium"}>
          <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>
            {isGracePeriod
              ? "Your subscription is in a grace period -- we're having trouble renewing it, but your premium access continues for now."
              : "You have premium access."}
          </Text>
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
            {status.analyses_remaining} of {status.analysis_allowance} analyses remaining this period.
          </Text>
          {status.will_renew !== null ? (
            <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>
              {status.will_renew ? "Renews automatically." : "Will not renew."}
              {status.expires_at ? ` Expires ${new Date(status.expires_at).toLocaleDateString()}.` : ""}
            </Text>
          ) : null}
        </Section>
      ) : (
        <Section title="Free plan">
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
            {status.analyses_remaining} of {status.analysis_allowance} free analyses remaining this period.
          </Text>
        </Section>
      )}

      {!actionsReady ? (
        <Section title="Purchases unavailable">
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>{readinessMessage(readiness)}</Text>
          {readiness === "IDENTITY_NOT_CONFIGURED" ? (
            <Button label="Retry" variant="secondary" onPress={() => revenueCat.retryIdentification()} />
          ) : null}
        </Section>
      ) : (
        <Section>
          {!isPremium ? (
            <Button label="Upgrade to Premium" onPress={handleUpgrade} />
          ) : null}
          {isPremium ? (
            <Button label="Manage subscription" variant="secondary" onPress={handleManageSubscription} />
          ) : null}
          <Button label="Restore purchases" variant="secondary" onPress={handleRestore} />
        </Section>
      )}

      {purchaseSyncing || syncMutation.isPending ? (
        <Section>
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
            Purchase received. We&apos;re syncing your access…
          </Text>
        </Section>
      ) : null}

      {syncMutation.isError && !purchaseSyncing ? (
        <Section title="Sync failed">
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
            We couldn&apos;t confirm your purchase with our server yet. Your existing access is unchanged.
          </Text>
          <Button label="Retry" variant="secondary" onPress={() => afterLocalPurchaseSignal()} />
        </Section>
      ) : null}

      {purchaseErrorCategory ? (
        <Section>
          <Text style={[theme.typography.body, { color: theme.colors.danger }]}>
            {purchaseErrorMessage(purchaseErrorCategory)}
          </Text>
        </Section>
      ) : null}

      {restoreErrorCategory ? (
        <Section>
          <Text style={[theme.typography.body, { color: theme.colors.danger }]}>
            {purchaseErrorMessage(restoreErrorCategory)}
          </Text>
        </Section>
      ) : null}

      {customerCenterErrorCategory ? (
        <Section>
          <Text style={[theme.typography.body, { color: theme.colors.danger }]}>
            {purchaseErrorMessage(customerCenterErrorCategory)}
          </Text>
        </Section>
      ) : null}
    </Screen>
  );
}

function readinessMessage(reason: ReturnType<typeof computeBillingActionReadiness>): string {
  switch (reason) {
    case "MOBILE_UNAVAILABLE":
      return "Subscriptions are temporarily unavailable. Please try again later.";
    case "SERVER_STATUS_UNAVAILABLE":
      return "We couldn't load your subscription status. Please try again.";
    case "BACKEND_DISABLED":
      return "Subscriptions aren't enabled yet.";
    case "IDENTITY_NOT_CONFIGURED":
      return "Setting up your account for purchases…";
    case "IDENTITY_MISMATCH":
    case "ENTITLEMENT_MISMATCH":
      return "We're having trouble verifying your account for purchases. Please try again later.";
    default:
      return "Subscriptions are temporarily unavailable. Please try again later.";
  }
}

function Section({ title, children }: { title?: string; children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <View style={{ gap: 8 }}>
      {title ? <Text style={[theme.typography.label, { color: theme.colors.muted }]}>{title}</Text> : null}
      {children}
    </View>
  );
}
