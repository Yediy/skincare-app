import { useRouter } from "expo-router";
import React from "react";
import { Text, View } from "react-native";

import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { useConsentQuery, useGrantConsentMutation } from "@/query/use-consent";
import { useTheme } from "@/theme/theme-provider";

/**
 * Consent copy deliberately avoids any diagnosis/clinical-validation
 * claim (section 10) -- this app describes what it does and what it
 * does not do, nothing more. The backend, not this screen, is the
 * source of truth for whether consent is current: granting here
 * always uses `required_policy_version` from GET /consent, never a
 * value hardcoded client-side.
 */
export default function Consent() {
  const theme = useTheme();
  const router = useRouter();
  const consentQuery = useConsentQuery({ enabled: true });
  const grantConsent = useGrantConsentMutation();

  if (consentQuery.isError) {
    return <ErrorState error={consentQuery.error} onRetry={() => consentQuery.refetch()} />;
  }
  if (!consentQuery.data) {
    return <LoadingState label="Loading…" />;
  }

  const policyVersion = consentQuery.data.required_policy_version;

  function handleAgree() {
    grantConsent.mutate(
      { policy_version: policyVersion, purpose: "facial skin analysis" },
      { onSuccess: () => router.replace("/") },
    );
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Before we continue</Text>

      <View style={{ gap: 12 }}>
        <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>
          This app is built around facial skin analysis. Here&apos;s exactly what that means:
        </Text>
        <Bullet
          text="A photo you choose to submit is analyzed to estimate visible traits like tone evenness, redness, oiliness, and texture -- not a medical diagnosis."
        />
        <Bullet text="Analysis happens for the purpose of generating routine and product guidance for you." />
        <Bullet text="Photos are processed temporarily for that analysis; the capture and upload flow itself arrives in a later build." />
        <Bullet text="You can withdraw this consent at any time from Settings. Facial analysis becomes unavailable again the moment you do." />
        <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>
          This is not a substitute for advice from a dermatologist or other qualified professional.
        </Text>
      </View>

      <Button label="I understand and agree" onPress={handleAgree} loading={grantConsent.isPending} />
      {grantConsent.isError ? <ErrorState error={grantConsent.error} onRetry={handleAgree} /> : null}
    </Screen>
  );
}

function Bullet({ text }: { text: string }) {
  const theme = useTheme();
  return (
    <View style={{ flexDirection: "row", gap: 8 }}>
      <Text style={{ color: theme.colors.accent }}>•</Text>
      <Text style={[theme.typography.body, { color: theme.colors.foreground, flex: 1 }]}>{text}</Text>
    </View>
  );
}
