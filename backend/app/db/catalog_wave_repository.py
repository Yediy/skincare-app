"""Read-only SQL for Production Catalog Wave 1 reporting
(PRODUCTION_CATALOG_WAVE_1.md). Deliberately kept separate from
`app.db.catalog_admin_repository` (the heavily-reviewed, privilege-
critical write layer -- migrations `13c1fff1867e`/`e5277cf2f0ee`) so
this pass's additions never risk that file's audited grant boundary.
Every query here is `SELECT`-only, safe over the same
`skincare_catalog_admin` pool the rest of the catalog-admin CLI already
uses (which has `SELECT` on every table these queries touch, same as
`skincare_app`).
"""
from typing import Any, Dict, List
from uuid import UUID

import asyncpg


async def list_import_records_for_source(pool: asyncpg.Pool, source_id: UUID) -> List[Dict[str, Any]]:
    """Every `catalog_import_records` row reachable from this source,
    across every batch it has ever produced -- the basis for Wave 1's
    per-source report. No `list_import_records_for_batch`-equivalent
    scoped by source exists in `catalog_admin_repository.py` (batches,
    not sources, are that module's own natural join key), so this is a
    genuinely new query, not a duplicate of an existing one."""
    rows = await pool.fetch(
        """
        SELECT r.*
        FROM catalog_import_records r
        JOIN catalog_import_batches b ON b.id = r.batch_id
        WHERE b.source_id = $1
        ORDER BY r.created_at
        """,
        source_id,
    )
    import json
    records = []
    for row in rows:
        record = dict(row)
        for field in ("raw_payload", "normalized_payload", "validation_errors", "review_reason_codes"):
            value = record.get(field)
            if isinstance(value, str):
                record[field] = json.loads(value)
        records.append(record)
    return records


async def list_batches_for_source(pool: asyncpg.Pool, source_id: UUID) -> List[Dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT * FROM catalog_import_batches WHERE source_id = $1 ORDER BY created_at", source_id,
    )
    return [dict(r) for r in rows]


async def count_open_review_items_by_reason_for_source(
    pool: asyncpg.Pool, source_id: UUID,
) -> Dict[str, int]:
    """Open review-item counts, grouped by reason_code, scoped to
    import records that trace back to this one source -- the Wave 1
    report's "unresolved ingredients"/"unresolved brands"/"unresolved
    product identities" figures. `PRODUCT_IDENTITY_AMBIGUOUS`/
    `BRAND_IDENTITY_AMBIGUOUS` are expected to always read zero today
    (see catalog_validation.py -- exact-match identity resolution
    cannot itself produce ambiguity in this architecture), reported
    honestly as a real query result, never hardcoded to zero."""
    rows = await pool.fetch(
        """
        SELECT ri.reason_code, count(*) AS n
        FROM catalog_review_items ri
        JOIN catalog_import_records r ON r.id = ri.import_record_id
        JOIN catalog_import_batches b ON b.id = r.batch_id
        WHERE b.source_id = $1 AND ri.status = 'OPEN'
        GROUP BY ri.reason_code
        """,
        source_id,
    )
    return {row["reason_code"]: row["n"] for row in rows}


async def count_supersede_events_for_source(pool: asyncpg.Pool, source_id: UUID) -> int:
    """Reformulation-detection count (this pass's own "formulation
    changes detected" report field): every `SUPERSEDE` audit-log entry
    whose triggering import record traces back to this source. Each
    genuine reformulation writes exactly one such entry
    (`CatalogPublicationService.publish()`, unmodified) -- this is a
    pure read of that existing, unmodified audit trail, never a second
    bookkeeping mechanism."""
    return await pool.fetchval(
        """
        SELECT count(*)
        FROM catalog_audit_log a
        JOIN catalog_import_records r ON r.id = a.import_record_id
        JOIN catalog_import_batches b ON b.id = r.batch_id
        WHERE b.source_id = $1 AND a.action = 'SUPERSEDE'
        """,
        source_id,
    )


async def count_published_formulations_and_provenance_for_source(
    pool: asyncpg.Pool, source_id: UUID,
) -> Dict[str, int]:
    """Provenance-completeness check (this pass's own report field):
    every formulation this source's import records ever published
    should have exactly one `catalog_formulation_provenance` row (a
    `UNIQUE (formulation_id)` constraint the existing schema already
    enforces -- see migration `2de8380d3618`). This query re-derives
    both counts independently from current DB state so a genuine
    mismatch would be visible, not merely trusted to match by
    construction."""
    published = await pool.fetchval(
        """
        SELECT count(DISTINCT r.formulation_id)
        FROM catalog_import_records r
        JOIN catalog_import_batches b ON b.id = r.batch_id
        WHERE b.source_id = $1 AND r.status = 'PUBLISHED' AND r.formulation_id IS NOT NULL
        """,
        source_id,
    )
    with_provenance = await pool.fetchval(
        """
        SELECT count(DISTINCT p.formulation_id)
        FROM catalog_formulation_provenance p
        JOIN catalog_import_records r ON r.id = p.import_record_id
        JOIN catalog_import_batches b ON b.id = r.batch_id
        WHERE b.source_id = $1
        """,
        source_id,
    )
    return {"published_formulations": published or 0, "formulations_with_provenance": with_provenance or 0}


async def source_record_counts(pool: asyncpg.Pool) -> List[Dict[str, Any]]:
    """One row per registered catalog_sources row, with its own total
    import-record count -- this pass's own "source counts" report
    field, computed catalog-wide (every source, not just one) for the
    no-`--source-id` form of `inspect-wave`."""
    rows = await pool.fetch(
        """
        SELECT s.id AS source_id, s.name, s.source_type, s.active, count(r.id) AS record_count
        FROM catalog_sources s
        LEFT JOIN catalog_import_batches b ON b.source_id = s.id
        LEFT JOIN catalog_import_records r ON r.batch_id = b.id
        GROUP BY s.id, s.name, s.source_type, s.active
        ORDER BY s.name
        """,
    )
    return [dict(r) for r in rows]
