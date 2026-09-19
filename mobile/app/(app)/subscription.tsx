import React, { useState } from "react";
import { Platform, Text, View } from "react-native";

import { PAYWALL_RESULT } from "@/billing/revenuecat-adapter";
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
 * state from -- RevenueCat's `customerInfo` is used only to decide
 * when to show a paywall/trigger a sync, never to flip `hasPremium`
 * directly (Part 12).
 */
export default function Subscription() {
  const theme = useTheme();
  const revenueCat = useRevenueCat();
  const statusQuery = useBillingStatusQuery({ enabled: true });
  const syncMutation = useBillingSyncMutation();

  const [purchaseSyncing, setPurchaseSyncing] = useState(false);
  const [purchaseErrorCategory, setPurchaseErrorCategory] = useState<PurchaseErrorCategory | null>(null);
  const [restoreErrorCategory, setRestoreErrorCategory] = useState<PurchaseErrorCategory | null>(null);

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
      if (result === PAYWALL_RESULT.CANCELLED || result === PAYWALL_RESULT.NOT_PRESENTED || result === null) {
        return; // user cancellation is not an error
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
    await revenueCat.presentCustomerCenter();
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
  const revenueCatUnavailable = revenueCat.availability !== "READY";

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

      {revenueCatUnavailable ? (
        <Section title="Purchases unavailable">
          <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
            {unavailableMessage(revenueCat.availability)}
          </Text>
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
    </Screen>
  );
}

function unavailableMessage(availability: string): string {
  switch (availability) {
    case "DISABLED":
      return "Subscriptions aren't enabled in this build.";
    case "WEB_UNSUPPORTED":
      return "Subscriptions aren't available on web.";
    case "MISSING_KEY":
      return "Subscriptions are temporarily unavailable. Please try again later.";
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
