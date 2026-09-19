# Release Readiness Gate V1

**Baseline:** `master` after Mobile C2 RevenueCat and Mobile C3 analysis history.

This gate separates **engineering readiness** from **store-release readiness**. A green unit/integration CI run is necessary but does not prove that Apple/Google identities, EAS project linkage, RevenueCat production keys, legal/support destinations, or real native purchase flows exist.

## What this pass implements

1. `mobile/scripts/release-readiness.mjs`
   - fail-closed production configuration validator;
   - checks iOS/Android application identifiers without inventing them;
   - requires a linked EAS project ID;
   - requires explicit EAS environment binding;
   - requires production HTTPS API/privacy/terms/support/account-deletion URLs;
   - requires RevenueCat to be enabled for the subscription-enabled release;
   - rejects a RevenueCat Test Store key in the production-key slots;
   - supports `--platform=ios`, `--platform=android`, or the default `all`.

2. `mobile/eas.json`
   - explicitly binds development, preview, and production build profiles to the matching EAS environments.

3. `npm run release:check`
   - operator-facing preflight. It is intentionally expected to fail until the external production values below actually exist.

4. `npm run test:release`
   - deterministic tests for the gate itself. CI runs these tests without fabricating production configuration.

## Why the real production check is not a normal CI gate yet

The repository intentionally does not contain:
- `ios.bundleIdentifier`;
- `android.package`;
- `extra.eas.projectId`;
- production RevenueCat public SDK keys;
- production API/legal/support URLs.

Those are external project/store decisions and credentials. CI must test that the validator rejects missing/unsafe values, not fill them with fake values and then claim release readiness.

Once the real values exist in the EAS production environment and app config, the release process must run:

```bash
cd mobile
eas env:pull --environment production
npm run release:check
```

A platform-specific preflight is also available:

```bash
npm run release:check -- --platform=ios
npm run release:check -- --platform=android
```

## External gates that still block a store release

### A. Stable app identity / EAS
- Choose and register the permanent iOS bundle identifier.
- Choose and register the permanent Android application ID.
- Run `eas init` so the real `extra.eas.projectId` is written to app config.
- Keep `development`, `preview`, and `production` EAS environments separate.

### B. Legal, privacy, and deletion surfaces
- Publish real privacy policy, terms, support, and account-deletion resources at HTTPS URLs.
- Keep the existing authenticated in-app account deletion path working.
- The external account-deletion resource must let a former/uninstalled user initiate deletion without requiring app reinstallation.
- Complete Apple App Privacy and Google Play Data Safety disclosures from the actual data flows, including camera/face-image processing and RevenueCat.
- Obtain product/legal approval before enabling async third-party image storage against real users; the repository still deliberately gates that feature off by default.

### C. RevenueCat / store billing
- Configure the `premium` entitlement and current Offering.
- Configure Test Store products/paywall/Customer Center for development validation.
- Use a Test Store API key only in development/preview.
- Replace it with platform-specific RevenueCat keys in production.
- Configure Apple and Google subscription products and connect them to RevenueCat.
- Configure the production webhook, backend RevenueCat API credentials, billing database credential, and webhook worker.

### D. Native validation
Before release, run at minimum:
- RevenueCat Test Store: purchase success, purchase failure, cancellation, restore, renewal/expiration.
- Apple sandbox/TestFlight: purchase, restore, account switch, cancel/manage subscription, grace/billing issue if enabled.
- Google Play internal/closed test: purchase, restore, account switch, cancel/manage subscription.
- password recovery/deep-link flow;
- account deletion;
- camera permission denial/acceptance;
- analysis submit/poll/result/history;
- force-kill/relaunch behavior;
- offline/provider-outage behavior;
- sign out user A → sign in user B on the same device, confirming no entitlement leakage.

## Current external references verified for this gate

- Expo app config: `ios.bundleIdentifier` and `android.package` are the standalone application identifiers.
- Expo EAS environments: build profiles can explicitly select `development`, `preview`, or `production`.
- Expo EAS project linkage: `eas init` writes `extra.eas.projectId`; EAS commands require a linked project.
- RevenueCat Test Store: use the Test Store key for development/testing and platform-specific keys for production.
- Google Play account deletion: an app with account creation needs an in-app deletion path and a web deletion resource.
- Apple account deletion: apps supporting account creation must support account deletion in-app; subscription users must be informed that store billing can continue until canceled.

## Release verdict semantics

- **ENGINEERING READY**: code + automated gates pass.
- **NATIVE TEST READY**: stable identifiers/EAS project exist and a development build can be created.
- **STORE SUBMISSION READY**: real legal URLs, store metadata/data disclosures, production RevenueCat/store configuration, and native sandbox/device tests are complete.
- **RELEASE READY**: submission artifacts have passed final device regression and store-side prerequisites.

Do not collapse these states into one green checkbox. That is how a passing test suite gets promoted into a production incident.
