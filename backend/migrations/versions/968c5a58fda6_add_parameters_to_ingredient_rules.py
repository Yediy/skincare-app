"""add parameters to ingredient rules

Revision ID: 968c5a58fda6
Revises: 0f5178eef3fe
Create Date: 2026-09-10 00:00:03.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '968c5a58fda6'
down_revision: Union[str, Sequence[str], None] = '0f5178eef3fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Fixes a real semantic bug this pass's own review caught: a rule
    saying "maximum 2 uses per week" is not the same fact as "the user
    exceeded 2 uses per week" -- app/domain/safety_engine.py previously
    appended MAX_FREQUENCY_EXCEEDED merely because a MAX_FREQUENCY-type
    rule existed on an ingredient in the formulation, regardless of
    whether any actual proposed-routine frequency was ever compared
    against it.

    `parameters JSONB` carries the rule's actual structured threshold,
    e.g. {"maximum_weekly_frequency": 2} for MAX_FREQUENCY,
    {"requires_active_barrier_recovery": true} for BARRIER_RECOVERY --
    nullable/empty for rule_types that don't need a numeric parameter
    (PREGNANCY, ALLERGY, etc.). Formulation-level evaluation now
    surfaces this as a `restriction` (a cap that *may* apply), never as
    an already-triggered `MAX_FREQUENCY_EXCEEDED` reason code -- that
    reason code is only emitted by routine-level evaluation, once an
    actual proposed schedule is compared against the parameter.
    """
    op.execute("ALTER TABLE ingredient_rules ADD COLUMN parameters JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE ingredient_rules DROP COLUMN IF EXISTS parameters")
