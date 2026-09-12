"""app.domain.catalog_ingestion_service -- real Postgres, through the
catalog_admin_db_pool (skincare_catalog_admin role), never mocked.
Covers Section 22's "Import" and part of "Ingredient resolution"/
"Completeness" test lists.
"""
import asyncio
import json
import uuid

import pytest

from app.domain.catalog_ingestion_service import (
    MAX_RECORD_BYTES,
    CatalogIngestionService,
    ImportRejectedError,
)


def _record(external_id, **overrides):
    base = {
        "external_record_id": external_id,
        "brand_name": "Ingest Brand",
        "product_name": f"Ingest Product {external_id}",
        "category": "moisturizer",
        "market_or_region": "global",
        "formulation_version": "1",
        "source_type": "manufacturer_disclosure",
        "ingredient_list_complete": True,
        "ingredients": [{"raw_name": "Water", "position": 1}],
        "skus": [{"sku": f"ING-{external_id}"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) "
        "VALUES ('Ingest Source', 'ingest source', 'curated_dataset') RETURNING id"
    )


@pytest.fixture
def service(catalog_admin_db_pool):
    return CatalogIngestionService(catalog_admin_db_pool)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


async def test_valid_jsonl_import(service, source_id):
    body = "\n".join(json.dumps(_record(f"r{i}")) for i in range(3)).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="jsonl")
    assert outcome.batch_is_new is True
    assert outcome.records_total == 3
    assert all(r.status == "NORMALIZED" for r in outcome.records)


async def test_valid_json_array_import(service, source_id):
    body = json.dumps([_record("a1"), _record("a2")]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    assert outcome.records_total == 2
    assert all(r.status == "NORMALIZED" for r in outcome.records)


async def test_malformed_whole_file_json_is_rejected(service, source_id):
    with pytest.raises(ImportRejectedError) as exc_info:
        await service.import_file(source_id=source_id, file_bytes=b"{not valid json", file_format="json")
    assert exc_info.value.code == "MALFORMED_FILE"


async def test_json_format_requires_top_level_array(service, source_id):
    with pytest.raises(ImportRejectedError):
        await service.import_file(source_id=source_id, file_bytes=json.dumps({"a": 1}).encode(), file_format="json")


async def test_one_malformed_jsonl_line_does_not_abort_the_batch(service, source_id):
    lines = [json.dumps(_record("good1")), "{not valid json", json.dumps(_record("good2"))]
    body = "\n".join(lines).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="jsonl")
    assert outcome.records_total == 3
    statuses = {r.external_record_id: r.status for r in outcome.records}
    assert statuses["good1"] == "NORMALIZED"
    assert statuses["good2"] == "NORMALIZED"
    malformed = [r for r in outcome.records if r.status == "MALFORMED"]
    assert len(malformed) == 1


async def test_one_malformed_record_shape_does_not_abort_the_batch(service, source_id):
    """A record that's valid JSON but fails the normalized contract
    (missing a required field) is MALFORMED, not a whole-file failure."""
    bad = {"external_record_id": "bad1", "category": "moisturizer"}  # missing required fields
    body = json.dumps([_record("good1"), bad]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    statuses = {r.external_record_id: r.status for r in outcome.records}
    assert statuses["good1"] == "NORMALIZED"
    assert statuses["bad1"] == "MALFORMED"


@pytest.mark.parametrize("scalar", ["a plain string", 42, True, None, []], ids=["string", "integer", "boolean", "null", "array"])
async def test_non_object_json_array_element_does_not_abort_the_batch(service, source_id, scalar):
    """Blocker 1 (independent review): a syntactically valid JSON value
    that isn't an object must become its own MALFORMED record, never
    an unhandled exception that rolls back the whole batch."""
    body = json.dumps([_record("good1"), scalar, _record("good2")]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")

    assert outcome.records_total == 3
    assert len(outcome.records) == 3
    statuses = [r.status for r in outcome.records]
    assert statuses[0] == "NORMALIZED"
    assert statuses[1] == "MALFORMED"
    assert statuses[2] == "NORMALIZED"
    assert outcome.records[1].validation_errors == ["NON_OBJECT_RECORD"]


@pytest.mark.parametrize("scalar", ["a plain string", 42, True, None, []], ids=["string", "integer", "boolean", "null", "array"])
async def test_non_object_jsonl_line_does_not_abort_the_batch(service, source_id, scalar):
    lines = [json.dumps(_record("good1")), json.dumps(scalar), json.dumps(_record("good2"))]
    body = "\n".join(lines).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="jsonl")

    assert outcome.records_total == 3
    statuses = [r.status for r in outcome.records]
    assert statuses == ["NORMALIZED", "MALFORMED", "NORMALIZED"]
    assert outcome.records[1].validation_errors == ["NON_OBJECT_RECORD"]


async def test_non_object_record_persists_bounded_evidence(service, source_id, catalog_admin_db_pool):
    body = json.dumps([{"just": "a plain object with no required fields is still a dict, not this case"}, "a scalar"]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    non_object_outcome = outcome.records[1]
    assert non_object_outcome.status == "MALFORMED"

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT raw_payload FROM catalog_import_records WHERE id = $1", non_object_outcome.import_record_id,
    )
    stored = row["raw_payload"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored["_non_object_value"] == "a scalar"


async def test_oversized_non_object_record_never_stores_the_actual_content(service, source_id, catalog_admin_db_pool):
    """An enormous nested value at record position must not evade the
    same size bound an oversized object record is already held to."""
    huge_array = ["x" * 1000] * 3000  # well over MAX_RECORD_BYTES once serialized
    body = json.dumps([huge_array]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    assert outcome.records[0].status == "MALFORMED"
    assert outcome.records[0].validation_errors == ["PAYLOAD_TOO_LARGE"]

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT raw_payload FROM catalog_import_records WHERE id = $1", outcome.records[0].import_record_id,
    )
    stored = row["raw_payload"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored.get("_rejected") == "PAYLOAD_TOO_LARGE"
    assert "_non_object_value" not in stored


async def test_non_object_record_dry_run_is_handled_safely(service, source_id):
    body = json.dumps([_record("good1"), "a scalar", None]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json", dry_run=True)
    assert outcome.dry_run is True
    statuses = [r.status for r in outcome.records]
    assert statuses == ["NORMALIZED", "MALFORMED", "MALFORMED"]
    assert all(r.validation_errors == ["NON_OBJECT_RECORD"] for r in outcome.records[1:])


async def test_import_rejects_inactive_source(service, catalog_admin_db_pool, clean_catalog_ingestion):
    source_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type, active) "
        "VALUES ('Inactive Source', 'inactive source', 'curated_dataset', false) RETURNING id"
    )
    body = json.dumps([_record("x1")]).encode("utf-8")
    with pytest.raises(ImportRejectedError) as exc_info:
        await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    assert exc_info.value.code == "SOURCE_INACTIVE"

    batch_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    assert batch_count == 0


async def test_import_rejects_unknown_source(service, clean_catalog_ingestion):
    with pytest.raises(ImportRejectedError) as exc_info:
        await service.import_file(source_id=uuid.uuid4(), file_bytes=json.dumps([_record("x1")]).encode(), file_format="json")
    assert exc_info.value.code == "SOURCE_NOT_FOUND"


async def test_oversized_file_is_rejected(service, source_id):
    huge = json.dumps([_record("x")]).encode("utf-8") + b" " * (50 * 1024 * 1024 + 1)
    with pytest.raises(ImportRejectedError) as exc_info:
        await service.import_file(source_id=source_id, file_bytes=huge, file_format="json")
    assert exc_info.value.code == "FILE_TOO_LARGE"


async def test_oversized_individual_record_never_stores_the_actual_content(service, source_id, catalog_admin_db_pool):
    huge_ingredients = [{"raw_name": f"Ingredient {i}", "position": i + 1} for i in range(1)]
    record = _record("huge1", ingredients=huge_ingredients)
    # Pad with a field pydantic will reject (extra=forbid) but that's
    # irrelevant here -- inflate raw JSON size directly instead.
    body = json.dumps([{
        **record,
        "description": "x" * (MAX_RECORD_BYTES + 1000),
    }]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    assert outcome.records[0].status == "MALFORMED"
    assert "PAYLOAD_TOO_LARGE" in outcome.records[0].validation_errors

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT raw_payload FROM catalog_import_records WHERE external_record_id = 'huge1'"
    )
    stored = row["raw_payload"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored.get("_rejected") == "PAYLOAD_TOO_LARGE"
    assert "description" not in stored


async def test_empty_file_is_rejected(service, source_id):
    with pytest.raises(ImportRejectedError):
        await service.import_file(source_id=source_id, file_bytes=json.dumps([]).encode(), file_format="json")


async def test_duplicate_batch_reuses_same_batch_no_duplicate_records(service, source_id, catalog_admin_db_pool):
    body = json.dumps([_record("dup1")]).encode("utf-8")
    first = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    second = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")

    assert second.batch_is_new is False
    assert second.batch_id == first.batch_id

    count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    assert count == 1
    record_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM catalog_import_records WHERE batch_id = $1", first.batch_id,
    )
    assert record_count == 1


async def test_changed_content_creates_a_new_batch_and_preserves_the_old_raw_record(
    service, source_id, catalog_admin_db_pool,
):
    """Same external_record_id, different file content -> a genuinely
    new batch + new import record, old one untouched (never mutated to
    make the duplicate disappear)."""
    body_v1 = json.dumps([_record("eid-1", product_name="Original Name")]).encode("utf-8")
    body_v2 = json.dumps([_record("eid-1", product_name="Revised Name")]).encode("utf-8")

    outcome_v1 = await service.import_file(source_id=source_id, file_bytes=body_v1, file_format="json")
    outcome_v2 = await service.import_file(source_id=source_id, file_bytes=body_v2, file_format="json")

    assert outcome_v1.batch_id != outcome_v2.batch_id
    v1_record = await catalog_admin_db_pool.fetchrow(
        "SELECT raw_payload FROM catalog_import_records WHERE batch_id = $1", outcome_v1.batch_id,
    )
    raw_v1 = v1_record["raw_payload"]
    raw_v1 = json.loads(raw_v1) if isinstance(raw_v1, str) else raw_v1
    assert raw_v1["product_name"] == "Original Name"


async def test_ten_concurrent_identical_imports_create_exactly_one_batch(catalog_admin_db_pool, source_id):
    body = json.dumps([_record("concurrent1")]).encode("utf-8")

    async def _do_import():
        service = CatalogIngestionService(catalog_admin_db_pool)
        return await service.import_file(source_id=source_id, file_bytes=body, file_format="json")

    outcomes = await asyncio.gather(*[_do_import() for _ in range(10)])
    batch_ids = {o.batch_id for o in outcomes}
    assert len(batch_ids) == 1

    batch_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    assert batch_count == 1
    record_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records")
    assert record_count == 1


# ---------------------------------------------------------------------------
# Validation / ingredient resolution / review flagging
# ---------------------------------------------------------------------------


async def test_validate_batch_marks_resolvable_record_validated(service, source_id, catalog_admin_db_pool):
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    body = json.dumps([_record("v1")]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    results = await service.validate_batch(outcome.batch_id)
    assert results[0].status == "VALIDATED"


async def test_validate_batch_flags_unknown_ingredient_for_review(service, source_id, catalog_admin_db_pool):
    body = json.dumps([_record("v2", ingredients=[{"raw_name": "Mystery Compound", "position": 1}])]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    results = await service.validate_batch(outcome.batch_id)

    assert results[0].status == "NEEDS_REVIEW"
    assert "UNKNOWN_INGREDIENT" in results[0].review_reason_codes

    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1", outcome.records[0].import_record_id,
    )
    assert len(review_items) == 1
    assert review_items[0]["reason_code"] == "UNKNOWN_INGREDIENT"
    assert review_items[0]["status"] == "OPEN"


async def test_validate_batch_never_auto_resolves_via_fuzzy_match(service, source_id, catalog_admin_db_pool):
    """A near-miss spelling must NOT resolve automatically -- only an
    exact canonical/alias match may."""
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Retinol', 'retinol')"
    )
    body = json.dumps([_record("v3", ingredients=[{"raw_name": "Retinoll", "position": 1}])]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    results = await service.validate_batch(outcome.batch_id)
    assert results[0].status == "NEEDS_REVIEW"
    assert "UNKNOWN_INGREDIENT" in results[0].review_reason_codes


async def test_validate_batch_flags_conflicting_sku(service, source_id, catalog_admin_db_pool):
    other_brand = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Other', 'other') RETURNING id"
    )
    other_product = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, 'Other Product', 'other product', 'moisturizer') RETURNING id",
        other_brand,
    )
    other_formulation = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        other_product,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'SKU-TAKEN')",
        other_product, other_formulation,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    body = json.dumps([_record("v4", skus=[{"sku": "SKU-TAKEN"}])]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    results = await service.validate_batch(outcome.batch_id)
    assert "SKU_CONFLICT" in results[0].review_reason_codes


async def test_validate_batch_dry_run_persists_nothing(service, source_id, catalog_admin_db_pool):
    body = json.dumps([_record("dr1", ingredients=[{"raw_name": "Mystery", "position": 1}])]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")

    results = await service.validate_batch(outcome.batch_id, dry_run=True)
    assert results[0].status == "NEEDS_REVIEW"

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", outcome.records[0].import_record_id,
    )
    assert row["status"] == "NORMALIZED"
    review_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_review_items")
    assert review_count == 0


async def test_batch_status_ready_when_all_records_validated(service, source_id, catalog_admin_db_pool):
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    body = json.dumps([_record("bs1")]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    await service.validate_batch(outcome.batch_id)
    batch = await catalog_admin_db_pool.fetchrow("SELECT * FROM catalog_import_batches WHERE id = $1", outcome.batch_id)
    assert batch["status"] == "READY"
    assert batch["records_valid"] == 1


async def test_batch_status_needs_review_when_any_record_flagged(service, source_id, catalog_admin_db_pool):
    body = json.dumps([_record("bs2", ingredients=[{"raw_name": "Mystery", "position": 1}])]).encode("utf-8")
    outcome = await service.import_file(source_id=source_id, file_bytes=body, file_format="json")
    await service.validate_batch(outcome.batch_id)
    batch = await catalog_admin_db_pool.fetchrow("SELECT * FROM catalog_import_batches WHERE id = $1", outcome.batch_id)
    assert batch["status"] == "NEEDS_REVIEW"
