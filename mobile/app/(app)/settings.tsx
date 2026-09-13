import { useRouter } from "expo-router";
import React from "react";
import { Alert, Text, View } from "react-native";

import { useDeleteAccountMutation, useSignOutMutation } from "@/auth/use-auth-actions";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { Screen } from "@/components/screen";
import { APP_VERSION } from "@/constants/config";
import { useConsentQuery, useWithdrawConsentMutation } from "@/query/use-consent";
import { useTheme } from "@/theme/theme-provider";

export default function Settings() {
  const theme = useTheme();
  const router = useRouter();
  const signOut = useSignOutMutation();
  const deleteAccount = useDeleteAccountMutation();
  const consentQuery = useConsentQuery({ enabled: true });
  const withdrawConsent = useWithdrawConsentMutation();

  function confirmDelete() {
    Alert.alert(
      "Delete account",
      "This permanently deletes your account and signs you out on every device. This can't be undone.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Delete", style: "destructive", onPress: () => deleteAccount.mutate() },
      ],
    );
  }

  function confirmWithdrawConsent() {
    Alert.alert(
      "Withdraw consent",
      "Facial analysis will be unavailable until you consent again.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Withdraw", style: "destructive", onPress: () => withdrawConsent.mutate() },
      ],
    );
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Settings</Text>

      <Section>
        <Button label="Profile" variant="secondary" onPress={() => router.push("/(app)/profile")} />
      </Section>

      <Section title="Privacy">
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          Facial analysis consent:{" "}
          {consentQuery.data?.has_valid_consent ? "Granted" : "Not granted"}
        </Text>
        {consentQuery.data?.has_valid_consent ? (
          <Button
            label="Withdraw consent"
            variant="secondary"
            onPress={confirmWithdrawConsent}
            loading={withdrawConsent.isPending}
          />
        ) : null}
        {withdrawConsent.isError ? <ErrorState error={withdrawConsent.error} /> : null}
      </Section>

      <Section>
        <Button label="Sign out" variant="secondary" onPress={() => signOut.mutate()} loading={signOut.isPending} />
      </Section>

      <Section>
        <Button label="Delete account" variant="danger" onPress={confirmDelete} loading={deleteAccount.isPending} />
        {deleteAccount.isError ? <ErrorState error={deleteAccount.error} /> : null}
      </Section>

      <Text style={[theme.typography.caption, { color: theme.colors.muted, textAlign: "center" }]}>
        Version {APP_VERSION}
      </Text>
    </Screen>
  );
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
