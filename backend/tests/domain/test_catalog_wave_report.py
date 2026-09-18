"""app.domain.catalog_wave_report -- Production Catalog Wave 1's
deterministic reporting. Real Postgres, through catalog_admin_db_pool,
same convention as every other catalog-ingestion test."""
import json

import pytest

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


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": "Wave Report Test Source",
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
    source = await repo.create_source(catalog_admin_db_pool, name="wave_report_test_source", source_type="curated_dataset")
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
