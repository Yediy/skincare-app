"""backfill user ingredient constraints from legacy arrays

Revision ID: 152a82ec2a29
Revises: 32231ea81bb5
Create Date: 2026-09-10 00:00:05.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '152a82ec2a29'
down_revision: Union[str, Sequence[str], None] = '32231ea81bb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app/db/catalog_repository.py's normalize_name() exactly
# (lowercase, trim, collapse internal whitespace to one space) -- the
# backfill's resolution must agree with what a live resolve_ingredient()
# call would produce for the same raw text, or a backfilled row could
# claim RESOLVED/UNRESOLVED differently than the application would.
_NORMALIZE_SQL = "lower(trim(regexp_replace({col}, '\\s+', ' ', 'g')))"


def upgrade() -> None:
    """Upgrade schema.

    Per this pass's explicit instruction: do not simply destroy
    existing user_profiles.allergies/avoid_ingredients data. Existing
    entries are backfilled into user_ingredient_constraints, resolved
    deterministically via the same exact-match rule
    catalog_repository.resolve_ingredient() uses (canonical name first,
    then alias) -- an entry that resolves cleanly becomes RESOLVED with
    a real ingredient_id; anything else becomes UNRESOLVED (never
    guessed), which Part I Phase 4's SafetyEngine gate then correctly
    treats as insufficient data for specific-product recommendation
    rather than silently ignoring.

    ON CONFLICT DO NOTHING: idempotent/safe to re-run, and yields to
    any row a concurrent live write (app/db/profile_repository.py's
    dual-write, already deployed by the time this runs in a real
    rolling deploy) may have already inserted for the same
    (user_id, constraint_type, normalized_raw_text).
    """
    normalized_raw = _NORMALIZE_SQL.format(col="a.raw_text")

    for column, constraint_type in (("allergies", "ALLERGY"), ("avoid_ingredients", "AVOID")):
        op.execute(f"""
            INSERT INTO user_ingredient_constraints
                (user_id, constraint_type, raw_text, normalized_raw_text, ingredient_id, resolution_status)
            SELECT
                up.user_id,
                '{constraint_type}',
                a.raw_text,
                {normalized_raw},
                COALESCE(i.id, alias_i.id) AS ingredient_id,
                CASE WHEN COALESCE(i.id, alias_i.id) IS NOT NULL THEN 'RESOLVED' ELSE 'UNRESOLVED' END
            FROM user_profiles up
            CROSS JOIN LATERAL unnest(up.{column}) AS a(raw_text)
            LEFT JOIN ingredients i ON i.normalized_name = {normalized_raw}
            LEFT JOIN ingredient_aliases al ON al.normalized_alias = {normalized_raw}
            LEFT JOIN ingredients alias_i ON alias_i.id = al.ingredient_id
            WHERE trim(a.raw_text) <> ''
            ON CONFLICT (user_id, constraint_type, normalized_raw_text) DO NOTHING
        """)


def downgrade() -> None:
    """Downgrade schema.

    Deliberately a no-op: this migration only backfills derived data
    from user_profiles (still the untouched source of truth for the
    legacy arrays) into a separate table. Deleting the backfilled rows
    on downgrade would also discard any real constraint resolved by
    live traffic (app/db/profile_repository.py's dual-write) since this
    migration ran -- which the legacy arrays alone can't reconstruct
    (resolution_status/ingredient_id aren't stored there). The
    downgrade of migration 0f5178eef3fe (which drops the table
    entirely) is what actually removes this data if the table itself
    is rolled back.
    """
    pass
