import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/theme/theme-provider";

type CheckboxRowProps = {
  label: string;
  description?: string;
  checked: boolean;
  onToggle: () => void;
};

/** Never encodes checked/unchecked through color alone -- a distinct
 * glyph plus accessibilityState carries the meaning (section 19). */
export function CheckboxRow({ label, description, checked, onToggle }: CheckboxRowProps) {
  const theme = useTheme();
  return (
    <Pressable
      onPress={onToggle}
      accessibilityRole="checkbox"
      accessibilityState={{ checked }}
      accessibilityLabel={label}
      style={[
        styles.row,
        {
          borderColor: theme.colors.border,
          backgroundColor: checked ? theme.colors.surfaceMuted : theme.colors.surface,
          borderRadius: theme.radii.md,
        },
      ]}
    >
      <View
        style={[
          styles.box,
          {
            borderColor: checked ? theme.colors.accent : theme.colors.border,
            backgroundColor: checked ? theme.colors.accent : "transparent",
          },
        ]}
      >
        {checked ? <Text style={{ color: theme.colors.accentForeground, fontWeight: "700" }}>✓</Text> : null}
      </View>
      <View style={styles.textColumn}>
        <Text style={[theme.typography.body, { color: theme.colors.foreground }]}>{label}</Text>
        {description ? (
          <Text style={[theme.typography.caption, { color: theme.colors.muted }]}>{description}</Text>
        ) : null}
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
    padding: 14,
    borderWidth: 1,
    minHeight: 48,
  },
  box: {
    width: 24,
    height: 24,
    borderRadius: 6,
    borderWidth: 2,
    alignItems: "center",
    justifyContent: "center",
    marginTop: 2,
  },
  textColumn: { flex: 1, gap: 2 },
});
