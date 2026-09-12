"""add publication_status to product_formulations

Revision ID: 37c88143a9ed
Revises: 1367b870bdcd
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '37c88143a9ed'
down_revision: Union[str, Sequence[str], None] = '1367b870bdcd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    First migration of the catalog-ingestion-admin pass. None of
    `product.status`, `product_formulations.is_current`, or
    `ingredient_data_status` fully describe the ingestion/review/
    publication lifecycle a controlled ingestion pipeline needs:
    `is_current` in particular has been carrying an implicit
    conflation of "verified" + "published" + "current" + "safe" +
    "complete" that this pass's own core invariant (INGESTED !=
    VERIFIED != PUBLISHED != SAFE FOR EVERY USER) requires pulling
    apart into a distinct, explicit field.

    `publication_status` is that field. Six states:

      DRAFT       -- exists, not yet reviewed/verified. The default
                     for every new row (fail-closed -- matches
                     ingredient_data_status's own NOT NULL DEFAULT
                     'UNKNOWN' precedent from migration ac641537d224:
                     nothing becomes eligible for recommendation
                     merely by being inserted).
      NEEDS_REVIEW-- ingestion flagged something uncertain; a human
                     must resolve it before this can proceed.
      VERIFIED    -- structural/completeness checks passed and a human
                     (or the ingestion pipeline's own deterministic
                     checks) has signed off, but publication (the
                     atomic move into the tables ProductMatchingService
                     actually queries) has not happened yet.
      PUBLISHED   -- trusted enough to be evaluated -- CatalogPublication
                     Service's own explicit output state. Never means
                     "safe for every user"; SafetyEngine still gates
                     every individual recommendation regardless.
      REJECTED    -- ingestion or review determined this record's
                     formulation should not be published at all.
      SUPERSEDED  -- was PUBLISHED, has since been replaced by a newer
                     formulation for the same product/market (see
                     Reformulation in CATALOG_INGESTION_ARCHITECTURE.md).
                     `is_current` also flips to false at the same time,
                     but this is the field that specifically answers
                     "why is this no longer current" -- `is_current`
                     alone can't distinguish "superseded by a newer
                     verified formulation" from, hypothetically, any
                     other reason a row might stop being current.

    A formulation may participate in ProductMatchingService's
    recommendation candidate query only when publication_status =
    'PUBLISHED' AND ingredient_data_status = 'COMPLETE' AND is_current
    = true (see migration 2de8380d3618's sibling change to
    app/db/catalog_repository.py -- enforced in application code, not
    a CHECK constraint, since it spans multiple columns each with
    their own independent lifecycle and existing tests already assert
    specific ingredient_data_status combinations independently of
    publication_status).

    NOT NULL DEFAULT 'DRAFT' -- every existing and future formulation
    starts DRAFT (fail-closed) and must be explicitly promoted to
    PUBLISHED by CatalogPublicationService, exactly the same posture
    ingredient_data_status already established for completeness.
    """
    op.execute("""
        ALTER TABLE product_formulations
            ADD COLUMN publication_status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
    """)
    op.execute("""
        ALTER TABLE product_formulations
            ADD CONSTRAINT product_formulations_publication_status_check
            CHECK (publication_status IN (
                'DRAFT', 'NEEDS_REVIEW', 'VERIFIED', 'PUBLISHED', 'REJECTED', 'SUPERSEDED'
            ))
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE product_formulations DROP CONSTRAINT IF EXISTS product_formulations_publication_status_check")
    op.execute("ALTER TABLE product_formulations DROP COLUMN IF EXISTS publication_status")
