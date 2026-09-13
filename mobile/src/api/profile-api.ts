import { authorizedRequest } from "@/auth/auth-client-singleton";
import type { Profile, ProfileUpdateRequest } from "./types";

export function getProfile(): Promise<Profile> {
  return authorizedRequest<Profile>("/profile");
}

export function updateProfile(input: ProfileUpdateRequest): Promise<{ detail: string }> {
  return authorizedRequest<{ detail: string }>("/profile", { method: "PUT", body: input });
}
