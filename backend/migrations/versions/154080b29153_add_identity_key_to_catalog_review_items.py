"""add identity_key to catalog_review_items

Revision ID: 154080b29153
Revises: e5277cf2f0ee
Create Date: 2026-09-12 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '154080b29153'
down_revision: Union[str, Sequence[str], None] = 'e5277cf2f0ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Independent review's third blocker on this branch: validation
    created exactly one `UNKNOWN_INGREDIENT` review item per import
    record, regardless of how many distinct raw ingredient names
    actually failed to resolve. Resolving *one* of them then found
    zero remaining OPEN review items and incorrectly transitioned the
    whole record to VALIDATED, even though other ingredients were
    still unresolved -- the review *state machine* was wrong, even
    though `CatalogPublicationService`'s own structural backstop
    (Section 16) still correctly refused to publish the result.

    `identity_key` (nullable) distinguishes multiple, independent
    instances of the *same* reason code on the *same* import record --
    today, only `UNKNOWN_INGREDIENT` needs this (each unresolved raw
    ingredient name gets its own row, keyed by its normalized form);
    every other reason code stays a single instance per record (NULL
    identity_key), matching their existing one-problem-per-type nature.
    `UNIQUE (import_record_id, reason_code, identity_key)` is real
    defense-in-depth against duplicate-review-item explosion if
    revalidation ever runs concurrently for the same record -- NULL
    identity_key values don't collide under this index (standard
    Postgres UNIQUE semantics), which is exactly the behavior wanted
    for the single-instance reason codes; the *primary* de-duplication
    mechanism is still `app/domain/catalog_validation.py::
    reconcile_review_state`'s own check-before-insert, not this index
    alone.

    See `app/domain/catalog_validation.py` for the canonical validation
    function this pairs with -- the single source of truth for both
    the initial `validate_batch` pass and post-review revalidation, so
    an import record can only become `VALIDATED` because the CURRENT
    data genuinely passes every check right now, never merely because
    a review item's row happened to flip to `RESOLVED`.
    """
    op.execute("ALTER TABLE catalog_review_items ADD COLUMN identity_key VARCHAR(500)")
    op.execute("""
        CREATE UNIQUE INDEX idx_catalog_review_items_identity_unique
            ON catalog_review_items(import_record_id, reason_code, identity_key)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_catalog_review_items_identity_unique")
    op.execute("ALTER TABLE catalog_review_items DROP COLUMN IF EXISTS identity_key")
