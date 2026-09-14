import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { ApiError, genericMessageFor } from "@/api/errors";
import { useTheme } from "@/theme/theme-provider";

import { Button } from "./button";

type ErrorStateProps = {
  error: unknown;
  onRetry?: () => void;
};

/**
 * Every network-driven screen renders its rejected state through
 * this, not a bespoke message -- see section 21. Never shows a raw
 * backend exception body; ApiError.message is already a safe,
 * user-facing string (src/api/errors.ts).
 */
export function ErrorState({ error, onRetry }: ErrorStateProps) {
  const theme = useTheme();
  const message = error instanceof ApiError ? error.message : genericMessageFor("UNKNOWN");
  const canRetry = !(error instanceof ApiError) || error.retryable || error.code === "UNAUTHORIZED";

  return (
    <View style={styles.container} accessibilityRole="alert">
      <Text style={[theme.typography.subtitle, { color: theme.colors.foreground }]}>
        Something didn&apos;t work
      </Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted, textAlign: "center" }]}>{message}</Text>
      {onRetry && canRetry ? <Button label="Try again" onPress={onRetry} variant="secondary" /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 12, paddingVertical: 32 },
});
