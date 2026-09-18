"""app.domain.catalog_source_adapter -- the Wave 1 CatalogSourceAdapter
boundary. Pure, in-memory -- no database access anywhere in this file
(PRODUCTION_CATALOG_WAVE_1.md's own "source manifest" and "source
architecture" sections)."""
import json

import pytest
from pydantic import ValidationError

from app.domain.catalog_normalization import normalize_record
from app.domain.catalog_source_adapter import (
    CuratedManifestSourceAdapter,
    SourceManifestRecord,
    compute_ingredient_fingerprint,
    group_key_for_record,
    group_manifest_records,
    is_reserved_test_source_name,
    manifest_record_has_sufficient_ingredient_evidence,
    manifest_record_to_raw_import_dict,
)


def _manifest_record(evidence_id="ev-1", **overrides):
    base = {
        "source_evidence_id": evidence_id,
        "source_name": "Unit Test Source",
        "source_type": "curated_dataset",
        "source_url": "https://example.invalid/source",
        "retrieved_at": "2026-09-17T00:00:00Z",
        "jurisdiction": "us",
        "brand": "Testonyx",
        "product_name": "Gentle Test Cleanser",
        "category": "cleanser",
        "product_url": "https://example.invalid/product",
        "formulation_version_evidence": None,
        "ingredient_list_raw": ["Water", "Glycerin", "Sodium Chloride"],
        "ingredient_list_complete": True,
        "ingredient_source": "manufacturer_label_text",
        "verification_date": "2026-09-17",
        "upc": None, "gtin": None, "sku": None, "size_value": None, "size_unit": None,
        "notes": "synthetic test fixture",
    }
    base.update(overrides)
    return base


def _jsonl(records):
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Manifest parsing / malformed-record isolation
# ---------------------------------------------------------------------------


def test_valid_manifest_parses_cleanly():
    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    result = adapter.parse(_jsonl([_manifest_record("a1"), _manifest_record("a2")]))
    assert result.total_records == 2
    assert len(result.valid_records) == 2
    assert result.issues == []


def test_malformed_jsonl_line_is_isolated_and_does_not_abort_the_manifest():
    body = _jsonl([_manifest_record("a1")]) + b"{not valid json\n" + _jsonl([_manifest_record("a2")])
    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    result = adapter.parse(body)
    assert result.total_records == 3
    assert len(result.valid_records) == 2
    assert any(i.code == "MALFORMED_JSON_LINE" for i in result.issues)


def test_non_object_record_is_isolated():
    body = b'"just a string"\n' + _jsonl([_manifest_record("a1")])
    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    result = adapter.parse(body)
    assert result.total_records == 2
    assert len(result.valid_records) == 1
    assert result.issues[0].code == "NON_OBJECT_RECORD"


def test_schema_invalid_record_is_isolated_not_fatal():
    bad = _manifest_record("bad-1", category="not_a_real_category")
    good = _manifest_record("good-1")
    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    result = adapter.parse(_jsonl([bad, good]))
    assert len(result.valid_records) == 1
    assert result.valid_records[0].source_evidence_id == "good-1"
    assert result.issues[0].code == "SCHEMA_VALIDATION_FAILED"
    assert result.issues[0].source_evidence_id == "bad-1"


def test_unrecognized_field_is_rejected_extra_forbid():
    with pytest.raises(ValidationError):
        SourceManifestRecord.model_validate({**_manifest_record(), "unexpected_field": "x"})


def test_json_array_format_also_supported():
    adapter = CuratedManifestSourceAdapter(file_format="json")
    result = adapter.parse(json.dumps([_manifest_record("a1"), _manifest_record("a2")]).encode())
    assert len(result.valid_records) == 2


# ---------------------------------------------------------------------------
# Ingredient ordering preservation
# ---------------------------------------------------------------------------


def test_ingredient_order_is_preserved_exactly():
    record = SourceManifestRecord.model_validate(
        _manifest_record(ingredient_list_raw=["Zinc Oxide", "Water", "Niacinamide", "Glycerin"])
    )
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    names_in_order = [i["raw_name"] for i in raw["ingredients"]]
    assert names_in_order == ["Zinc Oxide", "Water", "Niacinamide", "Glycerin"]
    positions = [i["position"] for i in raw["ingredients"]]
    assert positions == [1, 2, 3, 4]
    # And the mapped dict round-trips through the real, unmodified
    # normalization contract cleanly.
    normalized = normalize_record(raw)
    assert [i.raw_name for i in normalized.ingredients] == ["Zinc Oxide", "Water", "Niacinamide", "Glycerin"]


# ---------------------------------------------------------------------------
# Fingerprint determinism (deterministic reformulation-detection basis)
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_order_and_case_sensitive_appropriately():
    a = compute_ingredient_fingerprint(["Water", "Glycerin"])
    b = compute_ingredient_fingerprint(["Water", "Glycerin"])
    assert a == b
    # Whitespace/case differences collapse to the SAME fingerprint (same
    # normalize_name() rule every other identity lookup uses).
    c = compute_ingredient_fingerprint(["  water ", "GLYCERIN"])
    assert a == c
    # A genuinely different ingredient list produces a different one.
    d = compute_ingredient_fingerprint(["Water", "Niacinamide"])
    assert a != d
    # Reordering the same ingredients is a real, different disclosure.
    e = compute_ingredient_fingerprint(["Glycerin", "Water"])
    assert a != e


def test_explicit_formulation_version_evidence_wins_over_fingerprint():
    record = SourceManifestRecord.model_validate(_manifest_record(formulation_version_evidence="v2-2026"))
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    assert raw["formulation_version"] == "v2-2026"


def test_missing_version_evidence_uses_fingerprint():
    record = SourceManifestRecord.model_validate(_manifest_record(formulation_version_evidence=None))
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    assert raw["formulation_version"] == compute_ingredient_fingerprint(record.ingredient_list_raw)


# ---------------------------------------------------------------------------
# Source-type cross-check
# ---------------------------------------------------------------------------


def test_source_type_mismatch_is_rejected():
    record = SourceManifestRecord.model_validate(_manifest_record(source_type="curated_dataset"))
    with pytest.raises(Exception) as exc_info:
        manifest_record_to_raw_import_dict([record], registered_source_type="manufacturer_disclosure")
    assert getattr(exc_info.value, "code", None) == "SOURCE_TYPE_MISMATCH"


# ---------------------------------------------------------------------------
# SKU synthesis
# ---------------------------------------------------------------------------


def test_sku_is_synthesized_when_source_supplies_none():
    record = SourceManifestRecord.model_validate(_manifest_record(sku=None, upc=None, gtin=None))
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    assert raw["skus"][0]["sku"].startswith("WAVE1-")
    assert record.source_evidence_id in raw["skus"][0]["sku"]


def test_real_sku_is_used_when_provided():
    record = SourceManifestRecord.model_validate(_manifest_record(sku="REAL-BARCODE-123"))
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    assert raw["skus"][0]["sku"] == "REAL-BARCODE-123"


# ---------------------------------------------------------------------------
# Sufficiency predicate (INSUFFICIENT_SOURCE_DATA basis)
# ---------------------------------------------------------------------------


def test_empty_ingredient_list_is_insufficient():
    record = SourceManifestRecord.model_validate(
        _manifest_record(ingredient_list_raw=[], ingredient_list_complete=False)
    )
    assert manifest_record_has_sufficient_ingredient_evidence(record) is False


def test_incomplete_claim_with_ingredients_present_is_still_insufficient():
    record = SourceManifestRecord.model_validate(
        _manifest_record(ingredient_list_raw=["Water"], ingredient_list_complete=False)
    )
    assert manifest_record_has_sufficient_ingredient_evidence(record) is False


def test_complete_nonempty_ingredient_list_is_sufficient():
    record = SourceManifestRecord.model_validate(_manifest_record())
    assert manifest_record_has_sufficient_ingredient_evidence(record) is True


# ---------------------------------------------------------------------------
# Grouping / merge (duplicate product from same source, conflicting identity)
# ---------------------------------------------------------------------------


def test_same_formulation_two_records_group_together():
    a = SourceManifestRecord.model_validate(_manifest_record("v1", sku="SIZE-30ML"))
    b = SourceManifestRecord.model_validate(_manifest_record("v2", sku="SIZE-50ML"))
    assert group_key_for_record(a) == group_key_for_record(b)
    groups, issues = group_manifest_records([a, b])
    assert issues == []
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_grouped_records_merge_into_one_raw_dict_with_both_skus():
    a = SourceManifestRecord.model_validate(_manifest_record("v1", sku="SIZE-30ML"))
    b = SourceManifestRecord.model_validate(_manifest_record("v2", sku="SIZE-50ML"))
    groups, _ = group_manifest_records([a, b])
    raw = manifest_record_to_raw_import_dict(groups[0], registered_source_type="curated_dataset")
    assert raw["external_record_id"] == "v1"
    sku_values = sorted(s["sku"] for s in raw["skus"])
    assert sku_values == ["SIZE-30ML", "SIZE-50ML"]
    normalize_record(raw)  # still passes the real, unmodified contract


def test_different_formulation_content_is_not_merged():
    a = SourceManifestRecord.model_validate(_manifest_record("v1", ingredient_list_raw=["Water", "Glycerin"]))
    b = SourceManifestRecord.model_validate(_manifest_record("v2", ingredient_list_raw=["Water", "Niacinamide"]))
    groups, issues = group_manifest_records([a, b])
    assert issues == []
    assert len(groups) == 2


def test_conflicting_category_for_same_formulation_is_flagged_not_guessed():
    a = SourceManifestRecord.model_validate(_manifest_record("v1", category="cleanser"))
    b = SourceManifestRecord.model_validate(_manifest_record("v2", category="serum"))
    groups, issues = group_manifest_records([a, b])
    assert groups == []
    assert len(issues) == 2
    assert all(i.code == "CONFLICTING_PRODUCT_IDENTITY" for i in issues)


# ---------------------------------------------------------------------------
# Reserved test-source-name guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["test_wave1", "synthetic_catalog", "wave1_test_demo", "TEST_UPPER"])
def test_reserved_prefixes_are_detected(name):
    assert is_reserved_test_source_name(name) is True


@pytest.mark.parametrize("name", ["Acme Real Manufacturer Data", "smoke_test_source", "curated_wave1_prod"])
def test_non_reserved_names_pass(name):
    assert is_reserved_test_source_name(name) is False


# ---------------------------------------------------------------------------
# Provenance / no-PII-in-key style checks on the mapped payload
# ---------------------------------------------------------------------------


def test_source_reference_is_valid_json_carrying_full_provenance():
    record = SourceManifestRecord.model_validate(_manifest_record())
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    provenance = json.loads(raw["source_reference"])
    assert provenance["source_name"] == "Unit Test Source"
    assert provenance["source_url"] == "https://example.invalid/source"
    assert provenance["source_evidence_id"] == record.source_evidence_id
    assert provenance["verification_date"] == "2026-09-17"


def test_raw_import_dict_never_contains_a_field_normalized_import_record_does_not_declare():
    """extra="forbid" on NormalizedImportRecord means any leaked
    Wave-1-only field would fail normalize_record() outright for every
    single Wave 1 record -- this is the sharpest possible test of the
    field-mapping contract."""
    record = SourceManifestRecord.model_validate(_manifest_record())
    raw = manifest_record_to_raw_import_dict([record], registered_source_type="curated_dataset")
    normalize_record(raw)  # raises MalformedRecordError if this contract is ever violated
