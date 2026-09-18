"""Production Catalog Wave 1 -- deterministic reporting
(PRODUCTION_CATALOG_WAVE_1.md). Every number this module produces is
read fresh from durable Postgres state (never cached, never derived
from an in-memory object a previous command happened to still hold) --
running the same report twice against an unchanged database always
returns the same result, and the report is fully reconstructible after
a process restart. `app.catalog_admin.cli`'s `inspect-wave`,
`report-review-required`, and `report-verification-status` commands
are thin formatting wrappers around this module; no business logic
lives in the CLI layer, matching the existing architecture's own
"thin argparse adapter" discipline.

Wave 1 product-quality states (this pass's brief, "Wave 1 product-
quality states"): every candidate product lands in one of `VERIFIED`,
`REVIEW_REQUIRED`, `INSUFFICIENT_SOURCE_DATA`, `REJECTED`, plus two
pipeline-internal states this module also reports (`MALFORMED`,
`PUBLISHED`, `PENDING`) that are not literally new database columns --
they are a read-only classification computed from EXISTING columns
(`catalog_import_records.status`, `.normalized_payload`) the same way
every other cross-cutting read in this codebase (e.g.
`ProductMatchingService`'s own discovery query) is a view over existing
state, never a parallel bookkeeping mechanism. See
`classify_import_record_state()`'s own docstring for the exact
precedence rules.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_wave_repository as wave_repo
from app.domain.catalog_source_adapter import normalized_payload_has_sufficient_ingredient_evidence

VERIFIED = "VERIFIED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
INSUFFICIENT_SOURCE_DATA = "INSUFFICIENT_SOURCE_DATA"
REJECTED = "REJECTED"
MALFORMED = "MALFORMED"
PUBLISHED = "PUBLISHED"
PENDING = "PENDING"

_PENDING_STATUSES = frozenset({"RECEIVED", "NORMALIZING", "NORMALIZED"})


def classify_import_record_state(record: Dict[str, Any]) -> str:
    """Pure function of one `catalog_import_records` row (already
    JSON-decoded, e.g. via `catalog_wave_repository.
    list_import_records_for_source`). Precedence, most specific first:

    1. `status == 'MALFORMED'` -> `MALFORMED` (never normalized at all)
    2. `status == 'REJECTED'` -> `REJECTED` (terminal, human-decided)
    3. `status == 'PUBLISHED'` -> `PUBLISHED` -- but see
       `report_has_insufficient_publication_anomaly` below: a
       `PUBLISHED` record that fails the sufficiency predicate is
       counted here AND flagged separately as an anomaly, never
       silently reclassified as something else (a formulation that
       genuinely reached PUBLISHED is a real, if unusual, fact about
       the database -- see PRODUCTION_CATALOG_WAVE_1.md's "Publication
       safety gate" section for why this can still never reach a real
       user's recommendations regardless).
    4. `status == 'NEEDS_REVIEW'` -> `REVIEW_REQUIRED`
    5. `status == 'VALIDATED'` and NOT sufficient -> `INSUFFICIENT_SOURCE_DATA`
       (this is the gap `app.domain.catalog_validation.
       compute_validation_findings` deliberately does not flag on its
       own -- `ingredient_list_complete=False, ingredients=[]`
       normalizes and validates cleanly today; Wave 1's own, stricter
       bar classifies it here instead of silently calling it VERIFIED)
    6. `status == 'VALIDATED'` and sufficient -> `VERIFIED`
    7. `status` in (RECEIVED, NORMALIZING, NORMALIZED) -> `PENDING`
       (still mid-pipeline -- not yet a Wave 1 quality state at all)
    """
    status = record["status"]
    if status == "MALFORMED":
        return MALFORMED
    if status == "REJECTED":
        return REJECTED
    if status == "PUBLISHED":
        return PUBLISHED
    if status == "NEEDS_REVIEW":
        return REVIEW_REQUIRED
    if status == "VALIDATED":
        if normalized_payload_has_sufficient_ingredient_evidence(record.get("normalized_payload")):
            return VERIFIED
        return INSUFFICIENT_SOURCE_DATA
    if status in _PENDING_STATUSES:
        return PENDING
    return PENDING


def record_is_insufficient_publication_anomaly(record: Dict[str, Any]) -> bool:
    """True only for the narrow, unusual case of a `PUBLISHED` record
    whose own normalized payload fails Wave 1's sufficiency bar --
    possible only if an operator ran the pre-existing, unmodified
    `publish` CLI command directly against a record Wave 1's own
    tooling would have classified INSUFFICIENT_SOURCE_DATA and never
    recommended publishing (Wave 1 itself never calls `publish()`
    automatically -- see `catalog_wave_service`'s own module
    docstring). Even in this case, the resulting formulation's
    `ingredient_data_status` can never be `COMPLETE` (see
    `CatalogPublicationService._compute_ingredient_data_status`), so it
    remains structurally invisible to
    `list_current_active_formulations_by_category` -- this flag exists
    for report-time visibility/hygiene, not because the formulation
    could otherwise reach a real user."""
    return record["status"] == "PUBLISHED" and not normalized_payload_has_sufficient_ingredient_evidence(
        record.get("normalized_payload")
    )


@dataclass
class WaveReport:
    source_id: Optional[UUID]
    total_records_supplied: int
    imported: int
    malformed: int
    unresolved_brands: int
    unresolved_product_identities: int
    unresolved_ingredients: int
    review_required: int
    insufficient_source_data: int
    verified: int
    published: int
    rejected: int
    published_with_insufficient_source_data_anomaly: int
    formulation_changes_detected: int
    provenance_completeness: Dict[str, int]
    source_counts: List[Dict[str, Any]] = field(default_factory=list)


async def _total_records_supplied_for_source(pool: asyncpg.Pool, source_id: UUID, imported_count: int) -> int:
    """Reconstructed from the Wave 1 manifest-level audit entries
    `catalog_wave_service.import_manifest()` writes for every non-dry-
    run, genuinely-new-batch import (`catalog_audit_log`,
    action='IMPORT', metadata carrying `wave1_manifest_total_records`)
    -- the one place a manifest's own total record count (including
    records that failed Wave 1's own schema/grouping checks and never
    became a `catalog_import_records` row at all) is durably recorded.
    Falls back to `imported_count` (a safe lower bound) if no such
    audit entry exists for this source yet.

    Independent-review Blocker 3: a byte-identical, idempotent
    reimport of the same manifest reuses the SAME batch
    (`catalog_import_batches.content_sha256` unique constraint) but
    could previously still accumulate a second Wave 1 audit entry for
    it, double-counting this total. `import_manifest()` itself now
    only writes this audit entry when the batch is genuinely new
    (defense layer 1) -- this function additionally deduplicates by
    `batch_id` here (defense layer 2, `seen_batches`), keeping only the
    EARLIEST matching audit entry per batch, so even a duplicate entry
    left over from before that fix (or any other future source of
    duplication) can never inflate the total. Audit history itself is
    never deleted or modified -- this is a read-time dedup, not a
    mutation."""
    rows = await pool.fetch(
        """
        SELECT a.entity_id AS batch_id, a.after_metadata
        FROM catalog_audit_log a
        JOIN catalog_import_batches b ON b.id = a.entity_id
        WHERE b.source_id = $1 AND a.action = 'IMPORT' AND a.entity_type = 'catalog_import_batch'
        ORDER BY a.created_at
        """,
        source_id,
    )
    total = 0
    seen_batches = set()
    for row in rows:
        meta = row["after_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if not meta or not meta.get("wave1_manifest") or "wave1_manifest_total_records" not in meta:
            continue
        batch_id = row["batch_id"]
        if batch_id in seen_batches:
            continue
        seen_batches.add(batch_id)
        total += meta["wave1_manifest_total_records"]
    return max(total, imported_count)


async def compute_wave_report(pool: asyncpg.Pool, *, source_id: Optional[UUID] = None) -> WaveReport:
    """The one, canonical Wave 1 report -- `inspect-wave`. Scoped to a
    single source when `source_id` is given; otherwise `source_counts`
    covers every registered source catalog-wide, and every other count
    is left at its per-source-only, informational value of 0 (a
    catalog-wide roll-up of the record-level counts is deliberately not
    built this pass -- an operator inspects one source at a time for
    those; see PRODUCTION_CATALOG_WAVE_1.md)."""
    if source_id is None:
        source_counts = await wave_repo.source_record_counts(pool)
        return WaveReport(
            source_id=None, total_records_supplied=0, imported=0, malformed=0,
            unresolved_brands=0, unresolved_product_identities=0, unresolved_ingredients=0,
            review_required=0, insufficient_source_data=0, verified=0, published=0, rejected=0,
            published_with_insufficient_source_data_anomaly=0, formulation_changes_detected=0,
            provenance_completeness={"published_formulations": 0, "formulations_with_provenance": 0},
            source_counts=source_counts,
        )

    records = await wave_repo.list_import_records_for_source(pool, source_id)
    counts = {VERIFIED: 0, REVIEW_REQUIRED: 0, INSUFFICIENT_SOURCE_DATA: 0, REJECTED: 0, MALFORMED: 0, PUBLISHED: 0, PENDING: 0}
    anomalies = 0
    for record in records:
        counts[classify_import_record_state(record)] += 1
        if record_is_insufficient_publication_anomaly(record):
            anomalies += 1

    open_review = await wave_repo.count_open_review_items_by_reason_for_source(pool, source_id)
    supersede_count = await wave_repo.count_supersede_events_for_source(pool, source_id)
    provenance = await wave_repo.count_published_formulations_and_provenance_for_source(pool, source_id)
    imported_count = len(records)
    total_supplied = await _total_records_supplied_for_source(pool, source_id, imported_count)
    source_counts = await wave_repo.source_record_counts(pool)

    return WaveReport(
        source_id=source_id,
        total_records_supplied=total_supplied,
        imported=imported_count,
        malformed=counts[MALFORMED],
        unresolved_brands=open_review.get("BRAND_IDENTITY_AMBIGUOUS", 0),
        unresolved_product_identities=open_review.get("PRODUCT_IDENTITY_AMBIGUOUS", 0),
        unresolved_ingredients=open_review.get("UNKNOWN_INGREDIENT", 0),
        review_required=counts[REVIEW_REQUIRED],
        insufficient_source_data=counts[INSUFFICIENT_SOURCE_DATA],
        verified=counts[VERIFIED],
        published=counts[PUBLISHED],
        rejected=counts[REJECTED],
        published_with_insufficient_source_data_anomaly=anomalies,
        formulation_changes_detected=supersede_count,
        provenance_completeness=provenance,
        source_counts=[s for s in source_counts if s["source_id"] == source_id],
    )


@dataclass
class RecordVerificationStatus:
    import_record_id: UUID
    external_record_id: str
    state: str
    status: str


async def list_verification_status_for_source(pool: asyncpg.Pool, source_id: UUID) -> List[RecordVerificationStatus]:
    """`report-verification-status` -- the per-record complement to
    `compute_wave_report`'s aggregate counts."""
    records = await wave_repo.list_import_records_for_source(pool, source_id)
    return [
        RecordVerificationStatus(
            import_record_id=r["id"], external_record_id=r["external_record_id"],
            state=classify_import_record_state(r), status=r["status"],
        )
        for r in records
    ]


async def list_review_required_for_source(pool: asyncpg.Pool, source_id: UUID) -> List[Dict[str, Any]]:
    """`report-review-required` -- open review items for import records
    that trace back to this one source, reusing
    `catalog_wave_repository`'s own join rather than
    `CatalogReviewService.list_open()`'s catalog-wide listing (which
    has no source-scoping parameter at all -- deliberately not added
    to that existing, reviewed service this pass; the scoping happens
    here instead)."""
    records = await wave_repo.list_import_records_for_source(pool, source_id)
    needs_review_ids = {r["id"] for r in records if r["status"] == "NEEDS_REVIEW"}
    if not needs_review_ids:
        return []
    rows = await pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = ANY($1::uuid[]) AND status = 'OPEN' ORDER BY created_at",
        list(needs_review_ids),
    )
    return [dict(r) for r in rows]
