"""Production Catalog Wave 1 -- manifest-to-pipeline orchestration
(PRODUCTION_CATALOG_WAVE_1.md).

This module owns exactly two operations, both operator-invoked via the
catalog-admin CLI (`app.catalog_admin.cli`), and adds NO new database
table, NO new production-catalog write path, and NEVER calls
`CatalogPublicationService.publish()` itself -- publication stays a
distinct, deliberate, per-record CLI action exactly as it was before
Wave 1 (`python -m app.catalog_admin publish <import_record_id>`),
preserving the existing architecture's "INGESTED != VERIFIED !=
PUBLISHED != SAFE FOR EVERY USER" invariant unchanged.

    validate_manifest_bytes()  -- pure, no DB access at all. Schema-
                                   level validation of a Wave 1 curated
                                   manifest (`validate-source-manifest`).
    import_manifest()          -- converts schema-valid, grouped
                                   manifest records into raw import
                                   dicts (app.domain.catalog_source_
                                   adapter) and hands them to the
                                   EXISTING, unmodified
                                   CatalogIngestionService.import_file()
                                   as an in-memory JSONL byte stream
                                   (`import-source-manifest`). An
                                   operator still runs the existing
                                   `validate-batch` command afterward --
                                   Wave 1 does not duplicate that step.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.domain.catalog_ingestion_service import (
    MAX_FILE_BYTES,
    MAX_RECORDS_PER_BATCH,
    CatalogIngestionService,
    ImportOutcome,
)
from app.domain.catalog_source_adapter import (
    CuratedManifestSourceAdapter,
    ManifestRecordError,
    ManifestRecordIssue,
    group_manifest_records,
    is_reserved_test_source_name,
    manifest_record_has_sufficient_ingredient_evidence,
    manifest_record_to_raw_import_dict,
)


class WaveManifestRejectedError(Exception):
    """Mirrors `ImportRejectedError`'s own shape (a whole-manifest
    problem severe enough that nothing is imported at all) -- never a
    raw message string a caller would have to parse."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class ManifestValidationReport:
    """Output of `validate_manifest_bytes()` -- schema-level only, no
    DB access, no source_id required. Never claims anything about
    whether a record will actually import cleanly (a schema-valid
    record can still hit `SOURCE_TYPE_MISMATCH` at import time, since
    that cross-check needs the real registered source)."""

    total_records: int
    schema_valid: int
    schema_invalid: int
    sufficient_source_data: int
    insufficient_source_data: int
    category_counts: Dict[str, int] = field(default_factory=dict)
    source_name_counts: Dict[str, int] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)


def validate_manifest_bytes(file_bytes: bytes, *, file_format: str = "jsonl") -> ManifestValidationReport:
    """`validate-source-manifest` -- pure and read-only: parses and
    schema-validates every record, classifying each schema-valid one as
    sufficient/insufficient source data (`manifest_record_has_
    sufficient_ingredient_evidence`), but performs zero database
    access and writes nothing anywhere. Safe to run against a manifest
    an operator has not yet decided to import at all."""
    if len(file_bytes) > MAX_FILE_BYTES:
        raise WaveManifestRejectedError(
            f"manifest file is {len(file_bytes)} bytes, exceeds MAX_FILE_BYTES={MAX_FILE_BYTES}",
            code="FILE_TOO_LARGE",
        )

    adapter = CuratedManifestSourceAdapter(file_format=file_format)
    result = adapter.parse(file_bytes)

    if result.total_records == 0:
        raise WaveManifestRejectedError("manifest contains no records", code="EMPTY_FILE")
    if result.total_records > MAX_RECORDS_PER_BATCH:
        raise WaveManifestRejectedError(
            f"manifest contains {result.total_records} records, exceeds MAX_RECORDS_PER_BATCH={MAX_RECORDS_PER_BATCH}",
            code="TOO_MANY_RECORDS",
        )

    sufficient = 0
    insufficient = 0
    category_counts: Dict[str, int] = {}
    source_name_counts: Dict[str, int] = {}
    issues = [
        {"index": i.index, "source_evidence_id": i.source_evidence_id, "code": i.code, "detail": i.detail}
        for i in result.issues
    ]

    for record in result.valid_records:
        if manifest_record_has_sufficient_ingredient_evidence(record):
            sufficient += 1
        else:
            insufficient += 1
        category_counts[record.category] = category_counts.get(record.category, 0) + 1
        source_name_counts[record.source_name] = source_name_counts.get(record.source_name, 0) + 1

    _, group_issues = group_manifest_records(result.valid_records)
    issues.extend({
        "index": i.index, "source_evidence_id": i.source_evidence_id, "code": i.code, "detail": i.detail,
    } for i in group_issues)

    return ManifestValidationReport(
        total_records=result.total_records,
        schema_valid=len(result.valid_records),
        schema_invalid=len(result.issues),
        sufficient_source_data=sufficient,
        insufficient_source_data=insufficient,
        category_counts=category_counts,
        source_name_counts=source_name_counts,
        issues=issues,
    )


@dataclass
class WaveImportOutcome:
    source_id: UUID
    manifest_total_records: int
    manifest_issues: List[Dict[str, Any]]
    import_outcome: Optional[ImportOutcome]
    dry_run: bool = False


async def _ensure_wave_manifest_summary(
    pool: asyncpg.Pool, *, batch_id: UUID, manifest_total_records: int, manifest_issue_count: int,
) -> None:
    """Durability fix (independent review, final blocker): the Wave 1
    manifest-summary audit entry is written AFTER `CatalogIngestionService.
    import_file()`'s own transaction has already committed the batch
    and its import records -- a transient failure at THIS step (
    `repo.write_audit` propagates DB failures, same as every other call
    in this codebase) must never be allowed to permanently strand a
    batch without its summary, since `catalog_wave_report.py`'s
    `total_records_supplied` depends on it existing.

    Repairable and idempotent: called for EVERY successful non-dry-run
    import with a `batch_id`, regardless of whether the batch was
    genuinely new this call or reused (`batch_is_new`) -- a prior
    attempt may have committed the batch and then failed before ever
    reaching (or while inside) this write. Checks for an existing
    summary entry first and appends one only if genuinely missing,
    never updating or deleting anything -- audit history stays
    append-only. A plain existence check (not `INSERT ... ON CONFLICT`
    -- `catalog_audit_log` has no natural unique key for this, and none
    is added just for this) leaves a narrow theoretical race under
    truly concurrent repair attempts for the same batch; that is
    accepted deliberately (per this fix's own brief) because
    `catalog_wave_report._total_records_supplied_for_source`'s own
    `batch_id` deduplication already makes a duplicate entry harmless
    at read time -- this function's job is recoverability, not being
    the only line of defense against double-counting."""
    existing = await pool.fetch(
        "SELECT after_metadata FROM catalog_audit_log "
        "WHERE entity_id = $1 AND entity_type = 'catalog_import_batch' AND action = 'IMPORT'",
        batch_id,
    )
    for row in existing:
        meta = row["after_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if meta and meta.get("wave1_manifest"):
            return

    async with pool.acquire() as conn:
        async with conn.transaction():
            await repo.write_audit(
                conn, action="IMPORT", entity_type="catalog_import_batch", entity_id=batch_id,
                actor="catalog_admin_cli",
                after_metadata={
                    "wave1_manifest": True,
                    "wave1_manifest_total_records": manifest_total_records,
                    "wave1_manifest_issues": manifest_issue_count,
                },
            )


async def import_manifest(
    pool: asyncpg.Pool, *, source_id: UUID, file_bytes: bytes, file_format: str = "jsonl",
    dry_run: bool = False, allow_test_source: bool = False,
) -> WaveImportOutcome:
    """`import-source-manifest` -- the only DB-touching Wave 1
    operation, and even this delegates every actual mutation to the
    EXISTING, unmodified `CatalogIngestionService.import_file()`. Steps:

    1. Look up the registered source (must already exist -- `create-
       source` is unchanged and still how an operator registers one).
    2. Refuse a source whose registered name looks like a test fixture
       (`is_reserved_test_source_name`) unless `allow_test_source=True`
       -- "production data cannot masquerade as test fixture," a real
       technical guard, not just a naming convention. Automated tests
       are the only caller that ever passes `allow_test_source=True`.
    3. Schema-validate + group the manifest (same logic
       `validate_manifest_bytes` uses) and cross-check every group
       against the registered source's own `source_type`
       (`SOURCE_TYPE_MISMATCH` -- see `catalog_source_adapter`'s own
       docstring for why this is a load-bearing check, not decorative).
    4. Serialize the resulting raw import dicts to an in-memory JSONL
       byte stream and hand them to `CatalogIngestionService.
       import_file()` completely unchanged -- immutable raw-evidence
       persistence, normalization, and per-record idempotency are all
       reused verbatim, never reimplemented here.

    Manifest-level issues (schema failures, conflicting identity,
    source-type mismatches) never reach `import_file()` at all -- they
    are reported back as `manifest_issues`, distinct from whatever
    `import_outcome` reports for the records that DID reach the
    existing pipeline."""
    source = await repo.get_source_by_id(pool, source_id)
    if source is None:
        raise WaveManifestRejectedError(f"unknown source_id {source_id}", code="SOURCE_NOT_FOUND")
    if not allow_test_source and is_reserved_test_source_name(source["name"]):
        raise WaveManifestRejectedError(
            f"source {source['name']!r} looks like a test fixture (reserved name prefix) -- "
            "refusing to import real Wave 1 data into it. Test code must pass allow_test_source=True explicitly.",
            code="RESERVED_TEST_SOURCE",
        )

    if len(file_bytes) > MAX_FILE_BYTES:
        raise WaveManifestRejectedError(
            f"manifest file is {len(file_bytes)} bytes, exceeds MAX_FILE_BYTES={MAX_FILE_BYTES}",
            code="FILE_TOO_LARGE",
        )

    adapter = CuratedManifestSourceAdapter(file_format=file_format)
    parse_result = adapter.parse(file_bytes)
    if parse_result.total_records == 0:
        raise WaveManifestRejectedError("manifest contains no records", code="EMPTY_FILE")
    if parse_result.total_records > MAX_RECORDS_PER_BATCH:
        raise WaveManifestRejectedError(
            f"manifest contains {parse_result.total_records} records, exceeds MAX_RECORDS_PER_BATCH={MAX_RECORDS_PER_BATCH}",
            code="TOO_MANY_RECORDS",
        )

    manifest_issues: List[Dict[str, Any]] = [
        {"index": i.index, "source_evidence_id": i.source_evidence_id, "code": i.code, "detail": i.detail}
        for i in parse_result.issues
    ]

    groups, group_issues = group_manifest_records(parse_result.valid_records)
    manifest_issues.extend({
        "index": i.index, "source_evidence_id": i.source_evidence_id, "code": i.code, "detail": i.detail,
    } for i in group_issues)

    raw_dicts: List[Dict[str, Any]] = []
    for group in groups:
        try:
            raw_dicts.append(manifest_record_to_raw_import_dict(
                group, registered_source_type=source["source_type"], registered_source_name=source["name"],
            ))
        except ManifestRecordError as e:
            manifest_issues.append({
                "index": e.index, "source_evidence_id": e.source_evidence_id, "code": e.code, "detail": str(e),
            })

    if not raw_dicts:
        # Every record failed at the manifest layer -- nothing reaches
        # the existing pipeline at all, same "whole-file rejection"
        # posture import_file() itself uses for EMPTY_FILE.
        return WaveImportOutcome(
            source_id=source_id, manifest_total_records=parse_result.total_records,
            manifest_issues=manifest_issues, import_outcome=None, dry_run=dry_run,
        )

    file_bytes_for_pipeline = "\n".join(json.dumps(d) for d in raw_dicts).encode("utf-8")
    import_outcome = await CatalogIngestionService(pool).import_file(
        source_id=source_id, file_bytes=file_bytes_for_pipeline, file_format="jsonl", dry_run=dry_run,
    )

    if not dry_run and import_outcome.batch_id is not None:
        # Durable record of the manifest's OWN total record count
        # (including records that failed Wave 1's own schema/grouping
        # checks and never reached import_file() at all) -- the one
        # thing catalog_wave_report.py needs to reconstruct "total
        # records supplied" purely from database state later.
        #
        # Independent-review Blocker 3 (idempotency) and its own
        # follow-up durability blocker (recoverability): called
        # unconditionally -- not gated on `batch_is_new` -- because a
        # PRIOR call may have committed this exact batch via
        # `import_file()` and then failed before ever writing (or
        # while writing) this summary, permanently stranding it
        # without one if this were skipped on a reused batch.
        # `_ensure_wave_manifest_summary` itself is what makes this
        # safe to call every time: it checks for an existing summary
        # first and only ever appends, never duplicating one a prior
        # call already wrote.
        await _ensure_wave_manifest_summary(
            pool, batch_id=import_outcome.batch_id,
            manifest_total_records=parse_result.total_records,
            manifest_issue_count=len(manifest_issues),
        )

    return WaveImportOutcome(
        source_id=source_id, manifest_total_records=parse_result.total_records,
        manifest_issues=manifest_issues, import_outcome=import_outcome, dry_run=dry_run,
    )
