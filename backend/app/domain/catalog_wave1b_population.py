"""Production Catalog Wave 1B -- the reproducible, idempotent,
dry-run-capable operator population workflow (independent-review
Blocker 4, PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md).

Why this module exists: an earlier pass demonstrated Wave 1B
end-to-end by pre-seeding the `ingredients` table directly with every
raw ingredient string the acquired manifest disclosed, bypassing the
existing human-review queue entirely. That is not a reproducible
production population procedure -- it cannot be re-run from a clean
database by anyone reading this repository, it duplicates a raw string
straight into `ingredients.canonical_name` (recreating exactly the
"concentration baked into identity" problem this pass's own Blocker 3
fixes), and it never actually exercises `CatalogReviewService`'s own
audited resolution path at all.

This module runs a single, deterministic sequence, reusing EVERY
existing Wave 1/Wave 1A component completely unmodified
(`catalog_wave_service.import_manifest`, `CatalogIngestionService`,
`CatalogReviewService`, `CatalogPublicationService`):

    0. PREFLIGHT (read-only -- no batch, no import record, no
       ingredient, no alias is ever created here): load and fully
       validate the ingredient dictionary, schema-validate the
       manifest, and check every distinct ingredient identity the
       manifest discloses against the dictionary AND the current
       catalog -- BEFORE any mutating step ever runs (independent-
       review Blocker 1: a broken dictionary, or a manifest with
       genuine coverage gaps, must be discoverable without ever
       touching the database, and a dry run must exercise this exact
       same check, not skip past it).
    1. import the manifest (idempotent -- re-importing byte-identical
       content resolves to the SAME batch, no duplicate records)
    2. validate + resolve ingredients ONE RECORD AT A TIME, against the
       same dictionary already loaded in step 0 (never fuzzy matching,
       never an invented alias)
    3. report what remains genuinely unresolved (a dictionary gap) or
       needing non-ingredient human review
    4. publish only records that reach VALIDATED

`dry_run=True` returns immediately after step 0 -- the dictionary is
always loaded and validated, and the coverage report is always
computed, even on a dry run; nothing from step 1 onward ever runs, so
the database is provably untouched.

Why "one record at a time" (Section "Interleaved validation"): Wave
1A's own `CatalogReviewService.map_ingredient()`/`create_ingredient()`
deliberately, and correctly, refuse to act when their target raw
string ALREADY resolves to something (`ALREADY_RESOLVED` --
independent review's own hardened "fails closed even when the
out-of-band resolution matches the requested target" guarantee,
`tests/domain/test_catalog_review_service.py`). Calling
`CatalogIngestionService.validate_batch()` once, in bulk, over all N
records BEFORE resolving anything, would open N separate review items
for N products that all independently disclose the same common
ingredient (e.g. "Water" on 8 of 11 real Wave 1B products) -- and
after resolving the FIRST one, every other product's identical-raw-
string review item becomes permanently stuck: `map_ingredient()` on it
now correctly refuses (per that same hardened guarantee) because the
identity has already, genuinely, resolved.

Processing records ONE AT A TIME and resolving each record's own newly
discovered unknown ingredients (via the dictionary) BEFORE validating
the next record sidesteps this cleanly, using existing machinery
exactly as designed: by the time product 2 is validated, "Water"
already resolves (product 1 resolved it moments earlier in this same
run), so `reconcile_review_state` -- the exact same canonical function
`validate_batch()` itself calls -- never creates a review item for it
on product 2 at all. No modification to any previously-hardened
review-service code was needed or made.

Idempotency: running this whole function twice against the same
target database is safe and does not duplicate catalog truth --
`import_manifest()` is idempotent by content hash, `reconcile_review_
state()` never recreates an already-resolved (reason_code,
identity_key) review item, and `CatalogPublicationService.publish()`
is itself an idempotent no-op for an already-PUBLISHED record. The
ingredient dictionary is applied via exact, normalized-string matching
only -- an ingredient identity with no dictionary entry is left OPEN
and reported, never guessed.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.db.catalog_repository import normalize_name, resolve_ingredient
from app.domain.catalog_publication_service import CatalogPublicationService, PublicationError
from app.domain.catalog_review_service import CatalogReviewService, ReviewError
from app.domain.catalog_source_adapter import CuratedManifestSourceAdapter
from app.domain.catalog_validation import reconcile_review_state
from app.domain.catalog_wave_service import WaveManifestRejectedError, import_manifest, validate_manifest_bytes


class PopulationRejectedError(Exception):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class IngredientDictionaryEntry:
    canonical_name: str
    ingredient_type: Optional[str]
    inci_name: Optional[str]
    aliases: List[str]


def load_ingredient_dictionary(path: Path) -> List[IngredientDictionaryEntry]:
    """Loads the committed, reviewable ingredient dictionary (Section
    "Canonical ingredient dictionary" of PRODUCTION_CATALOG_WAVE_1B_
    REAL_DATA.md). Called FIRST, before any database access at all
    (independent-review Blocker 1) -- every failure mode here is a pure
    function of the file's own bytes and raises a structured
    `PopulationRejectedError`, never a raw `AttributeError`/`KeyError`/
    `TypeError` escaping from malformed operator input, and never after
    a batch or import record has been created.

    Validates, in order: the file is readable (`DICTIONARY_UNREADABLE`);
    it parses as JSON and the top level is an object with an `entries`
    list (`DICTIONARY_INVALID`); the list is non-empty
    (`DICTIONARY_EMPTY` -- refusing to run a production population
    against an empty dictionary, which could never resolve anything);
    every entry is an object with a non-blank `canonical_name`, an
    `aliases` list of non-blank strings (if present at all), and a
    string-or-null `ingredient_type`/`inci_name` (`DICTIONARY_INVALID`);
    and no two entries claim overlapping normalized spellings -- whether
    a literal duplicate canonical definition, a conflicting alias, or an
    alias that collides with a DIFFERENT entry's own canonical name --
    all `DICTIONARY_AMBIGUOUS`, never silently resolved by "first one
    wins"."""
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as e:
        raise PopulationRejectedError(f"could not read ingredient dictionary {path}: {e}", code="DICTIONARY_UNREADABLE") from e

    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        raise PopulationRejectedError(f"ingredient dictionary {path} is not valid JSON: {e}", code="DICTIONARY_INVALID") from e

    if not isinstance(raw, dict):
        raise PopulationRejectedError(
            f"ingredient dictionary {path} must be a JSON object at the top level, got {type(raw).__name__}",
            code="DICTIONARY_INVALID",
        )
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise PopulationRejectedError(
            f"ingredient dictionary {path} must have a top-level 'entries' list", code="DICTIONARY_INVALID",
        )
    if not entries_raw:
        raise PopulationRejectedError(
            f"ingredient dictionary {path} has zero entries -- refusing to run a production population "
            "against an empty dictionary",
            code="DICTIONARY_EMPTY",
        )

    entries: List[IngredientDictionaryEntry] = []
    spelling_owner: Dict[str, int] = {}  # normalized spelling -> owning entry index

    for index, raw_entry in enumerate(entries_raw):
        if not isinstance(raw_entry, dict):
            raise PopulationRejectedError(
                f"ingredient dictionary entry {index} must be a JSON object, got {type(raw_entry).__name__}",
                code="DICTIONARY_INVALID",
            )

        canonical_name = raw_entry.get("canonical_name")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            raise PopulationRejectedError(
                f"ingredient dictionary entry {index} has a missing or blank canonical_name",
                code="DICTIONARY_INVALID",
            )

        raw_aliases = raw_entry.get("aliases", [])
        if raw_aliases is None:
            raw_aliases = []
        if not isinstance(raw_aliases, list) or not all(isinstance(a, str) and a.strip() for a in raw_aliases):
            raise PopulationRejectedError(
                f"ingredient dictionary entry {index} ({canonical_name!r}) has an invalid 'aliases' field -- "
                "must be a list of non-blank strings",
                code="DICTIONARY_INVALID",
            )

        ingredient_type = raw_entry.get("ingredient_type")
        if ingredient_type is not None and not isinstance(ingredient_type, str):
            raise PopulationRejectedError(
                f"ingredient dictionary entry {index} ({canonical_name!r}) has a non-string, non-null ingredient_type",
                code="DICTIONARY_INVALID",
            )
        inci_name = raw_entry.get("inci_name")
        if inci_name is not None and not isinstance(inci_name, str):
            raise PopulationRejectedError(
                f"ingredient dictionary entry {index} ({canonical_name!r}) has a non-string, non-null inci_name",
                code="DICTIONARY_INVALID",
            )

        entry = IngredientDictionaryEntry(
            canonical_name=canonical_name, ingredient_type=ingredient_type, inci_name=inci_name,
            aliases=list(raw_aliases),
        )

        for spelling in [canonical_name, *entry.aliases]:
            key = normalize_name(spelling)
            owner_index = spelling_owner.get(key)
            if owner_index is not None and owner_index != index:
                owner_name = entries[owner_index].canonical_name
                raise PopulationRejectedError(
                    f"ingredient dictionary entry {index} ({canonical_name!r}) claims spelling {spelling!r} "
                    f"(normalizes to {key!r}), which entry {owner_index} ({owner_name!r}) already claims -- "
                    "ambiguous dictionary, refusing to load",
                    code="DICTIONARY_AMBIGUOUS",
                )
            spelling_owner[key] = index

        entries.append(entry)

    return entries


def _dictionary_lookup(entries: List[IngredientDictionaryEntry], identity_key: str) -> Optional[IngredientDictionaryEntry]:
    """Exact, normalized-string matching only -- never fuzzy, never a
    substring/prefix match. `identity_key` is already normalized (see
    `catalog_validation.normalize_identity`, the same rule `normalize_
    name` applies)."""
    for entry in entries:
        if normalize_name(entry.canonical_name) == identity_key:
            return entry
        if any(normalize_name(alias) == identity_key for alias in entry.aliases):
            return entry
    return None


@dataclass
class CoverageGap:
    """One ingredient identity the manifest discloses that neither the
    current catalog nor the dictionary can resolve -- computed purely
    at preflight time (before any import), so an operator sees exactly
    this before deciding whether to proceed."""

    identity_key: str
    example_raw_name: str
    source_evidence_ids: List[str]


@dataclass
class PreflightResult:
    manifest_total_records: int
    manifest_schema_valid: int
    manifest_issues: List[Dict[str, Any]]
    identities_total: int
    identities_already_in_catalog: int
    identities_covered_by_dictionary: int
    gaps: List[CoverageGap]

    @property
    def ready(self) -> bool:
        """Independent-review Blocker 1: "a dry-run with unresolved
        dictionary gaps should not claim READY." `manifest_schema_valid
        == manifest_total_records` additionally requires every record
        in the file to have parsed as a genuine Wave 1 manifest row --
        a schema-invalid record is never silently excused."""
        return (
            self.manifest_total_records > 0
            and self.manifest_schema_valid == self.manifest_total_records
            and not self.gaps
        )


async def _run_preflight(
    pool: asyncpg.Pool, manifest_bytes: bytes, dictionary: List[IngredientDictionaryEntry],
) -> PreflightResult:
    """Read-only against every catalog table (`resolve_ingredient()` is
    an ordinary `SELECT`) -- no batch, import record, ingredient, alias,
    product, formulation, review item, or audit row is ever created
    here. Always run before any mutating step, for BOTH a dry run and a
    real run (independent-review Blocker 1)."""
    validation = validate_manifest_bytes(manifest_bytes, file_format="jsonl")

    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    parse_result = adapter.parse(manifest_bytes)

    identities: Dict[str, Dict[str, Any]] = {}
    for record in parse_result.valid_records:
        names = (
            [d.raw_name for d in record.ingredient_details] if record.ingredient_details
            else list(record.ingredient_list_raw)
        )
        for name in names:
            key = normalize_name(name)
            bucket = identities.setdefault(key, {"example": name, "source_evidence_ids": []})
            bucket["source_evidence_ids"].append(record.source_evidence_id)

    already_in_catalog = 0
    covered_by_dictionary = 0
    gaps: List[CoverageGap] = []
    for key in sorted(identities):
        info = identities[key]
        # Already-resolvable catalog ingredient is acceptable -- an
        # identity a PRIOR run (or a separate, out-of-band, legitimately
        # reviewed decision) already resolved needs no dictionary entry
        # at all to be covered.
        if await resolve_ingredient(pool, info["example"]) is not None:
            already_in_catalog += 1
            continue
        if _dictionary_lookup(dictionary, key) is not None:
            covered_by_dictionary += 1
            continue
        gaps.append(CoverageGap(
            identity_key=key, example_raw_name=info["example"],
            source_evidence_ids=sorted(set(info["source_evidence_ids"])),
        ))

    return PreflightResult(
        manifest_total_records=validation.total_records,
        manifest_schema_valid=validation.schema_valid,
        manifest_issues=list(validation.issues),
        identities_total=len(identities),
        identities_already_in_catalog=already_in_catalog,
        identities_covered_by_dictionary=covered_by_dictionary,
        gaps=gaps,
    )


@dataclass
class ReviewItemOutcome:
    review_item_id: str
    import_record_id: str
    identity_key: Optional[str]
    action: str  # "CREATED_INGREDIENT" | "MAPPED_TO_EXISTING" | "UNRESOLVED_DICTIONARY_GAP" | "ERROR"
    detail: Optional[str] = None


@dataclass
class PublishOutcome:
    import_record_id: str
    status: str  # "PUBLISHED" | "PUBLISH_FAILED"
    formulation_id: Optional[str] = None
    reused_existing_formulation: Optional[bool] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


@dataclass
class PopulationReport:
    source_id: str
    batch_id: Optional[str]
    dry_run: bool

    # -- Preflight (always populated -- dry run or not; independent-
    #    review Blocker 1) --
    manifest_total_records: int = 0
    manifest_schema_valid: int = 0
    manifest_issues: List[Dict[str, Any]] = field(default_factory=list)
    dictionary_entry_count: int = 0
    identities_total: int = 0
    identities_already_in_catalog: int = 0
    identities_covered_by_dictionary: int = 0
    unresolved_dictionary_gaps: List[CoverageGap] = field(default_factory=list)
    preflight_ready: bool = False

    # -- Only populated when dry_run=False --
    records_imported: int = 0
    records_reused_from_prior_import: int = 0
    review_item_outcomes: List[ReviewItemOutcome] = field(default_factory=list)
    records_validated: int = 0
    records_needing_review: int = 0
    records_rejected_or_malformed: int = 0
    publish_outcomes: List[PublishOutcome] = field(default_factory=list)

    @property
    def records_published(self) -> int:
        return sum(1 for p in self.publish_outcomes if p.status == "PUBLISHED")


async def _reconcile_one_record(pool: asyncpg.Pool, record: Dict[str, Any]) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await reconcile_review_state(conn, record)


async def _resolve_records_own_unknown_ingredients(
    pool: asyncpg.Pool, *, import_record_id: UUID, dictionary: List[IngredientDictionaryEntry], actor: str,
    outcomes: List[ReviewItemOutcome],
) -> None:
    """Resolves every currently-OPEN UNKNOWN_INGREDIENT review item
    belonging to exactly this one import record, via the dictionary
    only. Scoped to `import_record_id` deliberately -- this function is
    only ever called immediately after reconciling THIS record, while
    its own new review items are still fresh (never touches an OPEN
    item some earlier record left behind; each record's items are
    resolved once, by this same per-record pass, before the next
    record is even validated -- see this module's own docstring)."""
    open_items = await repo.list_review_items(pool, status="OPEN", reason_code="UNKNOWN_INGREDIENT")
    this_record_items = [i for i in open_items if i["import_record_id"] == import_record_id]
    review_service = CatalogReviewService(pool)

    for item in this_record_items:
        identity_key = item.get("identity_key")
        entry = _dictionary_lookup(dictionary, identity_key) if identity_key else None
        if entry is None:
            outcomes.append(ReviewItemOutcome(
                review_item_id=str(item["id"]), import_record_id=str(import_record_id),
                identity_key=identity_key, action="UNRESOLVED_DICTIONARY_GAP",
                detail=f"no ingredient_dictionary.json entry for identity {identity_key!r}",
            ))
            continue
        try:
            existing = await resolve_ingredient(pool, entry.canonical_name)
            if existing is not None:
                await review_service.map_ingredient(item["id"], ingredient_id=existing["id"], actor=actor)
                outcomes.append(ReviewItemOutcome(
                    review_item_id=str(item["id"]), import_record_id=str(import_record_id),
                    identity_key=identity_key, action="MAPPED_TO_EXISTING",
                    detail=f"mapped to dictionary canonical {entry.canonical_name!r}",
                ))
            else:
                await review_service.create_ingredient(
                    item["id"], canonical_name=entry.canonical_name,
                    ingredient_type=entry.ingredient_type, inci_name=entry.inci_name, actor=actor,
                )
                outcomes.append(ReviewItemOutcome(
                    review_item_id=str(item["id"]), import_record_id=str(import_record_id),
                    identity_key=identity_key, action="CREATED_INGREDIENT",
                    detail=f"created dictionary canonical {entry.canonical_name!r}",
                ))
        except ReviewError as e:
            outcomes.append(ReviewItemOutcome(
                review_item_id=str(item["id"]), import_record_id=str(import_record_id),
                identity_key=identity_key, action="ERROR", detail=f"{e.code}: {e}",
            ))


def _merge_manifest_issues(existing: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> None:
    seen = {(i.get("index"), i.get("source_evidence_id"), i.get("code")) for i in existing}
    for issue in new:
        key = (issue.get("index"), issue.get("source_evidence_id"), issue.get("code"))
        if key not in seen:
            existing.append(issue)
            seen.add(key)


async def populate_wave1b(
    pool: asyncpg.Pool, *, source_id: UUID, manifest_bytes: bytes, dictionary_path: Path,
    actor: str, dry_run: bool = False, allow_test_source: bool = False,
) -> PopulationReport:
    """The one operator-invoked entry point (Section "Required
    solution", independent review).

    Order (independent-review Blocker 1, "preflight boundary"):

    1. `load_ingredient_dictionary()` -- pure, no database access at
       all. Every malformed-dictionary failure mode raises
       `PopulationRejectedError` here, before anything else runs.
    2. `_run_preflight()` -- read-only database access only
       (`resolve_ingredient()` SELECTs). Schema-validates the manifest
       and checks every distinct ingredient identity it discloses
       against the dictionary and the current catalog.
    3. `dry_run=True` returns HERE -- the database is provably
       untouched (no batch, no import record, no ingredient, no alias,
       no product, no formulation, no review item, no audit row), and
       the report already reflects the full preflight coverage check,
       never a shortcut that could pass a broken dictionary silently.
    4. For a REAL run (`dry_run=False`), `preflight.ready` is enforced
       HERE, before `import_manifest()` -- the first mutating call --
       ever runs. Wave 1B's own production-population command is an
       all-pack operation: a real invocation whose manifest has ANY
       schema-invalid record, or ANY ingredient identity neither the
       catalog nor the dictionary can resolve, raises
       `PopulationRejectedError(code="PREFLIGHT_NOT_READY")` and
       creates nothing at all -- not even the valid subset of the pack.
       (This is a policy this module alone enforces; the underlying,
       shared `CatalogIngestionService`/`import_manifest()` remain
       unmodified and still support importing a partially-valid
       manifest for every OTHER caller -- only Wave 1B's own dedicated
       production-population command is this strict.) A dry run is
       NEVER subject to this check -- `preflight_ready=False` on a dry
       run is exactly the useful signal an operator is running one to
       see, never an exception raised merely for asking."""
    dictionary = load_ingredient_dictionary(dictionary_path)
    preflight = await _run_preflight(pool, manifest_bytes, dictionary)

    report = PopulationReport(
        source_id=str(source_id), batch_id=None, dry_run=dry_run,
        manifest_total_records=preflight.manifest_total_records,
        manifest_schema_valid=preflight.manifest_schema_valid,
        manifest_issues=list(preflight.manifest_issues),
        dictionary_entry_count=len(dictionary),
        identities_total=preflight.identities_total,
        identities_already_in_catalog=preflight.identities_already_in_catalog,
        identities_covered_by_dictionary=preflight.identities_covered_by_dictionary,
        unresolved_dictionary_gaps=preflight.gaps,
        preflight_ready=preflight.ready,
    )

    if dry_run:
        return report

    if not preflight.ready:
        raise PopulationRejectedError(
            f"Wave 1B production pack is not ready to populate -- manifest_total_records="
            f"{preflight.manifest_total_records}, manifest_schema_valid={preflight.manifest_schema_valid}, "
            f"manifest_issue_count={len(preflight.manifest_issues)}, "
            f"unresolved_dictionary_gap_count={len(preflight.gaps)}. Run with dry_run=True to inspect the "
            "full coverage report before deciding how to proceed. No batch, import record, ingredient, alias, "
            "product, formulation, review item, or audit row was created.",
            code="PREFLIGHT_NOT_READY",
        )

    import_outcome = await import_manifest(
        pool, source_id=source_id, file_bytes=manifest_bytes, file_format="jsonl",
        dry_run=False, allow_test_source=allow_test_source,
    )
    _merge_manifest_issues(report.manifest_issues, import_outcome.manifest_issues)
    if import_outcome.import_outcome is None or import_outcome.import_outcome.batch_id is None:
        return report
    report.batch_id = str(import_outcome.import_outcome.batch_id)

    batch_id = import_outcome.import_outcome.batch_id
    report.records_imported = sum(1 for r in import_outcome.import_outcome.records if r.is_new)
    report.records_reused_from_prior_import = sum(1 for r in import_outcome.import_outcome.records if not r.is_new)

    # Interleaved validation (this module's own docstring): each
    # record is reconciled and its OWN new unknown ingredients resolved
    # before the next record is even looked at, so a common ingredient
    # already resolved by an earlier record in this same pass never
    # gets a fresh (and then permanently stuck) review item opened for
    # it again on a later one. Deterministic order (source_evidence_id,
    # i.e. external_record_id) -- never dependent on row insertion
    # timing -- so re-running this function against the same batch
    # always processes records in the same sequence.
    normalized_records = sorted(
        await repo.list_import_records_for_batch(pool, batch_id, status="NORMALIZED"),
        key=lambda r: r["external_record_id"],
    )
    for record in normalized_records:
        await _reconcile_one_record(pool, record)
        await _resolve_records_own_unknown_ingredients(
            pool, import_record_id=record["id"], dictionary=dictionary, actor=actor,
            outcomes=report.review_item_outcomes,
        )

    all_records = await repo.list_import_records_for_batch(pool, batch_id)
    report.records_validated = sum(1 for r in all_records if r["status"] == "VALIDATED")
    report.records_needing_review = sum(1 for r in all_records if r["status"] == "NEEDS_REVIEW")
    report.records_rejected_or_malformed = sum(1 for r in all_records if r["status"] in ("REJECTED", "MALFORMED"))

    # Attempted for both VALIDATED (this run's own new work) and
    # already-PUBLISHED records (a prior run's work) -- publish() is
    # itself an idempotent no-op for an already-published record (see
    # its own docstring), so re-running this whole function reports
    # the SAME true "published to this target DB" count every time,
    # never "0" on a repeat run just because nothing NEW happened.
    publishable_ids = sorted(
        (r["id"] for r in all_records if r["status"] in ("VALIDATED", "PUBLISHED")),
        key=str,
    )
    publish_service = CatalogPublicationService(pool)
    for record_id in publishable_ids:
        try:
            outcome = await publish_service.publish(record_id, actor=actor)
            report.publish_outcomes.append(PublishOutcome(
                import_record_id=str(record_id), status="PUBLISHED",
                formulation_id=str(outcome.formulation_id),
                reused_existing_formulation=outcome.reused_existing_formulation,
            ))
        except PublicationError as e:
            report.publish_outcomes.append(PublishOutcome(
                import_record_id=str(record_id), status="PUBLISH_FAILED",
                error_code=e.code, error_detail=str(e),
            ))

    return report
