"""add home_region and cell_id to users

Revision ID: 219c52642ed4
Revises: 9db3e5856a79
Create Date: 2026-09-10 00:00:08.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '219c52642ed4'
down_revision: Union[str, Sequence[str], None] = '9db3e5856a79'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Minimal cell-readiness metadata (Part VII of this pass -- deliberately
    *not* multi-region infrastructure): every user gets a home_region
    and cell_id, both nullable so app.domain.user_placement_service
    .UserPlacementService can assign the deployment's configured launch
    defaults (settings.launch_home_region/launch_cell_id -- never
    hardcoded here) rather than this migration baking in a value that
    would need a data migration to change later. analysis_requests
    already carries its own home_region/cell_id columns (migration
    b034483cb876) -- AnalysisSubmissionService snapshots the user's
    placement onto each request at submission time, so a later change
    to a user's placement never rewrites history for requests already
    submitted under the old one.

    No RLS needed here: the users table itself has none (see
    bd3e8b8e56bf), and these two columns carry no more sensitivity than
    the rest of that row.
    """
    op.execute("ALTER TABLE users ADD COLUMN home_region VARCHAR(50)")
    op.execute("ALTER TABLE users ADD COLUMN cell_id VARCHAR(50)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS cell_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS home_region")
