"""Production Catalog Wave 1 -- the `CatalogSourceAdapter` boundary
(PRODUCTION_CATALOG_WAVE_1.md).

This module is the ONLY new layer Wave 1 adds above the existing,
unmodified ingestion pipeline (CATALOG_INGESTION_ARCHITECTURE.md).
Nothing here talks to a database, a network socket, or a web page --
it is a pure, in-memory transform from a Wave 1 source manifest (bytes
an operator already has in hand, exactly like
`CatalogIngestionService.import_file()`'s own existing contract) into
the same raw `dict` shape `app.domain.catalog_normalization.
normalize_record()` already accepts. Everything below that seam --
immutable raw-evidence persistence, normalization, identity/ingredient
resolution, human review, atomic publication -- is reused verbatim,
never duplicated or modified.

`CatalogSourceAdapter` is deliberately a narrow ABC (one method,
`parse()`) so a future adapter (a manufacturer-hosted structured-data/
API adapter, explicitly optional and DEFERRED this pass -- see
PRODUCTION_CATALOG_WAVE_1.md's "What this pass does NOT build") is a
new subclass, not a change to anything below it. This pass ships
exactly one concrete adapter, `CuratedManifestSourceAdapter`, for a
curated JSON/JSONL manifest an operator (or a separate, out-of-scope
acquisition process) has already produced -- never a web crawler, never
arbitrary-URL fetching, never retailer-page scraping. See the module
docstring on `app.domain.catalog_ingestion_service` for the identical
"bytes already in hand" posture this pass's own adapter shares.

Field-mapping contract (Wave 1 manifest record -> raw import dict):

  source_evidence_id  -> external_record_id (the one stable identity
                         every idempotency/reformulation guarantee in
                         the existing pipeline already keys off of)
  brand                -> brand_name
  product_name          -> product_name
  category              -> category (Wave 1's own controlled
                         vocabulary -- WAVE1_TARGET_CATEGORIES below)
  notes                 -> description
  jurisdiction           -> market_or_region (defaults "global")
  formulation_version_evidence, or a deterministic ingredient-content
                         fingerprint when absent -> formulation_version
                         (see `compute_ingredient_fingerprint()` --
                         this IS this pass's "deterministic ingredient/
                         formulation fingerprinting", and it is the
                         SAME field `CatalogPublicationService.publish()`
                         already compares to detect a reformulation --
                         no new fingerprinting mechanism was invented)
  ingredient_source      -> source_type (mapped through
                         INGREDIENT_SOURCE_TO_FORMULATION_SOURCE_TYPE
                         into product_formulations' own existing
                         five-value vocabulary -- never a second one)
  ingredient_list_raw     -> ingredients (order preserved exactly:
                         position = list index + 1)
  ingredient_list_complete -> ingredient_list_complete (passed through
                         verbatim -- never inferred, per the existing
                         normalization contract's own rule)
  upc/gtin/sku/size_value/size_unit -> skus[0] (a placeholder SKU is
                         synthesized, clearly marked, only when the
                         source supplies none of these -- see
                         `_build_sku()`)

Everything else the manifest carries that has no first-class field on
`NormalizedImportRecord` (source_name, source_url, product_url,
retrieved_at, verification_date, jurisdiction, ingredient_source,
source_evidence_id again) is packed into `source_reference` as a
compact, sorted-key JSON string -- the one free-text field the existing
contract already provides for exactly this purpose. Nothing is lost:
`source_reference` is persisted verbatim onto both `catalog_import_
records.raw_payload` (immutable) and, at publish time, `product_
formulations.source_reference` / `catalog_formulation_provenance.
source_reference` -- reusing existing structures exactly as this pass's
brief instructs, never a new provenance table/column.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.catalog_repository import normalize_name
from app.domain.catalog_normalization import MAX_LONG_STRING, MAX_SHORT_STRING

# Wave 1's own controlled category vocabulary (PRODUCTION_CATALOG_
# WAVE_1.md "Target initial coverage"). Deliberately NOT the same
# object as app.domain.product_safety.CATEGORY_SAFETY_PROFILES -- that
# vocabulary belongs to PlanService's category-level ranking, a
# separate, pre-existing concern this pass does not touch. Three of
# these seven values ("cleanser", "moisturizer", "sunscreen") happen to
# already overlap it; the other four import/publish successfully but
# are not yet reachable by any recommendation until PlanService's own
# vocabulary is extended -- an honest, documented gap (see
# PRODUCTION_CATALOG_WAVE_1.md), never silently papered over here.
WAVE1_TARGET_CATEGORIES = frozenset({
    "cleanser",
    "moisturizer",
    "sunscreen",
    "acne_treatment",
    "serum",
    "exfoliant",
    "barrier_repair",
})

# Closed, Wave-1-specific vocabulary for how a manifest record's
# ingredient list was actually obtained -- distinct from (and mapped
# INTO) NormalizedImportRecord's own five-value `source_type`
# vocabulary, which this module never extends or duplicates.
INGREDIENT_SOURCE_TO_FORMULATION_SOURCE_TYPE: Dict[str, str] = {
    "manufacturer_label_text": "manufacturer_label",
    "manufacturer_website_disclosure": "manufacturer_disclosure",
    "manufacturer_pdf_or_sds": "manufacturer_disclosure",
    "regulatory_filing": "regulatory_filing",
    "third_party_verified_database": "third_party_verified",
    "retailer_listing_text": "user_submitted_unverified",
    "unverified": "user_submitted_unverified",
}
VALID_INGREDIENT_SOURCES = frozenset(INGREDIENT_SOURCE_TO_FORMULATION_SOURCE_TYPE)

# Reused verbatim from catalog_sources' own existing CHECK constraint
# (migration 2de8380d3618) -- the manifest's own `source_type` field
# describes the REGISTERED SOURCE this manifest is meant to be
# imported under (cross-checked against the real catalog_sources row
# at import time -- see catalog_wave_service.import_manifest()), never
# a second, differently-scoped taxonomy.
VALID_MANIFEST_SOURCE_TYPES = frozenset({
    "manufacturer_label", "manufacturer_disclosure", "regulatory_filing",
    "third_party_verified", "user_submitted_unverified", "curated_dataset",
})

# A curated manifest imported under a source whose registered name
# starts with one of these prefixes is refused unless the caller
# explicitly opts in (test code only -- see
# catalog_wave_service.import_manifest()'s `allow_test_source`
# parameter) -- "production data cannot masquerade as test fixture,"
# enforced as a real technical guard, not just a naming convention.
RESERVED_TEST_SOURCE_NAME_PREFIXES = ("test_", "synthetic_", "wave1_test_")


def is_reserved_test_source_name(name: str) -> bool:
    normalized = name.strip().lower()
    return any(normalized.startswith(prefix) for prefix in RESERVED_TEST_SOURCE_NAME_PREFIXES)


def compute_ingredient_fingerprint(ingredient_list_raw: List[str]) -> str:
    """Deterministic content fingerprint of an ordered raw ingredient
    list -- same list (modulo whitespace/case, via the same
    `normalize_name` every other identity lookup in this codebase
    uses) always produces the same fingerprint; any real ingredient
    change produces a different one. Used as `formulation_version`
    when a manifest record supplies no explicit
    `formulation_version_evidence` -- this is what makes reformulation
    detection automatic through the EXISTING, unmodified
    `CatalogPublicationService.publish()` (it already compares the
    incoming `formulation_version` against the currently-published
    formulation's own `version`; a changed fingerprint is exactly a
    "different version" to that unchanged code, correctly routing
    through the existing supersede-and-publish reformulation path).
    Order matters and is part of the fingerprint -- reordering the
    same ingredients is, correctly, a different disclosure."""
    canonical = "\n".join(normalize_name(name) for name in ingredient_list_raw)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"fp-{digest[:16]}"


class ManifestRecordError(ValueError):
    """Raised for one manifest record that fails Wave 1's own schema
    (distinct from a record that parses fine but lacks sufficient
    ingredient evidence -- see
    `manifest_record_has_sufficient_ingredient_evidence()`, which is
    not an error, just a classification). Carries enough context for a
    caller to build a `ManifestRecordIssue` without re-deriving it."""

    def __init__(self, message: str, *, code: str, index: int, source_evidence_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.index = index
        self.source_evidence_id = source_evidence_id


class SourceManifestRecord(BaseModel):
    """One row of a Wave 1 curated source manifest -- the schema this
    pass's brief asks for verbatim (source_name/source_type/source_url/
    retrieved_at/jurisdiction/brand/product_name/product_url/
    formulation-version-evidence/ingredient_list_raw/ingredient_source/
    verification_date/optional UPC-GTIN-SKU/optional size-variant/
    notes/source-evidence-identifier), plus `category` (required to
    route into the existing catalog's own `products.category`) and
    `ingredient_list_complete` (required, explicit, never inferred --
    same rule `NormalizedImportRecord` itself already enforces one
    layer down).

    `extra="forbid"`: an unrecognized manifest field fails the WHOLE
    record deliberately (never silently ignored) -- same fail-closed
    posture `NormalizedImportRecord` itself uses.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # -- source-evidence identity --
    source_evidence_id: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    source_name: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    source_type: str
    source_url: Optional[str] = Field(default=None, max_length=MAX_LONG_STRING)
    retrieved_at: datetime
    jurisdiction: Optional[str] = Field(default=None, max_length=50)

    # -- product identity --
    brand: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    product_name: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    category: str
    product_url: Optional[str] = Field(default=None, max_length=MAX_LONG_STRING)

    # -- formulation evidence --
    formulation_version_evidence: Optional[str] = Field(default=None, max_length=50)
    ingredient_list_raw: List[str] = Field(default_factory=list)
    ingredient_list_complete: bool
    ingredient_source: str
    verification_date: date

    # -- optional identifiers / variant metadata --
    upc: Optional[str] = Field(default=None, max_length=50)
    gtin: Optional[str] = Field(default=None, max_length=50)
    sku: Optional[str] = Field(default=None, max_length=100)
    size_value: Optional[float] = Field(default=None, ge=0)
    size_unit: Optional[str] = Field(default=None, max_length=20)

    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("category")
    @classmethod
    def _category_in_wave1_vocabulary(cls, v: str) -> str:
        if v not in WAVE1_TARGET_CATEGORIES:
            raise ValueError(f"category {v!r} is not a Wave 1 target category -- must be one of {sorted(WAVE1_TARGET_CATEGORIES)}")
        return v

    @field_validator("ingredient_source")
    @classmethod
    def _ingredient_source_known(cls, v: str) -> str:
        if v not in VALID_INGREDIENT_SOURCES:
            raise ValueError(f"ingredient_source {v!r} is not recognized -- must be one of {sorted(VALID_INGREDIENT_SOURCES)}")
        return v

    @field_validator("source_type")
    @classmethod
    def _source_type_known(cls, v: str) -> str:
        if v not in VALID_MANIFEST_SOURCE_TYPES:
            raise ValueError(f"source_type {v!r} is not recognized -- must be one of {sorted(VALID_MANIFEST_SOURCE_TYPES)}")
        return v

    @field_validator("ingredient_list_raw")
    @classmethod
    def _ingredients_bounded_and_nonblank(cls, v: List[str]) -> List[str]:
        if len(v) > 200:
            raise ValueError(f"ingredient_list_raw has {len(v)} entries, exceeds the 200-entry bound")
        for entry in v:
            if not entry or not entry.strip():
                raise ValueError("ingredient_list_raw entries must be non-blank -- an empty placeholder is never a real ingredient")
        return v


def manifest_record_has_sufficient_ingredient_evidence(record: SourceManifestRecord) -> bool:
    """The one, canonical Wave 1 predicate for "this record has enough
    to ever be verified" -- reused identically by manifest validation
    reporting (`catalog_wave_service.validate_manifest_bytes`) and by
    post-import reporting against durable DB state
    (`catalog_wave_report`, re-derived from `normalized_payload`, never
    a second copy of this rule). A record failing this is classified
    INSUFFICIENT_SOURCE_DATA -- it may still be imported (raw evidence
    is never discarded), but it can never be reported VERIFIED, and an
    operator following PRODUCTION_CATALOG_WAVE_1.md's own documented
    workflow never runs `publish` against it."""
    return len(record.ingredient_list_raw) > 0 and record.ingredient_list_complete is True


def _build_sku(record: SourceManifestRecord) -> Dict[str, Any]:
    """Real UPC/GTIN/SKU wins if the source supplied any of them
    (preferring an explicit SKU, then UPC, then GTIN). Only when the
    source supplied NONE of them is a placeholder synthesized -- always
    prefixed `WAVE1-`, so it can never be mistaken for a real retailer/
    manufacturer identifier downstream, and always derived from the
    record's own `source_evidence_id` (stable, deterministic -- the
    same manifest record always synthesizes the same placeholder)."""
    sku_value = record.sku or record.upc or record.gtin or f"WAVE1-{record.source_evidence_id}"
    return {
        "sku": sku_value,
        "upc_or_ean": record.upc or record.gtin,
        "size_value": record.size_value,
        "size_unit": record.size_unit,
        "market_or_region": record.jurisdiction or "global",
    }


def group_key_for_record(record: SourceManifestRecord) -> tuple:
    """Groups manifest records that describe the SAME formulation --
    same brand/product/market, and the SAME formulation content (either
    an identical explicit `formulation_version_evidence`, or an
    identical ingredient-content fingerprint when the source gives no
    explicit version). Multiple manifest rows sharing this key
    represent SKU/size *variants* of one formulation (this pass's own
    "duplicate product from same source" scenario) -- merged into ONE
    raw import record with multiple `skus`, never imported as separate,
    spuriously conflicting formulations. A genuinely different
    formulation (different ingredients, no matching version evidence)
    gets a different key and becomes its own group -- and, at publish
    time, the EXISTING reformulation/conflict logic in
    `CatalogPublicationService.publish()` decides what that means,
    unmodified."""
    fingerprint = record.formulation_version_evidence or compute_ingredient_fingerprint(record.ingredient_list_raw)
    return (
        normalize_name(record.brand), normalize_name(record.product_name),
        record.jurisdiction or "global", fingerprint,
    )


def group_manifest_records(
    records: List[SourceManifestRecord],
) -> tuple[List[List[SourceManifestRecord]], List[ManifestRecordIssue]]:
    """Groups schema-valid records by `group_key_for_record`, preserving
    first-seen order both across groups and within a group. A group
    whose members disagree on `category` is a genuine identity conflict
    (this pass's own "conflicting product identity" scenario) -- Wave 1
    never guesses which is right; every member of such a group is
    excluded and surfaced as a `CONFLICTING_PRODUCT_IDENTITY` issue
    instead, exactly the "never convert uncertain identity resolution
    into guessed catalog identity" rule this pass's brief states."""
    groups: Dict[tuple, List[SourceManifestRecord]] = {}
    order: List[tuple] = []
    for record in records:
        key = group_key_for_record(record)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)

    clean_groups: List[List[SourceManifestRecord]] = []
    issues: List[ManifestRecordIssue] = []
    for key in order:
        group = groups[key]
        categories = {r.category for r in group}
        if len(categories) > 1:
            for r in group:
                issues.append(ManifestRecordIssue(
                    index=-1, source_evidence_id=r.source_evidence_id, code="CONFLICTING_PRODUCT_IDENTITY",
                    detail=(
                        f"records for {r.brand!r}/{r.product_name!r} with the same formulation content "
                        f"disagree on category: {sorted(categories)}"
                    ),
                ))
            continue
        clean_groups.append(group)
    return clean_groups, issues


def manifest_record_to_raw_import_dict(group: List[SourceManifestRecord], *, registered_source_type: str) -> Dict[str, Any]:
    """The core field-mapping transform -- see this module's own
    docstring for the full contract. `group` is one or more manifest
    records sharing `group_key_for_record` (SKU/size variants of the
    same formulation -- see `group_manifest_records`); the first
    member is representative for every field except `skus`, which
    merges every member's own SKU (deduplicated by SKU value, first
    occurrence wins). Returns a dict containing ONLY the fields
    `app.domain.catalog_normalization.NormalizedImportRecord` accepts
    (that model is `extra="forbid"`, and this exact dict is what
    becomes `catalog_import_records.raw_payload` verbatim -- see
    `app.domain.catalog_ingestion_service._ingest_one_raw_record`), so
    nothing here may add a field that model doesn't already declare.

    Raises `ManifestRecordError` (`SOURCE_TYPE_MISMATCH`) if any
    member's own declared `source_type` doesn't match the
    catalog_sources row it's actually being imported under -- a real,
    load-bearing cross-check (see this module's docstring), not a
    decorative field."""
    record = group[0]
    for member in group:
        if member.source_type != registered_source_type:
            raise ManifestRecordError(
                f"manifest record declares source_type {member.source_type!r} but is being imported "
                f"under a source registered as {registered_source_type!r}",
                code="SOURCE_TYPE_MISMATCH", index=-1, source_evidence_id=member.source_evidence_id,
            )

    formulation_version = record.formulation_version_evidence or compute_ingredient_fingerprint(record.ingredient_list_raw)
    formulation_source_type = INGREDIENT_SOURCE_TO_FORMULATION_SOURCE_TYPE[record.ingredient_source]

    provenance = {
        "wave1_manifest_schema_version": 1,
        "source_name": record.source_name,
        "source_url": record.source_url,
        "product_url": record.product_url,
        "retrieved_at": record.retrieved_at.isoformat(),
        "verification_date": record.verification_date.isoformat(),
        "ingredient_source": record.ingredient_source,
        "jurisdiction": record.jurisdiction,
        "source_evidence_id": record.source_evidence_id,
        "merged_evidence_ids": [m.source_evidence_id for m in group[1:]] or None,
    }
    source_reference = json.dumps(provenance, sort_keys=True)
    if len(source_reference) > MAX_LONG_STRING:
        # Never silently truncated -- truncating provenance is exactly
        # the "silently normalize away uncertainty" this pass's brief
        # forbids. A record whose evidence genuinely doesn't fit the
        # existing free-text field fails deliberately instead.
        raise ManifestRecordError(
            f"provenance for {record.source_evidence_id!r} is {len(source_reference)} chars, "
            f"exceeds the {MAX_LONG_STRING}-char source_reference bound",
            code="PROVENANCE_TOO_LARGE", index=-1, source_evidence_id=record.source_evidence_id,
        )

    skus: List[Dict[str, Any]] = []
    seen_skus = set()
    for member in group:
        sku = _build_sku(member)
        if sku["sku"] in seen_skus:
            continue
        seen_skus.add(sku["sku"])
        skus.append(sku)

    return {
        "external_record_id": record.source_evidence_id,
        "brand_name": record.brand,
        "product_name": record.product_name,
        "category": record.category,
        "description": record.notes,
        "market_or_region": record.jurisdiction or "global",
        "formulation_version": formulation_version,
        "source_type": formulation_source_type,
        "source_reference": source_reference,
        "ingredient_list_complete": record.ingredient_list_complete,
        "ingredients": [
            {"raw_name": name, "position": i + 1} for i, name in enumerate(record.ingredient_list_raw)
        ],
        "skus": skus,
    }


@dataclass
class ManifestRecordIssue:
    index: int
    source_evidence_id: Optional[str]
    code: str
    detail: str


@dataclass
class ManifestParseResult:
    total_records: int
    valid_records: List[SourceManifestRecord] = field(default_factory=list)
    issues: List[ManifestRecordIssue] = field(default_factory=list)


class CatalogSourceAdapter(ABC):
    """Provider/source acquisition boundary (this pass's brief, Section
    "Source architecture"). A concrete adapter's only job is turning
    bytes an operator already has in hand into `SourceManifestRecord`s
    -- never fetching those bytes itself. A future manufacturer-
    hosted structured-data/API adapter (explicitly optional, DEFERRED
    this pass) would be a new subclass here, still handed bytes the
    caller already retrieved through its own, separately-reviewed
    acquisition step -- this ABC's contract does not change."""

    @abstractmethod
    def parse(self, raw_bytes: bytes) -> ManifestParseResult:
        raise NotImplementedError


def _split_json_or_jsonl(raw_bytes: bytes, file_format: str) -> List[Any]:
    """Independent of (never importing from)
    `catalog_ingestion_service._parse_raw_records` -- that function is
    private to a different contract (raw dicts destined directly for
    `normalize_record()`); this one produces raw JSON *values* destined
    for `SourceManifestRecord` validation, a genuinely different
    downstream shape. Same tolerant behavior: a malformed whole-file
    JSON document is a hard failure (nothing to salvage); one malformed
    JSONL line does not abort its siblings, surfaced as its own
    MALFORMED_JSON_LINE issue by the caller."""
    if file_format == "json":
        parsed = json.loads(raw_bytes)
        if not isinstance(parsed, list):
            raise ManifestRecordError(
                "manifest JSON format requires a top-level array of record objects",
                code="MALFORMED_MANIFEST_FILE", index=-1,
            )
        return parsed
    if file_format == "jsonl":
        values: List[Any] = []
        for line_number, line in enumerate(raw_bytes.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                values.append(json.loads(stripped))
            except (json.JSONDecodeError, UnicodeDecodeError):
                values.append({"_malformed_manifest_line": line_number})
        return values
    raise ManifestRecordError(f"unsupported manifest file_format {file_format!r}", code="UNSUPPORTED_FORMAT", index=-1)


class CuratedManifestSourceAdapter(CatalogSourceAdapter):
    """Wave 1's one initial supported source type: a curated structured
    JSON/JSONL manifest (this pass's brief, "Initial supported source
    type"). Every record's raw ingredient order is preserved exactly
    (list-index order, never re-sorted); a record failing Wave 1's own
    schema is isolated as a `ManifestRecordIssue` and does not abort
    its siblings, matching the existing pipeline's own per-record
    isolation guarantee one layer down."""

    def __init__(self, *, file_format: str = "jsonl"):
        self._file_format = file_format

    def parse(self, raw_bytes: bytes) -> ManifestParseResult:
        raw_values = _split_json_or_jsonl(raw_bytes, self._file_format)
        valid_records: List[SourceManifestRecord] = []
        issues: List[ManifestRecordIssue] = []

        for index, raw_value in enumerate(raw_values):
            if isinstance(raw_value, dict) and set(raw_value.keys()) == {"_malformed_manifest_line"}:
                issues.append(ManifestRecordIssue(
                    index=index, source_evidence_id=None, code="MALFORMED_JSON_LINE",
                    detail=f"line {raw_value['_malformed_manifest_line']} is not valid JSON",
                ))
                continue
            if not isinstance(raw_value, dict):
                issues.append(ManifestRecordIssue(
                    index=index, source_evidence_id=None, code="NON_OBJECT_RECORD",
                    detail=f"record position holds a {type(raw_value).__name__}, not an object",
                ))
                continue
            source_evidence_id = raw_value.get("source_evidence_id")
            try:
                record = SourceManifestRecord.model_validate(raw_value)
            except Exception as e:
                errors = e.errors() if hasattr(e, "errors") else [{"msg": str(e)}]
                issues.append(ManifestRecordIssue(
                    index=index, source_evidence_id=str(source_evidence_id) if source_evidence_id else None,
                    code="SCHEMA_VALIDATION_FAILED", detail=json.dumps(errors, default=str),
                ))
                continue
            valid_records.append(record)

        return ManifestParseResult(total_records=len(raw_values), valid_records=valid_records, issues=issues)
