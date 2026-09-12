"""CatalogIngestionService: the first two pipeline stages
(CATALOG_INGESTION_ARCHITECTURE.md) -- immutable raw import, then
normalization + identity/ingredient-resolution *checks* (read-only;
actually creating a brand/product/formulation is
CatalogPublicationService's job, not this one) that decide whether a
record is ready to publish or needs human review.

Adapter seam (Section 8): this pass implements exactly one adapter,
`import_file()`, for JSON (a top-level array of record objects) and
JSONL (one record object per line) -- the two canonical formats this
pass's brief asks for. A future CSV or provider-specific adapter would
be a new function that turns ITS shape into the same list of raw
`dict` records this one already hands to `_ingest_raw_records()`;
nothing below this seam (idempotent batch/record persistence,
normalization, validation) would need to change.

Explicitly NOT built here (Section 8): no web crawler, no arbitrary-
URL fetching. `import_file()`'s only input is bytes the caller already
has in hand (read from a controlled source file) -- acquisition is a
separate, deliberately out-of-scope concern.
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.domain.catalog_normalization import MalformedRecordError, normalize_record
from app.domain.catalog_validation import reconcile_review_state

logger = logging.getLogger(__name__)

# Real, deliberate bounds (Section 19) -- a malformed or hostile source
# file fails deliberately and immediately, before any of it reaches the
# database, rather than being accepted because it happened to parse.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB whole-file ceiling
MAX_RECORD_BYTES = 2 * 1024 * 1024  # 2MB per individual record
MAX_RECORDS_PER_BATCH = 5000


class ImportRejectedError(Exception):
    """Raised for a whole-file problem severe enough that no batch is
    ever created at all -- oversized file, unsupported format, no
    parseable records, or too many records. Distinct from a single
    record being MALFORMED (which does not abort the batch -- see
    _ingest_raw_records)."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class ImportRecordOutcome:
    import_record_id: Optional[UUID]
    external_record_id: str
    status: str  # NORMALIZED | MALFORMED (import) or VALIDATED | NEEDS_REVIEW | REJECTED (validate)
    is_new: bool = True
    validation_errors: List[Any] = field(default_factory=list)
    review_reason_codes: List[str] = field(default_factory=list)


@dataclass
class ImportOutcome:
    batch_id: UUID
    batch_is_new: bool
    records_total: int
    records: List[ImportRecordOutcome]
    dry_run: bool = False


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_raw_records(file_bytes: bytes, file_format: str) -> List[Dict[str, Any]]:
    """Splits a whole file into raw record dicts. Malformed *overall*
    JSON (an unparseable file, or a JSON document that isn't a list of
    objects) is a whole-file failure (ImportRejectedError) -- there is
    nothing to salvage record-by-record. A single malformed JSONL
    *line* is different: it becomes exactly one record dict carrying a
    `_malformed_line` marker (never raising here), so the rest of the
    file still imports -- surfaced as that one record's own MALFORMED
    status by _ingest_raw_records(), never silently dropped."""
    if file_format == "json":
        try:
            parsed = json.loads(file_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ImportRejectedError(f"file is not valid JSON: {e}", code="MALFORMED_FILE") from e
        if not isinstance(parsed, list):
            raise ImportRejectedError(
                "JSON import format requires a top-level array of record objects", code="MALFORMED_FILE",
            )
        return parsed
    if file_format == "jsonl":
        records: List[Dict[str, Any]] = []
        for line_number, line in enumerate(file_bytes.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, UnicodeDecodeError):
                records.append({"_malformed_line": line_number})
                continue
            records.append(obj)
        return records
    raise ImportRejectedError(f"unsupported file_format {file_format!r} -- must be 'json' or 'jsonl'", code="UNSUPPORTED_FORMAT")


class CatalogIngestionService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def import_file(
        self, *, source_id: UUID, file_bytes: bytes, file_format: str = "jsonl",
        source_reference: Optional[str] = None, dry_run: bool = False,
    ) -> ImportOutcome:
        """Stage 1-2: immutable raw import + normalization. Never
        touches identity resolution or ingredient resolution -- see
        validate_batch() for that. `dry_run=True` performs the exact
        same parsing/normalization in memory and reports what would
        happen, but writes NOTHING to the database at all (no batch,
        no import records) -- the strongest reading of "no production
        mutation" available for this command, not merely "no catalog-
        table mutation" (which would be true either way, since import
        never touches catalog tables regardless)."""
        if len(file_bytes) > MAX_FILE_BYTES:
            raise ImportRejectedError(
                f"file is {len(file_bytes)} bytes, exceeds MAX_FILE_BYTES={MAX_FILE_BYTES}",
                code="FILE_TOO_LARGE",
            )

        # Small hardening (independent review): an inactive source must
        # not go on quietly producing new imports -- deactivating a
        # source is meaningless if import_file() ignores it. Checked
        # even for dry_run, same as every other rejection above/below --
        # a preview should reflect the same real outcome, not a more
        # permissive one. This is deliberately just a status check, not
        # a source-management system: activation/deactivation itself
        # happens via `create-source`/direct SQL, not here.
        source = await repo.get_source_by_id(self._pool, source_id)
        if source is None:
            raise ImportRejectedError(f"unknown source_id {source_id}", code="SOURCE_NOT_FOUND")
        if not source["active"]:
            raise ImportRejectedError(f"source {source_id} is not active", code="SOURCE_INACTIVE")

        raw_records = _parse_raw_records(file_bytes, file_format)
        if not raw_records:
            raise ImportRejectedError("file contains no records", code="EMPTY_FILE")
        if len(raw_records) > MAX_RECORDS_PER_BATCH:
            raise ImportRejectedError(
                f"file contains {len(raw_records)} records, exceeds MAX_RECORDS_PER_BATCH={MAX_RECORDS_PER_BATCH}",
                code="TOO_MANY_RECORDS",
            )

        content_sha256 = _sha256_hex(file_bytes)

        if dry_run:
            outcomes = [self._normalize_only(r) for r in raw_records]
            return ImportOutcome(
                batch_id=None, batch_is_new=True, records_total=len(raw_records),
                records=outcomes, dry_run=True,
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                batch, batch_is_new = await repo.get_or_create_batch(
                    conn, source_id=source_id, content_sha256=content_sha256,
                    source_reference=source_reference, records_total=len(raw_records),
                )
                if not batch_is_new:
                    # Whole-file idempotency (Section 9): re-importing
                    # byte-identical content resolves to the SAME
                    # batch, no new records created at all.
                    existing_records = await repo.list_import_records_for_batch(conn, batch["id"])
                    return ImportOutcome(
                        batch_id=batch["id"], batch_is_new=False, records_total=batch["records_total"],
                        records=[
                            ImportRecordOutcome(
                                import_record_id=r["id"], external_record_id=r["external_record_id"],
                                status=r["status"], is_new=False,
                            )
                            for r in existing_records
                        ],
                    )

                outcomes = [await self._ingest_one_raw_record(conn, batch["id"], raw) for raw in raw_records]
                await self._recompute_batch_status_after_import(conn, batch["id"])
                await repo.write_audit(
                    conn, action="IMPORT", entity_type="catalog_import_batch", entity_id=batch["id"],
                    actor="catalog_admin_cli", after_metadata={"records_total": len(raw_records)},
                )
                normalized_count = sum(1 for o in outcomes if o.status == "NORMALIZED")
                malformed_count = len(outcomes) - normalized_count
                await repo.write_audit(
                    conn, action="NORMALIZE", entity_type="catalog_import_batch", entity_id=batch["id"],
                    actor="catalog_admin_cli",
                    after_metadata={"normalized": normalized_count, "malformed": malformed_count},
                )
                return ImportOutcome(
                    batch_id=batch["id"], batch_is_new=True, records_total=len(raw_records), records=outcomes,
                )

    def _normalize_only(self, raw_payload: Any) -> ImportRecordOutcome:
        if not isinstance(raw_payload, dict):
            # A syntactically valid JSON value (string/number/bool/
            # null/array) at record position -- not the malformed-shape
            # case normalize_record() itself catches (which requires a
            # dict to even attempt field-level validation), and not
            # something `.get`/normalization may ever be called on.
            return ImportRecordOutcome(
                import_record_id=None, external_record_id="?", status="MALFORMED",
                validation_errors=["NON_OBJECT_RECORD"],
            )
        external_record_id = str(raw_payload.get("external_record_id") or "?")
        if len(json.dumps(raw_payload).encode("utf-8")) > MAX_RECORD_BYTES:
            return ImportRecordOutcome(
                import_record_id=None, external_record_id=external_record_id, status="MALFORMED",
                validation_errors=["PAYLOAD_TOO_LARGE"],
            )
        try:
            normalize_record(raw_payload)
        except MalformedRecordError as e:
            return ImportRecordOutcome(
                import_record_id=None, external_record_id=external_record_id, status="MALFORMED",
                validation_errors=e.errors,
            )
        return ImportRecordOutcome(
            import_record_id=None, external_record_id=external_record_id, status="NORMALIZED",
        )

    async def _ingest_one_raw_record(
        self, conn: asyncpg.Connection, batch_id: UUID, raw_payload: Any,
    ) -> ImportRecordOutcome:
        if isinstance(raw_payload, dict) and set(raw_payload.keys()) == {"_malformed_line"}:
            external_record_id = f"_malformed_line_{raw_payload['_malformed_line']}"
            record, is_new = await repo.get_or_create_import_record(
                conn, batch_id=batch_id, external_record_id=external_record_id,
                raw_payload={"_malformed_line": raw_payload["_malformed_line"]},
                payload_sha256=_sha256_hex(str(raw_payload).encode("utf-8")),
            )
            if is_new:
                await repo.update_import_record(
                    conn, record["id"], status="MALFORMED", validation_errors=["MALFORMED_JSON_LINE"],
                )
            return ImportRecordOutcome(
                import_record_id=record["id"], external_record_id=external_record_id,
                status="MALFORMED", is_new=is_new, validation_errors=["MALFORMED_JSON_LINE"],
            )

        if not isinstance(raw_payload, dict):
            # Section 22/Blocker 1: syntactically valid JSON (a plain
            # string/number/bool/null/array at record position, from
            # either a JSON top-level array element or a JSONL line)
            # is NOT a malformed *parse* -- it parsed fine -- but it is
            # not a record, and nothing downstream (normalize_record(),
            # `.get`, membership tests meant for mappings) may ever be
            # called on it. Classified and persisted exactly like any
            # other single-record MALFORMED outcome, bounded the same
            # way (an oversized scalar/array never has its actual
            # content stored) -- never aborts the batch, never silently
            # dropped.
            return await self._ingest_non_object_record(conn, batch_id, raw_payload)

        raw_bytes = json.dumps(raw_payload, sort_keys=True).encode("utf-8")
        external_record_id = str(raw_payload.get("external_record_id") or "")
        payload_sha256 = _sha256_hex(raw_bytes)

        if not external_record_id:
            external_record_id = f"_missing_id_{payload_sha256[:16]}"

        if len(raw_bytes) > MAX_RECORD_BYTES:
            # Never persist the oversized content itself -- a small,
            # honest placeholder is the durable evidence instead (see
            # migration 2de8380d3618's docstring on why raw_payload
            # must stay immutable AND bounded).
            record, is_new = await repo.get_or_create_import_record(
                conn, batch_id=batch_id, external_record_id=external_record_id,
                raw_payload={"_rejected": "PAYLOAD_TOO_LARGE", "byte_length": len(raw_bytes)},
                payload_sha256=payload_sha256,
            )
            if is_new:
                await repo.update_import_record(
                    conn, record["id"], status="MALFORMED", validation_errors=["PAYLOAD_TOO_LARGE"],
                )
            return ImportRecordOutcome(
                import_record_id=record["id"], external_record_id=external_record_id,
                status="MALFORMED", is_new=is_new, validation_errors=["PAYLOAD_TOO_LARGE"],
            )

        record, is_new = await repo.get_or_create_import_record(
            conn, batch_id=batch_id, external_record_id=external_record_id,
            raw_payload=raw_payload, payload_sha256=payload_sha256,
        )
        if not is_new:
            return ImportRecordOutcome(
                import_record_id=record["id"], external_record_id=external_record_id,
                status=record["status"], is_new=False,
            )

        try:
            normalized = normalize_record(raw_payload)
        except MalformedRecordError as e:
            await repo.update_import_record(
                conn, record["id"], status="MALFORMED", validation_errors=e.errors,
            )
            return ImportRecordOutcome(
                import_record_id=record["id"], external_record_id=external_record_id,
                status="MALFORMED", validation_errors=e.errors,
            )

        await repo.update_import_record(
            conn, record["id"], status="NORMALIZED",
            normalized_payload=json.loads(normalized.model_dump_json()),
        )
        return ImportRecordOutcome(
            import_record_id=record["id"], external_record_id=external_record_id, status="NORMALIZED",
        )

    async def _ingest_non_object_record(
        self, conn: asyncpg.Connection, batch_id: UUID, raw_payload: Any,
    ) -> ImportRecordOutcome:
        """A record position holding a syntactically valid but
        non-object JSON value (string/number/bool/null/array). Bounded
        the same way as any other record -- an oversized value never
        has its actual content persisted, only a small placeholder
        recording that it was rejected for size -- and the value
        itself is wrapped in a small object before being stored, so
        `raw_payload` stays uniformly dict-shaped for every row in this
        table, MALFORMED or not."""
        try:
            serialized = json.dumps(raw_payload, sort_keys=True)
        except (TypeError, ValueError):
            serialized = json.dumps(str(raw_payload))
        raw_bytes = serialized.encode("utf-8")
        payload_sha256 = _sha256_hex(raw_bytes)
        external_record_id = f"_non_object_record_{payload_sha256[:16]}"

        if len(raw_bytes) > MAX_RECORD_BYTES:
            stored_payload = {
                "_rejected": "PAYLOAD_TOO_LARGE", "byte_length": len(raw_bytes),
                "json_type": type(raw_payload).__name__,
            }
            validation_errors = ["PAYLOAD_TOO_LARGE"]
        else:
            stored_payload = {"_non_object_value": raw_payload, "json_type": type(raw_payload).__name__}
            validation_errors = ["NON_OBJECT_RECORD"]

        record, is_new = await repo.get_or_create_import_record(
            conn, batch_id=batch_id, external_record_id=external_record_id,
            raw_payload=stored_payload, payload_sha256=payload_sha256,
        )
        if is_new:
            await repo.update_import_record(
                conn, record["id"], status="MALFORMED", validation_errors=validation_errors,
            )
        return ImportRecordOutcome(
            import_record_id=record["id"], external_record_id=external_record_id,
            status="MALFORMED", is_new=is_new, validation_errors=validation_errors,
        )

    async def _recompute_batch_status_after_import(self, conn: asyncpg.Connection, batch_id: UUID) -> None:
        counts = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'NORMALIZED') AS normalized,
                count(*) FILTER (WHERE status = 'MALFORMED') AS malformed,
                count(*) AS total
            FROM catalog_import_records WHERE batch_id = $1
            """,
            batch_id,
        )
        status = "VALIDATING" if counts["normalized"] > 0 else "FAILED"
        await repo.update_batch(conn, batch_id, status=status, records_rejected=counts["malformed"])

    # -----------------------------------------------------------------
    # Stage 3-6: identity-resolution checks, ingredient resolution,
    # validation, human review flagging. Read-only against production
    # catalog tables (Section 10: never silently merge/create here --
    # creation is CatalogPublicationService's job, at publish time).
    # -----------------------------------------------------------------

    async def validate_batch(self, batch_id: UUID, *, dry_run: bool = False) -> List[ImportRecordOutcome]:
        """Runs entirely inside one transaction, committed unless
        `dry_run=True` -- same rollback-based preview pattern as
        CatalogPublicationService.publish(), since this step does
        write staging state (review items, status transitions) an
        operator previewing a batch may want to see without committing
        to yet."""
        async with self._pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                outcomes: List[ImportRecordOutcome] = []
                records = await repo.list_import_records_for_batch(conn, batch_id, status="NORMALIZED")
                for record in records:
                    outcomes.append(await self._validate_one_record(conn, record))
                await self._recompute_batch_status_after_validate(conn, batch_id)
            except Exception:
                await tx.rollback()
                raise
            if dry_run:
                await tx.rollback()
            else:
                await tx.commit()
            return outcomes

    async def _validate_one_record(
        self, conn: asyncpg.Connection, record: Dict[str, Any],
    ) -> ImportRecordOutcome:
        """Delegates entirely to app/domain/catalog_validation.py's
        canonical `reconcile_review_state` -- the exact same function
        `CatalogReviewService` calls after a review resolution, so
        there is exactly one place that decides "is this record
        actually fine right now" (Blocker 3 of the independent review
        that found the original two-copies-of-validation design)."""
        result = await reconcile_review_state(conn, record)
        return ImportRecordOutcome(
            import_record_id=record["id"], external_record_id=record["external_record_id"],
            status=result["status"], review_reason_codes=result["review_reason_codes"],
        )

    async def _recompute_batch_status_after_validate(self, conn: asyncpg.Connection, batch_id: UUID) -> None:
        counts = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'VALIDATED') AS validated,
                count(*) FILTER (WHERE status = 'NEEDS_REVIEW') AS needs_review,
                count(*) FILTER (WHERE status IN ('MALFORMED', 'REJECTED')) AS rejected,
                count(*) FILTER (WHERE status = 'PUBLISHED') AS published,
                count(*) AS total
            FROM catalog_import_records WHERE batch_id = $1
            """,
            batch_id,
        )
        if counts["published"] > 0 and counts["published"] == counts["total"]:
            status = "PUBLISHED"
        elif counts["published"] > 0:
            status = "PARTIALLY_PUBLISHED"
        elif counts["needs_review"] > 0:
            status = "NEEDS_REVIEW"
        elif counts["validated"] > 0:
            status = "READY"
        else:
            status = "FAILED"
        await repo.update_batch(
            conn, batch_id, status=status, records_valid=counts["validated"],
            records_needing_review=counts["needs_review"], records_rejected=counts["rejected"],
            records_published=counts["published"],
        )
