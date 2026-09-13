import { useRouter } from "expo-router";
import React, { useState } from "react";
import { Text, View } from "react-native";

import { Button } from "@/components/button";
import { CheckboxRow } from "@/components/checkbox-row";
import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { Screen } from "@/components/screen";
import { TagInput } from "@/components/tag-input";
import { SKIN_GOAL_OPTIONS, type SkinGoalId } from "@/constants/goals";
import { useProfileQuery, useUpdateProfileMutation } from "@/profile/queries";
import { useTheme } from "@/theme/theme-provider";
import type { ExperienceLevel, ProfileUpdateInput } from "@/types/domain";

const EXPERIENCE_LEVELS: { id: ExperienceLevel; label: string }[] = [
  { id: "beginner", label: "Beginner" },
  { id: "intermediate", label: "Intermediate" },
  { id: "advanced", label: "Advanced" },
];

export default function ProfileScreen() {
  const theme = useTheme();
  const router = useRouter();
  const profileQuery = useProfileQuery({ enabled: true });
  const updateProfile = useUpdateProfileMutation();
  const [draft, setDraft] = useState<ProfileUpdateInput | null>(null);
  // Seeds local edit state from the fetched profile exactly once, the
  // first render after it arrives -- adjusted during render rather
  // than in an effect (React's own recommended pattern for "derive
  // state from a prop/query result once," see
  // https://react.dev/learn/you-might-not-need-an-effect), so a
  // later refetch (e.g. after Save) never clobbers in-progress edits.
  const [seededFrom, setSeededFrom] = useState<typeof profileQuery.data>(undefined);
  if (profileQuery.data && profileQuery.data !== seededFrom) {
    const { profile_set, ...rest } = profileQuery.data;
    setSeededFrom(profileQuery.data);
    setDraft(rest);
  }

  if (profileQuery.isLoading || !draft) {
    return <LoadingState />;
  }
  if (profileQuery.isError) {
    return <ErrorState error={profileQuery.error} onRetry={() => profileQuery.refetch()} />;
  }

  function patch(fields: Partial<ProfileUpdateInput>) {
    setDraft((prev) => (prev ? { ...prev, ...fields } : prev));
  }

  function toggleGoal(id: SkinGoalId) {
    if (!draft) return;
    const has = draft.skin_goals.includes(id);
    patch({ skin_goals: has ? draft.skin_goals.filter((g) => g !== id) : [...draft.skin_goals, id] });
  }

  function handleSave() {
    if (!draft) return;
    updateProfile.mutate(draft, { onSuccess: () => router.back() });
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Your profile</Text>

      <View style={{ gap: 8 }}>
        <Text style={[theme.typography.label, { color: theme.colors.muted }]}>Experience level</Text>
        <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
          {EXPERIENCE_LEVELS.map((level) => (
            <Button
              key={level.id}
              label={level.label}
              variant={draft.experience_level === level.id ? "primary" : "secondary"}
              onPress={() => patch({ experience_level: level.id })}
            />
          ))}
        </View>
      </View>

      <CheckboxRow
        label="I have sensitive skin"
        checked={draft.has_sensitive_skin}
        onToggle={() => patch({ has_sensitive_skin: !draft.has_sensitive_skin })}
      />
      <CheckboxRow
        label="I'm currently pregnant"
        checked={draft.is_pregnant}
        onToggle={() => patch({ is_pregnant: !draft.is_pregnant })}
      />
      <CheckboxRow
        label="I'm currently nursing"
        checked={draft.is_nursing}
        onToggle={() => patch({ is_nursing: !draft.is_nursing })}
      />

      <View style={{ gap: 8 }}>
        <Text style={[theme.typography.label, { color: theme.colors.muted }]}>Skin goals</Text>
        {SKIN_GOAL_OPTIONS.map((goal) => (
          <CheckboxRow
            key={goal.id}
            label={goal.label}
            description={goal.description}
            checked={draft.skin_goals.includes(goal.id)}
            onToggle={() => toggleGoal(goal.id)}
          />
        ))}
      </View>

      <TagInput label="Known allergies" values={draft.allergies} onChange={(allergies) => patch({ allergies })} />
      <TagInput
        label="Ingredients you want to avoid"
        values={draft.avoid_ingredients}
        onChange={(avoid_ingredients) => patch({ avoid_ingredients })}
      />

      <Button label="Save" onPress={handleSave} loading={updateProfile.isPending} />
      {updateProfile.isError ? <ErrorState error={updateProfile.error} onRetry={handleSave} /> : null}
    </Screen>
  );
}
