"""app.catalog_admin.cli -- thin argparse adapter, tested end-to-end
against real Postgres (catalog_admin_db_pool) by calling run()
directly (no subprocess needed -- run() takes an already-open pool).
"""
import io
import json
from pathlib import Path

import pytest

from app.catalog_admin.cli import run


async def _run(pool, argv):
    out, err = io.StringIO(), io.StringIO()
    code = await run(argv, pool, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) "
        "VALUES ('CLI Source', 'cli source', 'curated_dataset') RETURNING id"
    )


def _write_jsonl(tmp_path: Path, records) -> Path:
    path = tmp_path / "import.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def _record(external_id, **overrides):
    base = {
        "external_record_id": external_id,
        "brand_name": "CLI Brand",
        "product_name": f"CLI Product {external_id}",
        "category": "moisturizer",
        "market_or_region": "global",
        "formulation_version": "1",
        "source_type": "manufacturer_disclosure",
        "ingredient_list_complete": True,
        "ingredients": [{"raw_name": "Water", "position": 1}],
        "skus": [{"sku": f"CLI-{external_id}"}],
    }
    base.update(overrides)
    return base


async def test_create_source(catalog_admin_db_pool, clean_catalog_ingestion):
    code, out, err = await _run(catalog_admin_db_pool, [
        "create-source", "--name", "Manual Source", "--source-type", "curated_dataset",
    ])
    assert code == 0
    result = json.loads(out)
    assert result["name"] == "Manual Source"


async def test_import_file_and_validate_and_publish_end_to_end(catalog_admin_db_pool, source_id, tmp_path):
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    path = _write_jsonl(tmp_path, [_record("cli1")])

    code, out, err = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl",
    ])
    assert code == 0, err
    import_outcome = json.loads(out)
    batch_id = import_outcome["batch_id"]

    code, out, err = await _run(catalog_admin_db_pool, ["validate-batch", batch_id])
    assert code == 0, err
    validate_outcome = json.loads(out)
    assert validate_outcome[0]["status"] == "VALIDATED"
    import_record_id = validate_outcome[0]["import_record_id"]

    code, out, err = await _run(catalog_admin_db_pool, ["batch-status", batch_id])
    assert code == 0, err
    batch = json.loads(out)
    assert batch["status"] == "READY"

    code, out, err = await _run(catalog_admin_db_pool, [
        "publish", import_record_id, "--actor", "cli-tester",
    ])
    assert code == 0, err
    publish_outcome = json.loads(out)
    assert publish_outcome["ingredient_data_status"] == "COMPLETE"

    formulation = await catalog_admin_db_pool.fetchrow(
        "SELECT publication_status FROM product_formulations WHERE id = $1", publish_outcome["formulation_id"],
    )
    assert formulation["publication_status"] == "PUBLISHED"


async def test_import_file_dry_run_writes_nothing(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_record("dr1")])
    code, out, err = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl", "--dry-run",
    ])
    assert code == 0, err
    result = json.loads(out)
    assert result["dry_run"] is True
    count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    assert count == 0


async def test_batch_status_unknown_batch_returns_error(catalog_admin_db_pool, clean_catalog_ingestion):
    import uuid
    code, out, err = await _run(catalog_admin_db_pool, ["batch-status", str(uuid.uuid4())])
    assert code == 1
    assert "not found" in err


async def test_review_list_show_and_resolve_ingredient(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_record("rev1", ingredients=[{"raw_name": "Mystery Thing", "position": 1}])])

    code, out, _ = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl",
    ])
    batch_id = json.loads(out)["batch_id"]
    await _run(catalog_admin_db_pool, ["validate-batch", batch_id])

    code, out, err = await _run(catalog_admin_db_pool, ["review-list"])
    assert code == 0, err
    items = json.loads(out)
    assert len(items) == 1
    review_item_id = items[0]["id"]

    code, out, err = await _run(catalog_admin_db_pool, ["review-show", review_item_id])
    assert code == 0, err
    detail = json.loads(out)
    assert detail["context"]["unresolved_ingredient_names"] == ["Mystery Thing"]

    code, out, err = await _run(catalog_admin_db_pool, [
        "resolve-ingredient", review_item_id,
        "--create-canonical", "Mystery Thing Extract", "--actor", "cli-reviewer",
    ])
    assert code == 0, err
    result = json.loads(out)
    assert result["resolution"] == "CREATED_NEW_INGREDIENT"


async def test_resolve_ingredient_map_to_existing(catalog_admin_db_pool, source_id, tmp_path):
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Niacinamide', 'niacinamide') RETURNING id"
    )
    path = _write_jsonl(tmp_path, [_record("rev2", ingredients=[{"raw_name": "Vitamin B3", "position": 1}])])
    code, out, _ = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl",
    ])
    batch_id = json.loads(out)["batch_id"]
    await _run(catalog_admin_db_pool, ["validate-batch", batch_id])
    code, out, _ = await _run(catalog_admin_db_pool, ["review-list"])
    review_item_id = json.loads(out)[0]["id"]

    code, out, err = await _run(catalog_admin_db_pool, [
        "resolve-ingredient", review_item_id, "--map-to", str(ingredient_id),
    ])
    assert code == 0, err
    assert json.loads(out)["resolution"] == "MAPPED_TO_EXISTING_INGREDIENT"


async def test_reject_command(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_record("rej1", ingredients=[{"raw_name": "Bad Data", "position": 1}])])
    code, out, _ = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl",
    ])
    batch_id = json.loads(out)["batch_id"]
    await _run(catalog_admin_db_pool, ["validate-batch", batch_id])
    code, out, _ = await _run(catalog_admin_db_pool, ["review-list"])
    review_item_id = json.loads(out)[0]["id"]

    code, out, err = await _run(catalog_admin_db_pool, ["reject", review_item_id, "--reason", "unreliable source"])
    assert code == 0, err
    assert json.loads(out)["resolution"] == "REJECTED_SOURCE_VALUE"


async def test_publish_error_prints_code_and_returns_nonzero(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_record("notready1")])
    code, out, _ = await _run(catalog_admin_db_pool, [
        "import-file", str(path), "--source-id", str(source_id), "--format", "jsonl",
    ])
    import_record_id = json.loads(out)["records"][0]["import_record_id"]  # still NORMALIZED, never validated

    code, out, err = await _run(catalog_admin_db_pool, ["publish", import_record_id])
    assert code == 1
    assert "NOT_PUBLISHABLE_STATUS" in err
