import React, { useId } from "react";
import { StyleSheet, Text, TextInput, View, type TextInputProps } from "react-native";

import { useTheme } from "@/theme/theme-provider";

type TextFieldProps = TextInputProps & {
  label: string;
  error?: string;
  hint?: string;
};

/**
 * Reusable form primitive so no screen re-invents label placement,
 * error placement, or accessibility wiring. Error text is always
 * paired with an accessible announcement, never conveyed by color
 * alone (section 19).
 */
export function TextField({ label, error, hint, style, ...inputProps }: TextFieldProps) {
  const theme = useTheme();
  const reactId = useId();
  const inputId = `field-${reactId}`;

  return (
    <View style={styles.container}>
      <Text nativeID={`${inputId}-label`} style={[theme.typography.label, { color: theme.colors.muted }]}>
        {label}
      </Text>
      <TextInput
        {...inputProps}
        accessibilityLabel={label}
        accessibilityLabelledBy={`${inputId}-label`}
        placeholderTextColor={theme.colors.muted}
        style={[
          styles.input,
          {
            borderColor: error ? theme.colors.danger : theme.colors.border,
            color: theme.colors.foreground,
            backgroundColor: theme.colors.surface,
            borderRadius: theme.radii.sm,
          },
          style,
        ]}
      />
      {hint && !error ? (
        <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>{hint}</Text>
      ) : null}
      {error ? (
        <Text
          accessibilityLiveRegion="polite"
          style={[theme.typography.caption, { color: theme.colors.danger }]}
        >
          {error}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: 6 },
  input: {
    minHeight: 48,
    paddingHorizontal: 14,
    borderWidth: 1,
    fontSize: 16,
  },
});
