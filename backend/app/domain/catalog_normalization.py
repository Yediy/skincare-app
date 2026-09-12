"""Typed internal normalized import contract for catalog ingestion
(CATALOG_INGESTION_ARCHITECTURE.md). One canonical shape every source
adapter (JSON/JSONL today; CSV/provider-specific adapters later --
see the module docstring on app/domain/catalog_ingestion_service.py
for the adapter seam) must produce before anything downstream
(identity resolution, ingredient resolution, validation, publication)
ever runs.

Deliberately conservative and fail-closed:
  - `source_type` is restricted to product_formulations' own existing
    five-value vocabulary (migration d70e5fc90775) -- never a second,
    differently-worded taxonomy for the same underlying concept.
  - `ingredient_list_complete` is a plain, explicit boolean the SOURCE
    itself must assert -- this module never infers it from ingredient
    count, position, or any other heuristic (see
    app/domain/catalog_publication_service.py for how this interacts
    with `ingredient_data_status`; a source claiming completeness is
    necessary but not sufficient for COMPLETE).
  - Every bound below (string lengths, ingredient/SKU counts) exists
    specifically so a malformed or hostile source file fails
    normalization deliberately (`MalformedRecordError`, itself
    Pydantic's own `ValidationError`) rather than being silently
    accepted, or accepted and then exhausting memory/storage -- see
    Section 19 ("untrusted input") of this pass's own brief.
"""
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Bounds -- real, deliberate limits (not fabricated precision), chosen
# to comfortably accommodate any genuine skincare product disclosure
# (a full INCI list rarely exceeds a few dozen entries; a product
# rarely ships in more than a handful of sizes/markets) while still
# being a hard stop against a hostile or corrupted source file.
MAX_SHORT_STRING = 255
MAX_LONG_STRING = 2000
MAX_INGREDIENTS_PER_RECORD = 200
MAX_SKUS_PER_RECORD = 50

# Reused verbatim from product_formulations' own CHECK constraint
# (migration d70e5fc90775) -- see this module's own docstring for why
# no second vocabulary is introduced for the same concept. Import-
# specific values other than these five are ingestion-*source*
# classifications (catalog_sources.source_type, migration
# 2de8380d3618), a different, coarser concept -- never confused with
# this, the *formulation's own* evidentiary classification.
VALID_FORMULATION_SOURCE_TYPES = frozenset({
    "manufacturer_label",
    "manufacturer_disclosure",
    "regulatory_filing",
    "third_party_verified",
    "user_submitted_unverified",
})


class MalformedRecordError(ValueError):
    """Raised by normalize_record() when a raw source record cannot be
    normalized at all -- malformed shape, missing required field, a
    bound exceeded, or an unsupported value. Carries the underlying
    Pydantic ValidationError's own structured `.errors()` so a caller
    can persist genuinely useful `validation_errors` rather than a bare
    string."""

    def __init__(self, message: str, *, errors: Optional[list] = None):
        super().__init__(message)
        self.errors = errors or []


class NormalizedIngredient(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    raw_name: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    position: int = Field(ge=1)
    # NULL is a real, meaningful value ("undisclosed") -- never
    # fabricated. Matches formulation_ingredients.declared_concentration's
    # own NUMERIC(6, 3) precision/range (migration d70e5fc90775).
    declared_concentration: Optional[float] = Field(default=None, ge=0, le=100)
    concentration_unit: Optional[str] = Field(default=None, max_length=20)


class NormalizedSku(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sku: str = Field(min_length=1, max_length=100)
    upc_or_ean: Optional[str] = Field(default=None, max_length=50)
    size_value: Optional[float] = Field(default=None, ge=0)
    size_unit: Optional[str] = Field(default=None, max_length=20)
    market_or_region: str = Field(default="global", max_length=50)


class NormalizedImportRecord(BaseModel):
    """One source record, normalized. `external_record_id` is the
    source's own stable identifier for this record (never generated
    here) -- it is what makes cross-batch idempotency and
    reformulation detection possible (see
    app/domain/catalog_ingestion_service.py, app/domain/
    catalog_publication_service.py)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    external_record_id: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    brand_name: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    product_name: str = Field(min_length=1, max_length=MAX_SHORT_STRING)
    category: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=MAX_LONG_STRING)
    market_or_region: str = Field(default="global", max_length=50)
    formulation_version: str = Field(default="1", max_length=50)
    effective_from: Optional[date] = None
    source_type: str
    source_reference: Optional[str] = Field(default=None, max_length=MAX_LONG_STRING)

    # Explicit, source-asserted claim -- see module docstring. Never
    # inferred; `CatalogPublicationService` treats this as necessary,
    # not sufficient, for `ingredient_data_status = COMPLETE`.
    ingredient_list_complete: bool

    ingredients: List[NormalizedIngredient] = Field(default_factory=list)
    skus: List[NormalizedSku] = Field(min_length=1)

    @field_validator("source_type")
    @classmethod
    def _source_type_must_be_supported(cls, v: str) -> str:
        if v not in VALID_FORMULATION_SOURCE_TYPES:
            raise ValueError(
                f"unsupported source_type {v!r} -- must be one of {sorted(VALID_FORMULATION_SOURCE_TYPES)}"
            )
        return v

    @field_validator("ingredients")
    @classmethod
    def _bounded_ingredient_count(cls, v: List[NormalizedIngredient]) -> List[NormalizedIngredient]:
        if len(v) > MAX_INGREDIENTS_PER_RECORD:
            raise ValueError(f"ingredient count {len(v)} exceeds maximum {MAX_INGREDIENTS_PER_RECORD}")
        return v

    @field_validator("skus")
    @classmethod
    def _bounded_sku_count(cls, v: List[NormalizedSku]) -> List[NormalizedSku]:
        if len(v) > MAX_SKUS_PER_RECORD:
            raise ValueError(f"SKU count {len(v)} exceeds maximum {MAX_SKUS_PER_RECORD}")
        return v

    @model_validator(mode="after")
    def _ingredient_positions_contiguous_and_unique(self) -> "NormalizedImportRecord":
        """Structural soundness at normalization time -- independent
        of (and a prerequisite for) CatalogPublicationService's own
        pre-publish structural validation (Section 16), which re-checks
        this against the *resolved* ingredient set."""
        if not self.ingredients:
            return self
        positions = sorted(i.position for i in self.ingredients)
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError(
                f"ingredient positions must be contiguous starting at 1 -- got {positions}"
            )
        seen_names = set()
        for ingredient in self.ingredients:
            key = " ".join(ingredient.raw_name.split()).lower()
            if key in seen_names:
                raise ValueError(f"duplicate ingredient raw_name in one record: {ingredient.raw_name!r}")
            seen_names.add(key)
        return self


def normalize_record(raw_payload: dict) -> NormalizedImportRecord:
    """The one normalization entry point every source adapter calls.
    Raises MalformedRecordError (never lets a bare pydantic.
    ValidationError escape this module) on anything that doesn't fit
    the contract -- a malformed shape, a missing required field, a
    bound exceeded, or an unsupported value. Never raises on data that
    is merely *incomplete* in a business sense (missing ingredients,
    `ingredient_list_complete=False`) -- that is validation/review's
    job (app/domain/catalog_ingestion_service.py), not normalization's;
    normalization only rejects records it cannot even represent."""
    try:
        return NormalizedImportRecord.model_validate(raw_payload)
    except Exception as e:
        # Pydantic's ValidationError is the overwhelmingly common case
        # (and the one whose .errors() is preserved below), but a
        # non-dict raw_payload (e.g. a bare string or list where an
        # object was expected) raises a plain TypeError/AttributeError
        # from Pydantic's own coercion instead -- still a malformed
        # record, never allowed to propagate as an unhandled exception.
        errors = e.errors() if hasattr(e, "errors") else [{"msg": str(e)}]
        raise MalformedRecordError(f"record failed normalization: {e}", errors=errors) from e
