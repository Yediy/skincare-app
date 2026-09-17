import { authorizedRequest, postRefresh } from "@/auth/auth-client-singleton";
import type { MeResponse } from "@/types/domain";

import { request } from "./client";
import type {
  ForgotPasswordInput,
  ForgotPasswordResponse,
  LoginInput,
  LoginResponse,
  LogoutInput,
  RefreshResponse,
  ResetPasswordInput,
  ResetPasswordResponse,
  SignupInput,
  SignupResponse,
} from "./types";

/** /signup, /login, /refresh, /logout, /password/forgot, /password/reset
 * all require no Bearer token (backend/app/main.py) -- plain,
 * unauthenticated `request()`. */
export function signup(input: SignupInput): Promise<SignupResponse> {
  return request<SignupResponse>("/signup", { method: "POST", body: input });
}

export function login(input: LoginInput): Promise<LoginResponse> {
  return request<LoginResponse>("/login", { method: "POST", body: input });
}

export function refresh(refreshToken: string): Promise<RefreshResponse> {
  return postRefresh(refreshToken);
}

export function logout(input: LogoutInput): Promise<{ detail: string }> {
  return request<{ detail: string }>("/logout", { method: "POST", body: input });
}

/** Always resolves with the same generic message shape, whether or
 * not an account exists for the submitted email (backend/app/main.py's
 * POST /password/forgot) -- this function itself has no enumeration-
 * safety logic of its own to get wrong, since the backend already
 * guarantees an identical response either way. */
export function forgotPassword(input: ForgotPasswordInput): Promise<ForgotPasswordResponse> {
  return request<ForgotPasswordResponse>("/password/forgot", { method: "POST", body: input });
}

/** Rejects with ApiError (400) for any invalid/expired/already-used
 * token -- never distinguishes which case, matching the backend's own
 * generic failure response. */
export function resetPassword(input: ResetPasswordInput): Promise<ResetPasswordResponse> {
  return request<ResetPasswordResponse>("/password/reset", { method: "POST", body: input });
}

/** /me, /logout-all, DELETE /me all require Bearer auth. */
export function me(): Promise<MeResponse> {
  return authorizedRequest<MeResponse>("/me");
}

export function logoutAll(): Promise<{ detail: string }> {
  return authorizedRequest<{ detail: string }>("/logout-all", { method: "POST" });
}

export function deleteAccount(): Promise<{ detail: string }> {
  return authorizedRequest<{ detail: string }>("/me", { method: "DELETE" });
}
