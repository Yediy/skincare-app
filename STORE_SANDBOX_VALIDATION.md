# Store Configuration & Sandbox Validation V1

**Baseline:** Native Test Readiness V1 is merged.

This phase converts the already-engineered billing/account flows into verifiable Apple and Google store integrations. It does not treat a RevenueCat Test Store success as proof that App Store or Play billing works.

## 1. Permanent identity gate

Before any store product is created, the following identities must be final and consistent across Expo/EAS, Apple, Google, and RevenueCat:

| Identity | Required |
| --- | --- |
| iOS bundle identifier | yes |
| Android application ID | yes |
| EAS project ID | yes |
| RevenueCat project | yes |
| RevenueCat entitlement | `premium` |
| Production API origin | yes |

Do not rename the bundle/package after products are wired unless the stores require a migration. These identifiers are application identity, not decorative configuration.

## 2. Product model

V1 uses one backend-authoritative premium entitlement: `premium`.

Before store submission, define the exact subscription catalog. For every product record:

- internal plan name;
- Apple product ID;
- Google product/base-plan ID;
- billing period;
- trial/intro offer, if any;
- customer-facing price;
- RevenueCat package;
- RevenueCat Offering;
- entitlement mapping to `premium`;
- active/inactive status.

No mobile UI should infer premium from a product ID. RevenueCat events update the backend projection; the backend remains authoritative.

## 3. RevenueCat development validation

Use RevenueCat Test Store only in development/preview.

Required cases:

- new purchase grants backend premium;
- restore grants backend premium when appropriate;
- no purchase leaves backend free;
- expiration/revocation removes access;
- repeated webhook delivery is idempotent;
- out-of-order webhook delivery cannot regress a newer entitlement;
- reconciliation cannot overwrite a newer webhook;
- user A purchase cannot appear for user B after sign-out/sign-in;
- Customer Center/paywall failures fail closed.

Record provider event IDs and backend entitlement rows for failures.

## 4. Apple sandbox / TestFlight

Configure:

1. Apple app record matching the final bundle ID.
2. Subscription group.
3. Subscription products matching the approved V1 catalog.
4. Agreements/tax/banking prerequisites required by App Store Connect.
5. RevenueCat Apple app/store connection and product import.
6. Current RevenueCat Offering/package mapping.
7. App Store Server / RevenueCat integration required by the selected RevenueCat setup.

Validate on a real iPhone:

- purchase;
- restore;
- renewal;
- expiration;
- cancellation/manage-subscription path;
- account A → B isolation;
- kill/relaunch;
- network loss during purchase result handling;
- backend status after each transition.

## 5. Google Play internal/closed testing

Configure:

1. Play Console app matching the final application ID.
2. Subscription product and base plan(s).
3. License/test accounts and an internal or closed testing track.
4. RevenueCat Google Play connection and product import.
5. Current RevenueCat Offering/package mapping.

Validate on a real Android device:

- purchase;
- restore/resync;
- renewal;
- expiration;
- cancellation/manage-subscription path;
- account A → B isolation;
- kill/relaunch;
- network loss during purchase result handling;
- backend status after each transition.

## 6. Account deletion and subscription state

Account deletion and store subscription cancellation are distinct operations.

The app must:
- offer the existing in-app account deletion path;
- expose the configured external account-deletion resource;
- avoid claiming that deleting the app account automatically cancels Apple/Google billing unless the implemented store behavior actually guarantees that;
- direct a subscribed user to the relevant subscription-management flow when appropriate.

Deletion testing must verify that backend identity/session data is removed according to the implemented policy while store billing state is handled accurately and not silently misrepresented.

## 7. Evidence ledger

For every sandbox test record:

| Field | Required |
| --- | --- |
| Git SHA | yes |
| EAS build ID | yes |
| platform | yes |
| device / OS | yes |
| store environment | yes |
| test account alias | yes, no password |
| product ID | yes |
| RevenueCat App User ID | yes |
| RevenueCat event ID if applicable | yes |
| backend entitlement before | yes |
| backend entitlement after | yes |
| expected result | yes |
| actual result | yes |
| pass/fail | yes |
| screenshot/log reference | on failure |

Never place passwords, store secrets, private API keys, or service-account material in this ledger.

## 8. Exit criteria

This phase is complete only when:

1. permanent app identities and EAS project linkage exist;
2. RevenueCat development/Test Store matrix passes;
3. Apple sandbox/TestFlight purchase + restore + entitlement transitions pass;
4. Google Play internal/closed test purchase + restore + entitlement transitions pass;
5. account switching shows no entitlement leakage on either platform;
6. deletion/subscription messaging is accurate;
7. production keys are isolated from development/Test Store keys;
8. all failed cases are fixed and re-tested;
9. evidence is retained against exact build and Git SHAs.

After this phase, proceed to **Store Submission Readiness V1**: privacy/data disclosures, store metadata/assets, legal/support URLs, reviewer notes, production builds, and final submission checklist.
