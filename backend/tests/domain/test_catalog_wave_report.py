"""app.domain.catalog_wave_report -- Production Catalog Wave 1's
deterministic reporting. Real Postgres, through catalog_admin_db_pool,
same convention as every other catalog-ingestion test.

Includes independent-review-Blocker-3 regression coverage: a
byte-identical idempotent reimport must never inflate
`total_records_supplied`; and the final durability-blocker regression
coverage: a transient failure writing the Wave 1 manifest-summary
audit entry (which happens AFTER CatalogIngestionService.import_file()'s
own transaction has already committed) must be fully recoverable by a
plain retry, never stranding a batch without its summary.
"""
import json

import asyncpg
import pytest

from app.db import catalog_admin_repository as repo
from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_publication_service import CatalogPublicationService
from app.domain.catalog_wave_report import (
    INSUFFICIENT_SOURCE_DATA,
    PUBLISHED,
    REJECTED,
    REVIEW_REQUIRED,
    VERIFIED,
    classify_import_record_state,
    compute_wave_report,
    list_verification_status_for_source,
)
from app.domain.catalog_wave_service import import_manifest

REGISTERED_SOURCE_NAME = "Wave Report Test Source"


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": REGISTERED_SOURCE_NAME,
        "source_type": "curated_dataset",
        "source_url": None,
        "retrieved_at": "2026-09-17T00:00:00Z",
        "jurisdiction": "us",
        "brand": "WaveReportTestBrand",
        "product_name": f"Wave Report Test Product {evidence_id}",
        "category": "cleanser",
        "product_url": None,
        "formulation_version_evidence": None,
        "ingredient_list_raw": ["Water", "Glycerin"],
        "ingredient_list_complete": True,
        "ingredient_source": "manufacturer_label_text",
        "verification_date": "2026-09-17",
        "upc": None, "gtin": None, "sku": None, "size_value": None, "size_unit": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def _jsonl(records):
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo
    source = await repo.create_source(catalog_admin_db_pool, name=REGISTERED_SOURCE_NAME, source_type="curated_dataset")
    return source["id"]


@pytest.fixture
async def seeded_ingredients(catalog_admin_db_pool, clean_catalog_ingestion):
    async with catalog_admin_db_pool.acquire() as conn:
        for name in ("Water", "Glycerin", "Niacinamide"):
            await conn.execute(
                "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                name, name.lower(),
            )


# ---------------------------------------------------------------------------
# classify_import_record_state -- pure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,normalized_payload,expected", [
    ("MALFORMED", None, "MALFORMED"),
    ("REJECTED", {"ingredients": [], "ingredient_list_complete": False}, REJECTED),
    ("PUBLISHED", {"ingredients": [{"raw_name": "Water"}], "ingredient_list_complete": True}, PUBLISHED),
    ("NEEDS_REVIEW", {"ingredients": [{"raw_name": "Water"}], "ingredient_list_complete": True}, REVIEW_REQUIRED),
    ("VALIDATED", {"ingredients": [], "ingredient_list_complete": False}, INSUFFICIENT_SOURCE_DATA),
    ("VALIDATED", {"ingredients": [{"raw_name": "Water"}], "ingredient_list_complete": True}, VERIFIED),
    ("VALIDATED", {"ingredients": [{"raw_name": "Water"}], "ingredient_list_complete": False}, INSUFFICIENT_SOURCE_DATA),
    ("NORMALIZED", None, "PENDING"),
])
def test_classify_import_record_state(status, normalized_payload, expected):
    record = {"status": status, "normalized_payload": normalized_payload}
    assert classify_import_record_state(record) == expected


# ---------------------------------------------------------------------------
# Report determinism / reproducibility from DB state
# ---------------------------------------------------------------------------


async def test_report_is_reproducible_across_repeated_calls(source_id, catalog_admin_db_pool, seeded_ingredients):
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id,
        file_bytes=_jsonl([_manifest_record("r1"), _manifest_record("r2", ingredient_list_raw=[], ingredient_list_complete=False)]),
        allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)

    first = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    second = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert first == second


async def test_report_counts_total_supplied_even_when_some_records_never_became_db_rows(
    source_id, catalog_admin_db_pool,
):
    good = _manifest_record("good-1")
    body = _jsonl([good]) + b"{not valid json\n"
    await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.total_records_supplied == 2
    assert report.imported == 1


async def test_report_reflects_verification_and_review_and_insufficient_counts(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    records = [
        _manifest_record("verified-1"),
        _manifest_record("review-1", ingredient_list_raw=["Totally Unresolvable Compound"]),
        _manifest_record("insufficient-1", ingredient_list_raw=[], ingredient_list_complete=False),
    ]
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl(records), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)

    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.verified == 1
    assert report.review_required == 1
    assert report.insufficient_source_data == 1
    assert report.unresolved_ingredients == 1


# ---------------------------------------------------------------------------
# Provenance completeness
# ---------------------------------------------------------------------------


async def test_provenance_completeness_matches_published_count(source_id, catalog_admin_db_pool, seeded_ingredients):
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([_manifest_record("prov-1")]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome.import_outcome.records[0].import_record_id, actor="test",
    )
    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.published == 1
    assert report.provenance_completeness["published_formulations"] == 1
    assert report.provenance_completeness["formulations_with_provenance"] == 1


# ---------------------------------------------------------------------------
# Formulation-change (reformulation) detection counting
# ---------------------------------------------------------------------------


async def test_formulation_changes_detected_counts_reformulations(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    first = _manifest_record("reform-1", product_name="Reform Report Product", sku="REFORM-REPORT-SKU")
    outcome_1 = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([first]), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_1.import_outcome.batch_id)
    await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_1.import_outcome.records[0].import_record_id, actor="test",
    )

    report_before = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_before.formulation_changes_detected == 0

    second = _manifest_record(
        "reform-2", product_name="Reform Report Product", sku="REFORM-REPORT-SKU",
        ingredient_list_raw=["Water", "Niacinamide"],
    )
    outcome_2 = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([second]), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_2.import_outcome.batch_id)
    pub_2 = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_2.import_outcome.records[0].import_record_id, actor="test",
    )
    assert pub_2.is_reformulation is True

    report_after = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_after.formulation_changes_detected == 1


async def test_old_formulation_is_preserved_after_reformulation(source_id, catalog_admin_db_pool, seeded_ingredients):
    first = _manifest_record("preserve-1", product_name="Preserve Product", sku="PRESERVE-SKU")
    outcome_1 = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([first]), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_1.import_outcome.batch_id)
    pub_1 = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_1.import_outcome.records[0].import_record_id, actor="test",
    )
    original_ingredients = await catalog_admin_db_pool.fetch(
        "SELECT ingredient_id FROM formulation_ingredients WHERE formulation_id = $1 ORDER BY position",
        pub_1.formulation_id,
    )

    second = _manifest_record(
        "preserve-2", product_name="Preserve Product", sku="PRESERVE-SKU",
        ingredient_list_raw=["Water", "Niacinamide"],
    )
    outcome_2 = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([second]), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_2.import_outcome.batch_id)
    await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_2.import_outcome.records[0].import_record_id, actor="test",
    )

    # The OLD formulation row and its ingredient rows still exist,
    # unmodified, forever -- never deleted, never overwritten.
    still_there = await catalog_admin_db_pool.fetchrow(
        "SELECT id, is_current, publication_status FROM product_formulations WHERE id = $1", pub_1.formulation_id,
    )
    assert still_there is not None
    assert still_there["is_current"] is False
    assert still_there["publication_status"] == "SUPERSEDED"
    still_ingredients = await catalog_admin_db_pool.fetch(
        "SELECT ingredient_id FROM formulation_ingredients WHERE formulation_id = $1 ORDER BY position",
        pub_1.formulation_id,
    )
    assert [r["ingredient_id"] for r in still_ingredients] == [r["ingredient_id"] for r in original_ingredients]


# ---------------------------------------------------------------------------
# Per-record verification status listing
# ---------------------------------------------------------------------------


async def test_list_verification_status_reflects_each_record_independently(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    records = [_manifest_record("vs-1"), _manifest_record("vs-2", ingredient_list_raw=[], ingredient_list_complete=False)]
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl(records), allow_test_source=True)
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)

    statuses = await list_verification_status_for_source(catalog_admin_db_pool, source_id)
    by_id = {s.external_record_id: s.state for s in statuses}
    assert by_id["vs-1"] == VERIFIED
    assert by_id["vs-2"] == INSUFFICIENT_SOURCE_DATA


# ---------------------------------------------------------------------------
# Independent-review Blocker 3: idempotent reimport must not inflate
# total_records_supplied
# ---------------------------------------------------------------------------


async def test_idempotent_reimport_does_not_inflate_total_records_supplied(source_id, catalog_admin_db_pool):
    body = _jsonl([_manifest_record("idem-1"), _manifest_record("idem-2")])

    first = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    report_first = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_first.total_records_supplied == 2
    assert report_first.imported == 2

    second = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert second.import_outcome.batch_is_new is False
    assert second.import_outcome.batch_id == first.import_outcome.batch_id

    report_second = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_second.total_records_supplied == 2
    assert report_second.imported == 2

    # A third, also byte-identical, reimport -- still no inflation.
    third = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert third.import_outcome.batch_is_new is False
    report_third = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_third.total_records_supplied == 2
    assert report_third.imported == 2

    # Exactly two import records exist -- the reimports created none.
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 2

    # Audit history is append-only -- never deleted -- but only ONE
    # wave1-manifest-summary entry exists for this (single, reused)
    # batch, matching import_manifest()'s own batch_is_new gate.
    audit_rows = await catalog_admin_db_pool.fetch(
        "SELECT after_metadata FROM catalog_audit_log WHERE entity_id = $1 AND action = 'IMPORT'",
        first.import_outcome.batch_id,
    )
    wave1_entries = 0
    for row in audit_rows:
        meta = row["after_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if meta and meta.get("wave1_manifest"):
            wave1_entries += 1
    assert wave1_entries == 1


async def test_multiple_genuinely_distinct_batches_still_sum_correctly(source_id, catalog_admin_db_pool):
    batch_one = _jsonl([_manifest_record("multi-a1"), _manifest_record("multi-a2")])
    batch_two = _jsonl([_manifest_record("multi-b1"), _manifest_record("multi-b2"), _manifest_record("multi-b3")])

    await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=batch_one, allow_test_source=True)
    await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=batch_two, allow_test_source=True)

    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.total_records_supplied == 5
    assert report.imported == 5

    # Reimporting the FIRST batch again (idempotent) must not add to
    # the total the second, genuinely distinct batch already
    # contributed.
    await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=batch_one, allow_test_source=True)
    report_after_reimport = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_after_reimport.total_records_supplied == 5
    assert report_after_reimport.imported == 5


# ---------------------------------------------------------------------------
# Final durability blocker: a transient failure writing the Wave 1
# manifest-summary audit entry (AFTER the catalog import has already
# committed) must be fully recoverable by a plain retry.
# ---------------------------------------------------------------------------


def _wave1_summary_count(after_metadata) -> bool:
    meta = after_metadata
    if isinstance(meta, str):
        meta = json.loads(meta)
    return bool(meta and meta.get("wave1_manifest"))


async def test_post_commit_wave_summary_failure_is_recoverable_by_retry(
    source_id, catalog_admin_db_pool, monkeypatch,
):
    """Exact scenario from the independent review: the catalog batch
    and its import record(s) commit successfully inside
    CatalogIngestionService.import_file()'s own transaction; the
    SEPARATE Wave 1 manifest-summary audit write that happens after
    that commit then fails transiently. The caller retries the
    byte-identical manifest -- the existing batch is reused
    (batch_is_new=False), no new import records are created, and the
    previously-missing Wave 1 summary is repaired rather than
    permanently stranded."""
    good = _manifest_record("recoverable-1")
    body = _jsonl([good]) + b"{not valid json\n"  # one valid record, one manifest-level malformed record

    real_write_audit = repo.write_audit

    async def failing_write_audit(
        conn, *, action, entity_type, entity_id, actor, import_record_id=None,
        before_metadata=None, after_metadata=None, reason=None,
    ):
        if after_metadata and after_metadata.get("wave1_manifest"):
            raise asyncpg.exceptions.ConnectionDoesNotExistError("simulated transient DB failure")
        return await real_write_audit(
            conn, action=action, entity_type=entity_type, entity_id=entity_id, actor=actor,
            import_record_id=import_record_id, before_metadata=before_metadata,
            after_metadata=after_metadata, reason=reason,
        )

    monkeypatch.setattr(repo, "write_audit", failing_write_audit)
    with pytest.raises(Exception):
        await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    monkeypatch.undo()

    # The catalog import already committed before the failing write --
    # batch and import record durably exist despite import_manifest()
    # having raised.
    batch_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    record_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records")
    assert batch_count == 1
    assert record_count == 1

    audit_rows = await catalog_admin_db_pool.fetch(
        "SELECT after_metadata FROM catalog_audit_log WHERE action = 'IMPORT'"
    )
    assert not any(_wave1_summary_count(r["after_metadata"]) for r in audit_rows)

    # Retry the byte-identical manifest with normal audit behavior restored.
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert outcome.import_outcome.batch_is_new is False
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 1

    audit_rows_after_retry = await catalog_admin_db_pool.fetch(
        "SELECT after_metadata FROM catalog_audit_log WHERE action = 'IMPORT'"
    )
    wave1_entries = [r for r in audit_rows_after_retry if _wave1_summary_count(r["after_metadata"])]
    assert len(wave1_entries) == 1

    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.total_records_supplied == 2
    assert report.imported == 1

    # A third, sequential attempt must not create a second summary or
    # change either total.
    outcome_third = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert outcome_third.import_outcome.batch_is_new is False
    audit_rows_third = await catalog_admin_db_pool.fetch(
        "SELECT after_metadata FROM catalog_audit_log WHERE action = 'IMPORT'"
    )
    wave1_entries_third = [r for r in audit_rows_third if _wave1_summary_count(r["after_metadata"])]
    assert len(wave1_entries_third) == 1

    report_third = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report_third.total_records_supplied == 2
    assert report_third.imported == 1
