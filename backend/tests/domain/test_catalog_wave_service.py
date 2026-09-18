"""app.domain.catalog_wave_service -- Production Catalog Wave 1's
manifest-to-pipeline orchestration. Real Postgres, through
catalog_admin_db_pool (skincare_catalog_admin role), same convention as
every other catalog-ingestion test in this repository. Never publishes
through anything but the EXISTING, unmodified
CatalogPublicationService -- these tests prove Wave 1 feeds it real
data correctly, not that publication itself works (that's already
covered by tests/domain/test_catalog_publication_service.py, re-run
unmodified as part of this pass's own validation).

Includes independent-review-Blocker-1 regression coverage: a Wave 1
record with insufficient source data must be rejected DIRECTLY by
CatalogPublicationService.publish() itself, not merely excluded from
product matching after the fact.
"""
import json

import pytest

from app.db.catalog_repository import list_current_active_formulations_by_category
from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_publication_service import (
    WAVE1_INSUFFICIENT_SOURCE_DATA,
    CatalogPublicationService,
    PublicationError,
)
from app.domain.catalog_wave_report import classify_import_record_state, compute_wave_report
from app.domain.catalog_wave_service import WaveManifestRejectedError, import_manifest

REGISTERED_SOURCE_NAME = "Wave Service Test Source"


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": REGISTERED_SOURCE_NAME,
        "source_type": "curated_dataset",
        "source_url": "https://example.invalid/source",
        "retrieved_at": "2026-09-17T00:00:00Z",
        "jurisdiction": "us",
        "brand": "WaveServiceTestBrand",
        "product_name": f"Wave Service Test Cleanser {evidence_id}",
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
        for name in ("Water", "Glycerin", "Niacinamide", "Zinc Oxide"):
            await conn.execute(
                "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                name, name.lower(),
            )


# ---------------------------------------------------------------------------
# validate_manifest_bytes -- pure, no DB
# ---------------------------------------------------------------------------


async def test_valid_manifest_ingestion(source_id, catalog_admin_db_pool):
    body = _jsonl([_manifest_record("a1"), _manifest_record("a2")])
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert outcome.manifest_issues == []
    assert outcome.import_outcome.records_total == 2
    assert all(r.status == "NORMALIZED" for r in outcome.import_outcome.records)


async def test_malformed_manifest_record_is_isolated(source_id, catalog_admin_db_pool):
    good = _manifest_record("good-1")
    bad_line = b"{not valid json\n"
    body = _jsonl([good]) + bad_line
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    assert outcome.import_outcome.records_total == 1
    assert any(i["code"] == "MALFORMED_JSON_LINE" for i in outcome.manifest_issues)


async def test_missing_ingredient_list_imports_but_is_insufficient(source_id, catalog_admin_db_pool):
    record = _manifest_record("insufficient-1", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True)
    assert outcome.import_outcome.records[0].status == "NORMALIZED"

    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.insufficient_source_data == 1
    assert report.verified == 0


# ---------------------------------------------------------------------------
# Duplicate source record (same evidence id twice)
# ---------------------------------------------------------------------------


async def test_duplicate_source_record_same_evidence_id_is_idempotent(source_id, catalog_admin_db_pool):
    record = _manifest_record("dup-1")
    body = _jsonl([record])
    first = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    second = await import_manifest(catalog_admin_db_pool, source_id=source_id, file_bytes=body, allow_test_source=True)
    # Byte-identical manifest content -> same batch, same import record.
    assert second.import_outcome.batch_id == first.import_outcome.batch_id
    assert second.import_outcome.batch_is_new is False


# ---------------------------------------------------------------------------
# Duplicate product from same source (SKU/size variants merge)
# ---------------------------------------------------------------------------


async def test_duplicate_product_from_same_source_merges_sku_variants(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    variant_a = _manifest_record("var-a", product_name="Merged Cleanser", sku="SIZE-30ML")
    variant_b = _manifest_record("var-b", product_name="Merged Cleanser", sku="SIZE-50ML")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([variant_a, variant_b]), allow_test_source=True,
    )
    # Merged into ONE import record, not two.
    assert outcome.import_outcome.records_total == 1

    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    record_id = outcome.import_outcome.records[0].import_record_id
    pub = await CatalogPublicationService(catalog_admin_db_pool).publish(record_id, actor="test")

    skus = await catalog_admin_db_pool.fetch(
        "SELECT sku FROM product_skus WHERE formulation_id = $1 ORDER BY sku", pub.formulation_id,
    )
    assert sorted(s["sku"] for s in skus) == ["SIZE-30ML", "SIZE-50ML"]


async def test_duplicate_product_merge_is_order_independent_end_to_end(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    """Independent-review Blocker 2, exercised through the full
    import_manifest() path (not just the pure adapter function):
    reversing the two variant records' order in the manifest file must
    produce an identical outcome."""
    variant_a = _manifest_record("ord-a", product_name="Order Independent Cleanser", sku="ORD-30ML")
    variant_b = _manifest_record("ord-b", product_name="Order Independent Cleanser", sku="ORD-50ML")

    forward = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([variant_a, variant_b]), allow_test_source=True, dry_run=True,
    )
    reversed_outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([variant_b, variant_a]), allow_test_source=True, dry_run=True,
    )
    assert forward.manifest_issues == reversed_outcome.manifest_issues == []
    assert forward.import_outcome.records_total == reversed_outcome.import_outcome.records_total == 1
    forward_statuses = [r.status for r in forward.import_outcome.records]
    reversed_statuses = [r.status for r in reversed_outcome.import_outcome.records]
    assert forward_statuses == reversed_statuses == ["NORMALIZED"]


# ---------------------------------------------------------------------------
# Same product from two sources
# ---------------------------------------------------------------------------


async def test_same_product_from_two_sources_second_publish_sees_existing_current_formulation(
    catalog_admin_db_pool, clean_catalog_ingestion, seeded_ingredients,
):
    from app.db import catalog_admin_repository as repo

    source_a = await repo.create_source(catalog_admin_db_pool, name="Cross Source A", source_type="curated_dataset")
    source_b = await repo.create_source(catalog_admin_db_pool, name="Cross Source B", source_type="curated_dataset")

    record_a = _manifest_record(
        "a-1", source_name="Cross Source A", brand="CrossSourceBrand", product_name="Cross Source Cleanser", sku="XSRC-SKU",
    )
    outcome_a = await import_manifest(
        catalog_admin_db_pool, source_id=source_a["id"], file_bytes=_jsonl([record_a]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_a.import_outcome.batch_id)
    pub_a = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_a.import_outcome.records[0].import_record_id, actor="test",
    )
    assert pub_a.is_reformulation is False

    # Second source, same brand/product, DIFFERENT ingredients (a real
    # independent claim about the same product) -- the EXISTING publish()
    # logic decides what this means; Wave 1 does not special-case it.
    record_b = _manifest_record(
        "b-1", source_name="Cross Source B", brand="CrossSourceBrand", product_name="Cross Source Cleanser",
        ingredient_list_raw=["Water", "Niacinamide"], sku="XSRC-SKU-2",
    )
    outcome_b = await import_manifest(
        catalog_admin_db_pool, source_id=source_b["id"], file_bytes=_jsonl([record_b]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome_b.import_outcome.batch_id)
    pub_b = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome_b.import_outcome.records[0].import_record_id, actor="test",
    )
    # Different fingerprint (different ingredients) for the same
    # product/market -> the existing pipeline's reformulation path,
    # not a spurious conflict -- proving Wave 1 correctly feeds
    # cross-source evidence through the unmodified publish() logic.
    assert pub_b.is_reformulation is True
    assert pub_b.superseded_formulation_id == pub_a.formulation_id

    old = await catalog_admin_db_pool.fetchrow(
        "SELECT is_current, publication_status FROM product_formulations WHERE id = $1", pub_a.formulation_id,
    )
    assert old["is_current"] is False
    assert old["publication_status"] == "SUPERSEDED"


# ---------------------------------------------------------------------------
# Conflicting product identity
# ---------------------------------------------------------------------------


async def test_conflicting_product_identity_within_one_manifest_is_flagged_not_guessed(
    source_id, catalog_admin_db_pool,
):
    a = _manifest_record("conflict-a", product_name="Conflict Product", category="cleanser")
    b = _manifest_record("conflict-b", product_name="Conflict Product", category="serum")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([a, b]), allow_test_source=True,
    )
    assert outcome.import_outcome is None or outcome.import_outcome.records_total == 0
    codes = {i["code"] for i in outcome.manifest_issues}
    assert "CONFLICTING_PRODUCT_IDENTITY" in codes


# ---------------------------------------------------------------------------
# Unresolved ingredient
# ---------------------------------------------------------------------------


async def test_unresolved_ingredient_routes_to_review_required(source_id, catalog_admin_db_pool):
    record = _manifest_record("unresolved-1", ingredient_list_raw=["Totally Unknown Compound X"])
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    report = await compute_wave_report(catalog_admin_db_pool, source_id=source_id)
    assert report.review_required == 1
    assert report.unresolved_ingredients == 1


# ---------------------------------------------------------------------------
# Source provenance persistence
# ---------------------------------------------------------------------------


async def test_source_provenance_persists_through_publish(source_id, catalog_admin_db_pool, seeded_ingredients):
    record = _manifest_record("prov-1", source_url="https://example.invalid/prov-source")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    pub = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome.import_outcome.records[0].import_record_id, actor="test",
    )
    provenance = await catalog_admin_db_pool.fetchrow(
        "SELECT source_reference FROM catalog_formulation_provenance WHERE formulation_id = $1", pub.formulation_id,
    )
    parsed = json.loads(provenance["source_reference"])
    assert parsed["evidence"][0]["source_url"] == "https://example.invalid/prov-source"
    assert parsed["evidence"][0]["source_evidence_id"] == "prov-1"

    formulation = await catalog_admin_db_pool.fetchrow(
        "SELECT source_reference FROM product_formulations WHERE id = $1", pub.formulation_id,
    )
    assert json.loads(formulation["source_reference"])["evidence"][0]["source_evidence_id"] == "prov-1"


# ---------------------------------------------------------------------------
# Publication only after validation (Wave 1 never auto-publishes)
# ---------------------------------------------------------------------------


async def test_import_manifest_never_publishes_anything(source_id, catalog_admin_db_pool, seeded_ingredients):
    record = _manifest_record("no-auto-publish-1")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status, formulation_id FROM catalog_import_records WHERE id = $1",
        outcome.import_outcome.records[0].import_record_id,
    )
    assert row["status"] == "NORMALIZED"
    assert row["formulation_id"] is None
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations") == 0


# ---------------------------------------------------------------------------
# Independent-review Blocker 1: publication-time backstop
# ---------------------------------------------------------------------------


async def test_wave1_insufficient_record_cannot_publish(source_id, catalog_admin_db_pool):
    record = _manifest_record("insuff-pub-1", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id

    with pytest.raises(PublicationError) as exc_info:
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")
    assert exc_info.value.code == WAVE1_INSUFFICIENT_SOURCE_DATA


async def test_wave1_insufficient_record_publish_creates_no_formulation_row(source_id, catalog_admin_db_pool):
    record = _manifest_record("insuff-pub-2", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id

    with pytest.raises(PublicationError):
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")

    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations") == 0
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_formulation_provenance") == 0


async def test_wave1_insufficient_record_remains_non_published(source_id, catalog_admin_db_pool):
    record = _manifest_record("insuff-pub-3", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id

    with pytest.raises(PublicationError):
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status, formulation_id FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert row["status"] == "VALIDATED"
    assert row["formulation_id"] is None


async def test_wave1_insufficient_record_rejection_leaves_product_matching_unaffected(
    source_id, catalog_admin_db_pool,
):
    record = _manifest_record("insuff-pub-4", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id

    with pytest.raises(PublicationError):
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")

    candidates = await list_current_active_formulations_by_category(catalog_admin_db_pool, "cleanser", "us")
    assert candidates == []


async def test_wave1_sufficient_verified_record_still_publishes_normally(
    source_id, catalog_admin_db_pool, seeded_ingredients,
):
    """The backstop only fires for genuinely insufficient Wave 1
    records -- a sufficient, VERIFIED one publishes exactly as before
    this fix."""
    record = _manifest_record("sufficient-pub-1")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id
    assert classify_import_record_state(
        {"status": "VALIDATED", "normalized_payload": {"ingredients": [{"raw_name": "Water"}], "ingredient_list_complete": True}}
    ) == "VERIFIED"

    pub = await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")
    assert pub.ingredient_data_status == "COMPLETE"
    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status, formulation_id FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert row["status"] == "PUBLISHED"
    assert row["formulation_id"] == pub.formulation_id


# ---------------------------------------------------------------------------
# Inactive source rejection
# ---------------------------------------------------------------------------


async def test_inactive_source_is_rejected(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="Wave Prod Inactive Source", source_type="curated_dataset")
    await catalog_admin_db_pool.execute("UPDATE catalog_sources SET active = false WHERE id = $1", source["id"])

    with pytest.raises(Exception) as exc_info:
        await import_manifest(
            catalog_admin_db_pool, source_id=source["id"],
            file_bytes=_jsonl([_manifest_record("x1", source_name="Wave Prod Inactive Source")]),
            allow_test_source=True,
        )
    assert getattr(exc_info.value, "code", None) == "SOURCE_INACTIVE"


# ---------------------------------------------------------------------------
# Production data cannot masquerade as test fixture
# ---------------------------------------------------------------------------


async def test_reserved_test_source_name_is_refused_by_default(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="test_should_be_refused", source_type="curated_dataset")
    with pytest.raises(WaveManifestRejectedError) as exc_info:
        await import_manifest(
            catalog_admin_db_pool, source_id=source["id"],
            file_bytes=_jsonl([_manifest_record("x1", source_name="test_should_be_refused")]),
            # No allow_test_source -- defaults False.
        )
    assert exc_info.value.code == "RESERVED_TEST_SOURCE"
    # Nothing was written.
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 0


async def test_reserved_test_source_name_can_be_explicitly_allowed_for_test_code(
    catalog_admin_db_pool, clean_catalog_ingestion,
):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="synthetic_explicitly_allowed", source_type="curated_dataset")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source["id"],
        file_bytes=_jsonl([_manifest_record("x1", source_name="synthetic_explicitly_allowed")]),
        allow_test_source=True,
    )
    assert outcome.import_outcome.records_total == 1


async def test_ordinary_production_source_name_is_not_affected_by_the_guard(source_id, catalog_admin_db_pool):
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([_manifest_record("prod-1")]),
        # Real production import path never passes allow_test_source=True.
    )
    assert outcome.import_outcome.records_total == 1


# ---------------------------------------------------------------------------
# Source-type / source-name cross-check end to end
# ---------------------------------------------------------------------------


async def test_source_type_mismatch_is_isolated_as_manifest_issue(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="Wave Prod Mismatch Source", source_type="regulatory_filing")
    record = _manifest_record("mismatch-1", source_name="Wave Prod Mismatch Source", source_type="curated_dataset")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source["id"], file_bytes=_jsonl([record]), allow_test_source=True,
    )
    assert outcome.import_outcome is None
    codes = {i["code"] for i in outcome.manifest_issues}
    assert "SOURCE_TYPE_MISMATCH" in codes


async def test_source_name_mismatch_is_isolated_as_manifest_issue_end_to_end(source_id, catalog_admin_db_pool):
    record = _manifest_record("name-mismatch-1", source_name="A Completely Different Vendor")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    assert outcome.import_outcome is None
    codes = {i["code"] for i in outcome.manifest_issues}
    assert "SOURCE_NAME_MISMATCH" in codes
    # Nothing was written -- rejected, not silently imported under the
    # selected source_id anyway.
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 0
