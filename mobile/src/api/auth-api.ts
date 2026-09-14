import { authorizedRequest, postRefresh } from "@/auth/auth-client-singleton";
import type { MeResponse } from "@/types/domain";

import { request } from "./client";
import type {
  LoginInput,
  LoginResponse,
  LogoutInput,
  RefreshResponse,
  SignupInput,
  SignupResponse,
} from "./types";

/** /signup, /login, /refresh, /logout all require no Bearer token
 * (backend/app/main.py) -- plain, unauthenticated `request()`. */
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
