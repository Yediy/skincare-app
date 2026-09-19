# Mobile V1 Phase C2 — RevenueCat Purchasing and Entitlement Sync

**Branch this document describes:** `feat/mobile-c2-revenuecat`, base `master` `5157fb9621652845bc0d3be5c925474e9571d38a`.

Classification used throughout: **IMPLEMENTED** (real code exists and runs), **TESTED** (real automated test coverage exists), **DEFERRED** (out of scope, tracked in `OPEN_ENGINEERING_ITEMS.md`), **EXTERNAL** (requires operator action outside this repository, honestly not claimed as done).

## Purpose

Connect the mobile app to the already-existing, unmodified backend RevenueCat billing architecture (`BILLING_ARCHITECTURE.md`, `ENTITLEMENT_STATE_MACHINE.md`, `REVENUECAT_INTEGRATION_NOTES.md`). This pass builds **no second billing system** — every entitlement/quota decision still runs through `app/domain/entitlement.py`'s `UsagePolicyService`/`RevenueCatEntitlementService`, completely untouched.

## The central invariant

**RevenueCat `CustomerInfo` is useful for immediate purchase UX, but the backend `user_entitlements` projection remains the authority for premium access and analysis quota.** The mobile client never grants itself premium quota merely because the local RevenueCat SDK says a purchase succeeded. Every screen renders premium status/allowance from `GET /api/v2/billing/status`; a purchase or restore only ever *triggers* `POST /api/v2/billing/sync` and then re-reads server status.

## Identified-user lifecycle

- **Canonical App User ID**: the backend's own `users.id` UUID, never email/display-name/device-id/a hardcoded string.
- **Obtained via**: `GET /me`, only after `useSession().state.status === "AUTHENTICATED"` (`src/auth/session-context.tsx`, unmodified).
- **Configuration**: `src/billing/revenuecat-context.tsx`'s `RevenueCatProvider` (nested beneath `SessionProvider` in `app/_layout.tsx`) calls `Purchases.configure({ apiKey, appUserID })` exactly once per native process, guarded by a module-level singleton coordinator (`moduleConfiguredUserId`/`moduleConfiguringPromise`) that survives component remounts and prevents two concurrent effects from double-configuring. The installed SDK's own `Purchases.isConfigured()` facility is consulted as an additional guard (never a hand-invented equivalent — its exact signature was read from `node_modules/react-native-purchases/dist/purchases.d.ts` before use).
- **Never configured while signed out** — `UNKNOWN`/`SIGNED_OUT` session states are a deliberate no-op, so C2 never creates an unnecessary anonymous RevenueCat customer.

## Why SDK `logOut()` is intentionally never used

This app uses **custom App User IDs only** (the backend UUID). RevenueCat's own documented behavior for a custom-ID app is that `Purchases.logOut()` transitions the SDK to a **new anonymous** (`$RCAnonymousID:...`) customer — there is no "log out to nothing" state in the custom-ID model. Calling it on ordinary application sign-out would therefore manufacture an anonymous RevenueCat customer this app never wanted, and (per RevenueCat's own guidance) `logOut()` is intended for switching *away from* an identified user back to anonymous, not for switching *between* two identified users.

- **Ordinary sign-out** (`src/auth/session-context.tsx`, unmodified) clears the existing auth token pair and TanStack Query cache exactly as before this pass; `RevenueCatProvider` additionally clears its own in-memory billing UI state (`customerInfo`/`configuredUserId`) on the `SIGNED_OUT` transition. RevenueCat itself is left exactly as configured — no adapter call of any kind happens during sign-out (proven directly: `tests/billing/revenuecat-context.test.tsx`'s sign-out test asserts the adapter's own call counters are unchanged across the transition).
- **Account switching**: if a *different* backend user signs in during the same native process, `RevenueCatProvider` calls `Purchases.logIn(newUserId)` — never `logOut()` first. Proven directly: user A configured → sign-out → user B signs in → `logIn` called with user B's UUID, `configure()` called exactly once total (for user A), zero anonymous-ID-creating calls anywhere.
- **`RevenueCatAdapter` (`src/billing/revenuecat-adapter.ts`) has no `logOut` method at all** — this is a structural guarantee, not just a convention: there is nothing for any future call site to accidentally call.

## Public vs. secret RevenueCat keys

| | Public (mobile) | Secret (backend only) |
|---|---|---|
| Variables | `EXPO_PUBLIC_REVENUECAT_ENABLED`, `EXPO_PUBLIC_REVENUECAT_IOS_API_KEY`, `EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY`, `EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID` | `REVENUECAT_API_KEY`, `REVENUECAT_WEBHOOK_AUTH`, `REVENUECAT_WEBHOOK_SIGNING_SECRET`, `REVENUECAT_BILLING_DATABASE_URL` |
| Where declared | `mobile/src/constants/config.ts` | `backend/app/config.py` |
| Bundled into the client binary | Yes — this is RevenueCat's own documented client-safe key class | Never |
| Committed to this repository | Never a real value; `.env.example` documents the variable names only, commented out | Never |

An unconfigured deployment (`EXPO_PUBLIC_REVENUECAT_ENABLED` unset/not `"true"`, or no public key set for the running platform) runs the ordinary app normally — the subscription screen reports `DISABLED`/`MISSING_KEY` rather than throwing at startup or silently pretending billing works.

## Purchase → reconciliation → projection flow

0. **Purchase actions are gated before any of this can start** (`src/billing/billing-action-readiness.ts::computeBillingActionReadiness`, added after an independent review found native `availability === "READY"` alone was an insufficient gate): the Upgrade/Restore/Manage-subscription actions are only reachable when mobile RevenueCat availability is `READY`, the server's `GET /api/v2/billing/status` has loaded, the server reports `billing_enabled: true`, the RevenueCat-configured user id matches the server's `app_user_id`, and the mobile `EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID` matches the server's `entitlement_identifier`. Any mismatch renders an honest "Purchases unavailable" state instead — an existing server-granted entitlement is always still displayed regardless of this gate; only the ability to start a *new* purchase action is gated.
1. User taps **Upgrade to Premium** → `RevenueCatUI.presentPaywallIfNeeded({ requiredEntitlementIdentifier: "premium" })` against the RevenueCat dashboard's **current/default Offering** — no package/product identifier (`premium_monthly`/`premium_annual`) is hardcoded into purchase decision logic, so the dashboard remains free to change presentation without a client release.
2. `PAYWALL_RESULT.CANCELLED` → true no-op, not an error, no sync. `PAYWALL_RESULT.NOT_PRESENTED` is **not** treated as a no-op: since this button is only reachable when the server has already said this user is not premium, a `NOT_PRESENTED` result means the SDK's own local entitlement cache disagrees with the server (RevenueCat's own contract is that `presentPaywallIfNeeded` only presents when the required entitlement is not already locally active) — so it triggers the same backend sync as a real purchase, to let server state resolve the disagreement. `PAYWALL_RESULT.ERROR` → bounded error state (`src/billing/purchase-error.ts`).
3. `PAYWALL_RESULT.PURCHASED`/`RESTORED` → the screen shows **"Purchase received. We're syncing your access."** and calls `useBillingSyncMutation()` → `POST /api/v2/billing/sync`.
4. The backend route (`backend/app/api/v2/billing.py::sync_billing_status`) derives the user id from the bearer token only, requires `revenuecat_billing_enabled`, uses the dedicated `skincare_billing` DB pool, and calls `RevenueCatReconciliationService.reconcile_user()` (unmodified) — which calls RevenueCat's own real, documented `active_entitlements` REST resource and corrects the local `user_entitlements` projection on mismatch.
5. The mutation invalidates the `billingStatus` query → `GET /api/v2/billing/status` refetches → if the server now reports `ACTIVE`/`GRACE_PERIOD`, premium UI activates.
6. If RevenueCat's API is unavailable, `reconcile_user()` re-raises **before** touching local state (existing, unmodified behavior) → the route returns `503` → the screen shows a bounded "couldn't confirm your purchase" state with **Retry**, and the *existing* local entitlement is provably unchanged (`backend/tests/api/test_billing_route.py::test_sync_revenuecat_failure_leaves_existing_local_entitlement_unchanged`).

No step in this flow ever sets a local `hasPremium = true` flag directly from the paywall result.

## Restore flow

`Restore purchases` is always visible (free or premium). Calls `Purchases.restorePurchases()`; a restore finding nothing is not a crash — it's a normal, silent outcome. Success or failure both funnel through the same purchase-error classification and backend-sync-then-refresh path as a purchase. Never logs raw receipt data.

## Customer Center

`Manage subscription` (visible only for a premium/grace-period customer, and only when the purchase-action readiness gate above is satisfied) presents `RevenueCatUI.presentCustomerCenter()`. Whatever happens inside it (plan change, cancellation initiated, nothing), the app treats its closing as a signal to `sync` + refresh server status — it never assumes a specific outcome, and never attempts to cancel a subscription directly through this app's own backend (the store/provider owns subscription management). Cancellation initiated through Customer Center does not revoke access immediately client-side; the existing backend entitlement state machine (`ENTITLEMENT_STATE_MACHINE.md`) remains authoritative for exactly when access actually ends. If `presentCustomerCenter()` itself throws (Customer Center never successfully opened/closed), the screen shows a bounded error and does **not** call sync — nothing could have changed.

## Identified-user lifecycle: fail-closed account switching

`RevenueCatProvider` tracks two separate things: the module-level "last user the SDK itself successfully configured/logged in as" (`moduleConfiguredUserId`, which only ever advances on a successful `adapter.configure()`/`adapter.logIn()` call, so a failed switch correctly leaves the underlying SDK on its previous user without ever calling `logOut()`), and the React-exposed `configuredUserId` — "is the SDK demonstrably configured as *this session's* intended user right now." The latter is set to the resolved backend UUID **only** immediately after that exact user was successfully configured/switched to, and is explicitly set to `null` on any failure (a failed `/me` call, a missing public key, or a failed `adapter.logIn()`). A previous session's leftover `moduleConfiguredUserId` is never exposed to a new session merely because that new session's switch attempt failed — every purchase/restore/Customer-Center action gates on `configuredUserId`, so `null` makes all of them unreachable until identification for the *current* user actually succeeds. `useRevenueCat().retryIdentification()` (surfaced in the subscription screen as a **Retry** action when purchases are unavailable because identification hasn't completed) re-runs identification without ever calling `logOut()`.

## Test Store development flow

1. Create a RevenueCat project (or use an existing one) with a **Test Store** enabled — **EXTERNAL**, not done by this pass.
2. Obtain the Test Store's public SDK key from the RevenueCat dashboard.
3. Set it as `EXPO_PUBLIC_REVENUECAT_IOS_API_KEY`/`EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY` via EAS environment configuration (`eas env:create` or the EAS dashboard) for a development build profile — **never** commit a real key to `.env`/`app.json`/source.
4. Build a development client (`eas build --profile development`) — **EXTERNAL, NOT RUN this pass** (requires `ios.bundleIdentifier`/`android.package`, which do not exist in this repository yet — see below).
5. Install the development build on a physical device or simulator, sign in as a real app user, open **Settings → Subscription**, and attempt an upgrade against the Test Store.
6. Confirm: paywall presents the current Offering; a Test Store purchase completes; the screen shows the syncing state, then transitions to Premium; `GET /api/v2/billing/status` (inspected via `/docs` or a backend log) reports `ACTIVE`.

**This pass did not perform steps 4–6** — see "Native build honesty" below. Expo Go preview behavior is explicitly **not** treated as proof that real native purchasing works; RevenueCat's native modules require a development build, not Expo Go.

## Web exclusion

`Platform.OS === "web"` is checked before any adapter call reaches `presentPaywall`/`restorePurchases`/`presentCustomerCenter`/`configure`. The subscription screen renders an explicit "Subscription management isn't available on web yet" message instead. `npx expo export --platform web` stays green with `react-native-purchases`/`react-native-purchases-ui`/`expo-dev-client` installed (verified this pass, 982 modules bundled, no errors). C2 does not implement RevenueCat Billing/Stripe web checkout — that remains fully out of scope.

## Required external dashboard/store configuration — **EXTERNAL, not completed by this pass**

None of the following exist as a result of this pass. They are genuine, itemized prerequisites for a real purchase to ever happen, listed here so nothing is silently assumed:

- RevenueCat project
- RevenueCat Test Store enabled on that project
- The `premium` entitlement configured in the RevenueCat dashboard
- A current/default Offering containing the paid subscription packages (expected product identifiers: `premium_monthly`, `premium_annual` — external store configuration, never hardcoded in this app's purchase logic)
- RevenueCat Paywall configured (template/content) for that Offering
- RevenueCat Customer Center configured
- Public Apple (iOS) RevenueCat SDK key
- Public Google (Android) RevenueCat SDK key
- Backend RevenueCat secret/API configuration (`REVENUECAT_API_KEY`/`REVENUECAT_PROJECT_ID`/webhook secrets — separately tracked, `BILLING_ARCHITECTURE.md`)
- RevenueCat webhook destination pointed at this deployment
- The billing webhook worker actually deployed and running
- The `skincare_billing` runtime DB credential provisioned in the target environment
- An Apple Developer / App Store Connect application
- A Google Play Console application
- **`ios.bundleIdentifier`** — absent from `mobile/app.json`; **not invented by this pass**
- **`android.package`** — absent from `mobile/app.json`; **not invented by this pass**
- Real privacy policy / terms / support / account-deletion URLs (tracked separately, `ACCOUNT_RECOVERY_ARCHITECTURE.md`)
- An EAS project identity for this app
- A real EAS development build
- A real RevenueCat Test Store purchase smoke test
- A later real Apple/Google sandbox purchase smoke test

## Native build honesty

- **Native development build: NOT RUN — BLOCKED BY EXTERNAL CONFIG.** No `ios.bundleIdentifier`/`android.package` exist, no Apple/Google developer accounts or EAS project identity were provided, and none were fabricated to force a build through. `eas build` was not invoked.
- **Real RevenueCat Test Store purchase: NOT PERFORMED.** No development build exists to run it on.
- **Real iOS/Android sandbox purchase: NOT PERFORMED.**
- A clean `npx expo export --platform web` and a clean `npx expo-doctor` (21/21) are **not** claimed as proof of native purchasing — they prove the JS bundles and the Expo SDK/dependency graph are internally consistent, nothing about real StoreKit/Google Play Billing behavior.

## Jest exit diagnosis (independent-review Blocker 5)

A first version of this pass changed `mobile/package.json`'s `test` script from `jest` to `jest --forceExit` because the suite appeared to hang after all tests passed. An independent review correctly flagged `--forceExit` as a mask that could silently hide a real leaked listener/timer/unmounted client going forward, and required root-causing the actual leak instead. Two genuinely separate problems were found and fixed; `--forceExit` masked both.

**Problem 1 — a real multi-minute process hang.** Found by timing progressively narrower subsets of the suite (`tests/api` alone: ~12s; `tests/query` alone: hung for the full timeout) down to a single test, `tests/query/use-billing.test.tsx`'s `useBillingSyncMutation` test. Root cause, confirmed by reading TanStack Query v5's own installed source (`node_modules/@tanstack/query-core/build/modern/`): every `Query` and `Mutation` is a `Removable`, which schedules its own `setTimeout(..., gcTime)` (default `gcTime` is 5 minutes — `3e5` ms in `removable.js`) once created. `QueryCache.clear()` properly cancels each query's pending gc timeout (it calls `.remove()`, which calls `.destroy()`, which calls `.clearGcTimeout()`). `MutationCache.clear()` (`mutationCache.js`) does **not** — it only clears its own internal bookkeeping `Set` and fires a "removed" notification; it never calls `.destroy()` on any mutation. A mutation's own 5-minute gc timer therefore survived `queryClient.clear()`/`queryClient.unmount()` entirely and kept the Node process alive until it naturally fired — matching the observed ~5–7 minute delay past "Ran all test suites." exactly. (An earlier, unverified guess in this pass's own working notes attributed the hang to the since-removed CustomerInfo-update listener; that was wrong — bisection proved the listener was never involved, and removing it alone did not fix the hang.) Fix, in `tests/query/use-billing.test.tsx`'s `afterEach`: explicitly destroy every mutation before clearing/unmounting the client —
```ts
afterEach(() => {
  unmountHook?.();
  queryClient.getMutationCache().getAll().forEach((mutation) => mutation.destroy());
  queryClient.clear();
  queryClient.unmount();
});
```

**Problem 2 — a separate, much shorter (near-instant) but CI-failing race.** Even after Problem 1 was fixed, running `tests/billing/`'s 5 files together (never any single one alone) deterministically (4/4 runs) printed "Cannot log after tests are done" and set `process.exitCode = 1` — confirmed by reading `jest-runner`'s own `runTest.js`, which sets that exit code unconditionally whenever *anything* logs to console after a test file's own teardown, independent of whether any assertion failed (all 30 tests always passed). The message itself traces to `jest-expo`'s own preset `setup.js`, which lazily touches `global.fetch` (a self-replacing getter installed by `expo/src/winter/installGlobal.ts`) as a side effect of mocking `expo-modules-core`; because Jest's default worker pool reuses one OS process across several test files, that lazy resolution can straddle a file boundary and get attributed to whichever file happens to be active when it finally resolves — a jest-expo/Jest-worker-pool interaction, not a bug in any single test file (confirmed: every one of the 5 files passed with zero warning when run in isolation). Fix, in `tests/setup/jest.setup.ts` (runs once per test file, before that file's own code): touch `globalThis.fetch` once, synchronously, up front, so the resolution completes deterministically within each file's own setup phase instead of racing against an arbitrary later point —
```ts
void globalThis.fetch;
```
Verified stable across repeated runs after this fix (3/3 clean on `tests/billing/` alone, full-suite runs clean and fast).

`mobile/package.json`'s `test` script is back to plain `jest` — confirmed to exit cleanly, quickly, and with exit code 0 (no `--forceExit`, no `--detectOpenHandles`, no residual delay, no stray post-teardown console output) on repeated full-suite runs after both fixes.

## Code integration — what this pass actually verified

- `expo-dev-client@~57.0.19`, `react-native-purchases@^10.10.0`, `react-native-purchases-ui@^10.10.0` installed via `npx expo install` (Expo-SDK-57/RN-0.86-compatible versions, not manually pinned). `npx expo-doctor` — 21/21, before and after installation.
- Every SDK method call in `src/billing/revenuecat-adapter.ts` was written against the installed package's own TypeScript definitions (`node_modules/react-native-purchases/dist/purchases.d.ts`, `node_modules/react-native-purchases-ui/src/index.tsx`), inspected directly before writing any call — never invented from memory.
- 35 new mobile tests (`mobile/tests/billing/`, `mobile/tests/query/use-billing.test.tsx`, `mobile/tests/app/subscription.test.tsx`), every one injecting a fake, duck-typed `RevenueCatAdapter` (`tests/billing/fake-adapter.ts`) or mocking the query/context hooks directly — zero real network or native purchase calls in a normal Jest run, proven by construction (no test imports `react-native-purchases`'s real `Purchases` singleton without it being module-mocked first).
- 19 new backend tests (`backend/tests/api/test_billing_route.py`) against real Postgres, real auth, real rate limiting.
- `npx expo export --platform web` succeeds with all three native packages installed.
- TypeScript/`tsc --noEmit`, ESLint (0 errors), full backend + mobile suites all green — see the PR's own final validation report for exact counts.

## Data flow summary

```
Mobile (identified via /me → backend UUID)
  │
  ├─ Purchases.configure()/logIn()  ───────────────► RevenueCat SDK (native)
  │                                                        │
  │                                                   Purchase/restore/
  │                                                   Customer Center
  │                                                        │
  ├─ GET  /api/v2/billing/status  ◄───────────────┐        │
  ├─ POST /api/v2/billing/sync    ─────────────────┼────────┘
  │        │                                       │
  │        ▼                                       │
  │  RevenueCatReconciliationService.reconcile_user()
  │        │                                       │
  │        ▼                                       │
  │  RevenueCat REST API (active_entitlements)      │
  │        │                                       │
  │        ▼                                       │
  │  user_entitlements (local Postgres projection) ─┘
  │
  └─ UI renders ONLY from GET /api/v2/billing/status
```
