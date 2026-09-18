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

    1. import the manifest (idempotent -- re-importing byte-identical
       content resolves to the SAME batch, no duplicate records)
    2. validate + resolve ingredients ONE RECORD AT A TIME, against a
       committed, reviewable ingredient dictionary (never fuzzy
       matching, never an invented alias)
    3. report what remains genuinely unresolved (a dictionary gap) or
       needing non-ingredient human review
    4. publish only records that reach VALIDATED

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
from app.domain.catalog_validation import reconcile_review_state
from app.domain.catalog_wave_service import WaveManifestRejectedError, import_manifest


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
    REAL_DATA.md). Fails closed on a structurally malformed file or one
    whose entries claim overlapping normalized identities (two entries
    that would resolve the same raw string to two different canonical
    names is a genuine authoring error in the committed file itself,
    never silently resolved by "first one wins")."""
    try:
        raw = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as e:
        raise PopulationRejectedError(f"could not read ingredient dictionary {path}: {e}", code="DICTIONARY_UNREADABLE") from e

    entries: List[IngredientDictionaryEntry] = []
    seen_normalized: Dict[str, str] = {}
    for raw_entry in raw.get("entries", []):
        entry = IngredientDictionaryEntry(
            canonical_name=raw_entry["canonical_name"],
            ingredient_type=raw_entry.get("ingredient_type"),
            inci_name=raw_entry.get("inci_name"),
            aliases=list(raw_entry.get("aliases") or []),
        )
        for spelling in [entry.canonical_name, *entry.aliases]:
            key = normalize_name(spelling)
            if key in seen_normalized and seen_normalized[key] != entry.canonical_name:
                raise PopulationRejectedError(
                    f"ingredient dictionary entry {spelling!r} normalizes to {key!r}, which is already "
                    f"claimed by canonical ingredient {seen_normalized[key]!r} -- ambiguous dictionary, "
                    "refusing to load",
                    code="DICTIONARY_AMBIGUOUS",
                )
            seen_normalized[key] = entry.canonical_name
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
    manifest_total_records: int = 0
    manifest_issues: List[Dict[str, Any]] = field(default_factory=list)
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

    @property
    def unresolved_dictionary_gaps(self) -> List[ReviewItemOutcome]:
        return [o for o in self.review_item_outcomes if o.action == "UNRESOLVED_DICTIONARY_GAP"]


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


async def populate_wave1b(
    pool: asyncpg.Pool, *, source_id: UUID, manifest_bytes: bytes, dictionary_path: Path,
    actor: str, dry_run: bool = False, allow_test_source: bool = False,
) -> PopulationReport:
    """The one operator-invoked entry point (Section "Required
    solution", independent review). `dry_run=True` performs schema-
    level manifest validation only (identical to `import_manifest
    (dry_run=True)`'s own existing contract) and writes NOTHING to the
    database -- no batch, no review resolution, no publish -- the
    strongest available reading of "no production mutation," matching
    every other dry-run in this codebase."""
    import_outcome = await import_manifest(
        pool, source_id=source_id, file_bytes=manifest_bytes, file_format="jsonl",
        dry_run=dry_run, allow_test_source=allow_test_source,
    )

    report = PopulationReport(
        source_id=str(source_id),
        batch_id=str(import_outcome.import_outcome.batch_id) if (import_outcome.import_outcome and import_outcome.import_outcome.batch_id) else None,
        dry_run=dry_run,
        manifest_total_records=import_outcome.manifest_total_records,
        manifest_issues=import_outcome.manifest_issues,
    )
    if dry_run or import_outcome.import_outcome is None:
        if import_outcome.import_outcome is not None:
            report.records_imported = len(import_outcome.import_outcome.records)
        return report

    batch_id = import_outcome.import_outcome.batch_id
    report.records_imported = sum(1 for r in import_outcome.import_outcome.records if r.is_new)
    report.records_reused_from_prior_import = sum(1 for r in import_outcome.import_outcome.records if not r.is_new)

    dictionary = load_ingredient_dictionary(dictionary_path)

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
