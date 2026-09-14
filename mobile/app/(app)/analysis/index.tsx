import { useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { useTheme } from "@/theme/theme-provider";

/**
 * Capture Coach (section 4): guidance shown BEFORE the shutter opens.
 * Deliberately cautious wording throughout -- never implies makeup
 * must always be removed (the backend cannot reliably detect makeup),
 * never a medical claim, and the "estimate/could not measure reliably"
 * vocabulary matches src/analysis/analysis-presenter.ts exactly so
 * this screen never sets an expectation the results screen can't meet.
 */
const GUIDANCE_ITEMS: string[] = [
  "Remove glasses if you can do so comfortably.",
  "Move any hair or accessories away from your face.",
  "Avoid beauty filters or photo effects.",
  "Find an evenly lit space -- avoid strong backlighting or very dark rooms.",
  "Face the camera directly, with your whole face in view.",
  "Keep a neutral expression.",
  "Hold your device steady at arm's length.",
  "For the most consistent analysis, capture your skin without beauty filters and, when practical, before applying heavy makeup.",
];

export default function CaptureCoach() {
  const theme = useTheme();
  const router = useRouter();

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Before you start</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
        A few tips for a photo that gives the most consistent analysis.
      </Text>

      <View style={styles.list} accessibilityRole="list">
        {GUIDANCE_ITEMS.map((item) => (
          <View key={item} style={styles.row} accessibilityRole="text">
            <Text style={[theme.typography.body, { color: theme.colors.accent }]} accessibilityElementsHidden>
              •
            </Text>
            <Text style={[theme.typography.body, { color: theme.colors.foreground, flex: 1 }]}>{item}</Text>
          </View>
        ))}
      </View>

      <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>
        This analysis gives image-derived estimates of your skin&apos;s appearance -- it is not a medical or
        dermatological diagnosis.
      </Text>

      <Button label="I'm ready" onPress={() => router.push("/(app)/analysis/capture")} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  list: { gap: 12 },
  row: { flexDirection: "row", gap: 10, alignItems: "flex-start" },
});
