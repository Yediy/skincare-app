import React from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text } from "react-native";

import { useTheme } from "@/theme/theme-provider";

type ButtonVariant = "primary" | "secondary" | "danger";

type ButtonProps = {
  label: string;
  onPress: () => void;
  variant?: ButtonVariant;
  loading?: boolean;
  disabled?: boolean;
  accessibilityHint?: string;
};

export function Button({ label, onPress, variant = "primary", loading, disabled, accessibilityHint }: ButtonProps) {
  const theme = useTheme();
  const isDisabled = disabled || loading;

  const backgroundColor =
    variant === "primary"
      ? theme.colors.accent
      : variant === "danger"
        ? theme.colors.danger
        : theme.colors.surfaceMuted;
  const textColor =
    variant === "primary" ? theme.colors.accentForeground : variant === "danger" ? theme.colors.dangerForeground : theme.colors.foreground;

  return (
    <Pressable
      onPress={onPress}
      disabled={isDisabled}
      accessibilityRole="button"
      accessibilityState={{ disabled: isDisabled, busy: loading }}
      accessibilityHint={accessibilityHint}
      style={({ pressed }) => [
        styles.base,
        {
          backgroundColor,
          borderRadius: theme.radii.md,
          opacity: isDisabled ? 0.6 : pressed ? 0.85 : 1,
        },
      ]}
    >
      {loading ? (
        <ActivityIndicator color={textColor} />
      ) : (
        <Text style={[theme.typography.subtitle, { color: textColor }]}>{label}</Text>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: 48,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 20,
  },
});
