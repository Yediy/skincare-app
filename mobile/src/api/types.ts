import type { AuthTokens, ConsentStatus, MeResponse, Profile, ProfileUpdateInput } from "@/types/domain";

export type SignupInput = { email: string; password: string };
export type SignupResponse = { id: string; email: string; created_at: string };

export type LoginInput = { email: string; password: string };
export type LoginResponse = AuthTokens;

export type RefreshInput = { refresh_token: string };
export type RefreshResponse = AuthTokens;

export type LogoutInput = { refresh_token: string };

export type ConsentGrantInput = {
  consent_type?: string;
  policy_version: string;
  purpose: string;
  jurisdiction?: string;
  app_version?: string;
  platform?: string;
};
export type ConsentGrantResponse = { id: string; granted_at: string };

export type ConsentWithdrawInput = { consent_type?: string };

export type {
  AuthTokens,
  ConsentStatus,
  MeResponse,
  Profile,
  ProfileUpdateInput as ProfileUpdateRequest,
};
