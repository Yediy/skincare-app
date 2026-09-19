# Native Test Readiness V1

This phase begins only after Release Readiness Gate V1 is merged.

## Goal

Produce installable development builds and prove the real native boundaries on iOS and Android before any store-submission claim.

## Hard external inputs

These values are permanent or account-bound, so they are intentionally not fabricated in source control:

- iOS bundle identifier
- Android application ID
- EAS project linkage (`expo.extra.eas.projectId`)
- Expo account/project authorization
- RevenueCat development/Test Store public SDK keys
- backend URL reachable from physical test devices

Until those exist, the repository can be **engineering ready** but cannot truthfully be **native test ready**.

## Bootstrap sequence

From `mobile/`:

```bash
npx eas-cli@latest init
npx eas-cli@latest build:configure
npm run release:check -- --platform=ios
npm run release:check -- --platform=android
```

The production release check is intentionally stricter than a development build. For development/Test Store work, configure the development EAS environment with the development backend and RevenueCat Test Store values. Never copy a Test Store key into the production environment.

Then create development builds:

```bash
npx eas-cli@latest build --profile development --platform ios
npx eas-cli@latest build --profile development --platform android
```

## Native acceptance matrix

A phase is not complete merely because a binary installs. Humans have spent decades proving that "it launches" and "it works" are unrelated achievements.

| Area | iOS | Android | Required result |
| --- | --- | --- | --- |
| Install/launch | pending | pending | cold launch succeeds |
| Sign up/sign in | pending | pending | correct account restored |
| Password recovery | pending | pending | reset link completes |
| Camera denied | pending | pending | safe blocked state |
| Camera granted | pending | pending | capture succeeds |
| Analysis | pending | pending | submit → poll → result |
| History | pending | pending | completed analysis appears and opens |
| Test purchase | pending | pending | backend becomes premium |
| Restore | pending | pending | backend entitlement restored |
| Cancel/no purchase | pending | pending | free state remains free |
| Account A → B | pending | pending | no billing/data leakage |
| Account deletion | pending | pending | account removed and session invalidated |
| Offline launch | pending | pending | bounded error/recovery |
| Kill/relaunch | pending | pending | no stale cross-account state |

## Evidence required

For each platform retain:
- exact Git commit SHA;
- EAS build ID;
- app version/build number;
- test environment name;
- device + OS version;
- timestamp;
- pass/fail for every matrix row;
- screenshots or screen recording for failures;
- backend request/event identifiers when diagnosing billing or analysis failures.

## Exit criteria

Native Test Readiness V1 is complete only when:

1. stable iOS and Android identifiers are committed;
2. the repository is linked to the real EAS project;
3. both development builds install on real devices;
4. the matrix above passes on both platforms, or any failed row is explicitly classified as a release blocker;
5. RevenueCat Test Store purchase and restore are reflected by backend-authoritative entitlement state;
6. user switching demonstrates no data or entitlement leakage;
7. no production secret or Test Store key is committed to Git.

After this gate, proceed to Store Configuration & Sandbox Validation.
