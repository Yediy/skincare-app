"""scope analysis request idempotency keys per user

Revision ID: 5f2d7a9c1b34
Revises: 26164e7e1dde
Create Date: 2026-09-20 00:00:00.000000

A client-generated request_id is an idempotency key, not a globally
trusted namespace. The original analysis_usage/analysis_requests schema
made request_id globally UNIQUE. Because RLS hides another user's row,
a second user presenting the same UUID could hit the hidden global
unique constraint. In analysis_usage that could surface as a failed
fallback read of an invisible row; in analysis_requests an ON CONFLICT
could be followed by an RLS-scoped SELECT returning no row.

Scope both uniqueness contracts to (user_id, request_id), which is the
actual ownership boundary. Jobs remain unique by (job_type, request_id):
the analysis enqueue request_id is changed to a server-derived
user_id:request_id composite by the accompanying application change.
"""
from typing import Sequence, Union
from alembic import op

revision: str = "5f2d7a9c1b34"
down_revision: Union[str, Sequence[str], None] = "26164e7e1dde"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE analysis_usage DROP CONSTRAINT analysis_usage_request_id_unique")
    op.execute(
        "ALTER TABLE analysis_usage ADD CONSTRAINT analysis_usage_user_request_id_unique "
        "UNIQUE (user_id, request_id)"
    )
    op.execute("ALTER TABLE analysis_requests DROP CONSTRAINT analysis_requests_request_id_unique")
    op.execute(
        "ALTER TABLE analysis_requests ADD CONSTRAINT analysis_requests_user_request_id_unique "
        "UNIQUE (user_id, request_id)"
    )


def downgrade() -> None:
    # Downgrade is intentionally strict: if two users legitimately used
    # the same idempotency key after this migration, restoring global
    # uniqueness is impossible without discarding valid user data.
    op.execute("ALTER TABLE analysis_requests DROP CONSTRAINT analysis_requests_user_request_id_unique")
    op.execute("ALTER TABLE analysis_requests ADD CONSTRAINT analysis_requests_request_id_unique UNIQUE (request_id)")
    op.execute("ALTER TABLE analysis_usage DROP CONSTRAINT analysis_usage_user_request_id_unique")
    op.execute("ALTER TABLE analysis_usage ADD CONSTRAINT analysis_usage_request_id_unique UNIQUE (request_id)")
