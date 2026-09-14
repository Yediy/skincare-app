import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getProfile, updateProfile } from "@/api/profile-api";
import { queryKeys } from "@/query/keys";
import type { ProfileUpdateRequest } from "@/api/types";

export function useProfileQuery(options: { enabled: boolean }) {
  return useQuery({
    queryKey: queryKeys.profile,
    queryFn: getProfile,
    enabled: options.enabled,
  });
}

export function useUpdateProfileMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProfileUpdateRequest) => updateProfile(input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.profile });
    },
  });
}
