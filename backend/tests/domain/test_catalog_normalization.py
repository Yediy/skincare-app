"""app.domain.catalog_normalization -- pure, no database. Proves the
typed normalized import contract rejects malformed shapes deliberately
(MalformedRecordError, never a bare pydantic ValidationError escaping
this module) and preserves exactly what Section 22 requires: brand/
product normalization, ordered ingredients, null concentrations when
absent, and that `ingredient_list_complete` is read verbatim from the
source, never inferred.
"""
import pytest

from app.domain.catalog_normalization import MalformedRecordError, normalize_record

BASE_RECORD = {
    "external_record_id": "ext-1",
    "brand_name": "  Testonyx  ",
    "product_name": "Gentle Moisturizer",
    "category": "moisturizer",
    "market_or_region": "global",
    "formulation_version": "1",
    "source_type": "manufacturer_disclosure",
    "ingredient_list_complete": True,
    "ingredients": [
        {"raw_name": "Water", "position": 1},
        {"raw_name": "Glycerin", "position": 2, "declared_concentration": 5.0, "concentration_unit": "%"},
    ],
    "skus": [{"sku": "TNX-001"}],
}


def test_valid_record_normalizes():
    record = normalize_record(BASE_RECORD)
    assert record.brand_name == "Testonyx"
    assert record.ingredient_list_complete is True
    assert [i.raw_name for i in record.ingredients] == ["Water", "Glycerin"]


def test_ordered_ingredients_preserved():
    record = normalize_record(BASE_RECORD)
    assert [i.position for i in record.ingredients] == [1, 2]


def test_concentration_remains_null_when_absent():
    record = normalize_record(BASE_RECORD)
    assert record.ingredients[0].declared_concentration is None
    assert record.ingredients[1].declared_concentration == 5.0


def test_ingredient_list_complete_read_verbatim_never_inferred():
    """A record with exactly one ingredient and ingredient_list_complete=False
    must stay False -- never flipped to True merely because a list
    exists at all."""
    raw = {**BASE_RECORD, "ingredients": [{"raw_name": "Water", "position": 1}], "ingredient_list_complete": False}
    record = normalize_record(raw)
    assert record.ingredient_list_complete is False


def test_missing_required_field_is_malformed():
    raw = {k: v for k, v in BASE_RECORD.items() if k != "brand_name"}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_missing_skus_is_malformed():
    """`skus: List[NormalizedSku] = Field(min_length=1)` -- a record
    with zero SKUs cannot be resolved to a purchasable product at all."""
    raw = {**BASE_RECORD, "skus": []}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_unsupported_source_type_is_malformed():
    raw = {**BASE_RECORD, "source_type": "influencer_tiktok_video"}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_extra_unknown_field_is_malformed():
    """extra='forbid' -- a source sending a field this contract doesn't
    know about fails deliberately rather than silently ignoring it
    (which could hide a typo'd field name that was supposed to matter)."""
    raw = {**BASE_RECORD, "totally_unexpected_field": "surprise"}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_non_contiguous_positions_is_malformed():
    raw = {**BASE_RECORD, "ingredients": [
        {"raw_name": "Water", "position": 1},
        {"raw_name": "Glycerin", "position": 3},
    ]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_duplicate_position_is_malformed():
    raw = {**BASE_RECORD, "ingredients": [
        {"raw_name": "Water", "position": 1},
        {"raw_name": "Glycerin", "position": 1},
    ]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_duplicate_raw_ingredient_name_is_malformed():
    raw = {**BASE_RECORD, "ingredients": [
        {"raw_name": "Water", "position": 1},
        {"raw_name": "  water ", "position": 2},
    ]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_zero_ingredients_is_not_malformed():
    """An empty ingredient list is a real, honest state (UNKNOWN
    completeness downstream) -- not itself a malformed shape."""
    raw = {**BASE_RECORD, "ingredients": []}
    record = normalize_record(raw)
    assert record.ingredients == []


@pytest.mark.parametrize("count", [201])
def test_too_many_ingredients_is_malformed(count):
    raw = {**BASE_RECORD, "ingredients": [
        {"raw_name": f"Ingredient {i}", "position": i + 1} for i in range(count)
    ]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_too_many_skus_is_malformed():
    raw = {**BASE_RECORD, "skus": [{"sku": f"SKU-{i}"} for i in range(51)]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_oversized_string_field_is_malformed():
    raw = {**BASE_RECORD, "brand_name": "x" * 1000}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_negative_declared_concentration_is_malformed():
    raw = {**BASE_RECORD, "ingredients": [{"raw_name": "Water", "position": 1, "declared_concentration": -1}]}
    with pytest.raises(MalformedRecordError):
        normalize_record(raw)


def test_malformed_record_error_carries_structured_errors():
    raw = {k: v for k, v in BASE_RECORD.items() if k != "brand_name"}
    with pytest.raises(MalformedRecordError) as exc_info:
        normalize_record(raw)
    assert len(exc_info.value.errors) >= 1
    assert any("brand_name" in str(e.get("loc", "")) for e in exc_info.value.errors)


def test_non_dict_payload_is_malformed():
    with pytest.raises(MalformedRecordError):
        normalize_record("not a dict")  # type: ignore[arg-type]
