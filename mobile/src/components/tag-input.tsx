import React, { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/theme/theme-provider";

import { TextField } from "./text-field";

type TagInputProps = {
  label: string;
  hint?: string;
  values: string[];
  onChange: (values: string[]) => void;
};

/** Free-text list entry (allergies / ingredients to avoid) --
 * backend/app/db/profile_repository.py stores these as plain string
 * arrays today (documented placeholder representation, not a
 * normalized ingredient entity yet), so this input matches that
 * shape exactly rather than inventing client-side normalization the
 * backend doesn't have. */
export function TagInput({ label, hint, values, onChange }: TagInputProps) {
  const theme = useTheme();
  const [draft, setDraft] = useState("");

  function commit() {
    const trimmed = draft.trim();
    if (trimmed.length === 0) return;
    if (!values.some((v) => v.toLowerCase() === trimmed.toLowerCase())) {
      onChange([...values, trimmed]);
    }
    setDraft("");
  }

  function remove(value: string) {
    onChange(values.filter((v) => v !== value));
  }

  return (
    <View style={{ gap: 8 }}>
      <TextField
        label={label}
        hint={hint}
        value={draft}
        onChangeText={setDraft}
        onSubmitEditing={commit}
        returnKeyType="done"
        placeholder="Type and press done to add"
      />
      {values.length > 0 ? (
        <View style={styles.chipRow}>
          {values.map((value) => (
            <Pressable
              key={value}
              onPress={() => remove(value)}
              accessibilityRole="button"
              accessibilityLabel={`Remove ${value}`}
              style={[styles.chip, { backgroundColor: theme.colors.surfaceMuted, borderRadius: theme.radii.pill }]}
            >
              <Text style={[theme.typography.caption, { color: theme.colors.foreground }]}>{value} ✕</Text>
            </Pressable>
          ))}
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { paddingHorizontal: 12, paddingVertical: 8, minHeight: 36 },
});
