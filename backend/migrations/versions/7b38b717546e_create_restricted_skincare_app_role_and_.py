"""create restricted skincare_app role and enable RLS

Revision ID: 7b38b717546e
Revises: 59ebd09d437d
Create Date: 2026-09-08 02:13:46.869758

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b38b717546e'
down_revision: Union[str, Sequence[str], None] = '59ebd09d437d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Creates a restricted runtime role for the application to connect
    as, instead of the `postgres` superuser every prior migration (and
    the app itself, until now) has used. This role is explicitly
    NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOBYPASSRLS, owns no
    tables (this migration still runs as the superuser/owner), and
    only has CRUD grants on the specific application tables.

    RLS is enabled on user_profiles and consent_events -- the two
    tables whose entire access pattern is "one row (or row set) per
    authenticated user_id, looked up after authentication already
    happened". `users` and `refresh_tokens` are deliberately NOT
    included in this pass: `users` needs a pre-authentication lookup
    by email during /login (before any user_id/session context
    exists to scope RLS by), and `refresh_tokens` is looked up by an
    unguessable per-row secret (the token hash) rather than by
    session identity -- neither fits the same
    `user_id = current_setting('app.current_user_id')` isolation model
    cleanly. Extending RLS to those two is a real, tracked follow-up
    (OPEN_ENGINEERING_ITEMS.md), not something this migration silently
    skips without saying so.

    The dev password below is a local-dev-only default, same posture
    as docker-compose.yml's existing postgres/postgres -- not meant to
    be the real credential in any deployed environment.
    """
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'skincare_app') THEN
                CREATE ROLE skincare_app WITH LOGIN PASSWORD 'skincare_app_dev_only'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO skincare_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON users, refresh_tokens, user_profiles, consent_events TO skincare_app")

    op.execute("ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE consent_events ENABLE ROW LEVEL SECURITY")

    # NULLIF(..., '') matters: a custom GUC that has been touched by
    # SET LOCAL earlier in the session reverts to '' (empty string),
    # not NULL, once that transaction ends -- confirmed directly by a
    # real integration test forcing this exact case. Casting '' to
    # ::uuid raises rather than evaluating; NULLIF avoids that so an
    # unset context cleanly matches zero rows instead of raising.
    op.execute("""
        CREATE POLICY user_profiles_isolation ON user_profiles
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY consent_events_isolation ON consent_events
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS consent_events_isolation ON consent_events")
    op.execute("DROP POLICY IF EXISTS user_profiles_isolation ON user_profiles")
    op.execute("ALTER TABLE consent_events DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_profiles DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON users, refresh_tokens, user_profiles, consent_events FROM skincare_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM skincare_app")
    op.execute("DROP ROLE IF EXISTS skincare_app")
