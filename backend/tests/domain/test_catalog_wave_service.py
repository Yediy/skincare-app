"""app.domain.catalog_wave_service -- Production Catalog Wave 1's
manifest-to-pipeline orchestration. Real Postgres, through
catalog_admin_db_pool (skincare_catalog_admin role), same convention as
every other catalog-ingestion test in this repository. Never publishes
through anything but the EXISTING, unmodified
CatalogPublicationService -- these tests prove Wave 1 feeds it real
data correctly, not that publication itself works (that's already
covered by tests/domain/test_catalog_publication_service.py, re-run
unmodified as part of this pass's own validation)."""
import json

import pytest

from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_publication_service import CatalogPublicationService, PublicationError
from app.domain.catalog_wave_report import classify_import_record_state, compute_wave_report
from app.domain.catalog_wave_service import WaveManifestRejectedError, import_manifest, validate_manifest_bytes


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": "Wave Service Test Source",
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
    source = await repo.create_source(catalog_admin_db_pool, name="wave_prod_test_source", source_type="curated_dataset")
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


# ---------------------------------------------------------------------------
# Same product from two sources
# ---------------------------------------------------------------------------


async def test_same_product_from_two_sources_second_publish_sees_existing_current_formulation(
    catalog_admin_db_pool, clean_catalog_ingestion, seeded_ingredients,
):
    from app.db import catalog_admin_repository as repo

    source_a = await repo.create_source(catalog_admin_db_pool, name="wave_prod_test_source_a", source_type="curated_dataset")
    source_b = await repo.create_source(catalog_admin_db_pool, name="wave_prod_test_source_b", source_type="curated_dataset")

    record_a = _manifest_record("a-1", brand="CrossSourceBrand", product_name="Cross Source Cleanser", sku="XSRC-SKU")
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
        "b-1", brand="CrossSourceBrand", product_name="Cross Source Cleanser",
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
    assert parsed["source_url"] == "https://example.invalid/prov-source"
    assert parsed["source_evidence_id"] == "prov-1"

    formulation = await catalog_admin_db_pool.fetchrow(
        "SELECT source_reference FROM product_formulations WHERE id = $1", pub.formulation_id,
    )
    assert json.loads(formulation["source_reference"])["source_evidence_id"] == "prov-1"


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


async def test_publish_rejects_insufficient_source_data_record_still_status_validated(
    source_id, catalog_admin_db_pool,
):
    """Belt-and-suspenders proof: even if an operator ran `publish`
    directly against an INSUFFICIENT_SOURCE_DATA record (Wave 1's own
    tooling never does), the resulting formulation can never reach
    ingredient_data_status=COMPLETE, so it remains structurally
    invisible to ProductMatchingService regardless."""
    record = _manifest_record("insuff-pub-1", ingredient_list_raw=[], ingredient_list_complete=False)
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source_id, file_bytes=_jsonl([record]), allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    pub = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome.import_outcome.records[0].import_record_id, actor="test",
    )
    assert pub.ingredient_data_status == "UNKNOWN"
    candidates = await catalog_admin_db_pool.fetch(
        "SELECT * FROM product_formulations WHERE id = $1 AND publication_status = 'PUBLISHED' "
        "AND ingredient_data_status = 'COMPLETE' AND is_current = true",
        pub.formulation_id,
    )
    assert candidates == []


# ---------------------------------------------------------------------------
# Inactive source rejection
# ---------------------------------------------------------------------------


async def test_inactive_source_is_rejected(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="wave_prod_inactive_source", source_type="curated_dataset")
    await catalog_admin_db_pool.execute("UPDATE catalog_sources SET active = false WHERE id = $1", source["id"])

    with pytest.raises(Exception) as exc_info:
        await import_manifest(
            catalog_admin_db_pool, source_id=source["id"], file_bytes=_jsonl([_manifest_record("x1")]),
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
            catalog_admin_db_pool, source_id=source["id"], file_bytes=_jsonl([_manifest_record("x1")]),
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
        catalog_admin_db_pool, source_id=source["id"], file_bytes=_jsonl([_manifest_record("x1")]),
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
# Source-type cross-check end to end
# ---------------------------------------------------------------------------


async def test_source_type_mismatch_is_isolated_as_manifest_issue(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(catalog_admin_db_pool, name="wave_prod_mismatch_source", source_type="regulatory_filing")
    record = _manifest_record("mismatch-1", source_type="curated_dataset")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=source["id"], file_bytes=_jsonl([record]), allow_test_source=True,
    )
    assert outcome.import_outcome is None
    codes = {i["code"] for i in outcome.manifest_issues}
    assert "SOURCE_TYPE_MISMATCH" in codes
