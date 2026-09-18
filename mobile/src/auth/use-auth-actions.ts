import { useMutation } from "@tanstack/react-query";

import { forgotPassword, resetPassword } from "@/api/auth-api";

import { useSession } from "./session-context";

/** Thin useMutation wrappers around SessionProvider's action
 * functions -- gives screens consistent isPending/error/reset
 * semantics (section 7) without duplicating the actual session-state
 * transitions, which stay owned by session-context.tsx. */
export function useSignInMutation() {
  const { signIn } = useSession();
  return useMutation({
    mutationFn: (input: { email: string; password: string }) => signIn(input.email, input.password),
  });
}

export function useSignUpMutation() {
  const { signUp } = useSession();
  return useMutation({
    mutationFn: (input: { email: string; password: string }) => signUp(input.email, input.password),
  });
}

export function useSignOutMutation() {
  const { signOut } = useSession();
  return useMutation({ mutationFn: signOut });
}

export function useDeleteAccountMutation() {
  const { deleteAccount } = useSession();
  return useMutation({ mutationFn: deleteAccount });
}

/** Unauthenticated -- no session state to touch. Always resolves with
 * the same generic message regardless of account existence (see
 * src/api/auth-api.ts). */
export function useForgotPasswordMutation() {
  return useMutation({
    mutationFn: (input: { email: string }) => forgotPassword(input),
  });
}

/** Unauthenticated. Does not itself change local session state --
 * the reset-password screen clears any existing local session
 * defensively after a successful reset (see app/(public)/reset-password.tsx). */
export function useResetPasswordMutation() {
  return useMutation({
    mutationFn: (input: { token: string; newPassword: string }) =>
      resetPassword({ token: input.token, new_password: input.newPassword }),
  });
}
