import { useRouter } from "expo-router";
import React, { useState } from "react";
import { Text } from "react-native";

import { ApiError } from "@/api/errors";
import { useSignInMutation } from "@/auth/use-auth-actions";
import { Button } from "@/components/button";
import { Screen } from "@/components/screen";
import { TextField } from "@/components/text-field";
import { useTheme } from "@/theme/theme-provider";

export default function SignIn() {
  const theme = useTheme();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const signIn = useSignInMutation();

  async function handleSubmit() {
    signIn.mutate(
      { email, password },
      {
        onSuccess: () => router.replace("/"),
      },
    );
  }

  // /login's 401 always means "wrong email or password" -- unlike
  // every other authenticated call's 401, this one is not a session
  // problem at all, so it's shown directly rather than routed through
  // any refresh/logout logic (section 9).
  const errorMessage =
    signIn.error instanceof ApiError
      ? signIn.error.status === 401
        ? "Incorrect email or password."
        : signIn.error.message
      : undefined;

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Sign in</Text>
      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        textContentType="emailAddress"
      />
      <TextField
        label="Password"
        value={password}
        onChangeText={setPassword}
        secureTextEntry
        autoCapitalize="none"
        textContentType="password"
        error={errorMessage}
      />
      <Button label="Sign in" onPress={handleSubmit} loading={signIn.isPending} disabled={!email || !password} />
      <Button label="Back" onPress={() => router.back()} variant="secondary" />
    </Screen>
  );
}
