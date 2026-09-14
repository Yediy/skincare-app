"""add skin_goals to user_profiles

Revision ID: fd8df981ea49
Revises: 154080b29153
Create Date: 2026-09-12 09:36:52.119049

Mobile V1 foundation needs a place to persist the user's self-reported
skin goals from onboarding. This is deliberately just one more plain
array column on the existing user_profiles row, same shape/placeholder
status as allergies/avoid_ingredients (see f6862f2cbc66) -- not a new
table, not a normalized goal entity. Values are constrained at the
application layer to the existing PRIORITIES vocabulary
(app/domain/priorities.py) so "goals" never invents concepts the
scoring engine doesn't already have a name for. Purely self-reported
metadata for now: nothing in PlanService/SafetyEngine reads this
column yet -- that wiring is explicitly deferred, not implied by its
existence.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd8df981ea49'
down_revision: Union[str, Sequence[str], None] = '154080b29153'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE user_profiles ADD COLUMN skin_goals TEXT[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE user_profiles DROP COLUMN skin_goals")
