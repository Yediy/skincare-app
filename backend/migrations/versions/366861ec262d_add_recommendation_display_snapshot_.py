"""add recommendation display snapshot columns

Revision ID: 366861ec262d
Revises: fd8df981ea49
Create Date: 2026-09-15 20:56:21.952865

Mobile V1 Phase C1 needs to render a brand/product name and a
verification-date provenance note on the analysis results screen.
`analysis_product_recommendations` (b034483cb876) already persists
`product_id`/`formulation_id` -- resolvable back to a brand/product
name/verification date through a join -- but a completed analysis is
historical evidence: `products`/`brands`/`product_formulations` rows
are mutable current-catalog state (a product can be renamed, a
formulation superseded, a brand's identity corrected), so joining
through them at read time would silently let a past analysis's
displayed product name drift out from under it after the fact,
sometimes long after the analysis it was actually attached to.

`StepProductRecommendation` (app/domain/recommendation_service.py)
already carries `brand`/`product_name`/`verification_date` at the
exact moment a recommendation is made -- these columns just give
commit_analysis_result() somewhere to persist that snapshot alongside
the ids it already writes. Nullable and additive only: existing rows
get NULL (no backfill -- see this migration's own note below), and
nothing about analysis completion or publication depends on these
columns being populated.

verification_date_snapshot is TIMESTAMPTZ, matching the existing
domain representation of verification date throughout this codebase
(ProductMatch.verification_date: Optional[datetime],
products/product_formulations' own `verified_at TIMESTAMPTZ` columns
in d70e5fc90775/2de8380d3618) -- this is a timestamp, not a date-only
value, and this migration preserves that representation rather than
narrowing it.

Backfill decision: existing rows are left NULL (option A of this
phase's own brief), not backfilled from the current catalog. A
backfill would only ever be able to write *current* catalog metadata
under a column name that claims to be a historical-at-analysis
snapshot -- indistinguishable from genuine historical provenance once
written, and for a real user-facing skin-safety product, silently
fabricating provenance is worse than a client-side "current catalog"
fallback that's honest about what it is. The API layer
(get_product_recommendations()) is responsible for that fallback via
a read-only join, precedence: snapshot value when present, else
current catalog, else null display metadata -- never invented.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '366861ec262d'
down_revision: Union[str, Sequence[str], None] = 'fd8df981ea49'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE analysis_product_recommendations ADD COLUMN brand_name_snapshot VARCHAR(255)")
    op.execute("ALTER TABLE analysis_product_recommendations ADD COLUMN product_name_snapshot VARCHAR(255)")
    op.execute("ALTER TABLE analysis_product_recommendations ADD COLUMN verification_date_snapshot TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE analysis_product_recommendations DROP COLUMN verification_date_snapshot")
    op.execute("ALTER TABLE analysis_product_recommendations DROP COLUMN product_name_snapshot")
    op.execute("ALTER TABLE analysis_product_recommendations DROP COLUMN brand_name_snapshot")
