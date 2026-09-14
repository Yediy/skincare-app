import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getConsentStatus, grantConsent, withdrawConsent } from "@/api/consent-api";
import type { ConsentGrantInput } from "@/api/types";

import { queryKeys } from "./keys";

export function useConsentQuery(options: { enabled: boolean }) {
  return useQuery({
    queryKey: queryKeys.consent,
    queryFn: getConsentStatus,
    enabled: options.enabled,
  });
}

export function useGrantConsentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ConsentGrantInput) => grantConsent(input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.consent });
    },
  });
}

export function useWithdrawConsentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => withdrawConsent(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.consent });
    },
  });
}
