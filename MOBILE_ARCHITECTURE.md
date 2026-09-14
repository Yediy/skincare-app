# Mobile V1 Foundation Architecture

## Scope of this pass

Consumer mobile app (`mobile/`), built with Expo + React Native + TypeScript + Expo Router + TanStack Query + `expo-secure-store`. This pass covers exactly: application shell, navigation, API client, authentication, session restoration, consent, user profile, skin goals, safety constraints (allergies/avoid-ingredients/pregnancy/nursing), settings/account shell, and mobile test infrastructure.

**Explicitly NOT in this pass** (see "Deferred" below): camera capture, facial image upload, async analysis UI, analysis results, routine UI, product recommendations, RevenueCat native SDK/paywall, purchase flow, progress/history, notifications, social/community.

## Repository structure — IMPLEMENTED

```
mobile/
  app/                 Expo Router route files ONLY -- no components/services/logic
    _layout.tsx         provider composition (Theme, QueryClient, Session)
    index.tsx            the one always-valid "/" route; maps useBootstrapRoute() to a redirect
    (public)/            welcome, sign-in, sign-up
    (onboarding)/         consent, profile, goals, constraints
    (app)/                index (home), profile, settings
  src/
    api/                one typed fetch boundary: client.ts, errors.ts, auth-api.ts, profile-api.ts, consent-api.ts, types.ts
    auth/               session state machine, refresh coordinator, SecureStore adapter/token storage
    components/         reusable UI primitives (Screen, Button, TextField, TagInput, CheckboxRow, Loading/Error/EmptyState)
    constants/          config.ts (env), goals.ts (skin-goal vocabulary)
    hooks/              use-bootstrap-route.ts -- the one navigation decision boundary
    navigation/         bootstrap-route.ts -- the pure decision function itself
    profile/            profile queries/mutations, onboarding-form-context.tsx
    query/               QueryClient, query keys, consent hooks
    theme/               design tokens + ThemeProvider
    types/               shared domain types mirroring backend response shapes exactly
    utils/               logger.ts (redaction), storage-policy.ts (documented policy)
  tests/                mirrors src/ -- see "Test coverage" below
```

TypeScript path aliases: `@/*` -> `src/*`, `@app/*` -> `app/*` (`mobile/tsconfig.json`).

## Expo foundation — IMPLEMENTED, TESTED

Expo SDK 57 (latest stable at the time of this pass), managed workflow -- no `ios/`/`android/` directories, no custom native modules. `expo-doctor` reports 21/21 checks passing. Scripts: `npm install`, `npx expo start`, `npm test`, `npm run typecheck`, `npm run lint`.

## Navigation architecture — IMPLEMENTED, TESTED

Expo Router with four route groups: `(public)`, `(onboarding)`, `(app)`, plus the required always-valid `/` (`app/index.tsx`).

**One centralized decision boundary** (`src/navigation/bootstrap-route.ts::resolveBootstrapRoute`, a pure function, unit-tested directly with no rendering): given session status plus (once authenticated) consent/profile query results, it returns exactly one of `loading | public | onboarding-consent | onboarding-profile | app`. `app/index.tsx` maps that output to a `<Redirect>` -- no other screen calls `router.replace()`/`router.push()` based on session/onboarding state. Every route group's own `_layout.tsx` re-checks the same boundary (`(public)` and `(onboarding)` check session status; `(app)` re-runs the full `useBootstrapRoute()`) as defense in depth against a stale deep link landing on a screen that hasn't earned it.

Decision table (`tests/navigation/bootstrap-route.test.ts` proves each row):

| session | consent | profile | route |
|---|---|---|---|
| UNKNOWN | (any, even loaded) | (any) | `loading` |
| SIGNED_OUT | - | - | `public` |
| AUTHENTICATED | loading | (any) | `loading` |
| AUTHENTICATED | invalid/missing | (any) | `onboarding-consent` |
| AUTHENTICATED | valid | not set | `onboarding-profile` |
| AUTHENTICATED | valid | set | `app` |

## Session state — IMPLEMENTED, TESTED

Exactly three states (`src/auth/restore-session.ts::SessionState`): `UNKNOWN | SIGNED_OUT | AUTHENTICATED`. `SessionProvider` (`src/auth/session-context.tsx`) starts at `UNKNOWN` and only a completed `restoreSession()` call ever moves it -- no screen renders the signed-out interface before that resolves (`app/index.tsx` renders a loading state for `UNKNOWN`).

`restoreSession()` deliberately makes **no network call** -- it reads only whether a refresh token exists in SecureStore. This is what makes bootstrap fast and offline-safe: a dead network at launch can never be confused with "no credentials" (see Offline behavior below). Whether the *access* token is actually still valid is discovered lazily, the first time any authenticated query runs, through the exact same refresh-coordinator every other authenticated call uses.

Credentials live ONLY in `expo-secure-store`, through one seam: `SecureStoreAdapter` (`src/auth/secure-store-adapter.ts`) is an interface with a real (`createExpoSecureStoreAdapter`) and an in-memory fake (`createInMemorySecureStoreAdapter`) implementation; `TokenStorage` (`src/auth/token-storage.ts`) wraps whichever adapter is injected. Every unit test uses the fake -- no test ever touches the real device keychain (`tests/auth/token-storage.test.ts`, `tests/auth/restore-session.test.ts`, `tests/auth/refresh-coordinator.test.ts`).

## API client — IMPLEMENTED, TESTED

One typed fetch boundary (`src/api/`): `client.ts` (`request()`, no auth-awareness, deliberately -- this is what keeps signup/login/refresh's own calls from ever touching the refresh machinery), `errors.ts` (`ApiError`, `ApiErrorCode`), `auth-api.ts`, `profile-api.ts`, `consent-api.ts`, `types.ts`. No route component ever calls `fetch()` directly.

`ApiError` carries `status`, `code`, `message` (always a short, safe, user-facing string -- the raw backend response body is never surfaced), `requestId?` (the backend does not currently set one; wired for when it does), `retryable`, `retryAfterSeconds?` (parsed from a 429's `Retry-After` header). `code` is classified from the HTTP status code, since `backend/app/main.py` has no `code`/`request_id` convention on its own error bodies -- every backend error is `{"detail": "<message>"}` or, for 422, FastAPI's default validation array, and `client.ts` extracts a safe message from either shape without ever parsing structure the backend doesn't actually promise.

Deliberately handled, each with its own classified `ApiErrorCode` (`tests/api/client.test.ts`): network unavailable (`NETWORK_ERROR`), timeout (`TIMEOUT`, 15s), 401/403/404/409/422/429/5xx, and malformed JSON on an otherwise-2xx response (`MALFORMED_RESPONSE`, not a crash).

## Environment config — IMPLEMENTED, TESTED

`EXPO_PUBLIC_API_BASE_URL` (`.env.example`; `src/constants/config.ts`). Production builds (`!__DEV__`) require an `https://` origin -- enforced by throwing at module load, not merely documented (`tests/utils/config.test.ts`). Nothing under `EXPO_PUBLIC_*` is or will be a secret; production requires it as an explicit, deliberate value, never a hardcoded default.

## TanStack Query — IMPLEMENTED, TESTED

One `QueryClient` (`src/query/query-client.ts`), created once in `app/_layout.tsx`. Stable query keys (`src/query/keys.ts`): `["me"]`, `["profile"]`, `["consent"]`. Mutations for login/signup/logout/account-deletion (`src/auth/use-auth-actions.ts`, wrapping `SessionProvider`'s action functions) and profile/consent updates (`src/profile/queries.ts`, `src/query/use-consent.ts`), each invalidating its own query key on success. The Query cache is never persisted to disk -- it holds authenticated data (profile, consent, allergies, pregnancy/nursing) in memory for the running session only, and is explicitly `.clear()`-ed on sign-out, account deletion, and refresh failure (see Session state above).

## Authentication — IMPLEMENTED, TESTED

Wired to the **existing** backend auth routes (`backend/app/main.py`) -- no duplicate auth surface was invented: `POST /signup`, `POST /login`, `POST /refresh`, `POST /logout` (all unauthenticated, matching the backend's own dependency wiring), `GET /me`, `POST /logout-all`, `DELETE /me` (all Bearer-authenticated).

**Single-flight refresh** (`src/auth/refresh-coordinator.ts::createRefreshCoordinator`, the single most safety-critical piece of this pass, proven under real concurrency in `tests/auth/refresh-coordinator.test.ts`): `authorizedRequest()` attaches the current access token; on a 401 it calls `refreshOnce()`, which memoizes one in-flight `Promise` so N concurrent 401s produce exactly one `POST /refresh` call -- every other caller awaits that same promise and retries its own original request exactly once with the new access token. This matters because the backend's refresh-token rotation is single-use with family-wide revocation on replay (`backend/app/main.py`'s `/refresh` docstring) -- two concurrent refresh calls using the same refresh token would collide with that defense and could revoke a legitimate session. Never refreshes more than once per call (a 401 on the post-refresh retry itself is propagated, not refreshed again).

On successful refresh: access + refresh tokens are replaced together in `SecureStore` before any waiter reads the new access token. On a **definite** backend rejection of the refresh token (401/403 from `POST /refresh`): SecureStore is cleared and the registered session-invalid listener fires (`SessionProvider` clears the Query cache and transitions to `SIGNED_OUT`). On a network/timeout/5xx failure calling `/refresh` itself: the session is deliberately **preserved** -- see Offline behavior.

## Authorization failure classification — IMPLEMENTED

Not every 401 means "bad password." `/login`'s own 401 is shown directly as "incorrect email or password" (`app/(public)/sign-in.tsx`) -- it is the one 401 that is never routed through the refresh coordinator, since `/login` is an unauthenticated call. Every other 401 (an expired access token, a revoked family, a disabled/deleted account -- `backend/app/security/auth.py::get_current_user` returns 401 for all three, by design, since the corrective action is identical: refresh or log out) goes through `refreshOnce()` uniformly. Account deletion revokes the user's refresh-token family server-side, so a deleted account's session ends the first time any authenticated call's refresh attempt hits that revoked family.

## Consent — IMPLEMENTED, TESTED

Wired to the **existing** consent ledger (`backend/app/db/consent_repository.py`) with one additive, read-only backend change: **`GET /consent`** (new). Before this pass, `consent_events` was write-only from the API's perspective (`POST /consent`, `POST /consent/withdraw` existed; nothing read current status), which made "does this user need to (re-)consent" undecidable from backend truth alone -- exactly the local-boolean trap section 10 of this pass's brief explicitly forbids. `GET /consent` returns `{consent_type, required_policy_version, has_valid_consent}`, backed by the pre-existing `has_valid_consent()`/`REQUIRED_POLICY_VERSION` (never a value hardcoded client-side). Granting consent (`app/(onboarding)/consent.tsx`) always uses the `required_policy_version` value this endpoint returns. If the backend's required version is ever bumped, `has_valid_consent` flips to `false` and the bootstrap boundary routes back into onboarding-consent automatically -- no local boolean to go stale.

Consent copy makes no diagnostic or clinical-validation claim; it states what the (future) analysis does, that photo processing is temporary, and that consent is withdrawable at any time from Settings.

## Profile, skin goals, and constraints — IMPLEMENTED, TESTED

Wired to the **existing** `GET`/`PUT /profile` (`backend/app/db/profile_repository.py`), with two additive backend changes, both documented, tested, and non-breaking to the one existing caller (`PlanService`/`/analyze`):

1. **`profile_set: bool`** on the `GET /profile` response -- `true` only once a user has ever explicitly called `PUT /profile`, `false` when they've never set one (previously indistinguishable: an unset profile and a profile explicitly set to every default value returned the identical body). This is the field `bootstrap-route.ts` gates onboarding-profile on; without it, "authenticated + required profile incomplete -> onboarding profile" (this pass's own required decision rule) would be undecidable.
2. **`skin_goals: string[]`** (migration `fd8df981ea49`, plain array column on `user_profiles`, same placeholder shape as the pre-existing `allergies`/`avoid_ingredients`) -- self-reported subset of `backend/app/domain/priorities.py::PRIORITIES`' own concern vocabulary (`src/constants/goals.ts` mirrors those ids and labels directly, never inventing a concept the backend can't name). Purely informational for now: nothing in `PlanService`/`SafetyEngine` reads this column yet, and the goals screen's own copy says so ("This doesn't change your results yet").

The onboarding wizard (`(onboarding)/profile.tsx` -> `goals.tsx` -> `constraints.tsx`) collects fields across three screens but submits exactly once, on the last screen, via `OnboardingFormProvider` (`src/profile/onboarding-form-context.tsx`) -- there is no partial-patch endpoint, so a later step never has a chance to silently overwrite an earlier step's answer with a stale default. Allergies/ingredients-to-avoid are collected as plain free-text arrays (`src/components/tag-input.tsx`), matching the backend's own current (documented placeholder) representation exactly -- this pass does not invent client-side ingredient normalization the backend doesn't have.

## Application home — IMPLEMENTED

`app/(app)/index.tsx`: welcome, profile/consent status, and an explicit "Skin analysis arrives in the next build phase" placeholder -- never a fabricated result or a seeded fake recommendation.

## Settings / account — IMPLEMENTED, TESTED

Profile, Privacy (consent status + withdraw), Sign Out, Delete Account (confirmation required via a native `Alert`, backed by the existing `DELETE /me`), App version. Both sign-out and account deletion clear SecureStore and the Query cache and return to the public flow -- proven identical via the shared `TokenStorage.clear()` path (`tests/auth/token-storage.test.ts`).

## Privacy / local storage policy — IMPLEMENTED

Documented in `src/utils/storage-policy.ts`. Allowed: auth credentials in `expo-secure-store` only, via `src/auth/secure-store-adapter.ts`. Not allowed anywhere in this app: raw facial photos or analysis image data (this pass adds none at all -- Phase B owns that pipeline), bearer/refresh tokens in AsyncStorage/SQLite/a plain file/a persisted Query cache, or any of the above in a log line.

## Logging — IMPLEMENTED, TESTED

`src/utils/logger.ts` is the one place this app writes to the console; every call is redacted through a key-substring denylist (passwords, tokens, `Authorization`, allergy/pregnancy/nursing fields, image/photo/base64 data), recursively, before anything reaches `console.*` (`tests/utils/logger.test.ts`).

## UX foundation — IMPLEMENTED

Compact token system (`src/theme/tokens.ts`): spacing, radii, typography, and a semantic color set (background/surface/foreground/muted/border/accent/danger/success), each with a light and dark palette (`useColorScheme`-driven, `src/theme/theme-provider.tsx`). `Screen` (`src/components/screen.tsx`) wraps every route's content with `SafeAreaView` + `ScrollView contentInsetAdjustmentBehavior="automatic"`.

## Accessibility — IMPLEMENTED

`accessibilityRole`/`accessibilityLabel`/`accessibilityState` on every interactive primitive (`Button`, `TextField`, `CheckboxRow`, `TagInput`); minimum 48px touch targets; error text is always visible copy (`accessibilityLiveRegion="polite"`), never color alone; `CheckboxRow`'s checked state is carried by a distinct glyph plus `accessibilityState`, not color alone.

## Forms — IMPLEMENTED

Reusable primitives (`TextField`, `CheckboxRow`, `TagInput`, `Button`) so no screen re-implements validation/error-placement/loading/disabled state independently. Client-side validation (e.g. sign-up's minimum password length) mirrors the backend's own contract (`SignupRequest.password: Field(min_length=8, max_length=72)`) rather than inventing a separate rule.

## Loading / error / empty states — IMPLEMENTED

`LoadingState`, `ErrorState`, `EmptyState` (`src/components/`). `ErrorState` always renders `ApiError.message` (never a raw body) and offers "Try again" only when `ApiError.retryable` (or the error is an authorization failure a retry can plausibly resolve).

## Offline behavior — IMPLEMENTED, TESTED

"No network" is never confused with "invalid account" (`tests/auth/refresh-coordinator.test.ts::"network/server failure during refresh itself does NOT end the session"`): a network/timeout/5xx failure while calling `POST /refresh` leaves SecureStore and the refresh token untouched and does **not** fire the session-invalid listener -- only a definite 401/403 rejection of the refresh token itself does. Bootstrap (`restoreSession()`) makes no network call at all, so a dead network at launch can never look like "no credentials" either. This pass does not cache consent/profile writes offline -- that remains a synchronous, online-only mutation.

## Test coverage — TESTED

`mobile/tests/` (Jest + `jest-expo` + `@testing-library/react-native`), 51 tests, mirroring `src/`:

- `tests/auth/`: `token-storage.test.ts` (SecureStore-adapter writes/clears), `restore-session.test.ts` (UNKNOWN/no-creds/has-creds, never touches the network), `refresh-coordinator.test.ts` (success with no refresh; expired-access-token one-refresh-then-retry; **N-concurrent-401s-exactly-one-refresh-call**; definite-rejection secure logout; transient-failure session preservation; no stored refresh token fails closed; never refreshes more than once).
- `tests/navigation/bootstrap-route.test.ts`: every row of the decision table above.
- `tests/api/client.test.ts`: 401/403/404/409/422/429/500/503, network error, malformed JSON, 422-array message extraction, generic-fallback-on-unshaped-detail, 204, `Retry-After` parsing.
- `tests/profile/`: `profile-api.test.ts`, `consent-api.test.ts` -- successful mutation + backend error propagation, against a mocked `authorizedRequest` boundary (never the real SecureStore-backed singleton).
- `tests/utils/`: `logger.test.ts` (redaction), `config.test.ts` (production HTTPS enforcement).

All of this runs against fakes/mocks injected at the seams described above -- no test touches the real device keychain or a live backend.

## DEFERRED (tracked in `OPEN_ENGINEERING_ITEMS.md`)

- Camera capture, facial image upload, the ephemeral capture pipeline.
- Async analysis submission UI, analysis result visualization.
- Routine UI, product recommendation UI.
- RevenueCat mobile SDK, paywall, purchase flow.
- Progress/history, Personal Baseline, Outcome Engine, Digital Twin.
- Social feed, chat, voice/video, push notifications.
- Offline-first writes (consent/profile mutations require a live connection in this pass).
- A native iOS/Android CI build (this pass's CI runs `npm ci`/typecheck/lint/test only, matching the brief's explicit instruction not to rebuild native binaries on every PR in Phase A).
