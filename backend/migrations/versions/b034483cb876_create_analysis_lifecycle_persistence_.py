"""create analysis lifecycle persistence tables

Revision ID: b034483cb876
Revises: 152a82ec2a29
Create Date: 2026-09-10 00:00:06.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b034483cb876'
down_revision: Union[str, Sequence[str], None] = '152a82ec2a29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Part III (Phases 15-18): persists the full analysis lifecycle --
    previously /analyze's output was returned to the caller and
    discarded, never stored (OPEN_ENGINEERING_ITEMS.md's long-standing
    "no measurements/analysis_results/plans persistence table" item).
    Four tables, all user-owned data with RLS (same app.current_user_id
    pattern as user_profiles/consent_events/analysis_usage/
    user_ingredient_constraints):

    analysis_requests -- one row per logical analysis attempt,
    request_id UNIQUE (same idempotency contract as analysis_usage/
    jobs), tracks RECEIVED -> QUEUED -> PROCESSING -> COMPLETED/FAILED.
    home_region/cell_id are snapshotted here (Part VII, Phase 32) so
    job/object placement stays reproducible even if a user's home
    assignment changes later. image_object_key/image_expires_at
    reference the *ephemeral* image store (Part IV) -- never the image
    bytes themselves.

    analysis_results -- one row per COMPLETED request
    (UNIQUE(analysis_request_id) -- enforces "insert if absent"
    idempotency at the schema level, not just application discipline),
    the same JSONB shape perform_analysis() already returns.

    analysis_measurements -- one row per metric per request
    (UNIQUE(analysis_request_id, metric_name)), including ABSTAINED
    measurements (value NULL is a real, meaningful, preserved outcome,
    not an omission) -- prepares the future Personal Baseline Engine
    without building it now.

    analysis_product_recommendations -- one row per routine step that
    got a specific product match (UNIQUE(analysis_request_id,
    plan_step_key)), the same provenance
    app/domain/recommendation_service.py's StepProductRecommendation
    already carries, persisted for later audit/reconstruction.

    Grants are SELECT/INSERT only (no UPDATE, no DELETE) on the three
    result-shaped tables (results/measurements/product_recommendations)
    -- they are write-once, append-only rows; analysis_requests alone
    also gets UPDATE, since its own status/timestamp columns are
    genuinely mutated across its lifecycle.
    """
    op.execute("""
        CREATE TABLE analysis_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id),
            request_id VARCHAR(255) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'RECEIVED',
            usage_reservation_id UUID REFERENCES analysis_usage(id),
            image_object_key TEXT,
            image_expires_at TIMESTAMPTZ,
            home_region VARCHAR(50),
            cell_id VARCHAR(50),
            attempt_count INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            queued_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            error_code VARCHAR(100),
            CONSTRAINT analysis_requests_request_id_unique UNIQUE (request_id),
            CONSTRAINT analysis_requests_status_check
                CHECK (status IN ('RECEIVED', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED'))
        )
    """)
    op.execute("CREATE INDEX idx_analysis_requests_user_id ON analysis_requests(user_id)")

    op.execute("""
        CREATE TABLE analysis_results (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            analysis_request_id UUID NOT NULL REFERENCES analysis_requests(id),
            user_id UUID NOT NULL REFERENCES users(id),
            capture_assessment JSONB NOT NULL,
            scores JSONB NOT NULL,
            plan JSONB NOT NULL,
            eligible_for_longitudinal_comparison BOOLEAN NOT NULL,
            pipeline_version VARCHAR(50) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT analysis_results_request_unique UNIQUE (analysis_request_id)
        )
    """)

    op.execute("""
        CREATE TABLE analysis_measurements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            analysis_request_id UUID NOT NULL REFERENCES analysis_requests(id),
            user_id UUID NOT NULL REFERENCES users(id),
            metric_name VARCHAR(100) NOT NULL,
            value NUMERIC,
            confidence NUMERIC NOT NULL,
            status VARCHAR(20) NOT NULL,
            uncertainty_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            metric_version VARCHAR(50),
            calibration_version VARCHAR(50),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT analysis_measurements_unique UNIQUE (analysis_request_id, metric_name)
        )
    """)

    op.execute("""
        CREATE TABLE analysis_product_recommendations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            analysis_request_id UUID NOT NULL REFERENCES analysis_requests(id),
            user_id UUID NOT NULL REFERENCES users(id),
            plan_step_key VARCHAR(50) NOT NULL,
            product_id UUID NOT NULL REFERENCES products(id),
            formulation_id UUID NOT NULL REFERENCES product_formulations(id),
            rank_position INTEGER NOT NULL,
            safety_status VARCHAR(20) NOT NULL,
            reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
            restrictions JSONB NOT NULL DEFAULT '{}'::jsonb,
            rules_version VARCHAR(20) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT analysis_product_recommendations_unique UNIQUE (analysis_request_id, plan_step_key)
        )
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE ON analysis_requests TO skincare_app")
    op.execute("GRANT SELECT, INSERT ON analysis_results TO skincare_app")
    op.execute("GRANT SELECT, INSERT ON analysis_measurements TO skincare_app")
    op.execute("GRANT SELECT, INSERT ON analysis_product_recommendations TO skincare_app")

    for table in ("analysis_requests", "analysis_results", "analysis_measurements", "analysis_product_recommendations"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_isolation ON {table}
                USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        """)


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("analysis_product_recommendations", "analysis_measurements", "analysis_results", "analysis_requests"):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM skincare_app")
        op.execute(f"DROP TABLE IF EXISTS {table}")
