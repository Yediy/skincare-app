"""add ingredient data status to product formulations

Revision ID: ac641537d224
Revises: ee276e90a60f
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ac641537d224'
down_revision: Union[str, Sequence[str], None] = 'ee276e90a60f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Closes a real gap in the formulation-safety P0: a formulation
    having one recorded ingredient was already distinguishable from
    one with zero (the existing "zero ingredients -> INSUFFICIENT_DATA"
    check in SafetyEngine.evaluate_product_formulation), but was NOT
    distinguishable from a formulation whose ingredient list is
    genuinely, deliberately complete -- a manufacturer who disclosed
    only "contains retinol" produces the exact same shape of row as a
    manufacturer who disclosed a full INCI list. That is not sufficient
    grounds for a specific-product safety recommendation.

    ingredient_data_status is NOT NULL DEFAULT 'UNKNOWN' -- every
    existing and future formulation starts UNKNOWN (fails closed) and
    must be explicitly promoted to COMPLETE by the catalog
    ingestion/admin process (which does not exist as an HTTP route in
    this pass -- see PRODUCT_CATALOG_ARCHITECTURE.md; promotion
    currently happens the same way all catalog data is populated,
    through migrations/fixtures). A formulation is never inferred
    COMPLETE merely because one or more ingredients exist.
    """
    op.execute("""
        ALTER TABLE product_formulations
            ADD COLUMN ingredient_data_status VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN'
    """)
    op.execute("""
        ALTER TABLE product_formulations
            ADD CONSTRAINT product_formulations_ingredient_data_status_check
            CHECK (ingredient_data_status IN ('COMPLETE', 'PARTIAL', 'UNKNOWN'))
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE product_formulations DROP CONSTRAINT IF EXISTS product_formulations_ingredient_data_status_check")
    op.execute("ALTER TABLE product_formulations DROP COLUMN IF EXISTS ingredient_data_status")
