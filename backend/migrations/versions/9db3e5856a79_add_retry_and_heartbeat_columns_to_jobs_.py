"""add retry and heartbeat columns to jobs table

Revision ID: 9db3e5856a79
Revises: 071fab81f0ac
Create Date: 2026-09-10 00:00:08.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9db3e5856a79'
down_revision: Union[str, Sequence[str], None] = '071fab81f0ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Part VI, Phase 27: the existing pending/claimed/completed/failed
    model plus a visibility timeout (migration 2e77bc462867) is enough
    for exclusivity, but not for production retry semantics -- this
    pass's own review: a transient failure (DB hiccup, R2 timeout)
    should not immediately dead-letter a job the same way a genuinely
    invalid image should. attempt_count/max_attempts/next_attempt_at
    let PostgresJobQueue.fail() distinguish RETRYABLE (goes back to
    'pending', claimable again once next_attempt_at passes, backing off
    exponentially) from TERMINAL (goes straight to 'failed', the real
    dead-letter state -- no more claiming) without introducing a new
    status value or touching the existing CHECK constraint.
    """
    op.execute("ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")
    op.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TIMESTAMPTZ")
    # Claim candidates now also depend on next_attempt_at -- extend the
    # existing claim-candidate index to cover it, rather than relying
    # on a full scan once retryable jobs are common.
    op.execute("DROP INDEX IF EXISTS idx_jobs_claim_candidates")
    op.execute("CREATE INDEX idx_jobs_claim_candidates ON jobs(job_type, status, next_attempt_at, created_at)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_jobs_claim_candidates")
    op.execute("CREATE INDEX idx_jobs_claim_candidates ON jobs(job_type, status, created_at)")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS next_attempt_at")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS max_attempts")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS attempt_count")
