"""row-level security isolating the billing role's job_type on the shared jobs queue

Revision ID: 4e5cda3a6bb0
Revises: 9815eb266923
Create Date: 2026-09-11 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e5cda3a6bb0'
down_revision: Union[str, Sequence[str], None] = '9815eb266923'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Independent review's second merge blocker: migration 9815eb266923
    gave `skincare_billing` unrestricted SELECT/INSERT/UPDATE on the
    entire `jobs` table so webhook ingestion and the RevenueCat worker
    could enqueue/claim/acknowledge `revenuecat_webhook` jobs. But
    `jobs` is a *shared* queue -- `analysis` jobs (app/workers/
    analysis_worker.py) live in the same table -- and a table-wide
    grant meant the billing credential could just as easily claim,
    complete, fail, or dead-letter someone's `analysis` job. That is
    not a hypothetical: nothing in the application layer stopped it,
    only convention (the billing code happens to only ever pass
    job_type='revenuecat_webhook' to PostgresJobQueue). Application-
    layer convention is not a boundary.

    This migration adds a real, database-enforced one: row-level
    security scoped by job_type, least-complex option that still
    produces genuine enforcement (jobs.py's own docstring already
    explains why a user-scoped RLS policy like user_entitlements/
    analysis_requests use doesn't fit this table -- a worker claiming
    jobs isn't scoped to one user's session context; job_type is the
    dimension that actually needs isolating here).

    `skincare_billing` may only see/write rows where job_type =
    'revenuecat_webhook'. `skincare_app` (the ordinary runtime role,
    which still owns every other job_type -- currently just `analysis`,
    but this is deliberately an exclusion, not an allowlist tied to
    today's one other job_type, so a genuinely new non-billing job_type
    added later needs no migration change to keep working through the
    ordinary role) may see/write anything EXCEPT
    job_type='revenuecat_webhook'. Neither policy grants access the
    other role's policy would deny -- there is no overlap, so no
    permissive policy accidentally restores table-wide access.

    `FOR ALL` policies apply their USING clause to SELECT/UPDATE/DELETE
    and their WITH CHECK clause to INSERT/UPDATE -- so this covers every
    operation PostgresJobQueue actually performs (enqueue/claim/
    acknowledge/fail/extend_visibility), including the `SELECT ... FOR
    UPDATE SKIP LOCKED` claim-candidate subquery, which is filtered by
    the same USING clause as a plain SELECT would be. Neither role is
    the table owner and neither has BYPASSRLS, so RLS is not silently
    skipped for either of them (verified for skincare_billing by
    tests/database/test_revenuecat_billing_privilege.py; skincare_app
    has never had BYPASSRLS -- migration 7b38b717546e).

    See tests/database/test_revenuecat_jobs_isolation.py for the
    privilege tests this is required to pass, and
    tests/queue/test_postgres_job_queue.py / tests/workers/
    test_analysis_worker.py (run unmodified against skincare_app
    afterward) for proof the ordinary analysis queue path is unaffected.
    """
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY jobs_billing_role_scoped_to_revenuecat_webhook ON jobs
            FOR ALL
            TO skincare_billing
            USING (job_type = 'revenuecat_webhook')
            WITH CHECK (job_type = 'revenuecat_webhook')
    """)
    op.execute("""
        CREATE POLICY jobs_app_role_excludes_revenuecat_webhook ON jobs
            FOR ALL
            TO skincare_app
            USING (job_type <> 'revenuecat_webhook')
            WITH CHECK (job_type <> 'revenuecat_webhook')
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS jobs_app_role_excludes_revenuecat_webhook ON jobs")
    op.execute("DROP POLICY IF EXISTS jobs_billing_role_scoped_to_revenuecat_webhook ON jobs")
    op.execute("ALTER TABLE jobs DISABLE ROW LEVEL SECURITY")
