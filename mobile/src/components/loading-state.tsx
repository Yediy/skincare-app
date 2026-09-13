import React from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/theme/theme-provider";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  const theme = useTheme();
  return (
    <View style={styles.container} accessibilityRole="progressbar" accessibilityLabel={label}>
      <ActivityIndicator color={theme.colors.accent} />
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 12, paddingVertical: 32 },
});
