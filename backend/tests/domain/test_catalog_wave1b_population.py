"""app.domain.catalog_wave1b_population -- the reproducible, idempotent,
dry-run-capable Wave 1B operator population workflow
(independent-review Blocker 4, PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md).

Real-Postgres integration tests only (this module's whole job is
orchestrating real database state through the existing pipeline) --
`catalog_admin_db_pool` + `clean_catalog_ingestion`, same convention as
`tests/domain/test_catalog_acquisition.py`'s own integration tests.
"""
import json
from pathlib import Path

import pytest

from app.db import catalog_admin_repository as repo
from app.domain.catalog_wave1b_population import (
    PopulationRejectedError,
    load_ingredient_dictionary,
    populate_wave1b,
)

WAVE1B_DATA = Path(__file__).resolve().parents[3] / "catalog_data" / "production" / "wave1b"
REAL_MANIFEST = WAVE1B_DATA / "manifest.jsonl"
REAL_DICTIONARY = WAVE1B_DATA / "ingredient_dictionary.json"

SOURCE_NAME = "Wave 1B Manufacturer Acquisition"


@pytest.fixture
async def wave1b_source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    source = await repo.create_source(catalog_admin_db_pool, name=SOURCE_NAME, source_type="curated_dataset")
    return source["id"]


def _write_dictionary(tmp_path, entries):
    path = tmp_path / "ingredient_dictionary.json"
    path.write_text(json.dumps({"entries": entries}))
    return path


def _write_dictionary_raw(tmp_path, obj):
    """For tests exercising the top-level shape itself (not a valid
    {'entries': [...]} object)."""
    path = tmp_path / "ingredient_dictionary.json"
    path.write_text(json.dumps(obj))
    return path


# ---------------------------------------------------------------------------
# Ingredient dictionary loading -- exact matching only, never fuzzy
# ---------------------------------------------------------------------------


def test_dictionary_loads_real_committed_file():
    entries = load_ingredient_dictionary(REAL_DICTIONARY)
    assert len(entries) >= 100
    water = next(e for e in entries if e.canonical_name == "Water")
    assert "Aqua (Water)" in water.aliases


def test_dictionary_rejects_ambiguous_entries(tmp_path):
    """Two entries whose canonical/alias spellings normalize to the
    SAME identity but claim two different canonical names is a genuine
    authoring error in the committed file -- must fail closed at load
    time, never silently resolved by "whichever loads first"."""
    path = _write_dictionary(tmp_path, [
        {"canonical_name": "Water", "ingredient_type": None, "inci_name": None, "aliases": []},
        {"canonical_name": "Glycerin", "ingredient_type": None, "inci_name": None, "aliases": ["Water"]},
    ])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_AMBIGUOUS"


def test_dictionary_unreadable_file_fails_closed(tmp_path):
    path = tmp_path / "does_not_exist.json"
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_UNREADABLE"


def test_dictionary_invalid_json_fails_closed(tmp_path):
    path = tmp_path / "ingredient_dictionary.json"
    path.write_text("{not valid json")
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_top_level_array_fails_closed(tmp_path):
    """The committed file must be a JSON object with an 'entries' key --
    a bare top-level array (an easy authoring mistake) fails
    structurally, never silently treated as the entries list itself."""
    path = tmp_path / "ingredient_dictionary.json"
    path.write_text(json.dumps([{"canonical_name": "Water", "aliases": []}]))
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_missing_entries_key_fails_closed(tmp_path):
    path = _write_dictionary_raw(tmp_path, {"not_entries": []})
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_empty_entries_fails_closed(tmp_path):
    path = _write_dictionary(tmp_path, [])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_EMPTY"


def test_dictionary_entry_not_an_object_fails_closed(tmp_path):
    path = _write_dictionary_raw(tmp_path, {"entries": ["Water"]})
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_missing_canonical_name_fails_closed(tmp_path):
    path = _write_dictionary(tmp_path, [{"aliases": []}])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_blank_canonical_name_fails_closed(tmp_path):
    path = _write_dictionary(tmp_path, [{"canonical_name": "   ", "aliases": []}])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


@pytest.mark.parametrize("bad_aliases", [
    "Water",  # a string, not a list
    ["Water", 42],  # non-string entry
    ["Water", "   "],  # blank entry
    7,  # not a list at all
])
def test_dictionary_invalid_aliases_shape_fails_closed(tmp_path, bad_aliases):
    path = _write_dictionary(tmp_path, [{"canonical_name": "Water", "aliases": bad_aliases}])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_non_string_ingredient_type_fails_closed(tmp_path):
    path = _write_dictionary(tmp_path, [{"canonical_name": "Water", "aliases": [], "ingredient_type": 7}])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_non_string_inci_name_fails_closed(tmp_path):
    path = _write_dictionary(tmp_path, [{"canonical_name": "Water", "aliases": [], "inci_name": []}])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_INVALID"


def test_dictionary_duplicate_canonical_definition_fails_closed(tmp_path):
    """Two SEPARATE entries both literally declaring canonical_name
    'Water' is a duplicate definition, not merely an alias conflict --
    must fail exactly like any other ambiguous dictionary."""
    path = _write_dictionary(tmp_path, [
        {"canonical_name": "Water", "aliases": []},
        {"canonical_name": "Water", "aliases": []},
    ])
    with pytest.raises(PopulationRejectedError) as exc_info:
        load_ingredient_dictionary(path)
    assert exc_info.value.code == "DICTIONARY_AMBIGUOUS"


# ---------------------------------------------------------------------------
# Reproducibility / idempotency against a clean database, using the
# REAL committed manifest + dictionary
# ---------------------------------------------------------------------------


async def test_clean_db_population_is_reproducible(wave1b_source_id, catalog_admin_db_pool):
    """The core Blocker 4 proof: starting from a genuinely clean
    database (no pre-seeded ingredients, no raw SQL bootstrap), running
    ONLY `populate_wave1b()` against the real committed manifest and
    the real committed dictionary reaches VALIDATED and PUBLISHED for
    every real acquired product, with zero unresolved dictionary gaps
    and zero records left needing review."""
    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
        dictionary_path=REAL_DICTIONARY, actor="test",
    )
    assert report.manifest_issues == []
    assert report.records_imported == report.manifest_total_records
    assert report.records_needing_review == 0
    assert report.records_rejected_or_malformed == 0
    assert report.unresolved_dictionary_gaps == []
    assert report.records_validated == report.manifest_total_records
    assert report.records_published == report.manifest_total_records
    assert all(p.status == "PUBLISHED" for p in report.publish_outcomes)

    published_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE publication_status = 'PUBLISHED'"
    )
    assert published_count == report.manifest_total_records


async def test_second_population_run_is_idempotent(wave1b_source_id, catalog_admin_db_pool):
    """Running the exact same population twice against the same target
    database never duplicates catalog truth -- same ingredient count,
    same product count, same formulation count, same published count,
    on the second run as the first."""
    first = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
        dictionary_path=REAL_DICTIONARY, actor="test",
    )
    ingredients_after_1 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredients")
    products_after_1 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM products")
    formulations_after_1 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations")
    aliases_after_1 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")

    second = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
        dictionary_path=REAL_DICTIONARY, actor="test",
    )
    ingredients_after_2 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredients")
    products_after_2 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM products")
    formulations_after_2 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations")
    aliases_after_2 = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")

    assert second.records_imported == 0
    assert second.records_reused_from_prior_import == first.records_imported
    # publish() is idempotently re-attempted against already-PUBLISHED
    # records -- the report still reflects the TRUE current published
    # count, never "0" just because nothing NEW happened this run.
    assert second.records_published == first.records_published
    assert ingredients_after_2 == ingredients_after_1
    assert products_after_2 == products_after_1
    assert formulations_after_2 == formulations_after_1
    assert aliases_after_2 == aliases_after_1


_DRY_RUN_TABLES = (
    "catalog_import_batches", "catalog_import_records", "catalog_review_items", "catalog_audit_log",
    "ingredients", "ingredient_aliases", "brands", "products", "product_formulations", "product_skus",
)


async def _table_counts(pool):
    return {table: await pool.fetchval(f"SELECT count(*) FROM {table}") for table in _DRY_RUN_TABLES}


async def test_dry_run_loads_and_validates_the_dictionary(wave1b_source_id, catalog_admin_db_pool):
    """A dry run must exercise the SAME dictionary validation a real
    run would -- independent-review Blocker 1's own complaint was that
    the dictionary was previously never even loaded on a dry run."""
    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
        dictionary_path=REAL_DICTIONARY, actor="test", dry_run=True,
    )
    assert report.dictionary_entry_count > 0
    assert report.identities_total > 0
    assert report.preflight_ready is True
    assert report.unresolved_dictionary_gaps == []


async def test_dry_run_reports_unresolved_dictionary_gaps(wave1b_source_id, catalog_admin_db_pool, tmp_path):
    manifest_bytes = _synthetic_manifest_line(
        evidence_id="synthetic-dry-run-gap",
        ingredient_list_raw=["Water", "Totally Undictionaried Novel Compound XJ9"],
    ).encode("utf-8")
    dictionary_path = _write_dictionary(tmp_path, [{"canonical_name": "Water", "aliases": []}])

    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=manifest_bytes,
        dictionary_path=dictionary_path, actor="test", dry_run=True,
    )
    assert report.preflight_ready is False
    assert len(report.unresolved_dictionary_gaps) == 1
    assert report.unresolved_dictionary_gaps[0].identity_key == "totally undictionaried novel compound xj9"
    assert report.identities_already_in_catalog == 0  # clean DB -- "Water" only resolves via the dictionary
    assert report.identities_covered_by_dictionary == 1


async def test_dry_run_writes_nothing(wave1b_source_id, catalog_admin_db_pool):
    """Zero batches, records, ingredients, aliases, brands, products,
    formulations, SKUs, review items, and audit rows -- a dry run is
    provably read-only against every table this workflow could ever
    touch."""
    before = await _table_counts(catalog_admin_db_pool)

    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
        dictionary_path=REAL_DICTIONARY, actor="test", dry_run=True,
    )
    assert report.dry_run is True
    assert report.batch_id is None
    assert report.manifest_total_records > 0  # reported...
    assert report.records_imported == 0  # ...but nothing was actually imported

    after = await _table_counts(catalog_admin_db_pool)
    assert before == after


async def test_missing_dictionary_fails_before_any_catalog_batch_is_created(
    wave1b_source_id, catalog_admin_db_pool, tmp_path,
):
    before = await _table_counts(catalog_admin_db_pool)
    with pytest.raises(PopulationRejectedError) as exc_info:
        await populate_wave1b(
            catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
            dictionary_path=tmp_path / "does_not_exist.json", actor="test",
        )
    assert exc_info.value.code == "DICTIONARY_UNREADABLE"
    after = await _table_counts(catalog_admin_db_pool)
    assert before == after


async def test_invalid_json_dictionary_fails_before_any_catalog_batch_is_created(
    wave1b_source_id, catalog_admin_db_pool, tmp_path,
):
    bad_path = tmp_path / "ingredient_dictionary.json"
    bad_path.write_text("{not valid json")
    before = await _table_counts(catalog_admin_db_pool)
    with pytest.raises(PopulationRejectedError) as exc_info:
        await populate_wave1b(
            catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=REAL_MANIFEST.read_bytes(),
            dictionary_path=bad_path, actor="test",
        )
    assert exc_info.value.code == "DICTIONARY_INVALID"
    after = await _table_counts(catalog_admin_db_pool)
    assert before == after


# ---------------------------------------------------------------------------
# Unresolved ingredients / non-fuzzy dictionary matching, synthetic data
# ---------------------------------------------------------------------------


def _synthetic_manifest_line(*, evidence_id, ingredient_list_raw, ingredient_details=None):
    record = {
        "source_evidence_id": evidence_id,
        "source_name": SOURCE_NAME,
        "source_type": "curated_dataset",
        "source_url": "https://www.cerave.com/skincare/example",
        "final_url": "https://www.cerave.com/skincare/example",
        "content_sha256": "a" * 64,
        "retrieved_at": "2026-09-18T00:00:00+00:00",
        "jurisdiction": "us",
        "brand": "CeraVe",
        "product_name": f"Synthetic Product {evidence_id}",
        "category": "cleanser",
        "product_url": "https://www.cerave.com/skincare/example",
        "formulation_version_evidence": None,
        "ingredient_list_raw": ingredient_list_raw,
        "ingredient_details": ingredient_details,
        "ingredient_list_complete": True,
        "ingredient_source": "manufacturer_website_disclosure",
        "verification_date": "2026-09-18",
        "upc": None, "gtin": None, "sku": None, "size_value": None, "size_unit": None,
        "notes": "synthetic Wave1B-shaped record for population-workflow testing",
    }
    return json.dumps(record) + "\n"


async def test_unresolved_ingredient_prevents_publication(wave1b_source_id, catalog_admin_db_pool, tmp_path):
    """An ingredient identity that has NO dictionary entry is left as
    an open, unresolved review item -- the record must stay
    NEEDS_REVIEW and must never be published, never guessed."""
    manifest_bytes = _synthetic_manifest_line(
        evidence_id="synthetic-unknown-ingredient",
        ingredient_list_raw=["Water", "Totally Undictionaried Novel Compound XJ9"],
    ).encode("utf-8")
    dictionary_path = _write_dictionary(tmp_path, [
        {"canonical_name": "Water", "ingredient_type": None, "inci_name": None, "aliases": []},
    ])

    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=manifest_bytes,
        dictionary_path=dictionary_path, actor="test",
    )
    assert report.records_validated == 0
    assert report.records_needing_review == 1
    assert len(report.unresolved_dictionary_gaps) == 1
    assert "totally undictionaried novel compound xj9" == report.unresolved_dictionary_gaps[0].identity_key
    assert report.publish_outcomes == []
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations") == 0


async def test_dictionary_matching_is_exact_never_fuzzy(wave1b_source_id, catalog_admin_db_pool, tmp_path):
    """A near-miss spelling (extra whitespace aside, which
    normalize_name already collapses) that is NOT an exact dictionary
    entry must never be silently accepted as a fuzzy match -- it is
    reported as a genuine dictionary gap."""
    manifest_bytes = _synthetic_manifest_line(
        evidence_id="synthetic-near-miss",
        ingredient_list_raw=["Glycerine"],  # dictionary only has "Glycerin" (no trailing e)
    ).encode("utf-8")
    dictionary_path = _write_dictionary(tmp_path, [
        {"canonical_name": "Glycerin", "ingredient_type": None, "inci_name": None, "aliases": []},
    ])

    report = await populate_wave1b(
        catalog_admin_db_pool, source_id=wave1b_source_id, manifest_bytes=manifest_bytes,
        dictionary_path=dictionary_path, actor="test",
    )
    assert report.records_needing_review == 1
    assert len(report.unresolved_dictionary_gaps) == 1
    assert report.publish_outcomes == []
