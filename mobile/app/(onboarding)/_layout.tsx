import { Redirect, Stack } from "expo-router";
import React from "react";

import { useSession } from "@/auth/session-context";
import { OnboardingFormProvider } from "@/profile/onboarding-form-context";

export default function OnboardingLayout() {
  const { state } = useSession();
  if (state.status !== "AUTHENTICATED") {
    return <Redirect href="/" />;
  }
  return (
    <OnboardingFormProvider>
      <Stack screenOptions={{ headerShown: false }} />
    </OnboardingFormProvider>
  );
}
