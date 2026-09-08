"""create consent_events table

Revision ID: 59ebd09d437d
Revises: f6862f2cbc66
Create Date: 2026-09-08 01:22:31.692787

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '59ebd09d437d'
down_revision: Union[str, Sequence[str], None] = 'f6862f2cbc66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE consent_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            consent_type VARCHAR(64) NOT NULL,
            policy_version VARCHAR(32) NOT NULL,
            purpose TEXT NOT NULL,
            jurisdiction VARCHAR(8),
            granted_at TIMESTAMPTZ NOT NULL,
            withdrawn_at TIMESTAMPTZ,
            app_version VARCHAR(32),
            platform VARCHAR(32),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_consent_events_user_type ON consent_events(user_id, consent_type)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE consent_events")
