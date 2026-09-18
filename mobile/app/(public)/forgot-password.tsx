import { useRouter } from "expo-router";
import React, { useState } from "react";
import { Text } from "react-native";

import { useForgotPasswordMutation } from "@/auth/use-auth-actions";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { Screen } from "@/components/screen";
import { TextField } from "@/components/text-field";
import { useTheme } from "@/theme/theme-provider";

/**
 * Account-enumeration-safe by construction: POST /password/forgot
 * (backend/app/main.py) always returns the same generic response
 * regardless of whether the submitted email belongs to an account, so
 * this screen has no "email not found" branch to accidentally render
 * -- there is only ever one outcome to show after a successful
 * submission, and it is not conditioned on anything the backend told
 * this screen about the account.
 */
export default function ForgotPassword() {
  const theme = useTheme();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const forgotPassword = useForgotPasswordMutation();

  function handleSubmit() {
    forgotPassword.mutate(
      { email },
      { onSuccess: () => setSubmitted(true) },
    );
  }

  if (submitted) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Check your email</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          If an eligible account exists for that email, password reset instructions will be sent.
        </Text>
        <Button label="Back to sign in" onPress={() => router.replace("/(public)/sign-in")} />
      </Screen>
    );
  }

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Forgot password</Text>
      <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
        Enter your email and we&apos;ll send you instructions to reset your password.
      </Text>
      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        textContentType="emailAddress"
      />
      <Button
        label="Send reset instructions"
        onPress={handleSubmit}
        loading={forgotPassword.isPending}
        disabled={!email}
      />
      {/* Genuine failures only (network error, rate limiting, malformed
          email) -- never an "account not found" branch, since the
          backend's response never distinguishes that case. */}
      {forgotPassword.isError ? <ErrorState error={forgotPassword.error} /> : null}
      <Button label="Back" onPress={() => router.back()} variant="secondary" />
    </Screen>
  );
}
