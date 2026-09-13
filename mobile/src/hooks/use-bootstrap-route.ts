import { useSession } from "@/auth/session-context";
import { resolveBootstrapRoute, type BootstrapRoute } from "@/navigation/bootstrap-route";
import { useConsentQuery } from "@/query/use-consent";
import { useProfileQuery } from "@/profile/queries";

/**
 * The only place app/_layout.tsx needs to look to decide what to
 * render. Fetches consent/profile only once actually authenticated
 * (queries are `enabled: isAuthenticated`) -- a signed-out user never
 * fires an authorized request that would just 401.
 */
export function useBootstrapRoute(): { route: BootstrapRoute; isFetching: boolean } {
  const { state } = useSession();
  const isAuthenticated = state.status === "AUTHENTICATED";

  const consentQuery = useConsentQuery({ enabled: isAuthenticated });
  const profileQuery = useProfileQuery({ enabled: isAuthenticated });

  const route = resolveBootstrapRoute({
    session: state,
    consent: consentQuery.data,
    profile: profileQuery.data,
  });

  return {
    route,
    isFetching: consentQuery.isFetching || profileQuery.isFetching,
  };
}
