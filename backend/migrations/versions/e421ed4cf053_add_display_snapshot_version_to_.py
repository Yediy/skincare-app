"""add display snapshot version to analysis product recommendations

Revision ID: e421ed4cf053
Revises: 366861ec262d
Create Date: 2026-09-16 01:06:49.728522

Independent review of migration 366861ec262d found that per-field
COALESCE(snapshot, current_catalog_value) at read time cannot
distinguish two different NULL cases:

  A. a legacy row that predates snapshot support (366861ec262d),
     where falling back to the current catalog is the intended,
     honest display behavior;

  B. a post-snapshot row where the value was captured at analysis
     time and was itself genuinely NULL (StepProductRecommendation.
     verification_date is legitimately Optional -- a formulation can
     be recommended before it has ever been verified).

Per-field COALESCE conflates these: once the catalog formulation is
verified some time after a (B) analysis completes,
COALESCE(NULL, pf.verified_at) silently exposes that LATER
verification date as though it belonged to the historical analysis --
exactly the provenance drift 366861ec262d's own snapshot was meant to
prevent.

`display_snapshot_version` is an explicit marker, independent of
whether any individual snapshot field happens to be NULL:

  - IS NOT NULL  => this row went through the display-snapshot
    contract at analysis-commit time. Every snapshot column is
    authoritative for this row, INCLUDING a NULL one. No current-
    catalog fallback is applied, field by field or otherwise.

  - IS NULL      => genuinely legacy (written before this migration
    existed). Current-catalog fallback is the correct, honest
    behavior, exactly as 366861ec262d's own read path already did.

Nullable and additive only, same posture as 366861ec262d: existing
rows get NULL (they ARE the legacy case this marker exists to
identify -- not backfilled, for the same reason 366861ec262d declined
to backfill the snapshot columns themselves). app/db/analysis_
repository.py::commit_analysis_result() is updated in the same change
to always write display_snapshot_version = 1 for every recommendation
row it inserts from here on, since that function is the sole
production write path and it always captures the full display-
snapshot contract (brand/product_name/verification_date) at the
moment StepProductRecommendation is built, even when
verification_date itself is None.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e421ed4cf053'
down_revision: Union[str, Sequence[str], None] = '366861ec262d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE analysis_product_recommendations ADD COLUMN display_snapshot_version SMALLINT")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE analysis_product_recommendations DROP COLUMN display_snapshot_version")
