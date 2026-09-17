import { useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { useConsentQuery } from "@/query/use-consent";
import { useProfileQuery } from "@/profile/queries";
import { useTheme } from "@/theme/theme-provider";

/**
 * Home stays intentionally simple through Phase C1: an entry point
 * into skin analysis plus account status, nothing else. Per this
 * phase's own scope boundary, no subscription/paywall, progress
 * charts, purchase UI, history, or notifications belong here yet
 * (Phase C2/C3) -- this screen never fakes any of those or seeds a
 * placeholder into a real data path.
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
          Take a photo to get an image-derived estimate of your skin&apos;s appearance, your top priorities,
          measurements, and a suggested AM/PM routine -- with a compatible product match for each step when one is
          available.
        </Text>
        <Button label="Start skin analysis" onPress={() => router.push("/(app)/analysis")} />
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
