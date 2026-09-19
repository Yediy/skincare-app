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

## Password recovery — IMPLEMENTED, TESTED (V1 account recovery pass)

Two new public routes, wired to the **existing**-shape backend auth
surface's newest members, `POST /password/forgot` / `POST
/password/reset`:

- `app/(public)/forgot-password.tsx` -- email entry, linked from
  `sign-in.tsx`'s new "Forgot password?" button. Renders exactly one
  generic confirmation on success, regardless of what the backend
  actually did (no "account not found" branch exists in this screen at
  all -- the backend's own response never distinguishes that case
  either, see `ACCOUNT_RECOVERY_ARCHITECTURE.md` section 5).
- `app/(public)/reset-password.tsx` -- reached via a deep link carrying
  `?token=...` (the app's existing `skincare` scheme in dev; a future
  HTTPS universal/app-link origin in production, see
  `ACCOUNT_RECOVERY_ARCHITECTURE.md` section 10). Reads the token with
  `useLocalSearchParams`; new/confirm password fields with client-side
  mismatch validation; on success, defensively clears any local
  session (`useSession().signOut()`) and routes to sign-in -- never
  auto-authenticates. **The reset token lives only in this component's
  own React state** for the duration of the flow: never written to
  SecureStore, AsyncStorage, SQLite, the filesystem, the persisted
  TanStack Query cache, or a log line.

Both screens reuse the existing form primitives (`Button`, `Screen`,
`TextField`), theme, and `ErrorState` conventions -- no new component
system was introduced. `src/api/auth-api.ts` gained `forgotPassword`/
`resetPassword`, both plain unauthenticated `request()` calls, and
`src/auth/use-auth-actions.ts` gained matching mutation hooks that do
not touch session state themselves (the reset screen owns the explicit
post-success session clear).

## Consent — IMPLEMENTED, TESTED

Wired to the **existing** consent ledger (`backend/app/db/consent_repository.py`) with one additive, read-only backend change: **`GET /consent`** (new). Before this pass, `consent_events` was write-only from the API's perspective (`POST /consent`, `POST /consent/withdraw` existed; nothing read current status), which made "does this user need to (re-)consent" undecidable from backend truth alone -- exactly the local-boolean trap section 10 of this pass's brief explicitly forbids. `GET /consent` returns `{consent_type, required_policy_version, has_valid_consent}`, backed by the pre-existing `has_valid_consent()`/`REQUIRED_POLICY_VERSION` (never a value hardcoded client-side). Granting consent (`app/(onboarding)/consent.tsx`) always uses the `required_policy_version` value this endpoint returns. If the backend's required version is ever bumped, `has_valid_consent` flips to `false` and the bootstrap boundary routes back into onboarding-consent automatically -- no local boolean to go stale.

Consent copy makes no diagnostic or clinical-validation claim; it states what the (future) analysis does, that photo processing is temporary, and that consent is withdrawable at any time from Settings.

## Profile, skin goals, and constraints — IMPLEMENTED, TESTED

Wired to the **existing** `GET`/`PUT /profile` (`backend/app/db/profile_repository.py`), with two additive backend changes, both documented, tested, and non-breaking to the one existing caller (`PlanService`/`/analyze`):

1. **`profile_set: bool`** on the `GET /profile` response -- `true` only once a user has ever explicitly called `PUT /profile`, `false` when they've never set one (previously indistinguishable: an unset profile and a profile explicitly set to every default value returned the identical body). This is the field `bootstrap-route.ts` gates onboarding-profile on; without it, "authenticated + required profile incomplete -> onboarding profile" (this pass's own required decision rule) would be undecidable.
2. **`skin_goals: string[]`** (migration `fd8df981ea49`, plain array column on `user_profiles`, same placeholder shape as the pre-existing `allergies`/`avoid_ingredients`) -- self-reported subset of `backend/app/domain/priorities.py::PRIORITIES`' own concern vocabulary (`src/constants/goals.ts` mirrors those ids and labels directly, never inventing a concept the backend can't name). Purely informational for now: nothing in `PlanService`/`SafetyEngine` reads this column yet, and the goals screen's own copy says so ("This doesn't change your results yet").

The onboarding wizard (`(onboarding)/profile.tsx` -> `goals.tsx` -> `constraints.tsx`) collects fields across three screens but submits exactly once, on the last screen, via `OnboardingFormProvider` (`src/profile/onboarding-form-context.tsx`) -- there is no partial-patch endpoint, so a later step never has a chance to silently overwrite an earlier step's answer with a stale default. Allergies/ingredients-to-avoid are collected as plain free-text arrays (`src/components/tag-input.tsx`), matching the backend's own current (documented placeholder) representation exactly -- this pass does not invent client-side ingredient normalization the backend doesn't have.

## Application home — IMPLEMENTED

`app/(app)/index.tsx`: welcome, profile/consent status, and an entry point into skin analysis (`Start skin analysis` -> `(app)/analysis`). Updated in Mobile V1 Phase C1 to describe what analysis now actually returns (priorities, measurements, an AM/PM routine, and a compatible product match per step when one is available) in place of the pre-Phase-B "arrives in the next build phase" placeholder, and to drop a separate "Your routine" placeholder card that never had anywhere real to source a persistent routine from (a routine only ever exists inside one specific completed analysis's own result screen -- a home-level "current routine" surface is Phase C3 territory, not invented here). Per Phase C1's own scope boundary: no subscription/paywall, progress charts, purchase UI, history, or notifications.

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

## Credential storage — token pair, single SecureStore value (Phase A repair pass) — IMPLEMENTED, TESTED

`src/auth/token-storage.ts` stores access + refresh together as one versioned JSON value under `auth.token_pair`, replacing an earlier two-independent-keys scheme an independent review flagged: a second-write failure across two keys could have left a new access token paired with an old, already-consumed (single-use) refresh token, colliding with the backend's replay-family revocation. `clear()` also removes the old two-key values for any dev install still carrying them. A SecureStore write failure occurring *after* a successful server refresh (`src/auth/refresh-coordinator.ts`) fails closed: the old refresh token is never retried, storage is best-effort cleared, and the session ends (`TOKEN_STORAGE_ERROR`) rather than trusting an inconsistent local state.

## EAS build foundation (V1 account recovery / release-foundation pass) — CONFIGURED, NOT BUILT

`mobile/eas.json`: `development` (`developmentClient: true`, internal
distribution), `preview` (internal distribution), `production`
(`autoIncrement`). This pass only wrote the profile declarations --
no `eas build` was run, no RevenueCat packages were installed, and
`expo-dev-client` was **not** installed (installing it is left to
Mobile C2). **STILL REQUIRED BEFORE C2 STORE INTEGRATION:**
`ios.bundleIdentifier` / `android.package` are absent from
`mobile/app.json` -- no canonical value could be verified from
repository/Expo project state, so this pass left that irreversible
decision explicit rather than guessing.

## Release URL seams (V1 account recovery / release-foundation pass) — IMPLEMENTED, CONFIGURATION EXTERNAL

Four optional, public (non-secret) environment variables
(`EXPO_PUBLIC_PRIVACY_POLICY_URL`, `EXPO_PUBLIC_TERMS_URL`,
`EXPO_PUBLIC_SUPPORT_URL`, `EXPO_PUBLIC_ACCOUNT_DELETION_URL`,
`src/constants/config.ts`), each independently optional in development
(Settings' new "Legal & support" section renders only the links whose
URL is actually set) and required to be `https://` if configured in a
production build (enforced at startup, same pattern
`EXPO_PUBLIC_API_BASE_URL` already uses). **None of the four is
actually configured yet, and no privacy/terms/support/account-deletion
page content was authored by this pass** -- see
`ACCOUNT_RECOVERY_ARCHITECTURE.md` section 13. The account-deletion
link is additive: existing in-app `DELETE /me` is unchanged.

## Bootstrap error state (Phase A repair pass) — IMPLEMENTED, TESTED

`src/navigation/bootstrap-route.ts`'s decision table gained a sixth state, `bootstrap-error`: an authenticated user whose consent/profile query settles into a permanent failure (offline at launch, a timeout, a backend 5xx) previously read as `loading` forever (undefined data, indistinguishable from "still fetching"). `resolveBootstrapRoute` now takes each gating query's `isError` alongside its `data`, checked *before* the still-loading branch, and both `/` (`app/index.tsx`) and the protected `(app)` layout render a deliberate `ErrorState` + Retry rather than an infinite spinner. Never touches session state or stored credentials on a transient failure -- that remains exclusively the refresh coordinator's job for a definite 401/403.

## Test coverage — TESTED

`mobile/tests/` (Jest + `jest-expo` + `@testing-library/react-native`), 117 tests, mirroring `src/`:

- `tests/auth/`: `token-storage.test.ts` (single-key pair storage, no-torn-pair, legacy-key cleanup, write-failure propagation), `restore-session.test.ts`, `refresh-coordinator.test.ts` (success with no refresh; expired-access-token one-refresh-then-retry; **N-concurrent-401s-exactly-one-refresh-call**, now also proven at 10 concurrent callers with exactly one token-pair persistence; definite-rejection secure logout; transient-failure session preservation; post-refresh storage-failure fails closed without retrying the consumed refresh token; no stored refresh token fails closed; never refreshes more than once).
- `tests/navigation/bootstrap-route.test.ts`: every row of the decision table, including every `bootstrap-error` trigger (consent error, profile error, either-fails, session-preserved-throughout).
- `tests/api/client.test.ts`, `tests/api/analysis-api.test.ts`: status/error classification, including 403/422/429/5xx/network propagation for analysis submission.
- `tests/profile/`: `profile-api.test.ts`, `consent-api.test.ts`.
- `tests/utils/`: `logger.test.ts`, `config.test.ts`.
- `tests/analysis/` (Phase B, see below): `camera-permission.test.ts`, `capture-file.test.ts`, `request-id.test.ts`, `analysis-lifecycle.test.ts`, `analysis-presenter.test.ts`, `use-submit-analysis.test.ts`.

All of this runs against fakes/mocks injected at the seams described above -- no test touches the real device keychain, camera hardware, filesystem, or a live backend.

## Phase B — Capture Coach, camera capture, async analysis, results — IMPLEMENTED, TESTED (see DEFERRED for what stayed out)

Consumes the backend's *existing* async analysis architecture (`ASYNC_ANALYSIS_ARCHITECTURE.md`, `POST`/`GET /api/v2/analyses`) end to end -- no second analysis system, no new queue, no new job type.

**Flow**: `(app)/index.tsx`'s "Start skin analysis" (reachable only once the `(app)` layout's existing bootstrap gate has already confirmed current consent + a set profile -- Phase B duplicates none of that checking) -> `(app)/analysis/index.tsx` (Capture Coach) -> `(app)/analysis/capture.tsx` (permission -> camera -> capture -> review/retake -> submit) -> `(app)/analysis/[analysisId].tsx` (poll -> result). `(app)/analysis/_layout.tsx` wraps the group in one `AnalysisSessionProvider` (`src/analysis/analysis-session.tsx`) holding the one `request_id` for the whole attempt.

**Camera permission** (`src/analysis/camera-permission.ts`) -- IMPLEMENTED, TESTED: a pure `resolveCameraPermissionState()` maps `expo-camera`'s `PermissionResponse` into exactly `unknown | requesting | granted | denied | blocked`, unit-tested without touching the real permission API. `denied` offers a retry prompt; `blocked` (the OS says asking again is a no-op) deep-links to system Settings via `expo-linking`. Only `CAMERA` is ever requested -- the Android manifest's `RECORD_AUDIO` permission is explicitly suppressed (`app.json`'s `expo-camera` plugin config, `recordAudioAndroid: false`, plus the `CameraView`'s own `mode="picture"` -- this app never records video, so there is no code path that could ever prompt for microphone access); contacts/location/Bluetooth are never touched.

**Camera readiness and capture concurrency** (`src/analysis/camera-capture-policy.ts`, `(app)/analysis/capture.tsx`) -- IMPLEMENTED, TESTED (post-merge audit repair pass, hardened by a second independent-review pass): real-device `takePictureAsync()` requires waiting for `CameraView`'s `onCameraReady` callback first -- the Capture button is disabled until that fires, tracked via the pure `cameraReadinessReducer()` (`{ cameraReady }`, driven by `CAMERA_READY`/`CAMERA_INVALIDATED` events) plus plain `capturePending`/`captureError` state. **Every newly mounted `CameraView` starts NOT READY** (second independent-review pass, item 1): the original `useState<boolean>` version let `cameraReady` survive a successful capture -- the live `CameraView` unmounts in favor of the review screen, but the boolean stayed `true`, so a Retake's brand-new `CameraView` (a fresh native instance that has never fired its own `onCameraReady`) could have the Capture button active before that new instance was actually ready. `cameraReadinessReducer` closes this structurally: `CAMERA_INVALIDATED` is dispatched the instant a capture succeeds (invalidating the instance that just produced it, before the transition to review) and again on Retake (defense in depth), so `cameraReady` is provably `false` for the entire span between "we stopped trusting the old instance" and "the new instance's own `onCameraReady` fired" -- the only event that can ever set it `true` (`tests/analysis/camera-capture-policy.test.ts` exercises the full capture -> review -> retake -> new-ready transition). The pure, unit-tested `canStartCapture()` gate (both ready AND not already mid-capture) prevents a rapid double-tap from firing two concurrent `takePictureAsync()` calls; `capturePending` is always cleared in a `finally`, and a successful capture always clears any prior `captureError`. `onMountError` surfaces camera initialization failure as safe, nontechnical copy -- never a raw exception, and never a file URI in any of this state or in any log call.

**Capture-in-flight unmount / orphan-file race** (`src/analysis/camera-capture-policy.ts`'s `decideCaptureOwnership()`, `(app)/analysis/capture.tsx`) -- IMPLEMENTED, TESTED (second independent-review pass, item 2): a native `takePictureAsync()` promise can outlive the screen that started it -- system Back, a gesture, a deep link, or any other navigation can unmount `capture.tsx` while the promise is still pending. If it resolves after that, the returned photo was previously never entered into `capture` state, so the existing `[capture]` cleanup effect never learned the file existed -- a real orphaned facial-image temp file, left for OS cache reclamation instead of deliberate deletion. Fixed with a `stillOwnsCaptureLifecycleRef` (the standard React `isMountedRef` pattern, flipped `false` in the screen's unmount cleanup) and the pure `decideCaptureOwnership(stillOwnsCaptureLifecycle): "ADOPT" | "DISCARD"` decision it feeds: when `takePictureAsync()` resolves, the handler checks ownership *before* touching any state -- `"ADOPT"` dispatches `CAPTURED` as before; `"DISCARD"` immediately best-effort-deletes `photo.uri` (never logging it) and returns without dispatching anything or calling any state setter. The same ref also gates the `catch`/`finally` blocks (`setCaptureError`/`setCapturePending`), so no state update is ever attempted after ownership is gone. The live-camera Cancel button is now disabled whenever `capturePending` (both in the JSX `disabled` prop and independently inside `handleCancelBeforeCapture` itself), so it can never be tapped mid-capture. React/native camera rendering remains impractical to exercise in this repository's test environment (no `expo-camera` mock exists, and no other screen-level test does this) -- `decideCaptureOwnership()` and `cameraReadinessReducer()` are unit-tested as the pure decision layer instead, per this pass's own explicit scope; on-device verification of the full unmount-mid-capture race is tracked in the manual checklist below.

**Capture Coach** (`(app)/analysis/index.tsx`) -- IMPLEMENTED: a fixed guidance checklist (even lighting, face the camera, remove glasses/heavy obstruction if practical, no beauty filters, hold steady) using deliberately cautious wording for makeup ("for the most consistent analysis... when practical" -- never "you must remove makeup," since the backend cannot reliably detect it) and no medical claim.

**Capture screen** (`(app)/analysis/capture.tsx`) -- IMPLEMENTED: front-facing `expo-camera` `CameraView` with a **static** visual framing overlay (an oval, a center dot, fixed guidance text) -- explicitly not real-time face-geometry tracking; this pass adds no on-device ML pipeline to back a "smarter" overlay. After capture, a review step (retake, submit, or **Cancel**) -- local validation is deliberately lightweight (the captured file exists and has real bytes), never a claim of face-detection or quality scoring mobile doesn't actually perform; the backend's own `CaptureAssessment` remains the authoritative quality judgment, surfaced on the results screen. An explicit, visible **Cancel** control exists in both the live-camera and review states (post-merge audit repair pass -- previously only implied by documentation, with no actual UI): before capture it exits the flow with nothing to clean up; after capture it deletes the current original capture, resets submission state, and exits. Both Cancel and Retake are disabled while `submission.isPending`, so neither can ever race an in-flight `POST /api/v2/analyses` and strand the user between accepted backend state and an abandoned local capture.

**Image handling and bounds** (`src/analysis/capture-file.ts`, `src/analysis/capture-resize.ts`, `src/constants/capture.ts`) -- IMPLEMENTED, TESTED: `expo-camera`'s own capture already lands in the OS cache directory (never the photo library, never written by this app anywhere durable). `prepareCaptureForSubmission()` caps the image's LONG EDGE at 1600px (`MAX_IMAGE_DIMENSION`) at JPEG quality 0.8 (`expo-image-manipulator`) -- the pure, unit-tested `computeResizeDimensions()` (post-merge audit repair pass) decides the actual resize target from the camera's own reported width/height (never guessed from the URI), preserving aspect ratio for portrait/landscape/square alike, and issues **no resize action at all** when the source is already within bounds: a low-resolution capture is never upscaled, which would otherwise corrupt the backend's own resolution-quality signal (`CaptureAssessor._assess_resolution()`). Base64 is a plain function return value, held only for the duration of one `submitAnalysis()` call -- never placed in the Query cache, global React state, or a log line (`tests/analysis/capture-file.test.ts` asserts the produced base64 string never appears in any logger call). `deleteLocalCaptureFile()` (`expo-file-system`'s modern `File` API) is best-effort and never throws, and never logs the file URI. Temp-file cleanup invariant (post-merge audit repair pass -- corrects an earlier version of this document, see below): the **prepared/resized working copy** is deleted after every submission attempt, success or failure alike (`use-submit-analysis.ts`'s `try/finally`), so it never accumulates across retries; if image manipulation produces a working file but no base64, that file is best-effort-deleted before the resulting error is thrown. The **original capture** is retained on a transient submission failure (so a retry can resubmit the identical photo) and deleted only on confirmed success, retake, cancel, or the capture screen's own unmount.

**Request identity** (`src/analysis/request-id.ts`, `capture-attempt.ts`) -- IMPLEMENTED, TESTED: `expo-crypto`'s `randomUUID()` generates a `request_id` bound to the individual **captured photo**, not to the `(app)/analysis/*` route group's lifetime (post-merge audit repair pass -- corrects a defect in the original `AnalysisSessionProvider`/`analysis-session.tsx` design, since removed: a route-session-scoped id let a failed submission followed by Retake resubmit a *different* photo under the *same* `request_id`, which the backend's own idempotent-replay behavior would then honor literally, returning the first photo's analysis for the second photo's submission). The pure `captureAttemptReducer()` (`capture-attempt.ts`) mints a fresh id on every `CAPTURED` action and clears it entirely on `DISCARDED` (Retake, Cancel-after-capture) -- a captured photo and its `request_id` exist and are destroyed together, so two different photos can never share one id, and resubmitting the SAME photo after a transient failure trivially reuses the same id (no new `CAPTURED` action fires in between). Matches the backend's own idempotency contract (`analysis_usage`/`analysis_requests.request_id UNIQUE`) exactly.

**Submission API** (`src/api/analysis-api.ts`) -- IMPLEMENTED, TESTED: `submitAnalysis()`/`getAnalysis()`, both through the existing `authorizedRequest` boundary -- a 401 here is transparently retried once by the same single-flight refresh coordinator as every other authenticated call. 403 (consent required/withdrawn mid-flow) routes back to `(onboarding)/consent` from the capture screen, never bypassed client-side (section 22's actual security boundary stays the backend's). 429 (quota exceeded) shows a fixed "You've reached your current analysis limit" message with no automatic retry loop and no new `request_id` consumed. 422/5xx/network errors render inline with a manual retry that reuses the same capture and `request_id`. The backend independently re-validates image size/dimensions server-side regardless of what this client resized to or claims (`ASYNC_ANALYSIS_ARCHITECTURE.md`'s section-7 image limits) -- this client is not, and was never meant to be, a security boundary.

**Analysis lifecycle and polling** (`src/analysis/analysis-lifecycle.ts`, `use-analysis-polling.ts`) -- IMPLEMENTED, TESTED: mobile's own `draft | submitting | queued | processing | completed | failed | unavailable` phase set maps 1:1 onto backend truth for every phase except `unavailable`, which is purely a client-side fact about polling itself (`RECEIVED`/`CANCELLED` fold into the closest honest backend-derived phase; nothing else is invented). `getPollingIntervalMs()` -- a pure, fully unit-tested function -- decides 2s initially, backing off to 4s after 5 polls, `false` (stop) once terminal; `useAnalysisPolling` wires this into TanStack Query's own `refetchInterval`, so there is no hand-rolled `setInterval` and polling stops automatically on unmount. Polling failures are split by `ApiError.retryable` (post-merge audit repair pass -- the original implementation read `q.state.data?.status`, which stays `undefined` forever on a request that never once succeeded, defaulted that to `"queued"`, and retried every error identically regardless of status code, so a permanent 404/403 could poll forever while rendering as merely "still queued"): a TRANSIENT failure (network/timeout/5xx/retryable-429) never converts the analysis into `failed` -- TanStack Query retains the last successfully fetched `data` across a failed background refetch, so `phase` only ever reflects the last known-good backend status while `isPollingError` drives a "having trouble checking status" banner with its own Retry. A PERMANENT failure (`ApiError.retryable === false` -- 404, 403, any other nonretryable 4xx) stops all further polling immediately (`retry` returns `false` on the very first such failure) and surfaces the pure, unit-tested `phase === "unavailable"`, rendered as a dedicated "This analysis isn't available" screen with a "Start over" recovery action -- never as an indefinite "queued" state and never as a fabricated `FAILED` analysis outcome.

**Results rendering** (`src/analysis/analysis-presenter.ts`, `(app)/analysis/[analysisId].tsx`) -- IMPLEMENTED, TESTED: renders exactly the backend's own `plan.top_priorities`/`am_routine`/`pm_routine` (step action/why text only -- no product cards, no shopping/affiliate UI, per this pass's own scope boundary) and the new `metric_results` per-metric detail (see below). The one structural safety guarantee: `formatMetricValueForDisplay()` returns `null` for any `ABSTAINED` metric regardless of what its `value` field contains, so no call site can accidentally render a fabricated number for a metric the backend didn't measure -- `BORDERLINE` renders its real value plus a distinct "Less certain" label (never color-only). `deriveAnalysisQualityLabel()` gives a compact High/Moderate/Limited summary derived only from the real `capture_assessment.quality_status` and average non-abstained metric confidence -- never a separate, unrelated "AI confidence score." Language throughout uses "estimate"/"image-derived"/"could not measure reliably," never "diagnosed"/"medical condition"/"clinical score."

**Backend change (additive, non-breaking)**: `GET /api/v2/analyses/{id}` now also returns `metric_results` (`app/db/analysis_repository.py::get_measurements`, `backend/tests/api/test_analyses_v2.py::test_get_completed_analysis_includes_metric_results`) -- this data was already captured in `analysis_measurements` by the existing worker/execution path but never read back out over HTTP before this pass. No existing field, route, or caller changed. `get_measurements()` projects exactly the mobile/API contract's seven fields (`metric_name`/`value`/`confidence`/`status`/`uncertainty_reasons`/`metric_version`/`calibration_version`), never `SELECT *` (post-merge audit repair pass, section 9) -- internal columns (`id`/`analysis_request_id`/`user_id`/`created_at`) are never exposed, independent of and in addition to RLS already scoping the row to its owner.

**Server-side image input limits (post-merge audit repair pass, section 7)**: the mobile client's own <=1600px-long-edge resize is not a security boundary. `AnalysisSubmissionService.submit()` (`backend/app/domain/analysis_submission_service.py`) independently rejects an oversized encoded payload (`MAX_ANALYSIS_BASE64_CHARS`, 413) or decoded payload (`MAX_ANALYSIS_DECODED_BYTES`, 413), and a malformed or oversized image (a cheap Pillow header-only read, never a full pixel decompression -- `MAX_ANALYSIS_IMAGE_LONG_EDGE`/`MAX_ANALYSIS_IMAGE_PIXELS`, 422), before the image is ever stored or enqueued for the worker's own `cv2.imdecode()`. Any rejection releases the usage-quota reservation it consumed, exactly like the pre-existing invalid-base64 path. See `ASYNC_ANALYSIS_ARCHITECTURE.md` for the full detail and `DOKPLOY_DEPLOYMENT.md`'s "Ingress request-body limit (mandatory)" section (second independent-review pass, item 3 -- the module docstring pointed here before this section actually existed) for the separate, still-required transport/edge request-body size limit this application-level check does not replace. That section documents the requirement and where to configure it per reverse-proxy topology; it does not itself claim the requirement has been configured or verified against any real deployed ingress.

**Deliberately deferred within Phase B**: cross-restart active-analysis restoration (`OPEN_ENGINEERING_ITEMS.md` item 6a) -- if the app restarts mid-`QUEUED`/`PROCESSING`, the user lands back on the app home rather than the polling screen resuming automatically; the analysis itself is unaffected server-side. Not attempted this pass because doing it cleanly would mean persisting an active `analysis_id` somewhere durable, which risks becoming a precedent for persisting more than a bare UUID -- deferred rather than rushed.

## Phase C1 — Recommendation experience — IMPLEMENTED, TESTED (see DEFERRED for what stayed out)

Turns the existing backend recommendation truth (`PRODUCT_RECOMMENDATION_PIPELINE.md`'s "Client-facing projection" section) into the first product/routine UI. The architectural rule this phase exists to preserve, unchanged and unweakened: **safety filtering before product matching before client presentation** -- mobile never manufactures a recommendation, never substitutes another product, and never performs its own formulation-safety decision. It renders exactly what `GET /api/v2/analyses/{id}`'s `product_recommendations` field already contains.

**Types** (`src/types/domain.ts`) -- IMPLEMENTED: `ProductRecommendation` replaces the previous `product_recommendations?: unknown[] | null` placeholder, matching `ProductRecommendationOut` (`app/api/v2/analyses.py`) field-for-field -- this app never invents a field the backend doesn't return. `safety_status` is deliberately typed `string`, not a `"SAFE" | "RESTRICTED"` union: the backend invariant is that nothing else can reach a concrete recommendation, but this type must not assume that invariant holds forever, so the presenter layer below fails closed on an unrecognized value instead of a strict union check silently miscompiling or crashing.

**Routine-step association** (`src/analysis/recommendation-matching.ts`) -- IMPLEMENTED, TESTED: `getRecommendationForRoutineStep(daypart, stepNumber, recommendations)` matches a routine step to its recommendation by the backend's own `plan_step_key` contract (`"AM:<step_number>"` / `"PM:<step_number>"`, `buildPlanStepKey()`), never by array position or index -- a client that instead zipped `plan.am_routine[i]` with `product_recommendations[i]` would silently mismatch the moment either list's ordering or length diverges. Returns `null` (never a guess) both when no recommendation exists for a step and when more than one recommendation unexpectedly shares the same key (structurally should be impossible, given the backend's own `UNIQUE(analysis_request_id, plan_step_key)` constraint, but this function fails deterministically rather than picking one if it ever happens).

**Presentation layer** (`src/analysis/product-recommendation-presenter.ts`) -- IMPLEMENTED, TESTED: the one place backend data becomes display copy. `getSafetyStatusPresentation()` renders calm, neutral copy for `SAFE`, visible-restriction copy for `RESTRICTED`, and a generic fail-closed label for anything else; `isKnownSafetyStatus()` classifies only those same two values as known. This presenter layer computes the classification but does not by itself decide what renders -- that enforcement lives in the component layer below, which is the piece that actually determines whether a status is presented as a normal recommended product. `describeReasonCode()`/`describeReasonCodes()` translate the machine-readable codes `app/domain/safety_engine.py` actually emits (`PREGNANCY_RESTRICTION`, `NURSING_RESTRICTION`, `SENSITIVE_SKIN_INTENSITY_LIMIT`, `ACTIVE_INTERACTION_CONFLICT`, `MAX_FREQUENCY_EXCEEDED`, `ALLERGY_CONFLICT`, `USER_AVOID_INGREDIENT`) into human copy; an unrecognized code degrades to the exact fixed fallback "Additional compatibility considerations apply." -- never the raw code, never an invented medical interpretation. `summarizeRestrictions()` only recognizes the specific `restrictions` JSON shapes `evaluate_product_formulation()` actually produces today and silently ignores anything else, rather than dumping raw keys/values. `formatVerificationProvenance()` renders "Formulation verified: `<date>`" or `null` when absent -- never "unverified" (the backend contract doesn't classify absence that way) and never a claim of clinical validation. `getProductDisplayLabel()` never invents a name: brand-only, product-only, both, or "Product details unavailable" when neither resolved.

**UI** (`app/(app)/analysis/[analysisId].tsx`, `src/components/product-recommendation-card.tsx`) -- IMPLEMENTED: each AM/PM routine step now also renders its `product_category` and, via `getRecommendationForRoutineStep()`, either a `ProductRecommendationCard` or a `NoProductMatchNotice` ("No specific product match is available for this step yet.") when the backend attached no recommendation record at all -- Phase 11's documented fallback behavior, never treated as malformed and never hidden. `ProductRecommendationCard` is purely presentational: no API fetching, no safety/ranking decisions, no navigation, and (per this phase's own scope) no interactive/purchase element at all. It is also where the fail-closed status invariant is actually enforced, not just classified: it checks `presentation.isKnownStatus` before rendering anything, and for `SAFE`/`RESTRICTED` renders the normal card (brand, product name, compatibility status as text, considerations, optional verification provenance) while every other status -- `UNSAFE`, `INSUFFICIENT_DATA`, or any future value -- renders `UnconfirmedProductMatchNotice` ("Specific product match unavailable. Compatibility for the product returned with this analysis could not be confirmed.") instead: no "Recommended match" label, no brand/product identity, no verification provenance, no interpreted restrictions/reasons. (Closed as a merge-blocker in independent review: an earlier version of this component always rendered "Recommended match," ignoring `isKnownStatus` entirely.) Compatibility is always communicated as text (`accessibilityLabel` includes the full status + considerations), never color alone. Every existing Phase B section of the results screen (analysis quality, top priorities, measurements, disclaimers, Analyze again) is unchanged.

**Language discipline**: neither the presenter nor the card ever uses "clinically proven"/"medically approved"/"guaranteed"/"best"/"optimal"/"dermatologist recommended" -- only "product match"/"compatible match"/"recommended for this routine"/"safety status"/"restrictions," per this phase's own requirement (asserted directly by `product-recommendation-presenter.test.ts`). `rank_position` is carried through the type/projection as provenance only and is never rendered as "#1 product"/"best product"/"top ranked product" -- no clinical-efficacy or commercial ranking exists (`PRODUCT_RECOMMENDATION_PIPELINE.md`'s own "What this does not claim" section).

**No commerce/affiliate features** -- deliberately, not by oversight: no price, "Buy now," retailer button, affiliate link, savings percentage, popularity score, or efficacy stars anywhere in this phase's UI. The catalog has no trustworthy generalized price/merchant/affiliate/availability data to render honestly yet; that belongs to a later commercial/catalog enrichment architecture, not this phase.

**Backend companion changes** (`app/db/analysis_repository.py`, `app/api/v2/analyses.py`, migrations `366861ec262d` and `e421ed4cf053`): see `ANALYSIS_DATA_MODEL.md` and `PRODUCT_RECOMMENDATION_PIPELINE.md`'s "Client-facing projection" section for the snapshot columns, the `display_snapshot_version` marker governing when the snapshot vs. the current catalog is authoritative, the explicit `ProductRecommendationOut` projection (never `SELECT *`), and the current-catalog display fallback for genuinely legacy rows.

**Component-level test coverage**: `tests/components/product-recommendation-card.test.tsx` (`@testing-library/react-native`, this repository's first component-render test -- every prior test, Phase A/B/C1's own presenter tests included, exercised pure functions/hooks only) renders `ProductRecommendationCard` directly and asserts on the actual output tree, not just the presenter's classification: `UNSAFE`/`INSUFFICIENT_DATA`/an arbitrary future status all render `UnconfirmedProductMatchNotice` with none of "Recommended match," the product's brand/name, or verification provenance anywhere in the tree, while `SAFE`/`RESTRICTED` still render the normal card. This test is what actually caught the merge-blocker described above (a presenter-only test could show `isKnownSafetyStatus()` returning `false` without ever proving the component acted on it). "A routine step is never hidden when it has no product match" (`NoProductMatchNotice` vs. absence) remains a structural property of `[analysisId].tsx`'s own JSX, verified by reading the component rather than a runtime test of that specific screen -- same category of gap as Phase B's own camera-runtime behavior, tracked the same way (see Device-test status below, which this phase does not change).

## Phase C2 — RevenueCat purchasing and entitlement sync — IMPLEMENTED, TESTED (see DEFERRED for what stayed out)

Connects this app to the already-existing, unmodified backend RevenueCat billing architecture (`BILLING_ARCHITECTURE.md`) -- this phase builds no second billing system. Full detail in the new `MOBILE_C2_REVENUECAT.md`.

**Central invariant this phase exists to preserve**: local RevenueCat `CustomerInfo` is useful for immediate purchase UX, but the backend `user_entitlements` projection remains the sole authority for premium access and analysis quota. No mobile code anywhere gates analysis submission, or any other feature, on `customerInfo.entitlements.active.premium` -- `UsagePolicyService`/`RevenueCatEntitlementService` (backend, unmodified) remain the entire security boundary.

**Native dependencies** -- `expo-dev-client`, `react-native-purchases`, `react-native-purchases-ui`, installed at Expo-SDK-57/RN-0.86-compatible versions (`npx expo install`, never a manually-pinned obsolete version). `npx expo-doctor` stays 21/21 with all three installed; no RevenueCat package is added to `expo.install.exclude`.

**Public configuration** (`src/constants/config.ts`) -- `EXPO_PUBLIC_REVENUECAT_ENABLED`/`EXPO_PUBLIC_REVENUECAT_IOS_API_KEY`/`EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY`/`EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID` (default `"premium"`), all optional -- an unconfigured deployment runs normally, with the subscription screen reporting billing as unavailable rather than throwing at startup. These are RevenueCat's own public mobile SDK keys, not backend secrets; `REVENUECAT_API_KEY`/`REVENUECAT_WEBHOOK_AUTH`/`REVENUECAT_WEBHOOK_SIGNING_SECRET`/the billing database URL are backend-only and never referenced by any mobile file.

**Adapter boundary** (`src/billing/revenuecat-adapter.ts`) -- a narrow `RevenueCatAdapter` interface (`isConfigured`/`configure`/`logIn`/`getCustomerInfo`/`restorePurchases`/`presentPaywall`/`presentCustomerCenter`/CustomerInfo-update-listener subscribe-unsubscribe) is the only thing any screen or provider touches; no component imports `react-native-purchases`/`react-native-purchases-ui` directly. Deliberately has **no `logOut` method** -- see the identified-user lifecycle below. Every method signature was taken from the installed SDK's own TypeScript definitions (inspected directly, never invented from memory). Tests inject a plain, duck-typed fake adapter (`tests/billing/fake-adapter.ts`); no unit test requires a real RevenueCat account, native store, or network call.

**Identified-user lifecycle** (`src/billing/revenuecat-context.tsx`, `RevenueCatProvider`, nested beneath `SessionProvider` in `app/_layout.tsx`) -- this app's canonical RevenueCat App User ID is the backend's own `users.id` UUID, obtained via `GET /me` only after `useSession().state.status === "AUTHENTICATED"`. RevenueCat is never configured while `SIGNED_OUT`/`UNKNOWN` (no unnecessary anonymous customer is ever created). A module-level singleton coordinator (`moduleConfiguredUserId`/`moduleConfiguringPromise`) guarantees `Purchases.configure()` runs at most once per native process even under concurrent/duplicate effect firing, and the installed SDK's own `Purchases.isConfigured()` facility is consulted as an additional guard (never a hand-invented equivalent).

**Sign-out / account switching** -- this app's custom-App-User-ID model means calling the SDK's `logOut()` would immediately mint an unwanted anonymous `$RCAnonymousID` customer, so ordinary application sign-out never calls it (there is nothing to call -- the adapter interface has no such method). Sign-out clears only the existing auth token pair, TanStack Query state (unchanged, pre-existing behavior), and C2's own in-memory billing UI state (`customerInfo`/`configuredUserId`); RevenueCat itself stays configured for whichever user it last identified. If a *different* account signs in during the same native process, the provider calls `Purchases.logIn()` with the new UUID -- never `logOut()` first. Proven directly (`tests/billing/revenuecat-context.test.tsx`'s account-switching test: user A configured, sign-out, user B signs in, RevenueCat transitions directly via `logIn`, zero `logOut`-equivalent calls at any point).

**Server status API** (`src/api/billing-api.ts`, `src/query/use-billing.ts`) -- `getBillingStatus()`/`syncBillingStatus()` call the new backend `GET`/`POST /api/v2/billing/status`/`sync` (`BILLING_ARCHITECTURE.md`'s "Mobile client integration (C2)" section) through the existing `authorizedRequest` infrastructure, same pattern as every other authenticated API module. `useBillingStatusQuery`/`useBillingSyncMutation` are the only hooks any screen calls for billing state -- server status is the source of truth for premium badge, analysis allowance, remaining allowance, and whether paid functionality is unlocked; these values are never derived from `CustomerInfo` alone.

**Subscription screen** (`app/(app)/subscription.tsx`, linked from Settings) -- explicit states for: loading server entitlement, RevenueCat unavailable/config missing (`DISABLED`/`WEB_UNSUPPORTED`/`MISSING_KEY`), free tier (current allowance, Upgrade, Restore purchases), purchase-syncing ("Purchase received. We're syncing your access." with Retry), premium active (allowance, renewal status, expiration when known, Manage subscription, Restore purchases), premium grace period (a distinct message, still premium UI), and provider/network error. Displays exactly what the server returns for allowance -- never promises unlimited analyses regardless of what RevenueCat's local state might suggest.

**Paywall** (`src/billing/revenuecat-context.tsx::presentPaywall`, RevenueCatUI's `presentPaywallIfNeeded`) -- presents the RevenueCat dashboard's current/default Offering for the `premium` entitlement; no package/product identifier is hardcoded into purchase decision logic, so the dashboard can change presentation without a client release. `PAYWALL_RESULT.CANCELLED`/`NOT_PRESENTED` are handled as non-errors. A `PURCHASED`/`RESTORED` result never flips a local `hasPremium` flag directly -- it triggers `POST /api/v2/billing/sync`, then relies on the invalidated `useBillingStatusQuery` to refresh; premium UI activates only once the server confirms `ACTIVE`/`GRACE_PERIOD`.

**Restore purchases** -- an explicit action using the SDK's `restorePurchases()`; a restore that finds nothing is not a crash. On success, triggers the same backend sync + query invalidation as a paywall purchase. A user-cancel/store/network failure surfaces a bounded error state via the purchase-error classifier below -- quota is never unlocked client-side as a workaround.

**Customer Center** (`RevenueCatUI.presentCustomerCenter()`) -- exposed as "Manage subscription" for a premium/grace-period customer. After it closes, the same backend-sync-plus-refresh trigger fires, regardless of what happened inside it -- this app never attempts to cancel a subscription directly through the backend; the store/provider owns subscription management, and cancellation does not revoke access immediately if the server entitlement remains active through the paid period (the existing backend entitlement state machine is unmodified and remains authoritative).

**CustomerInfo update listener** -- `addCustomerInfoUpdateListener`/`removeCustomerInfoUpdateListener` (removed on provider unmount, no leak across navigation) update local `customerInfo` state only; nothing in this codebase treats a CustomerInfo change as authorization by itself -- see the subscription screen's own purchase/restore/Customer-Center flows for where a sync is actually triggered.

**Purchase error classification** (`src/billing/purchase-error.ts`) -- `classifyPurchaseError()` maps a RevenueCat `PurchasesError` to one of `USER_CANCELLED`/`STORE_UNAVAILABLE`/`NETWORK_ERROR`/`CONFIGURATION_ERROR`/`GENERIC_FAILURE` before anything reaches `src/utils/logger.ts` -- no full RevenueCat error object (which may carry transaction/customer metadata) is ever logged, only a bounded category.

**Web** (`Platform.OS === "web"`) -- never calls `configure`/`presentPaywall`/`restorePurchases`/`presentCustomerCenter`; the subscription screen renders an honest "not available on web" state instead. `npx expo export --platform web` stays green with all three native packages installed.

**Test Store development flow** -- documented in `MOBILE_C2_REVENUECAT.md` (placing a RevenueCat Test Store public SDK key into `EXPO_PUBLIC_REVENUECAT_*`, never committed; a development-build smoke procedure). No claim is made anywhere in this repository that a real Test Store purchase was completed -- none was, since a development build requires external store/EAS configuration this pass does not have (see that doc's "Native build honesty" section).

**Test coverage**: `mobile/tests/billing/` (identified-user lifecycle: waits for `/me`, configures once, account switching without `logOut`, availability derivation for DISABLED/WEB_UNSUPPORTED/MISSING_KEY/READY, purchase-error classification), `mobile/tests/query/use-billing.test.tsx` (server status is the source of truth, sync invalidates the status query), `mobile/tests/app/subscription.test.tsx` (every documented UI state, including purchase-succeeded-but-sync-fails never unlocking premium locally, and the web-unsupported path never calling a native method). Every test injects a fake adapter or mocks the query hooks directly -- zero real network/native purchase calls in a normal Jest run.

## Device-test status (post-merge audit repair pass)

This pass's own automated checks (Jest unit tests, TypeScript, ESLint, `npx expo-doctor`, `npx expo export --platform web`) run in an environment with no real camera hardware. They verify the pure decision logic (`captureAttemptReducer`, `computeResizeDimensions`, `canStartCapture`, `isPermanentPollingError`, etc.) and that the app type-checks/lints/bundles correctly -- **none of them constitute physical camera-runtime verification**, and this document does not claim otherwise. In particular, real-device behavior of `onCameraReady`/`onMountError` timing, actual rapid-tap gesture handling, and real OS permission-prompt flows have not been exercised on a physical device or simulator with a camera as part of this pass. The manual checklist below is required before this repair can be considered device-verified:

- [ ] Camera permission: allow, deny, and blocked (denied + "don't ask again") states each render correctly and, for blocked, the Settings deep link actually opens this app's settings page.
- [ ] Camera reports ready (`onCameraReady`) and the Capture button is visibly disabled until it does.
- [ ] Rapid repeated taps on Capture produce exactly one photo, never two, and never an orphaned temp file.
- [ ] Capture succeeds and transitions to the review screen with the correct preview image.
- [ ] Review screen: Retake discards the photo and returns to a working camera view, and the new camera view's Capture button is visibly disabled again until its own `onCameraReady` fires (second independent-review pass, item 1) -- it must not become tappable immediately on Retake just because the previous camera instance was ready.
- [ ] Leave the live-camera screen (system Back / swipe-back gesture) while a capture is in flight (tap Capture, then immediately navigate away before the photo appears): no crash, no captured photo silently appears elsewhere in the app, and no facial-image temp file is left behind (second independent-review pass, item 2 -- verify with a device file browser or logging the cache directory listing before/after, never a log line containing the URI itself).
- [ ] Cancel from the live-camera view exits the flow with no confirmation photo taken.
- [ ] Cancel from the review view exits the flow and the original capture's temp file is gone afterward.
- [ ] Submitting the same captured photo again after a simulated network loss (e.g. airplane mode mid-submit, then retry) uses the same request and succeeds without creating a duplicate analysis.
- [ ] Retake, then a new capture, produces a genuinely new analysis (never a replay of the discarded photo's result) -- i.e. the request-identity fix actually holds on-device, not just in unit tests.
- [ ] A successful submission transitions to polling, then to a completed result.
- [ ] Polling renders a completed analysis with at least one ABSTAINED metric correctly (no fabricated numeric value shown).
- [ ] Backgrounding the app mid-poll and returning to the foreground resumes polling/rendering correctly.
- [ ] Simulated camera initialization failure (`onMountError`) renders safe, nontechnical error copy rather than crashing.

## DEFERRED (tracked in `OPEN_ENGINEERING_ITEMS.md`)

- RevenueCat mobile SDK, paywall, purchase flow.
- Product-shopping/affiliate UI, routine editing, product substitution, budget optimization.
- Progress/history, Personal Baseline, Outcome Engine, Digital Twin, N-of-1 experiments.
- Social feed, chat, voice/video, push notifications.
- Cross-restart active-analysis restoration (item 6a).
- Offline-first writes (consent/profile mutations require a live connection).
- A native iOS/Android CI build (CI runs `npm ci`/typecheck/lint/test/`npm audit` only -- no native binary is built on every PR).
