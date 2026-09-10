"""add recommended action to ingredient interactions

Revision ID: 32231ea81bb5
Revises: 968c5a58fda6
Create Date: 2026-09-10 00:00:04.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32231ea81bb5'
down_revision: Union[str, Sequence[str], None] = '968c5a58fda6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Routine-level (cross-product) interaction evaluation (Part I, Phase
    8) must not blindly classify every recorded interaction as total
    exclusion -- `interaction_type` (INCOMPATIBLE/REDUCES_EFFICACY/
    INCREASES_IRRITATION/REQUIRES_SPACING) describes *what kind* of
    interaction it is; `recommended_action` is the new, separate,
    structured field describing what routine-level evaluation should
    actually *do* about it:

    - EXCLUDE_COMBINATION: the two ingredients must not appear in the
      same routine at all.
    - SEPARATE_DAYPART: fine together in one routine as long as one is
      AM and the other PM.
    - ALTERNATE_DAYS: fine together in one routine as long as they're
      used on different days, not the same day.
    - REDUCE_FREQUENCY: fine together, but the combined routine's
      frequency for the affected ingredient(s) must be capped.
    - ADVISORY: no schedule constraint -- surfaced to the user as
      information, never blocks or reshapes the routine.

    Defaults to ADVISORY (the least destructive interpretation) rather
    than EXCLUDE_COMBINATION, so existing seeded interactions don't
    silently become full exclusions the moment this migration runs --
    each interaction's actual recommended_action must be set
    deliberately by whoever records it, matching this pass's explicit
    instruction not to fabricate clinical rules.
    """
    op.execute("ALTER TABLE ingredient_interactions ADD COLUMN recommended_action VARCHAR(30) NOT NULL DEFAULT 'ADVISORY'")
    op.execute("""
        ALTER TABLE ingredient_interactions
            ADD CONSTRAINT ingredient_interactions_recommended_action_check
            CHECK (recommended_action IN ('EXCLUDE_COMBINATION', 'SEPARATE_DAYPART', 'ALTERNATE_DAYS', 'REDUCE_FREQUENCY', 'ADVISORY'))
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE ingredient_interactions DROP CONSTRAINT IF EXISTS ingredient_interactions_recommended_action_check")
    op.execute("ALTER TABLE ingredient_interactions DROP COLUMN IF EXISTS recommended_action")
