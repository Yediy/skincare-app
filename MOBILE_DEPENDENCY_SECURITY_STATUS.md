# Mobile Dependency Security Status

Tracked exceptions to the mobile CI security gate -- every vulnerability
`npm audit` reports against `mobile/package-lock.json` is either fixed or
explicitly accepted here with a reason and a review date, never silently
suppressed. Companion to `SECURITY_VULNERABILITY_STATUS.md` (the backend's
equivalent document) and produced the same way: classify, document, gate on
severity that actually matters, never blindly run `npm audit fix --force`.

Measured this pass (`npm audit --omit=dev --json` and `npm audit --json`,
`mobile/package-lock.json` as of this PR): **14 findings, all `moderate`,
zero `high`/`critical`, in both the production-only and full audit** (i.e.
none of these are dev/test-only-dependency findings that a `--omit=dev`
audit would hide).

## Why none of these are fixed in this PR

Every one of the 14 findings' `fixAvailable` suggestion from `npm audit` is
a **semver-major downgrade** of `expo` (-> 46.0.21), `expo-router` (->
5.1.11), or `expo-splash-screen` (-> 55.0.25) -- all *older* than the
Expo SDK 57 versions this app is actually pinned to. Running `npm audit fix
--force` here would not upgrade past a vulnerability; it would downgrade the
entire Expo toolchain by more than ten major versions, breaking the app.
There is currently no compatible patched release for Expo SDK 57's own
dependency graph, so "keep the documented, justified exception" is the
correct call per this pass's own instruction, not a shortcut.

## Findings

| Package | Direct/Transitive | Where it actually runs | Advisory | Severity | Runtime reachability | Decision |
|---|---|---|---|---|---|---|
| `expo`, `expo-router`, `expo-splash-screen` | Direct (in `package.json` `dependencies`) | Both -- see their transitive deps below for what's actually flagged | (flagged only via transitive deps below) | moderate | N/A -- the packages themselves aren't the vulnerable code | Accepted |
| `@expo/cli`, `@expo/config`, `@expo/config-plugins`, `@expo/inline-modules`, `@expo/local-build-cache-provider`, `@expo/metro-config`, `@expo/prebuild-config` | Transitive (via `expo`) | **Build-time only.** These are Expo's CLI/dev-server/native-prebuild tooling -- they run on the developer's or CI's machine during `expo start`/`expo prebuild`/`eas build`, and are never bundled into the JS the app ships to a device. | (inherited from `xcode`/`uuid` below) | moderate | None -- not part of the shipped runtime bundle | Accepted, tracked |
| `xcode` | Transitive (via `@expo/config-plugins`) | **Build-time only.** Manipulates `.xcodeproj` files during native prebuild; never runs on-device. | [GHSA-w5hq-g745-h8pq](https://github.com/advisories/GHSA-w5hq-g745-h8pq) (via `uuid`) | moderate | None | Accepted, tracked |
| `uuid` | Transitive (via `xcode`) | Build-time only (see `xcode` above) | [GHSA-w5hq-g745-h8pq](https://github.com/advisories/GHSA-w5hq-g745-h8pq): missing buffer bounds check in `v3`/`v5`/`v6` **only when a caller passes an explicit `buf` argument** | moderate | None here -- `xcode`'s own usage doesn't pass `buf`, and nothing in this app calls `uuid` directly at all | Accepted, tracked |
| `query-string` | Transitive (via `expo-router`) | **Runtime.** `expo-router` uses this to parse route/deep-link query strings, so it IS part of the shipped app bundle. | (inherited from `decode-uri-component` below) | moderate | Same as `decode-uri-component` | Accepted, tracked -- the one finding with real on-device reachability |
| `decode-uri-component` | Transitive (via `query-string`) | Runtime, via `query-string` (see above) | [GHSA-vcc3-ghjq-m6fr](https://github.com/advisories/GHSA-vcc3-ghjq-m6fr): denial-of-service via exponential-time decoding of a malformed percent-encoded string | moderate | **Reachable in principle**: a maliciously crafted deep link/URL opened by this app could trigger slow parsing. Impact is a transient parse-time DoS on that one screen's render (not memory corruption, data exposure, or RCE), and requires the user to open a specifically crafted external link. | Accepted, tracked -- highest-priority of the 14 to revisit |

## Process

- No blanket ignores. Every accepted finding above names a package, an
  advisory, and a review date.
- `mobile-test`'s CI job (`.github/workflows/ci.yml`) runs `npm audit
  --omit=dev --audit-level=high` as a real gate: it fails the build on any
  **production** `high`/`critical` finding, which is currently zero. A full,
  non-gating `npm audit --json` also runs (informational) so a new moderate
  finding is visible in CI logs even though it doesn't fail the build.
- Review this document whenever the CI audit step reports a new package, a
  severity above `moderate` on anything currently listed here, or when Expo
  ships an SDK line whose own dependency pins have moved past these
  advisories (check via `npm outdated` / the Expo SDK changelog).
- Review date: 2026-12-09 (aligned with the backend's own
  `SECURITY_VULNERABILITY_STATUS.md` protobuf review date, for one shared
  quarterly checkpoint).
