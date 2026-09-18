"""app.catalog_admin.cli's five Production Catalog Wave 1 subcommands
(validate-source-manifest, import-source-manifest, inspect-wave,
report-review-required, report-verification-status) -- same run()-based
testing pattern as tests/catalog_admin/test_cli.py: no subprocess, real
Postgres through catalog_admin_db_pool."""
import io
import json
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.catalog_admin.cli import run
from app.domain.catalog_wave_service import import_manifest


async def _run(pool, argv):
    out, err = io.StringIO(), io.StringIO()
    code = await run(argv, pool, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": "Wave1 CLI Test Source",
        "source_type": "curated_dataset",
        "source_url": None,
        "retrieved_at": "2026-09-17T00:00:00Z",
        "jurisdiction": "us",
        "brand": "Wave1CliBrand",
        "product_name": f"Wave1 CLI Product {evidence_id}",
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


def _write_jsonl(tmp_path: Path, records, name="manifest.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo
    source = await repo.create_source(catalog_admin_db_pool, name="Wave1 CLI Test Source", source_type="curated_dataset")
    return str(source["id"])


@pytest.fixture
async def seeded_ingredients(catalog_admin_db_pool, clean_catalog_ingestion):
    async with catalog_admin_db_pool.acquire() as conn:
        for name in ("Water", "Glycerin"):
            await conn.execute(
                "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                name, name.lower(),
            )


async def test_validate_source_manifest_is_pure_and_reports_counts(catalog_admin_db_pool, clean_catalog_ingestion, tmp_path):
    path = _write_jsonl(tmp_path, [_manifest_record("v1"), _manifest_record("v2", ingredient_list_raw=[], ingredient_list_complete=False)])
    code, out, err = await _run(catalog_admin_db_pool, ["validate-source-manifest", str(path)])
    assert code == 0, err
    result = json.loads(out)
    assert result["total_records"] == 2
    assert result["sufficient_source_data"] == 1
    assert result["insufficient_source_data"] == 1
    # Pure -- no DB rows written.
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 0


async def test_import_source_manifest_end_to_end_through_validate_and_publish(
    catalog_admin_db_pool, source_id, seeded_ingredients, tmp_path,
):
    path = _write_jsonl(tmp_path, [_manifest_record("cli-e2e-1")])
    code, out, err = await _run(
        catalog_admin_db_pool,
        ["import-source-manifest", str(path), "--source-id", source_id, "--allow-test-source"],
    )
    assert code == 0, err
    result = json.loads(out)
    assert result["import_outcome"]["records_total"] == 1
    import_record_id = result["import_outcome"]["records"][0]["import_record_id"]

    code, out, err = await _run(catalog_admin_db_pool, ["validate-batch", result["import_outcome"]["batch_id"]])
    assert code == 0, err

    code, out, err = await _run(catalog_admin_db_pool, ["publish", import_record_id])
    assert code == 0, err
    publish_result = json.loads(out)
    assert publish_result["ingredient_data_status"] == "COMPLETE"


async def test_import_source_manifest_dry_run_writes_nothing(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_manifest_record("dry-1")])
    code, out, err = await _run(
        catalog_admin_db_pool,
        ["import-source-manifest", str(path), "--source-id", source_id, "--allow-test-source", "--dry-run"],
    )
    assert code == 0, err
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_records") == 0


async def test_import_source_manifest_refuses_reserved_test_source_without_flag(catalog_admin_db_pool, clean_catalog_ingestion, tmp_path):
    from app.db import catalog_admin_repository as repo
    source = await repo.create_source(catalog_admin_db_pool, name="test_reserved_cli_source", source_type="curated_dataset")
    path = _write_jsonl(tmp_path, [_manifest_record("guard-1")])
    code, out, err = await _run(
        catalog_admin_db_pool, ["import-source-manifest", str(path), "--source-id", str(source["id"])],
    )
    assert code == 1
    assert "RESERVED_TEST_SOURCE" in err


async def test_inspect_wave_reports_per_source_counts(catalog_admin_db_pool, source_id, seeded_ingredients, tmp_path):
    path = _write_jsonl(tmp_path, [_manifest_record("iw-1")])
    await _run(catalog_admin_db_pool, ["import-source-manifest", str(path), "--source-id", source_id, "--allow-test-source"])

    code, out, err = await _run(catalog_admin_db_pool, ["inspect-wave", "--source-id", source_id])
    assert code == 0, err
    report = json.loads(out)
    assert report["imported"] == 1
    assert report["total_records_supplied"] == 1


async def test_inspect_wave_without_source_id_lists_every_source(catalog_admin_db_pool, source_id):
    code, out, err = await _run(catalog_admin_db_pool, ["inspect-wave"])
    assert code == 0, err
    report = json.loads(out)
    assert isinstance(report["source_counts"], list)
    assert any(s["source_id"] == source_id for s in report["source_counts"])


async def test_report_review_required_lists_open_items_for_source(catalog_admin_db_pool, source_id, tmp_path):
    path = _write_jsonl(tmp_path, [_manifest_record("rr-1", ingredient_list_raw=["Totally Unresolvable For CLI Test"])])
    code, out, err = await _run(
        catalog_admin_db_pool, ["import-source-manifest", str(path), "--source-id", source_id, "--allow-test-source"],
    )
    batch_id = json.loads(out)["import_outcome"]["batch_id"]
    await _run(catalog_admin_db_pool, ["validate-batch", batch_id])

    code, out, err = await _run(catalog_admin_db_pool, ["report-review-required", "--source-id", source_id])
    assert code == 0, err
    items = json.loads(out)
    assert len(items) == 1
    assert items[0]["reason_code"] == "UNKNOWN_INGREDIENT"


async def test_report_verification_status_lists_each_record(catalog_admin_db_pool, source_id, seeded_ingredients, tmp_path):
    path = _write_jsonl(tmp_path, [_manifest_record("rvs-1")])
    code, out, err = await _run(
        catalog_admin_db_pool, ["import-source-manifest", str(path), "--source-id", source_id, "--allow-test-source"],
    )
    batch_id = json.loads(out)["import_outcome"]["batch_id"]
    await _run(catalog_admin_db_pool, ["validate-batch", batch_id])

    code, out, err = await _run(catalog_admin_db_pool, ["report-verification-status", "--source-id", source_id])
    assert code == 0, err
    statuses = json.loads(out)
    assert statuses[0]["external_record_id"] == "rvs-1"
    assert statuses[0]["state"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Restricted admin-role enforcement -- ordinary app role cannot mutate
# catalog evidence (Wave 1 introduces no new table/write path, so this
# reuses the same GRANT boundary tests/database/test_catalog_admin_
# privilege.py already proves -- this is a focused confirmation that
# Wave 1's own CLI still runs exclusively over the admin pool).
# ---------------------------------------------------------------------------


async def test_wave1_cli_commands_require_the_catalog_admin_pool_not_app_pool(
    app_db_pool, catalog_admin_db_pool, source_id, tmp_path,
):
    """Wave 1 introduces no new table and no new write path -- the same
    GRANT boundary tests/database/test_catalog_admin_privilege.py
    already proves for every existing catalog-admin write applies here
    unchanged, since `import_manifest()` writes exclusively through
    the existing, unmodified `CatalogIngestionService.import_file()`.
    The restricted `skincare_app` role (`app_db_pool`) can read the
    registered source (SELECT-only, same as everywhere else) but
    cannot write the resulting import record at all."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await import_manifest(
            app_db_pool, source_id=UUID(source_id), file_bytes=_write_jsonl(tmp_path, [_manifest_record("priv-1")]).read_bytes(),
            allow_test_source=True,
        )
