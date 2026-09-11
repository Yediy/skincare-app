"""create user ingredient constraints table

Revision ID: 0f5178eef3fe
Revises: 8701e32c92f1
Create Date: 2026-09-10 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f5178eef3fe'
down_revision: Union[str, Sequence[str], None] = '8701e32c92f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Normalized replacement for user_profiles.allergies/avoid_ingredients'
    permanent free-text-array matching -- each entry is resolved (once,
    at write time) against the canonical ingredient catalog
    (app/db/catalog_repository.py's resolve_ingredient(), the same
    exact-match/alias-resolution path formulation safety evaluation
    itself uses) and the outcome is stored, not re-derived every read.

    resolution_status/ingredient_id are kept consistent by a CHECK
    constraint, not just application discipline: RESOLVED rows must
    carry a real ingredient_id, UNRESOLVED rows must not carry a
    fabricated one. An unresolved constraint is not silently dropped --
    see app/domain/safety_engine.py's INCOMPLETE_FORMULATION_DATA/
    UNRESOLVED_*_CONSTRAINT handling (Part I, Phase 4): an unresolved
    allergy/avoid entry must fail specific-product recommendation
    closed, not be treated as though it didn't exist.

    normalized_raw_text is stored (not just raw_text) so duplicate
    entries under different casing/whitespace can't accumulate --
    UNIQUE(user_id, constraint_type, normalized_raw_text).

    User-owned data: RLS, same app.current_user_id pattern as
    user_profiles/consent_events/analysis_usage. user_id is a direct
    column (not nested in JSON) per this pass's explicit instruction,
    for future sharding/cell-routing compatibility.
    """
    op.execute("""
        CREATE TABLE user_ingredient_constraints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            constraint_type VARCHAR(20) NOT NULL,
            raw_text VARCHAR(255) NOT NULL,
            normalized_raw_text VARCHAR(255) NOT NULL,
            ingredient_id UUID REFERENCES ingredients(id),
            resolution_status VARCHAR(20) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT user_ingredient_constraints_type_check
                CHECK (constraint_type IN ('ALLERGY', 'AVOID')),
            CONSTRAINT user_ingredient_constraints_resolution_check
                CHECK (resolution_status IN ('RESOLVED', 'UNRESOLVED')),
            CONSTRAINT user_ingredient_constraints_resolution_consistency CHECK (
                (resolution_status = 'RESOLVED' AND ingredient_id IS NOT NULL) OR
                (resolution_status = 'UNRESOLVED' AND ingredient_id IS NULL)
            ),
            CONSTRAINT user_ingredient_constraints_unique_per_user
                UNIQUE (user_id, constraint_type, normalized_raw_text)
        )
    """)
    op.execute("CREATE INDEX idx_user_ingredient_constraints_user_id ON user_ingredient_constraints(user_id)")
    op.execute(
        "CREATE INDEX idx_user_ingredient_constraints_unresolved "
        "ON user_ingredient_constraints(user_id) WHERE resolution_status = 'UNRESOLVED'"
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_ingredient_constraints TO skincare_app")

    op.execute("ALTER TABLE user_ingredient_constraints ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY user_ingredient_constraints_isolation ON user_ingredient_constraints
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS user_ingredient_constraints_isolation ON user_ingredient_constraints")
    op.execute("ALTER TABLE user_ingredient_constraints DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON user_ingredient_constraints FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS user_ingredient_constraints")
