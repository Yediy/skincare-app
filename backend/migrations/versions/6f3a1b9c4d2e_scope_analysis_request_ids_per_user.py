"""scope analysis idempotency keys to their owning user

Revision ID: 6f3a1b9c4d2e
Revises: 1367b870bdcd
Create Date: 2026-09-20 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "6f3a1b9c4d2e"
down_revision: Union[str, Sequence[str], None] = "1367b870bdcd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.execute("ALTER TABLE analysis_usage DROP CONSTRAINT analysis_usage_request_id_unique")
    op.execute("ALTER TABLE analysis_usage ADD CONSTRAINT analysis_usage_user_request_id_unique UNIQUE (user_id, request_id)")
    op.execute("ALTER TABLE analysis_requests DROP CONSTRAINT analysis_requests_request_id_unique")
    op.execute("ALTER TABLE analysis_requests ADD CONSTRAINT analysis_requests_user_request_id_unique UNIQUE (user_id, request_id)")
    op.execute("DROP INDEX idx_jobs_type_request_id")
    op.execute("CREATE UNIQUE INDEX idx_jobs_type_user_request_id ON jobs(job_type, (payload->>'user_id'), request_id) WHERE request_id IS NOT NULL AND job_type = 'analysis'")
    op.execute("CREATE UNIQUE INDEX idx_jobs_type_request_id_non_analysis ON jobs(job_type, request_id) WHERE request_id IS NOT NULL AND job_type <> 'analysis'")

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_jobs_type_request_id_non_analysis")
    op.execute("DROP INDEX IF EXISTS idx_jobs_type_user_request_id")
    op.execute("CREATE UNIQUE INDEX idx_jobs_type_request_id ON jobs(job_type, request_id) WHERE request_id IS NOT NULL")
    op.execute("ALTER TABLE analysis_requests DROP CONSTRAINT analysis_requests_user_request_id_unique")
    op.execute("ALTER TABLE analysis_requests ADD CONSTRAINT analysis_requests_request_id_unique UNIQUE (request_id)")
    op.execute("ALTER TABLE analysis_usage DROP CONSTRAINT analysis_usage_user_request_id_unique")
    op.execute("ALTER TABLE analysis_usage ADD CONSTRAINT analysis_usage_request_id_unique UNIQUE (request_id)")
