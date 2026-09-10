"""create analysis usage reservation ledger

Revision ID: ee276e90a60f
Revises: 16b82dde6e7d
Create Date: 2026-09-09 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee276e90a60f'
down_revision: Union[str, Sequence[str], None] = '16b82dde6e7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Quota-reservation ledger (Phase 7/9 of the product/usage foundation
    pass), independent of any billing provider -- see
    app/domain/entitlement.py. One row per reservation attempt,
    request_id UNIQUE so the same logical request can never reserve
    (or consume) quota twice, matching the same idempotency philosophy
    already established by the jobs table (migration 2e77bc462867):
    request_id maps to exactly one row forever, including after a
    RELEASE -- a genuinely new attempt requires a new request_id, a
    deliberate scope decision, not an oversight.

    This table IS user-owned data (unlike the catalog tables above, or
    `jobs`), so it gets the same RLS treatment as user_profiles/
    consent_events (migration 7b38b717546e): user_id = current_setting
    ('app.current_user_id'), SET LOCAL-scoped as the first statement of
    every transaction that touches it. skincare_app gets SELECT/INSERT/
    UPDATE (never DELETE -- this ledger is an append/update audit trail,
    not something rows are removed from).

    The atomicity that actually caps concurrent reservations at the
    configured allowance lives in app/db/usage_repository.py
    (a pg_advisory_xact_lock keyed by user_id+period_key serializes
    concurrent reservation attempts for the same user+period before
    either the idempotency check or the count-against-allowance check
    runs), not in this schema alone -- the UNIQUE(request_id)
    constraint here is necessary for idempotency but not, by itself,
    sufficient for the quota-overrun guarantee.
    """
    op.execute("""
        CREATE TABLE analysis_usage (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            request_id VARCHAR(255) NOT NULL,
            period_key VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'RESERVED',
            reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            consumed_at TIMESTAMPTZ,
            released_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT analysis_usage_request_id_unique UNIQUE (request_id),
            CONSTRAINT analysis_usage_status_check CHECK (status IN ('RESERVED', 'CONSUMED', 'RELEASED'))
        )
    """)
    op.execute("CREATE INDEX idx_analysis_usage_user_period ON analysis_usage(user_id, period_key, status)")

    op.execute("GRANT SELECT, INSERT, UPDATE ON analysis_usage TO skincare_app")

    op.execute("ALTER TABLE analysis_usage ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY analysis_usage_isolation ON analysis_usage
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS analysis_usage_isolation ON analysis_usage")
    op.execute("ALTER TABLE analysis_usage DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON analysis_usage FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS analysis_usage")
