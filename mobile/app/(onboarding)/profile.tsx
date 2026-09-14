import { useRouter } from "expo-router";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { Button } from "@/components/button";
import { CheckboxRow } from "@/components/checkbox-row";
import { Screen } from "@/components/screen";
import { useOnboardingForm } from "@/profile/onboarding-form-context";
import { useTheme } from "@/theme/theme-provider";
import type { ExperienceLevel } from "@/types/domain";

const EXPERIENCE_LEVELS: { id: ExperienceLevel; label: string }[] = [
  { id: "beginner", label: "Beginner" },
  { id: "intermediate", label: "Intermediate" },
  { id: "advanced", label: "Advanced" },
];

export default function OnboardingProfile() {
  const theme = useTheme();
  const router = useRouter();
  const { form, update } = useOnboardingForm();

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Your skin, briefly</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
        This helps us keep recommendations appropriate for you. You can change any of this later in Settings.
      </Text>

      <View style={{ gap: 8 }}>
        <Text style={[theme.typography.label, { color: theme.colors.muted }]}>Experience level</Text>
        <View style={styles.chipRow}>
          {EXPERIENCE_LEVELS.map((level) => {
            const selected = form.experience_level === level.id;
            return (
              <Button
                key={level.id}
                label={level.label}
                variant={selected ? "primary" : "secondary"}
                onPress={() => update({ experience_level: level.id })}
              />
            );
          })}
        </View>
      </View>

      <CheckboxRow
        label="I have sensitive skin"
        checked={form.has_sensitive_skin}
        onToggle={() => update({ has_sensitive_skin: !form.has_sensitive_skin })}
      />
      <CheckboxRow
        label="I'm currently pregnant"
        checked={form.is_pregnant}
        onToggle={() => update({ is_pregnant: !form.is_pregnant })}
      />
      <CheckboxRow
        label="I'm currently nursing"
        checked={form.is_nursing}
        onToggle={() => update({ is_nursing: !form.is_nursing })}
      />

      <Button label="Continue" onPress={() => router.push("/(onboarding)/goals")} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  chipRow: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
});
