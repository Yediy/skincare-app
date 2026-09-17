"""create password_reset_tokens table (V1 account recovery pass)

Revision ID: dec963f29e8d
Revises: 44a74f2a79a7
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dec963f29e8d'
down_revision: Union[str, Sequence[str], None] = '44a74f2a79a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    password_reset_tokens follows the same pre-auth-vs-post-auth RLS
    split feb038fd05bd already established for refresh_tokens:

    - Issuance (POST /password/forgot) only ever learns a real user_id
      after a SECURITY DEFINER pre-identity lookup by email (see
      password_reset_eligibility_lookup_by_email below, the same
      pattern as login_lookup_by_email) -- everything from that point
      on (the INSERT here, invalidating this user's prior outstanding
      tokens) is scoped by `app.current_user_id`, same GUC signup/
      login/logout-all already use.
    - Consumption (POST /password/reset) is genuinely pre-identity: the
      caller has proven nothing but possession of the raw token. It is
      scoped by its own `app.current_reset_token_hash` GUC, mirroring
      refresh_tokens' `app.current_token_hash` -- a token_hash is an
      unguessable 48-byte-random-equivalent (32-byte urlsafe) bearer
      secret, so proof of possession of it is itself a valid
      authorization boundary the same way a refresh token's hash is.

    token_hash is VARCHAR(64), not TEXT -- a SHA-256 hex digest is
    always exactly 64 characters, and refresh_tokens.token_hash already
    established this exact convention for the same reason
    (app/security/tokens.py's hash_refresh_token). The plaintext reset
    token itself is never persisted anywhere; only this irreversible
    digest is.

    There is deliberately no separate "revoked"/"invalidated" column
    distinct from `used_at`: "an old token was invalidated because a
    newer one was issued" and "this token was consumed by a successful
    reset" are both, for this table's purposes, exactly the single
    security fact a reader of this table needs -- "is this token still
    usable" -- so both set the same column. A row's `created_at` vs.
    `used_at` ordering already lets an operator distinguish the two
    cases after the fact if ever needed for incident review.

    The partial index below (WHERE used_at IS NULL) is what both the
    "invalidate my other outstanding tokens" query (issuance) and the
    account-eligibility-joined consumption UPDATE's planner benefit
    from -- the active-token set per user is always small regardless of
    how many historical (expired/used) rows accumulate.
    """
    op.execute("""
        CREATE TABLE password_reset_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_password_reset_tokens_user_id ON password_reset_tokens(user_id)")
    op.execute(
        "CREATE INDEX idx_password_reset_tokens_user_id_active "
        "ON password_reset_tokens(user_id) WHERE used_at IS NULL"
    )

    # This codebase's skincare_app role has no default-privilege grant
    # for newly created tables (see 7b38b717546e's own comment/every
    # later table-creating migration, e.g. b034483cb876) -- an explicit
    # GRANT here is required, not optional, or every query in
    # app/db/password_reset_repository.py fails with a bare Postgres
    # permission-denied error under the app's real runtime role. No
    # DELETE: this table is append-only from the application's
    # perspective, rows are invalidated via `used_at`, never removed.
    op.execute("GRANT SELECT, INSERT, UPDATE ON password_reset_tokens TO skincare_app")

    op.execute("ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY password_reset_tokens_by_user ON password_reset_tokens
            FOR ALL USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY password_reset_tokens_by_hash ON password_reset_tokens
            FOR ALL USING (token_hash = NULLIF(current_setting('app.current_reset_token_hash', true), ''))
            WITH CHECK (token_hash = NULLIF(current_setting('app.current_reset_token_hash', true), ''))
    """)

    op.execute("""
        CREATE FUNCTION password_reset_eligibility_lookup_by_email(p_email VARCHAR(255))
        RETURNS TABLE(id UUID, is_active BOOLEAN, deleted_at TIMESTAMPTZ)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id, is_active, deleted_at FROM users WHERE email = p_email;
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION password_reset_eligibility_lookup_by_email(VARCHAR) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION password_reset_eligibility_lookup_by_email(VARCHAR) TO skincare_app")

    # Second SECURITY DEFINER function, for the OTHER pre-identity read
    # this pass needs: POST /password/reset's atomic consume-and-verify
    # UPDATE (app/db/password_reset_repository.py's
    # consume_token_and_apply_reset) must check the *account*'s
    # is_active/deleted_at state in the very same statement that
    # consumes the token by token_hash -- at that point in the
    # transaction only `app.current_reset_token_hash` has been set, not
    # `app.current_user_id` (that would be circular: the whole point of
    # the check is to learn whether this user_id may still reset at
    # all). A direct `EXISTS (SELECT 1 FROM users WHERE id = ...)`
    # inside that UPDATE's WHERE clause would be silently blocked by
    # users' own `users_identity_scope` RLS policy, making every
    # account look ineligible. This function runs as its owner (same
    # SECURITY DEFINER bypass as password_reset_eligibility_lookup_by_email
    # and login_lookup_by_email above) so the UPDATE can call it
    # directly in its WHERE clause instead.
    op.execute("""
        CREATE FUNCTION password_reset_account_eligible(p_user_id UUID)
        RETURNS BOOLEAN
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM users WHERE id = p_user_id AND is_active = true AND deleted_at IS NULL
            );
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION password_reset_account_eligible(UUID) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION password_reset_account_eligible(UUID) TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE ALL ON FUNCTION password_reset_account_eligible(UUID) FROM skincare_app")
    op.execute("DROP FUNCTION IF EXISTS password_reset_account_eligible(UUID)")

    op.execute("REVOKE ALL ON FUNCTION password_reset_eligibility_lookup_by_email(VARCHAR) FROM skincare_app")
    op.execute("DROP FUNCTION IF EXISTS password_reset_eligibility_lookup_by_email(VARCHAR)")

    op.execute("DROP POLICY IF EXISTS password_reset_tokens_by_hash ON password_reset_tokens")
    op.execute("DROP POLICY IF EXISTS password_reset_tokens_by_user ON password_reset_tokens")
    op.execute("ALTER TABLE password_reset_tokens DISABLE ROW LEVEL SECURITY")

    op.execute("REVOKE ALL ON password_reset_tokens FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
