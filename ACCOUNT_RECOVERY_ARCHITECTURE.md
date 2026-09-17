# Account Recovery Architecture (V1 Launch Foundation)

**Branch:** `feat/v1-account-recovery-release-foundation`, based on `master`
after PR #9 (Mobile V1 Phase C1) and PR #10 (System Integrity Gate V1) both
merged.

This is a launch-readiness pass, not a feature phase. It closes four
launch-foundation gaps before Mobile C2 introduces native RevenueCat
billing: password recovery, a provider-neutral transactional-email
boundary, EAS build scaffolding, and external release URL seams. It does
not touch RevenueCat, C3/history/progress, the recommendation/safety
model, or the catalog.

Documents only what this pass actually implements and tests. For
everything else, see `ARCHITECTURE_CURRENT.md` / `MOBILE_ARCHITECTURE.md`
/ `OPEN_ENGINEERING_ITEMS.md`.

## 1. The recovery sequence

```
Forgot password
  -> POST /password/forgot (always the same outward response)
  -> hashed, single-use, expiring token issued (only if the account is
     real and eligible -- invisible to the caller either way)
  -> token delivered by email, via the TransactionalEmailService
     boundary (never Resend called directly from a route/domain module)
Reset
  -> POST /password/reset (token + new password)
  -> one atomic statement: verify token exists/unused/unexpired AND the
     account is still active/not-deleted, consume the token, in the
     same UPDATE
  -> on success, in the SAME transaction: update password hash,
     invalidate every other outstanding reset token for this user,
     revoke every refresh-token family for this user
  -> every previously issued access/refresh session becomes unusable;
     the resetting device is NOT auto-signed-in
```

## 2. Data model

**IMPLEMENTED.** Migration `dec963f29e8d` (`backend/migrations/versions/
dec963f29e8d_create_password_reset_tokens_table.py`), forward-only,
`down_revision = '44a74f2a79a7'` (the System Integrity Gate V1 tip).

```sql
password_reset_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    token_hash VARCHAR(64) NOT NULL UNIQUE,   -- sha256 hex digest, same
                                               -- convention as refresh_tokens
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Indexes: `idx_password_reset_tokens_user_id` (plain), plus
`idx_password_reset_tokens_user_id_active` (partial, `WHERE used_at IS
NULL`) for the small active-token-per-user lookups issuance/consumption
both need.

There is deliberately no separate "invalidated"/"revoked" column apart
from `used_at` -- "superseded by a newer token" and "consumed by a
successful reset" are, for this table's purposes, the single fact a
reader needs ("is this token still usable"), and `created_at` vs.
`used_at` already lets an operator distinguish the two after the fact.

RLS follows the same pre-identity/post-identity split
`feb038fd05bd` already established for `refresh_tokens`:

- `password_reset_tokens_by_user` (`user_id = app.current_user_id`) --
  used once a real `user_id` is known (issuance, and post-consumption
  cleanup/session revocation).
- `password_reset_tokens_by_hash` (`token_hash =
  app.current_reset_token_hash`) -- used for the genuinely pre-identity
  consumption lookup, mirroring `refresh_tokens`' `app.current_token_hash`.

Two `SECURITY DEFINER` functions (same bypass-RLS-for-a-narrow-purpose
pattern as `login_lookup_by_email`), both `REVOKE ALL FROM PUBLIC` /
`GRANT EXECUTE TO skincare_app` only:

- `password_reset_eligibility_lookup_by_email(p_email)` -- pre-identity
  lookup for `POST /password/forgot` (`id, is_active, deleted_at`).
- `password_reset_account_eligible(p_user_id)` -- called *inside* the
  atomic consume-and-verify `UPDATE` (`POST /password/reset`), because at
  that point in the transaction only `app.current_reset_token_hash` is
  set, not `app.current_user_id` -- a direct `EXISTS (SELECT ... FROM
  users ...)` in that `UPDATE`'s `WHERE` clause would be silently
  blocked by `users`' own `users_identity_scope` RLS policy, making
  every account look ineligible. This function exists specifically to
  let that one `UPDATE` prove eligibility without needing the
  circular "know the user_id before you've verified the token" GUC.

An explicit `GRANT SELECT, INSERT, UPDATE ON password_reset_tokens TO
skincare_app` is required in the same migration -- this codebase's
`skincare_app` role has no default-privilege grant for new tables (every
table-creating migration since `7b38b717546e` grants explicitly).

## 3. Token security model

**IMPLEMENTED, TESTED.**

- Generated with `secrets.token_urlsafe(32)` (`app/domain/
  password_reset_service.py::generate_reset_token`) -- a cryptographically
  secure random source, same class of generator `app/security/tokens.py`'s
  `generate_refresh_token()` already uses for refresh tokens.
- Only the SHA-256 hex digest (`hash_reset_token`) is ever persisted.
  The plaintext token exists only transiently: in-process while building
  the reset URL, and in the outbound email body.
- Never logged, never put in `app.observability.events`, never returned
  in any API response body other than embedded once in the reset email,
  never stored in mobile persistent storage (SecureStore/AsyncStorage/
  SQLite/filesystem/persisted query cache) -- see section 6.
- Issuing a new token invalidates every other outstanding (unused) token
  for that user, in the same transaction as the insert
  (`password_reset_repository.issue_reset_token`).
- A successfully used token becomes permanently unusable (`used_at` set
  in the same atomic statement that verifies it).

## 4. Single-use / concurrency

**IMPLEMENTED, TESTED under real concurrent Postgres access**
(`backend/tests/auth/test_password_reset.py::
test_reset_concurrent_double_use_exactly_one_succeeds`).

`password_reset_repository.consume_token_and_apply_reset` is ONE
`UPDATE ... WHERE token_hash = $1 AND used_at IS NULL AND expires_at >
now() AND password_reset_account_eligible(prt.user_id) RETURNING
prt.user_id` -- never a `SELECT` to check followed by a separate
`UPDATE` to consume, which would reopen a TOCTOU window. Two concurrent
callers racing the same raw token can both reach this statement, but
Postgres row-level locking on the matched row serializes them:
whichever commits first flips `used_at` to non-NULL, so the second's
`WHERE` clause no longer matches and it updates zero rows. This is the
same one-statement-proves-everything discipline System Integrity Gate
V1 established for `analysis_repository.mark_processing()`'s `job_id`
proof (see `SYSTEM_INTEGRITY_GATE.md` section 2) -- account-recovery
security-critical code reuses that exact pattern rather than
reinventing a weaker one.

## 5. Account-enumeration protection

**IMPLEMENTED, TESTED.** `POST /password/forgot`'s outward response is
byte-for-byte identical for an existing account, a nonexistent email, a
disabled account (`is_active = false`), and a deleted account
(`deleted_at` set):

```json
{"message": "If an eligible account exists for that email, password reset instructions will be sent."}
```

`PasswordResetService.request_reset()` silently no-ops for every
ineligible case rather than raising or varying behavior -- there is no
code path in the route handler that branches on account existence. The
same discipline extends to `POST /password/reset`'s failure response
(one generic 400 for expired/used/unknown/malformed token, or an
otherwise-valid token whose account has since become ineligible).

## 6. Session invalidation after reset

**IMPLEMENTED, TESTED**, including a Redis-outage scenario.

Within the SAME transaction as the token consumption
(`consume_token_and_apply_reset`): `users.password_hash` is updated,
every other outstanding reset token for the user is invalidated, and
every `refresh_tokens` row for that user has `revoked_at` set --
identical in spirit to `/logout-all`'s own SQL. System Integrity Gate
V1's `get_current_user` (`app/security/auth.py`) is what actually makes
this durable: Postgres is the authoritative revocation check, Redis only
a best-effort fast-deny cache. `POST /password/reset` sets the same
`user_tokens_invalid_before:{user_id}` Redis marker `/logout-all` sets,
wrapped in the same `try/except RedisError` best-effort pattern -- a
Redis outage at that point does not fail the request (the durable reset
already committed) and, critically, cannot *restore* an old session,
because `get_current_user` falls through to the authoritative Postgres
check on any `RedisError` rather than failing open.

The resetting device is not automatically signed in -- `POST
/password/reset`'s response body carries no `access_token`/
`refresh_token`; sign-in with the new password is required.

## 7. Abuse protection

**IMPLEMENTED, TESTED.** Two independent limiters on `POST
/password/forgot`, both enforced unconditionally before
`PasswordResetService` ever learns whether the address is real (so being
rate-limited never itself discloses account existence):

- `AUTH_POLICY` (existing, per-IP, `app/middleware/rate_limiter.py`) --
  the same policy `/signup`/`/login`/`/refresh`/`/logout` already use.
- `PASSWORD_RESET_EMAIL_POLICY` (new), keyed by a SHA-256 hash of the
  submitted (lowercased) email itself, default 5 requests / hour
  (`RATE_LIMIT_PASSWORD_RESET_EMAIL_MAX` / `_WINDOW_SECONDS`) -- closes
  the gap `AUTH_POLICY` alone leaves: a single target address spammed
  from many different IPs.

No CAPTCHA in this pass, per scope.

## 8. Transactional email boundary

**IMPLEMENTED, TESTED** (`app/domain/transactional_email.py`).

`TransactionalEmailService` (ABC) is the interface the domain layer
depends on; `PasswordResetService`/`app/main.py` never import Resend
directly. Implementations:

- `ResendTransactionalEmailService` -- real, calls `https://
  api.resend.com/emails`. Tested against `httpx.MockTransport` only
  (same pattern `RevenueCatAPIClient` already established) -- no test in
  this repository ever sends a real email.
- `NullTransactionalEmailService` -- explicit no-op default
  (`EMAIL_PROVIDER=none`), logs one structured line with no email/URL/
  token, and can never become the production provider merely by an
  operator leaving `EMAIL_PROVIDER` unset (production config validation
  rejects it -- section 9).
- `InMemoryTransactionalEmailService` -- test-only fake, records calls in
  memory. `build_transactional_email_service()` never returns this.

Settings: `EMAIL_PROVIDER`, `RESEND_API_KEY`, `PASSWORD_RESET_FROM_EMAIL`,
`PASSWORD_RESET_URL_BASE`, `PASSWORD_RESET_TOKEN_TTL_MINUTES` (default 30
minutes). `RESEND_API_KEY` is a backend-only secret -- never exposed to
mobile/Expo (mobile has no `EXPO_PUBLIC_RESEND_*` variable, and never
will; email sending is entirely server-side).

## 9. Production config validation

**IMPLEMENTED, TESTED** (`backend/tests/unit/test_config_validation.py`).

Unlike `revenuecat_billing_enabled`/`catalog_admin_enabled`, there is no
`PASSWORD_RESET_ENABLED` opt-in flag -- `POST /password/forgot`/`POST
/password/reset` always exist as core V1 surface, so
`Settings._reject_unsafe_production_config` always requires, in
production:

- `EMAIL_PROVIDER == "resend"` (rejects the `"none"` default outright).
- `RESEND_API_KEY` / `PASSWORD_RESET_FROM_EMAIL` non-blank and not a
  known placeholder value.
- `PASSWORD_RESET_URL_BASE` an `https://` origin, not a dev-scheme
  (`skincare://...`) value and not an obvious placeholder
  (`example.com`/`localhost`/`127.0.0.1`).
- `PASSWORD_RESET_TOKEN_TTL_MINUTES > 0`.

Development/CI are unaffected -- these checks only run when
`ENVIRONMENT=production`.

## 10. Deep-link contract

**PARTIALLY CONFIGURED EXTERNALLY.** The app's existing Expo scheme,
`skincare` (`mobile/app.json`), is preserved and reused: a dev/preview
build can open `skincare://reset-password?token=...` directly into
`mobile/app/(public)/reset-password.tsx` via Expo Router's file-based
deep linking -- no additional linking configuration was needed.

`PASSWORD_RESET_URL_BASE` is what the backend actually embeds in the
reset email, and is fully external/configurable
(`skincare://reset-password` in dev, required to be a real `https://`
origin in production -- section 9). This is deliberate: the production
email URL is NOT hardwired to the custom scheme. The architecture
permits, but this pass does **not** implement or claim to have
configured, a future HTTPS universal/app-link page
(`https://<public-domain>/reset-password?token=...`) that would open the
installed app directly or fall back to a web reset flow. **STILL
REQUIRED FOR STORE RELEASE:** the actual apple-app-site-association /
Android App Links `assetlinks.json` configuration and a real public
domain -- neither exists yet, and this pass does not claim otherwise.

## 11. Mobile screens

**IMPLEMENTED, TESTED.**

- `mobile/app/(public)/forgot-password.tsx` -- email entry, submits to
  `POST /password/forgot`, shows one generic confirmation regardless of
  mutation outcome (no "account not found" branch exists in this
  screen's code at all -- see section 5).
- `mobile/app/(public)/reset-password.tsx` -- reads `token` from the
  route's search params (`useLocalSearchParams`, works for both the
  `skincare://` scheme and, later, an HTTPS universal link), new/confirm
  password fields, submits to `POST /password/reset`. On success:
  defensively clears any local session (`useSession().signOut()` --
  idempotent if nothing was stored), shows "Your password has been reset.
  Sign in with your new password.", and routes to sign-in. Never
  auto-authenticates.
- `mobile/app/(public)/sign-in.tsx` gained a "Forgot password?" link.

**The reset token never touches SecureStore, AsyncStorage, SQLite, the
filesystem, the persisted TanStack Query cache, or a log line** -- it
lives only in `reset-password.tsx`'s own React state for the duration of
the flow, read once from the route params and passed directly to the
`POST /password/reset` mutation.

## 12. EAS build foundation

**IMPLEMENTED (config only -- no build has been run).**
`mobile/eas.json`: `development` (developmentClient, internal
distribution), `preview` (internal distribution), `production`
(`autoIncrement`). No RevenueCat packages installed, no
`expo-dev-client` installed (this pass only wrote the profile
declaration; installing it is left to C2, per this pass's own scope).

**STILL REQUIRED BEFORE C2 STORE INTEGRATION:** `ios.bundleIdentifier`
and `android.package` are absent from `mobile/app.json` and this pass
did **not** invent them -- no canonical identifier could be verified
from repository/Expo project state. This is an irreversible decision
(App Store Connect / Google Play bind permanently to whatever identifier
is first submitted) left explicit rather than guessed.

## 13. Release URL seams

**IMPLEMENTED (config + UI seam); CONFIGURATION EXTERNAL; content NOT
authored.**

`EXPO_PUBLIC_PRIVACY_POLICY_URL`, `EXPO_PUBLIC_TERMS_URL`,
`EXPO_PUBLIC_SUPPORT_URL`, `EXPO_PUBLIC_ACCOUNT_DELETION_URL`
(`mobile/src/constants/config.ts`) -- all optional public (non-secret)
configuration. An unconfigured URL in development is simply omitted from
Settings, which now has a "Legal & support" section that renders only
the links whose URL is actually set. A configured URL must be `https://`
in a production build (enforced at startup, same pattern
`EXPO_PUBLIC_API_BASE_URL` already uses) -- **STILL REQUIRED FOR STORE
RELEASE:** none of these four URLs are actually configured yet, and no
privacy policy/terms/support/account-deletion page content was authored
by this pass (explicitly out of scope -- see Part 14 of this pass's own
brief). A future pass's production build/config validation is expected
to require all four before an actual store-release gate; this pass only
built the seam.

## 14. Support / account-deletion contract

**UNCHANGED / ADDITIVE ONLY.** The existing authenticated in-app
deletion (`DELETE /me`) is untouched. `EXPO_PUBLIC_ACCOUNT_DELETION_URL`
is an *additional* store-compliance surface (Apple/Google both expect a
web path to request deletion even from outside the app), not a
replacement. This pass creates only the mobile configuration + Settings
link -- **no web deletion page exists**, and no unauthenticated
email-only deletion endpoint was created or would ever be created: the
eventual web deletion page must authenticate/verify ownership before
acting, exactly like the existing in-app path already does.

## 15. What this pass explicitly did not touch

RevenueCat mobile SDK/paywall/purchases/`CustomerInfo`/restore purchases,
C3 history/progress/baseline/Outcome Engine, social, push notifications,
product shopping/affiliate links, catalog data acquisition, passwordless
login, social login, MFA, and no legal-policy text was invented anywhere
in this pass.
