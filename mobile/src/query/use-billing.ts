import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getBillingStatus, syncBillingStatus } from "@/api/billing-api";

import { queryKeys } from "./keys";

/**
 * Server billing status is the ONLY source of truth this app renders
 * from for premium badge / analysis allowance / remaining allowance /
 * whether paid functionality is unlocked (Mobile C2 Part 10) -- never
 * derived from local RevenueCat CustomerInfo alone.
 */
export function useBillingStatusQuery(options: { enabled: boolean }) {
  return useQuery({
    queryKey: queryKeys.billingStatus,
    queryFn: getBillingStatus,
    enabled: options.enabled,
  });
}

export function useBillingSyncMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => syncBillingStatus(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.billingStatus });
    },
  });
}
