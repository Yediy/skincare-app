import { useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { useTheme } from "@/theme/theme-provider";

export default function Welcome() {
  const theme = useTheme();
  const router = useRouter();

  return (
    <Screen scroll={false}>
      <View style={styles.spacer} />
      <View style={styles.copy}>
        <Text style={[theme.typography.displayLarge, { color: theme.colors.foreground }]}>
          Skincare, understood.
        </Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          A calm, science-minded companion for the products you already own -- and the ones you&apos;re
          considering next.
        </Text>
      </View>
      <View style={styles.actions}>
        <Button label="Create account" onPress={() => router.push("/(public)/sign-up")} />
        <Button label="Sign in" onPress={() => router.push("/(public)/sign-in")} variant="secondary" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  spacer: { flex: 1 },
  copy: { gap: 12, marginBottom: 32 },
  actions: { gap: 12, marginBottom: 8 },
});
