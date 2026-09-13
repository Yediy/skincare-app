import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/theme/theme-provider";

export function EmptyState({ title, description }: { title: string; description?: string }) {
  const theme = useTheme();
  return (
    <View style={styles.container}>
      <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>{title}</Text>
      {description ? (
        <Text style={[theme.typography.body, { color: theme.colors.muted, textAlign: "center" }]}>
          {description}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { alignItems: "center", justifyContent: "center", gap: 8, paddingVertical: 32 },
});
