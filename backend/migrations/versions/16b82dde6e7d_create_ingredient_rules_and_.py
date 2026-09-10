"""create ingredient rules and interactions tables

Revision ID: 16b82dde6e7d
Revises: d70e5fc90775
Create Date: 2026-09-09 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16b82dde6e7d'
down_revision: Union[str, Sequence[str], None] = 'd70e5fc90775'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Machine-readable, per-ingredient safety rules and pairwise
    ingredient interactions -- what app/domain/safety_engine.py's new
    formulation-level evaluation path actually reads. Same posture as
    the previous migration: global reference data, SELECT-only grant
    to skincare_app, no RLS (nothing here is scoped to a user_id).

    ingredient_interactions canonicalizes pair ordering at the
    database level (ingredient_a_id < ingredient_b_id, enforced by a
    CHECK constraint) so that (A, B) and (B, A) can never both exist
    as separate, potentially-contradictory rows -- every caller must
    canonicalize the pair before querying/inserting (see
    app/db/catalog_repository.py's canonical_ingredient_pair()), and
    the CHECK constraint is what makes a violation of that a hard
    schema error rather than a silently-possible duplicate.
    """
    op.execute("""
        CREATE TABLE ingredient_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ingredient_id UUID NOT NULL REFERENCES ingredients(id),
            rule_type VARCHAR(30) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            action VARCHAR(20) NOT NULL,
            reason_code VARCHAR(50) NOT NULL,
            evidence_grade VARCHAR(20),
            source_reference TEXT,
            rules_version VARCHAR(20) NOT NULL DEFAULT '1.0',
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ingredient_rules_rule_type_check CHECK (rule_type IN (
                'ALLERGY', 'USER_AVOID', 'SENSITIVE_SKIN', 'PREGNANCY', 'NURSING',
                'PHOTOSENSITIVITY', 'IRRITATION', 'MAX_FREQUENCY', 'BARRIER_RECOVERY'
            )),
            CONSTRAINT ingredient_rules_severity_check CHECK (severity IN ('LOW', 'MODERATE', 'HIGH', 'CRITICAL')),
            CONSTRAINT ingredient_rules_action_check CHECK (action IN ('EXCLUDE', 'RESTRICT', 'WARN'))
        )
    """)
    op.execute("CREATE INDEX idx_ingredient_rules_ingredient_id ON ingredient_rules(ingredient_id)")
    op.execute("CREATE INDEX idx_ingredient_rules_active_type ON ingredient_rules(ingredient_id, rule_type) WHERE active = true")

    op.execute("""
        CREATE TABLE ingredient_interactions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ingredient_a_id UUID NOT NULL REFERENCES ingredients(id),
            ingredient_b_id UUID NOT NULL REFERENCES ingredients(id),
            interaction_type VARCHAR(30) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            reason_code VARCHAR(50) NOT NULL,
            recommendation TEXT,
            rules_version VARCHAR(20) NOT NULL DEFAULT '1.0',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ingredient_interactions_canonical_order CHECK (ingredient_a_id < ingredient_b_id),
            CONSTRAINT ingredient_interactions_pair_type_unique UNIQUE (ingredient_a_id, ingredient_b_id, interaction_type),
            CONSTRAINT ingredient_interactions_type_check CHECK (interaction_type IN (
                'INCOMPATIBLE', 'REDUCES_EFFICACY', 'INCREASES_IRRITATION', 'REQUIRES_SPACING'
            )),
            CONSTRAINT ingredient_interactions_severity_check CHECK (severity IN ('LOW', 'MODERATE', 'HIGH', 'CRITICAL'))
        )
    """)
    op.execute("CREATE INDEX idx_ingredient_interactions_a ON ingredient_interactions(ingredient_a_id)")
    op.execute("CREATE INDEX idx_ingredient_interactions_b ON ingredient_interactions(ingredient_b_id)")

    op.execute("GRANT SELECT ON ingredient_rules TO skincare_app")
    op.execute("GRANT SELECT ON ingredient_interactions TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE ALL ON ingredient_interactions FROM skincare_app")
    op.execute("REVOKE ALL ON ingredient_rules FROM skincare_app")
    op.execute("DROP TABLE IF EXISTS ingredient_interactions")
    op.execute("DROP TABLE IF EXISTS ingredient_rules")
