"""app.catalog_admin.cli's populate-wave1b-real-data subcommand
(independent-review Blocker 4, PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md)
-- same run()-based testing pattern as tests/catalog_admin/test_wave1_cli.py:
no subprocess, real Postgres through catalog_admin_db_pool.
"""
import io
import json
from pathlib import Path

from app.catalog_admin.cli import run

WAVE1B_DATA = Path(__file__).resolve().parents[3] / "catalog_data" / "production" / "wave1b"
REAL_MANIFEST = WAVE1B_DATA / "manifest.jsonl"
REAL_DICTIONARY = WAVE1B_DATA / "ingredient_dictionary.json"


async def _run(pool, argv):
    out, err = io.StringIO(), io.StringIO()
    code = await run(argv, pool, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


async def test_populate_wave1b_real_data_cli_end_to_end(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(
        catalog_admin_db_pool, name="Wave 1B Manufacturer Acquisition", source_type="curated_dataset",
    )
    code, out, err = await _run(catalog_admin_db_pool, [
        "populate-wave1b-real-data", str(REAL_MANIFEST),
        "--source-id", str(source["id"]), "--dictionary-path", str(REAL_DICTIONARY),
    ])
    assert code == 0, err
    result = json.loads(out)
    assert result["preflight_ready"] is True
    assert result["unresolved_dictionary_gaps"] == []
    assert result["records_needing_review"] == 0
    assert result["records_rejected_or_malformed"] == 0
    published = [p for p in result["publish_outcomes"] if p["status"] == "PUBLISHED"]
    assert len(published) == result["manifest_total_records"]


async def test_populate_wave1b_real_data_cli_dry_run_writes_nothing(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(
        catalog_admin_db_pool, name="Wave 1B Manufacturer Acquisition", source_type="curated_dataset",
    )
    before = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    code, out, err = await _run(catalog_admin_db_pool, [
        "populate-wave1b-real-data", str(REAL_MANIFEST),
        "--source-id", str(source["id"]), "--dictionary-path", str(REAL_DICTIONARY), "--dry-run",
    ])
    assert code == 0, err
    result = json.loads(out)
    assert result["dry_run"] is True
    assert result["dictionary_entry_count"] > 0
    assert result["preflight_ready"] is True
    after = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM catalog_import_batches")
    assert before == after


async def test_populate_wave1b_real_data_cli_refuses_reserved_test_source_without_flag(
    catalog_admin_db_pool, clean_catalog_ingestion,
):
    from app.db import catalog_admin_repository as repo

    source = await repo.create_source(
        catalog_admin_db_pool, name="test_reserved_source", source_type="curated_dataset",
    )
    code, out, err = await _run(catalog_admin_db_pool, [
        "populate-wave1b-real-data", str(REAL_MANIFEST),
        "--source-id", str(source["id"]), "--dictionary-path", str(REAL_DICTIONARY),
    ])
    assert code == 1
    assert "RESERVED_TEST_SOURCE" in err
