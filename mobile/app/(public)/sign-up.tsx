import { useRouter } from "expo-router";
import React, { useState } from "react";
import { Text } from "react-native";

import { ApiError } from "@/api/errors";
import { useSignUpMutation } from "@/auth/use-auth-actions";
import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { TextField } from "@/components/text-field";
import { useTheme } from "@/theme/theme-provider";

const MIN_PASSWORD_LENGTH = 8;

export default function SignUp() {
  const theme = useTheme();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const signUp = useSignUpMutation();

  const passwordTooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH;

  async function handleSubmit() {
    if (passwordTooShort) return;
    signUp.mutate({ email, password }, { onSuccess: () => router.replace("/") });
  }

  const errorMessage =
    signUp.error instanceof ApiError
      ? signUp.error.status === 409
        ? "An account with this email already exists."
        : signUp.error.message
      : undefined;

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Create your account</Text>
      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        textContentType="emailAddress"
        error={errorMessage}
      />
      <TextField
        label="Password"
        value={password}
        onChangeText={setPassword}
        secureTextEntry
        autoCapitalize="none"
        textContentType="newPassword"
        hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
        error={passwordTooShort ? `Password must be at least ${MIN_PASSWORD_LENGTH} characters.` : undefined}
      />
      <Button
        label="Create account"
        onPress={handleSubmit}
        loading={signUp.isPending}
        disabled={!email || !password || passwordTooShort}
      />
      <Button label="Back" onPress={() => router.back()} variant="secondary" />
    </Screen>
  );
}
