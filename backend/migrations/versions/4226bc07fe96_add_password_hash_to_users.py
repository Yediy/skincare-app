"""add password_hash to users

Revision ID: 4226bc07fe96
Revises: bd3e8b8e56bf
Create Date: 2026-09-06 18:21:44.850887

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4226bc07fe96'
down_revision: Union[str, Sequence[str], None] = 'bd3e8b8e56bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE users DROP COLUMN password_hash")
