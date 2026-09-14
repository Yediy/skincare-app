import { useRouter } from "expo-router";
import React from "react";
import { Text } from "react-native";

import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { Screen } from "@/components/screen";
import { TagInput } from "@/components/tag-input";
import { useOnboardingForm } from "@/profile/onboarding-form-context";
import { useUpdateProfileMutation } from "@/profile/queries";
import { useTheme } from "@/theme/theme-provider";

/**
 * The last onboarding screen -- this is where the accumulated
 * OnboardingFormProvider state (profile.tsx + goals.tsx + this
 * screen) is submitted as ONE PUT /profile call. Backend safety
 * logic (SafetyEngine) resolves these against the normalized
 * ingredient catalog itself; this screen just collects the raw,
 * self-reported strings the backend already expects on this field
 * (backend/app/db/profile_repository.py) -- it does not attempt its
 * own normalization.
 */
export default function OnboardingConstraints() {
  const theme = useTheme();
  const router = useRouter();
  const { form, update } = useOnboardingForm();
  const updateProfile = useUpdateProfileMutation();

  function handleFinish() {
    updateProfile.mutate(form, { onSuccess: () => router.replace("/") });
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Anything we should avoid?</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
        This directly affects which products and routine steps we&apos;re willing to suggest.
      </Text>

      <TagInput
        label="Known allergies"
        hint="e.g. fragrance, nuts"
        values={form.allergies}
        onChange={(allergies) => update({ allergies })}
      />
      <TagInput
        label="Ingredients you want to avoid"
        hint="e.g. sulfates, alcohol"
        values={form.avoid_ingredients}
        onChange={(avoid_ingredients) => update({ avoid_ingredients })}
      />

      <Button label="Finish" onPress={handleFinish} loading={updateProfile.isPending} />
      {updateProfile.isError ? <ErrorState error={updateProfile.error} onRetry={handleFinish} /> : null}
    </Screen>
  );
}
