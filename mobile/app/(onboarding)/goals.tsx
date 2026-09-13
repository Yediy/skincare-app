import { useRouter } from "expo-router";
import React from "react";
import { Text } from "react-native";

import { Button } from "@/components/button";
import { CheckboxRow } from "@/components/checkbox-row";
import { Screen } from "@/components/screen";
import { SKIN_GOAL_OPTIONS, type SkinGoalId } from "@/constants/goals";
import { useOnboardingForm } from "@/profile/onboarding-form-context";
import { useTheme } from "@/theme/theme-provider";

export default function OnboardingGoals() {
  const theme = useTheme();
  const router = useRouter();
  const { form, update } = useOnboardingForm();

  function toggle(id: SkinGoalId) {
    const has = form.skin_goals.includes(id);
    update({ skin_goals: has ? form.skin_goals.filter((g) => g !== id) : [...form.skin_goals, id] });
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>What are you hoping to work on?</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
        Pick as many as apply. This doesn&apos;t change your results yet -- it helps us know what to build next.
      </Text>

      {SKIN_GOAL_OPTIONS.map((goal) => (
        <CheckboxRow
          key={goal.id}
          label={goal.label}
          description={goal.description}
          checked={form.skin_goals.includes(goal.id)}
          onToggle={() => toggle(goal.id)}
        />
      ))}

      <Button label="Continue" onPress={() => router.push("/(onboarding)/constraints")} />
    </Screen>
  );
}
