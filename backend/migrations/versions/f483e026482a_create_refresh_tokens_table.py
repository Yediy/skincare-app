"""create refresh_tokens table

Revision ID: f483e026482a
Revises: 4226bc07fe96
Create Date: 2026-09-07 03:55:34.958098

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f483e026482a'
down_revision: Union[str, Sequence[str], None] = '4226bc07fe96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE refresh_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            family_id UUID NOT NULL,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX idx_refresh_tokens_family_id ON refresh_tokens(family_id)")
    op.execute("CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens(user_id)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE refresh_tokens")
