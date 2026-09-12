"""transition skincare_billing from LOGIN to NOLOGIN

Revision ID: 1367b870bdcd
Revises: 4e5cda3a6bb0
Create Date: 2026-09-11 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1367b870bdcd'
down_revision: Union[str, Sequence[str], None] = '4e5cda3a6bb0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Independent review's second-pass credential-design blocker,
    finished correctly this time. The first attempt at this fix edited
    migration `9815eb266923` in place -- changing its `CREATE ROLE
    skincare_billing WITH LOGIN PASSWORD 'skincare_billing_dev_only'`
    to `WITH NOLOGIN`. That is a real bug, caught by a second
    independent review: `9815eb266923` already merged into `master`
    (PR #2) with the original `LOGIN PASSWORD` text. Alembic identifies
    a migration by its revision id and never re-runs a revision a
    database has already recorded in `alembic_version` -- so any
    database that had already applied `9815eb266923` (i.e. every real
    deployment past that merge) would simply never see the edited
    version at all, and would be stuck with the hardcoded-password
    `LOGIN` role forever. Editing an already-merged migration is a
    real footgun this codebase must not repeat -- `9815eb266923` is
    restored verbatim to its merged-master contents, and this new,
    separate, forward-only revision performs the LOGIN -> NOLOGIN
    transition instead, so every database -- whether it already
    applied `9815eb266923` months ago or is being created fresh right
    now -- passes through this same, single, explicit step and ends up
    in the same state.

    Deliberately does not (and must not) embed any login password here
    -- `skincare_billing` becomes a pure NOLOGIN privilege/group role,
    never a connectable credential again. The separately-provisioned
    runtime login (`skincare_billing_runtime` in this repository's own
    dev/CI infrastructure -- see tests/conftest.py -- or whatever a
    given deployment names its equivalent) already holds membership in
    `skincare_billing` from before this migration runs (that GRANT is,
    and always was, issued outside of Alembic entirely -- see
    BILLING_ARCHITECTURE.md and .env.example) and is completely
    unaffected by this role losing its own, separate ability to log in
    directly: membership and login capability are independent
    properties in Postgres, and revoking the latter here does not
    touch the former.

    Deliberately fails loudly (RAISE EXCEPTION), rather than silently
    creating a fresh `skincare_billing` role, if the role does not
    already exist -- that would be an impossible schema state given
    this migration's own `down_revision` chain (`9815eb266923` ->
    `4e5cda3a6bb0` -> this one), and fabricating a role to paper over
    an inconsistent migration history would hide a real problem rather
    than surface it.

    See tests/database/test_billing_role_migration_lineage.py for the
    regression guard proving (a) `9815eb266923`'s own source still
    creates a LOGIN role (i.e. was never itself edited to do this
    transition), (b) this migration's own source contains no embedded
    password, and (c) executing each migration's actual captured SQL
    in sequence against a real (rollback-guarded, never persisted)
    role transitions LOGIN -> NOLOGIN exactly as claimed.
    """
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'skincare_billing') THEN
                RAISE EXCEPTION 'skincare_billing role does not exist -- migration 9815eb266923 should already have created it. Refusing to fabricate a new one here; this database''s migration history needs investigating before retrying.';
            END IF;
        END
        $$;
    """)
    op.execute("ALTER ROLE skincare_billing NOLOGIN")


def downgrade() -> None:
    """Downgrade schema.

    Deliberately does NOT restore `skincare_billing` to a LOGIN role
    with a hardcoded password -- that would silently reintroduce the
    exact vulnerability this migration (and the credential-design half
    of the second independent review pass) exists to close. A genuine
    rollback that truly needs `skincare_billing` to accept direct
    connections again requires a deliberate, out-of-band `ALTER ROLE
    skincare_billing LOGIN PASSWORD '<a real secret from that
    environment's own secret manager>'` -- never something an
    automated migration downgrade should manufacture on its own.
    """
    pass
