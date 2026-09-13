import { useMutation } from "@tanstack/react-query";

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
