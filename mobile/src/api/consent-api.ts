import { authorizedRequest } from "@/auth/auth-client-singleton";
import type { ConsentGrantInput, ConsentGrantResponse, ConsentStatus, ConsentWithdrawInput } from "./types";

export function getConsentStatus(): Promise<ConsentStatus> {
  return authorizedRequest<ConsentStatus>("/consent");
}

export function grantConsent(input: ConsentGrantInput): Promise<ConsentGrantResponse> {
  return authorizedRequest<ConsentGrantResponse>("/consent", { method: "POST", body: input });
}

export function withdrawConsent(input: ConsentWithdrawInput = {}): Promise<{ detail: string }> {
  return authorizedRequest<{ detail: string }>("/consent/withdraw", { method: "POST", body: input });
}
