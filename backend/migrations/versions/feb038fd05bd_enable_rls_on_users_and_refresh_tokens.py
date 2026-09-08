"""enable RLS on users and refresh_tokens (P0-1)

Revision ID: feb038fd05bd
Revises: 7b38b717546e
Create Date: 2026-09-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'feb038fd05bd'
down_revision: Union[str, Sequence[str], None] = '7b38b717546e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Closes OPEN_ENGINEERING_ITEMS.md P0-1. The 7b38b717546e migration
    deliberately left `users` and `refresh_tokens` out of RLS because
    neither fits the plain `user_id = current_setting(...)` pattern
    used for user_profiles/consent_events -- both tables have a real
    pre-authentication lookup path where no user_id/session context
    exists yet. This migration gives each table its own genuinely
    different mechanism instead of forcing that one pattern:

    `users`:
      - A pre-auth lookup happens exactly once, during /login, keyed
        by email (a value the caller supplies, not a secret). Email
        can't be used as an RLS session-context key the way a bearer
        secret can -- the app would just be setting the GUC to
        whatever the client claims, which is no restriction at all.
        So the /login lookup is carved out into a narrow SECURITY
        DEFINER function (`login_lookup_by_email`) that returns only
        `id, password_hash` for exactly one email. SECURITY DEFINER
        functions run as their owner; this one is created by this
        migration (running as the superuser/table owner), so it
        transparently bypasses RLS the same way any table-owner query
        would, without granting skincare_app itself a bypass.
        `search_path` is pinned to prevent search-path hijacking of a
        SECURITY DEFINER function -- a well-known Postgres footgun.
      - Every other access to `users` (post-auth self-lookup in
        get_current_user, self soft-delete in DELETE /me) is scoped by
        `id = current_setting('app.current_user_id')`, same GUC as the
        other RLS-protected tables.
      - INSERT (signup) has its own permissive policy with
        `WITH CHECK (true)`: a new row's server-generated id can't be
        known in advance to match a session GUC, and there is no other
        row to leak by allowing a fresh self-registration.

    `refresh_tokens`:
      - The token-hash lookups in /refresh and /logout are genuinely
        pre-identity too: the raw refresh token possessed by the
        caller is the only credential presented, and its hash is what
        the query keys on before anyone knows *whose* token it is.
        Unlike email, a token_hash is an unguessable 48-byte-random
        bearer secret -- proof of possession of it is itself a valid
        authorization boundary, so (unlike users/email) it's safe to
        scope directly by a second session GUC,
        `app.current_token_hash`, set to the exact hash the caller
        presented immediately before the lookup.
      - Every other access (successor-token insert, family-wide
        revocation once the owning user_id is known, logout-all,
        account-deletion cleanup) is scoped by the existing
        `app.current_user_id` GUC.
      - Both policies are permissive and OR together: a row is
        visible/writable if it matches by owning user_id *or* by the
        exact token_hash currently in context, whichever the caller
        has legitimately established for that step of the flow.

    NULLIF(..., '') is used throughout for the same reason as
    7b38b717546e: an unset-this-transaction GUC reads back as '' (not
    NULL) once touched earlier in the session, and casting '' to
    ::uuid raises rather than failing closed.
    """
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY users_insert_self ON users
            FOR INSERT WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY users_identity_scope ON users
            FOR ALL USING (id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)

    op.execute("""
        CREATE POLICY refresh_tokens_by_user ON refresh_tokens
            FOR ALL USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY refresh_tokens_by_hash ON refresh_tokens
            FOR ALL USING (token_hash = NULLIF(current_setting('app.current_token_hash', true), ''))
            WITH CHECK (token_hash = NULLIF(current_setting('app.current_token_hash', true), ''))
    """)

    op.execute("""
        CREATE FUNCTION login_lookup_by_email(p_email VARCHAR(255))
        RETURNS TABLE(id UUID, password_hash VARCHAR(255))
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id, password_hash FROM users WHERE email = p_email;
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION login_lookup_by_email(VARCHAR) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION login_lookup_by_email(VARCHAR) TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE ALL ON FUNCTION login_lookup_by_email(VARCHAR) FROM skincare_app")
    op.execute("DROP FUNCTION IF EXISTS login_lookup_by_email(VARCHAR)")

    op.execute("DROP POLICY IF EXISTS refresh_tokens_by_hash ON refresh_tokens")
    op.execute("DROP POLICY IF EXISTS refresh_tokens_by_user ON refresh_tokens")
    op.execute("ALTER TABLE refresh_tokens DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS users_identity_scope ON users")
    op.execute("DROP POLICY IF EXISTS users_insert_self ON users")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
