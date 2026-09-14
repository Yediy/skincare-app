import { useSession } from "@/auth/session-context";
import { resolveBootstrapRoute, type BootstrapRoute } from "@/navigation/bootstrap-route";
import { useConsentQuery } from "@/query/use-consent";
import { useProfileQuery } from "@/profile/queries";

export type UseBootstrapRouteResult = {
  route: BootstrapRoute;
  isFetching: boolean;
  /** Only meaningful when route === "bootstrap-error" -- the first
   * failed gating query's error (an ApiError in practice), so an
   * error screen can classify offline/timeout/backend-failure and
   * show the right copy via src/api/errors.ts's genericMessageFor. */
  error: unknown;
  /** Refetches whichever gating quer(y/ies) actually failed. Safe to
   * call from a Retry button regardless of which query (or both)
   * errored -- react-query no-ops a refetch() on a query that isn't
   * currently in an error/stale state. */
  retry: () => void;
};

/**
 * The only place app/_layout.tsx needs to look to decide what to
 * render. Fetches consent/profile only once actually authenticated
 * (queries are `enabled: isAuthenticated`) -- a signed-out user never
 * fires an authorized request that would just 401.
 *
 * Passes `isError` (not just `data`) into resolveBootstrapRoute --
 * see that function's own docstring for why the ordering of the
 * error-vs-loading check there matters. This hook itself never
 * touches session/token state on a query failure -- that stays the
 * refresh coordinator's job for a definite 401/403 (see
 * src/auth/refresh-coordinator.ts); a transient network/timeout/5xx
 * failure here only ever affects what this hook returns, never the
 * stored session.
 */
export function useBootstrapRoute(): UseBootstrapRouteResult {
  const { state } = useSession();
  const isAuthenticated = state.status === "AUTHENTICATED";

  const consentQuery = useConsentQuery({ enabled: isAuthenticated });
  const profileQuery = useProfileQuery({ enabled: isAuthenticated });

  const route = resolveBootstrapRoute({
    session: state,
    consent: { data: consentQuery.data, isError: consentQuery.isError },
    profile: { data: profileQuery.data, isError: profileQuery.isError },
  });

  return {
    route,
    isFetching: consentQuery.isFetching || profileQuery.isFetching,
    error: consentQuery.error ?? profileQuery.error,
    retry: () => {
      if (consentQuery.isError) consentQuery.refetch();
      if (profileQuery.isError) profileQuery.refetch();
    },
  };
}
