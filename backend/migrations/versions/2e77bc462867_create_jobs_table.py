"""create jobs table for PostgresJobQueue (Phase 15)

Revision ID: 2e77bc462867
Revises: feb038fd05bd
Create Date: 2026-09-08 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2e77bc462867'
down_revision: Union[str, Sequence[str], None] = 'feb038fd05bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Backing store for app/queue/base.py's JobQueue interface
    (PostgresJobQueue implementation). A generic queue table, not an
    analysis-specific one: `job_type` + `payload` (JSONB) carry
    whatever a given job actually is, so this same table can back
    analysis jobs and any future job type without a new migration per
    job type.

    Deliberately NOT given row-level security like users/refresh_tokens/
    user_profiles/consent_events (migrations 7b38b717546e,
    feb038fd05bd): a job's "owner" (if any) is a domain-specific detail
    inside its `payload`, not a queue-level concern -- a worker process
    claiming jobs needs to see pending jobs across all users
    regardless of who created them, which is fundamentally incompatible
    with the `user_id = current_setting('app.current_user_id')` model
    those tables use. If/when a future endpoint lets a user query their
    own job's status, that authorization check belongs in the domain
    layer reading the job back (verifying payload's user_id against the
    authenticated caller), not in RLS on this table. This is a stated
    scope decision, not a silent gap.

    Idempotent enqueue (Phase 16): the partial unique index below means
    `INSERT ... ON CONFLICT (job_type, request_id) WHERE request_id IS
    NOT NULL DO NOTHING` (see PostgresJobQueue.enqueue) either creates a
    new job or safely no-ops against an existing one with the same
    (job_type, request_id) -- the same logical request submitted twice
    produces one logical job, regardless of that job's current status.

    Claim exclusivity (Phase 15's core guarantee): PostgresJobQueue.claim
    uses `SELECT ... FOR UPDATE SKIP LOCKED` inside the UPDATE's
    subquery, so two concurrent claimers against the same job_type can
    never receive the same row -- proven by a real test running two
    concurrent claims via asyncio.gather, not asserted from reading the
    SQL.
    """
    op.execute("""
        CREATE TABLE jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_type VARCHAR(100) NOT NULL,
            payload JSONB NOT NULL,
            request_id VARCHAR(255),
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            claimed_at TIMESTAMPTZ,
            claimed_until TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT jobs_status_check CHECK (status IN ('pending', 'claimed', 'completed', 'failed'))
        )
    """)
    op.execute("CREATE INDEX idx_jobs_claim_candidates ON jobs(job_type, status, created_at)")
    op.execute("""
        CREATE UNIQUE INDEX idx_jobs_type_request_id ON jobs(job_type, request_id)
            WHERE request_id IS NOT NULL
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE ALL ON jobs FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS jobs")
