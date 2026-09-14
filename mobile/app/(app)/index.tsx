import { useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { useConsentQuery } from "@/query/use-consent";
import { useProfileQuery } from "@/profile/queries";
import { useTheme } from "@/theme/theme-provider";

/**
 * Phase A home is intentionally simple (section 14): no facial
 * capture/analysis UI exists yet, and this screen never fakes a
 * result or seeds a placeholder recommendation into a real data path
 * -- it just says plainly what's coming.
 */
export default function AppHome() {
  const theme = useTheme();
  const router = useRouter();
  const consentQuery = useConsentQuery({ enabled: true });
  const profileQuery = useProfileQuery({ enabled: true });

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Welcome back</Text>

      <Card>
        <Row label="Profile" value={profileQuery.data?.profile_set ? "Complete" : "Incomplete"} />
        <Row
          label="Consent"
          value={consentQuery.data?.has_valid_consent ? "Up to date" : "Needs attention"}
        />
      </Card>

      <Card>
        <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>Skin analysis</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          Skin analysis arrives in the next build phase.
        </Text>
      </Card>

      <Card>
        <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>Your routine</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          Your personalized routine will appear here once analysis is available.
        </Text>
      </Card>

      <Button label="Settings" variant="secondary" onPress={() => router.push("/(app)/settings")} />
    </Screen>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <View
      style={[
        styles.card,
        { backgroundColor: theme.colors.surface, borderColor: theme.colors.border, borderRadius: theme.radii.md },
      ]}
    >
      {children}
    </View>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  const theme = useTheme();
  return (
    <View style={styles.row}>
      <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>{label}</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { padding: 16, borderWidth: 1, gap: 8 },
  row: { flexDirection: "row", justifyContent: "space-between" },
});
