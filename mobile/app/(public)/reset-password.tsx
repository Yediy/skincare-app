import { useLocalSearchParams, useRouter } from "expo-router";
import React, { useState } from "react";
import { Text } from "react-native";

import { ApiError } from "@/api/errors";
import { useSession } from "@/auth/session-context";
import { useResetPasswordMutation } from "@/auth/use-auth-actions";
import { Button } from "@/components/button";
import { ErrorState } from "@/components/error-state";
import { Screen } from "@/components/screen";
import { TextField } from "@/components/text-field";
import { useTheme } from "@/theme/theme-provider";

/**
 * Reached via a deep link carrying `?token=...` -- either the app's
 * own `skincare://reset-password?token=...` dev scheme, or (once a
 * production HTTPS reset origin exists) a universal/app link that
 * opens straight into this route. See ACCOUNT_RECOVERY_ARCHITECTURE.md
 * for the distinction; this screen itself doesn't care which brought
 * it here.
 *
 * The token lives ONLY in this component's own React state for the
 * duration of the reset flow -- never written to SecureStore,
 * AsyncStorage, a persisted query cache, or a log line (see
 * src/utils/storage-policy.ts's discipline for why token-shaped values
 * never reach any persistence layer other than the credential-specific
 * one they belong to, which this isn't).
 */
export default function ResetPassword() {
  const theme = useTheme();
  const router = useRouter();
  const { token } = useLocalSearchParams<{ token?: string }>();
  const { signOut } = useSession();
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [success, setSuccess] = useState(false);
  const resetPassword = useResetPasswordMutation();

  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;

  async function handleSubmit() {
    if (!token || mismatch || !newPassword) return;
    resetPassword.mutate(
      { token, newPassword },
      {
        onSuccess: async () => {
          // Defensive: clears any existing local session/token pair
          // regardless of whether this device happened to be signed
          // in as the account that was just reset -- the backend has
          // already revoked every server-side session for that
          // account, so no local credential should survive this
          // point either. signOut() is a no-op past its own local
          // clear if there was nothing stored.
          await signOut();
          setSuccess(true);
        },
      },
    );
  }

  if (!token) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Invalid reset link</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          This password reset link is missing or malformed. Request a new one from the sign-in screen.
        </Text>
        <Button label="Back to sign in" onPress={() => router.replace("/(public)/sign-in")} />
      </Screen>
    );
  }

  if (success) {
    return (
      <Screen>
        <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Password reset</Text>
        <Text style={[theme.typography.body, { color: theme.colors.muted }]}>
          Your password has been reset. Sign in with your new password.
        </Text>
        <Button label="Sign in" onPress={() => router.replace("/(public)/sign-in")} />
      </Screen>
    );
  }

  // /password/reset's 400 always means "invalid, expired, or already-
  // used token" -- shown directly as a generic message, never the raw
  // backend detail string, and never distinguishable from any other
  // 400 cause (section 2's enumeration-safety discipline extends here
  // too: this screen must not help a caller learn *why* a token
  // failed).
  const errorMessage =
    resetPassword.error instanceof ApiError && resetPassword.error.status === 400
      ? "This reset link is invalid or has expired. Request a new one."
      : undefined;

  return (
    <Screen>
      <Text style={[theme.typography.title, { color: theme.colors.foreground }]}>Reset your password</Text>
      <TextField
        label="New password"
        value={newPassword}
        onChangeText={setNewPassword}
        secureTextEntry
        autoCapitalize="none"
        textContentType="newPassword"
      />
      <TextField
        label="Confirm new password"
        value={confirmPassword}
        onChangeText={setConfirmPassword}
        secureTextEntry
        autoCapitalize="none"
        textContentType="newPassword"
        error={mismatch ? "Passwords don't match." : undefined}
      />
      {errorMessage ? (
        <Text style={[theme.typography.body, { color: theme.colors.danger }]}>{errorMessage}</Text>
      ) : resetPassword.isError ? (
        <ErrorState error={resetPassword.error} />
      ) : null}
      <Button
        label="Reset password"
        onPress={handleSubmit}
        loading={resetPassword.isPending}
        disabled={!newPassword || !confirmPassword || mismatch}
      />
      <Button label="Back to sign in" onPress={() => router.replace("/(public)/sign-in")} variant="secondary" />
    </Screen>
  );
}
