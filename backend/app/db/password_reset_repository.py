"""Password reset token persistence (V1 account recovery pass,
migration dec963f29e8d).

Mirrors refresh_tokens' own pre-identity-vs-post-identity RLS split
(migration feb038fd05bd): issuance (POST /password/forgot) only ever
learns a real user_id after the SECURITY DEFINER eligibility lookup
below, so every write from that point on is scoped by
`app.current_user_id`, the same GUC signup/login/logout-all already
use. Consumption (POST /password/reset) is genuinely pre-identity --
the caller has proven nothing but possession of the raw token -- so
it's scoped by its own `app.current_reset_token_hash` GUC instead,
mirroring refresh_tokens' `app.current_token_hash`.

No function in this module ever accepts, logs, or returns the raw
plaintext reset token -- only its SHA-256 hex digest (produced by
app.domain.password_reset_service.hash_reset_token) ever reaches this
module or Postgres.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg


async def lookup_eligible_user_by_email(pool: asyncpg.Pool, email: str) -> Optional[dict]:
    """Pre-identity: goes through the SECURITY DEFINER
    password_reset_eligibility_lookup_by_email function, not a direct
    SELECT -- no user_id/session context exists yet for RLS to scope
    one by, and email is caller-supplied rather than a secret, same
    reasoning as /login's login_lookup_by_email.

    Returns None for a nonexistent email. Returns a row (possibly with
    is_active=False or deleted_at set) for an existing one --
    PasswordResetService, not this function, is responsible for
    treating "None" and "found but ineligible" identically in its
    outward behavior (POST /password/forgot's enumeration-safety
    invariant)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_active, deleted_at FROM password_reset_eligibility_lookup_by_email($1)",
            email,
        )
    return dict(row) if row is not None else None


async def issue_reset_token(pool: asyncpg.Pool, user_id: UUID, token_hash: str, expires_at: datetime) -> None:
    """Invalidates every other outstanding (unused) reset token for
    this user before inserting the new one, in the same transaction --
    "old reset tokens for the same user are invalidated when a new one
    is successfully issued" is one atomic operation, not two
    independent statements a crash between them could leave half-done.

    app.current_user_id is set to the target user's id before either
    statement, the same "establish identity as part of the operation"
    pattern /signup uses before its own INSERT -- this call only ever
    runs after PasswordResetService has already proven eligibility via
    lookup_eligible_user_by_email above."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE password_reset_tokens SET used_at = now() WHERE user_id = $1 AND used_at IS NULL",
                user_id,
            )
            await conn.execute(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
                user_id, token_hash, expires_at,
            )


async def consume_token_and_apply_reset(
    pool: asyncpg.Pool, token_hash: str, new_password_hash: str,
) -> Optional[UUID]:
    """The security-critical atomic core of POST /password/reset --
    same one-statement-proves-everything discipline System Integrity
    Gate V1 established for analysis_repository.mark_processing()'s
    job_id proof. ONE UPDATE proves token exists + unused + unexpired +
    the owning account is still active/not-deleted, and consumes the
    token, as a single atomically-locked conditional write -- never a
    SELECT to check followed by a separate UPDATE to consume, which
    would reopen a TOCTOU window between the two statements.

    Two concurrent callers racing the same raw token can both reach
    this UPDATE, but Postgres row-level locking on the matched row
    serializes them: whichever commits first flips used_at to
    non-NULL, so the second's WHERE clause (`used_at IS NULL`) no
    longer matches that row and it updates zero rows -- exactly one of
    two concurrent attempts on the same token can ever succeed.

    Returns the owning user_id if this call actually consumed the
    token; None if it did not (already used, unknown/malformed hash,
    expired, or the account is no longer active/not-deleted). The
    caller (PasswordResetService) must treat every None case
    identically and generically -- the same enumeration-safety
    discipline as lookup_eligible_user_by_email above.

    On success, within the SAME transaction: updates
    users.password_hash, invalidates every other outstanding reset
    token for this user, and revokes every refresh_token family for
    this user (System Integrity Gate V1's durable, Postgres-
    authoritative revocation -- see app/security/auth.py). All four
    effects commit together, or -- on any error -- none do."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_reset_token_hash', $1, true)", token_hash)
            row = await conn.fetchrow(
                """
                UPDATE password_reset_tokens prt
                SET used_at = now()
                WHERE prt.token_hash = $1
                  AND prt.used_at IS NULL
                  AND prt.expires_at > now()
                  AND password_reset_account_eligible(prt.user_id)
                RETURNING prt.user_id
                """,
                token_hash,
            )
            if row is None:
                return None

            user_id: UUID = row["user_id"]
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE users SET password_hash = $2, updated_at = now() WHERE id = $1",
                user_id, new_password_hash,
            )
            await conn.execute(
                "UPDATE password_reset_tokens SET used_at = now() WHERE user_id = $1 AND used_at IS NULL",
                user_id,
            )
            await conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL",
                user_id,
            )
            return user_id
