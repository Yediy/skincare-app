"""create catalog ingestion staging, review, provenance, and audit tables

Revision ID: 2de8380d3618
Revises: 37c88143a9ed
Create Date: 2026-09-12 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2de8380d3618'
down_revision: Union[str, Sequence[str], None] = '37c88143a9ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The controlled ingestion pipeline CATALOG_INGESTION_ARCHITECTURE.md
    describes:

        External/curated source -> immutable raw import -> normalization
        -> identity resolution -> ingredient resolution -> validation
        -> human review when uncertain -> verified draft -> atomic
        publication -> existing production catalog

    Six new tables, all global/operational data (no `user_id`, no RLS
    -- same posture as the existing catalog tables from migrations
    d70e5fc90775/16b82dde6e7d, and the pre-existing `jobs` table).
    Grants for these are NOT issued here -- see migration
    13c1fff1867e, which creates the dedicated `skincare_catalog_admin`
    role and grants it (and only it) write access; `skincare_app`
    receives no grant on any table in this migration at all, since no
    HTTP route in this pass reads or writes catalog-ingestion data
    (CLI-only, per this pass's own "admin surface: CLI first" scope
    decision).

    catalog_sources -- explicitly no credentials column. Acquisition
    (how bytes get from a manufacturer/provider to this machine) is a
    separate, deliberately out-of-scope concern from ingestion (what
    happens to those bytes once handed to the importer) -- see
    CATALOG_INGESTION_ARCHITECTURE.md's "What this pass does NOT
    build" section. `source_type` reuses product_formulations' own
    five-value vocabulary verbatim, plus one ingestion-specific
    addition (`curated_dataset`) -- deliberately not a second,
    differently-worded taxonomy for the same underlying concept.

    catalog_import_batches -- one row per ingestion run of one file.
    `UNIQUE (source_id, content_sha256)` is whole-file idempotency:
    re-importing byte-identical content from the same source resolves
    to the *same* batch (see app/domain/catalog_ingestion_service.py),
    never a duplicate.

    catalog_import_records -- one row per source record within a
    batch, persisted *before* any production-table write. `raw_payload`
    is immutable after receipt (application code never issues an
    UPDATE touching it -- only `normalized_payload`/`status`/
    `validation_errors`/`review_reason_codes`/`formulation_id` change
    after insert). `UNIQUE (batch_id, external_record_id)` prevents a
    duplicate record within one batch; cross-batch idempotency (the
    same external record re-imported via a *different* file) is
    handled at publication time by the production tables' own
    pre-existing unique constraints (`brands.normalized_name`,
    `products(brand_id, normalized_name)`, `product_skus(product_id,
    sku)`) plus `payload_sha256` comparison against the currently-
    published formulation's own provenance -- see
    app/domain/catalog_publication_service.py.

    catalog_review_items -- durable human-review queue, explicit
    reason codes only (never arbitrary prose as the *only* record of
    why something needs review -- `resolution_notes` is free text but
    supplementary, not load-bearing).

    catalog_formulation_provenance -- `UNIQUE (formulation_id)`: every
    formulation this pipeline ever publishes gets exactly one
    provenance row, permanently, answering "what import record and
    verification produced this formulation" even after a later
    reformulation supersedes it (the provenance row is never deleted
    when its formulation is superseded -- only the formulation's own
    `is_current`/`publication_status` change).

    catalog_audit_log -- append-only, never updated or deleted by any
    code path in this pass. `before_metadata`/`after_metadata` hold
    small structured summaries (e.g. `{"publication_status": "DRAFT"}`
    -> `{"publication_status": "PUBLISHED"}`), never a duplicated copy
    of a giant payload the immutable `raw_payload` above already holds
    -- see that column's own docstring note and
    CATALOG_INGESTION_ARCHITECTURE.md's "Audit log" section.
    """
    op.execute("""
        CREATE TABLE catalog_sources (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL,
            source_type VARCHAR(50) NOT NULL,
            description TEXT,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_sources_normalized_name_unique UNIQUE (normalized_name),
            CONSTRAINT catalog_sources_source_type_check CHECK (source_type IN (
                'manufacturer_label', 'manufacturer_disclosure', 'regulatory_filing',
                'third_party_verified', 'user_submitted_unverified', 'curated_dataset'
            ))
        )
    """)

    op.execute("""
        CREATE TABLE catalog_import_batches (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id UUID NOT NULL REFERENCES catalog_sources(id),
            source_reference TEXT,
            content_sha256 VARCHAR(64) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'RECEIVED',
            records_total INTEGER NOT NULL DEFAULT 0,
            records_valid INTEGER NOT NULL DEFAULT 0,
            records_needing_review INTEGER NOT NULL DEFAULT 0,
            records_rejected INTEGER NOT NULL DEFAULT 0,
            records_published INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_import_batches_status_check CHECK (status IN (
                'RECEIVED', 'VALIDATING', 'READY', 'NEEDS_REVIEW',
                'PARTIALLY_PUBLISHED', 'PUBLISHED', 'FAILED'
            )),
            CONSTRAINT catalog_import_batches_source_content_unique UNIQUE (source_id, content_sha256)
        )
    """)
    op.execute("CREATE INDEX idx_catalog_import_batches_source_id ON catalog_import_batches(source_id)")

    op.execute("""
        CREATE TABLE catalog_import_records (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            batch_id UUID NOT NULL REFERENCES catalog_import_batches(id),
            external_record_id VARCHAR(255) NOT NULL,
            raw_payload JSONB NOT NULL,
            normalized_payload JSONB,
            payload_sha256 VARCHAR(64) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'RECEIVED',
            validation_errors JSONB,
            review_reason_codes JSONB,
            formulation_id UUID REFERENCES product_formulations(id),
            superseded_formulation_id UUID REFERENCES product_formulations(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_import_records_status_check CHECK (status IN (
                'RECEIVED', 'NORMALIZING', 'NORMALIZED', 'MALFORMED',
                'NEEDS_REVIEW', 'VALIDATED', 'REJECTED', 'PUBLISHED'
            )),
            CONSTRAINT catalog_import_records_batch_external_unique UNIQUE (batch_id, external_record_id)
        )
    """)
    op.execute("CREATE INDEX idx_catalog_import_records_batch_id ON catalog_import_records(batch_id)")
    op.execute("CREATE INDEX idx_catalog_import_records_status ON catalog_import_records(status)")
    op.execute("CREATE INDEX idx_catalog_import_records_formulation_id ON catalog_import_records(formulation_id)")

    op.execute("""
        CREATE TABLE catalog_review_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            import_record_id UUID NOT NULL REFERENCES catalog_import_records(id),
            reason_code VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            resolution VARCHAR(50),
            resolution_notes TEXT,
            reviewed_by VARCHAR(255),
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_review_items_reason_code_check CHECK (reason_code IN (
                'UNKNOWN_INGREDIENT', 'PRODUCT_IDENTITY_AMBIGUOUS', 'BRAND_IDENTITY_AMBIGUOUS',
                'SKU_CONFLICT', 'UPC_CONFLICT', 'FORMULATION_VERSION_CONFLICT',
                'INGREDIENT_LIST_INCOMPLETE', 'INVALID_MARKET', 'SOURCE_INSUFFICIENT',
                'DUPLICATE_ALIAS_CONFLICT'
            )),
            CONSTRAINT catalog_review_items_status_check CHECK (status IN ('OPEN', 'RESOLVED', 'DISMISSED')),
            CONSTRAINT catalog_review_items_resolution_check CHECK (resolution IS NULL OR resolution IN (
                'MAPPED_TO_EXISTING_INGREDIENT', 'CREATED_NEW_INGREDIENT', 'CREATED_NEW_ALIAS',
                'REJECTED_SOURCE_VALUE', 'IDENTITY_CONFIRMED', 'IDENTITY_REJECTED', 'MANUAL_OVERRIDE'
            ))
        )
    """)
    op.execute("CREATE INDEX idx_catalog_review_items_import_record_id ON catalog_review_items(import_record_id)")
    op.execute("CREATE INDEX idx_catalog_review_items_open ON catalog_review_items(status) WHERE status = 'OPEN'")

    op.execute("""
        CREATE TABLE catalog_formulation_provenance (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            formulation_id UUID NOT NULL REFERENCES product_formulations(id),
            import_record_id UUID NOT NULL REFERENCES catalog_import_records(id),
            source_reference TEXT,
            verified_at TIMESTAMPTZ,
            verification_actor VARCHAR(255),
            superseded_formulation_id UUID REFERENCES product_formulations(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_formulation_provenance_formulation_unique UNIQUE (formulation_id)
        )
    """)
    op.execute("CREATE INDEX idx_catalog_formulation_provenance_import_record_id ON catalog_formulation_provenance(import_record_id)")

    op.execute("""
        CREATE TABLE catalog_audit_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            action VARCHAR(30) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id UUID,
            import_record_id UUID REFERENCES catalog_import_records(id),
            actor VARCHAR(255) NOT NULL,
            before_metadata JSONB,
            after_metadata JSONB,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT catalog_audit_log_action_check CHECK (action IN (
                'IMPORT', 'NORMALIZE', 'APPROVE', 'REJECT', 'CREATE_INGREDIENT',
                'ADD_ALIAS', 'PUBLISH', 'SUPERSEDE'
            ))
        )
    """)
    op.execute("CREATE INDEX idx_catalog_audit_log_entity ON catalog_audit_log(entity_type, entity_id)")
    op.execute("CREATE INDEX idx_catalog_audit_log_import_record_id ON catalog_audit_log(import_record_id)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS catalog_audit_log")
    op.execute("DROP TABLE IF EXISTS catalog_formulation_provenance")
    op.execute("DROP TABLE IF EXISTS catalog_review_items")
    op.execute("DROP TABLE IF EXISTS catalog_import_records")
    op.execute("DROP TABLE IF EXISTS catalog_import_batches")
    op.execute("DROP TABLE IF EXISTS catalog_sources")
