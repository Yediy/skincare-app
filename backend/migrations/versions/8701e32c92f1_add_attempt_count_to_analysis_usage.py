"""add attempt count to analysis usage

Revision ID: 8701e32c92f1
Revises: ac641537d224
Create Date: 2026-09-10 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8701e32c92f1'
down_revision: Union[str, Sequence[str], None] = 'ac641537d224'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Supports the corrected RELEASED-retry semantics in
    app/db/usage_repository.py (Part II of this pass): a RELEASED
    reservation may be re-reserved (RELEASED -> RESERVED, atomically,
    subject to current quota availability) rather than staying
    permanently terminal the way the previous pass's "request_id maps
    to one row forever" rule implied. attempt_count tracks how many
    times that has happened, starting at 1 for the first reservation.
    """
    op.execute("ALTER TABLE analysis_usage ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE analysis_usage DROP COLUMN IF EXISTS attempt_count")
