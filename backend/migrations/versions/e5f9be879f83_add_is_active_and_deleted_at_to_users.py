"""add is_active and deleted_at to users

Revision ID: e5f9be879f83
Revises: f483e026482a
Create Date: 2026-09-08 00:41:47.345874

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f9be879f83'
down_revision: Union[str, Sequence[str], None] = 'f483e026482a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true")
    op.execute("ALTER TABLE users ADD COLUMN deleted_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE users DROP COLUMN deleted_at")
    op.execute("ALTER TABLE users DROP COLUMN is_active")
