"""CatalogReviewService: the durable human-review queue
(catalog_review_items, migration 2de8380d3618) a record lands in when
CatalogIngestionService.validate_batch() couldn't resolve it
automatically. Every resolution here is audited (catalog_audit_log)
and, for ingredient-identity decisions, uses only the EXISTING exact
canonical-name/alias resolver (app/db/catalog_repository.resolve_
ingredient) -- this module never introduces fuzzy matching into any
automatic path; a human explicitly choosing "map this raw string to
ingredient X" is not fuzzy matching, it's an audited human decision
that HAPPENS to then be recorded as a durable, exact alias so future
imports resolve it automatically (Section 6).

Identity binding (hotfix, independent review): `map_ingredient()`/
`create_ingredient()` used to accept a caller-supplied `raw_name` and
trust it outright -- nothing checked that it actually matched the
`UNKNOWN_INGREDIENT` review item being resolved. An operator (or a
scripting mistake) could resolve item A's review row while creating an
alias for a completely unrelated string B, leaving A's own real
problem silently marked RESOLVED without ever actually being fixed.
`_resolve_target_raw_name()` below is now the ONLY way either method
learns what raw string it's operating on: derived from the review
item's own `identity_key` cross-referenced against the import record's
current `normalized_payload`, never from an argument a caller could
get wrong. `raw_name` is no longer a parameter of either method at
all -- see that function's own docstring for the exact binding rule.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.db.catalog_repository import resolve_ingredient
from app.domain.catalog_validation import normalize_identity, reconcile_review_state

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class AliasConflictError(ReviewError):
    """Section 17: a conflicting alias must fail closed, never
    silently overwrite an existing mapping."""

    def __init__(self, message: str, *, conflict: Dict[str, Any]):
        super().__init__(message, code="ALIAS_CONFLICT")
        self.conflict = conflict


@dataclass
class ReviewItemDetail:
    review_item: Dict[str, Any]
    import_record: Dict[str, Any]
    # Recomputed live at show()-time rather than stored redundantly on
    # the review item row -- avoids the two ever drifting apart. For
    # UNKNOWN_INGREDIENT specifically: every raw ingredient name in
    # this record that still doesn't resolve, right now.
    context: Dict[str, Any] = field(default_factory=dict)


class CatalogReviewService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def list_open(self, *, reason_code: Optional[str] = None) -> List[Dict[str, Any]]:
        return await repo.list_review_items(self._pool, status="OPEN", reason_code=reason_code)

    async def show(self, review_item_id: UUID) -> ReviewItemDetail:
        review_item = await repo.get_review_item(self._pool, review_item_id)
        if review_item is None:
            raise ReviewError("review item not found", code="REVIEW_ITEM_NOT_FOUND")
        import_record = await repo.get_import_record(self._pool, review_item["import_record_id"])

        context: Dict[str, Any] = {}
        if review_item["reason_code"] == "UNKNOWN_INGREDIENT" and import_record.get("normalized_payload"):
            unresolved = []
            for ingredient in import_record["normalized_payload"]["ingredients"]:
                if await resolve_ingredient(self._pool, ingredient["raw_name"]) is None:
                    unresolved.append(ingredient["raw_name"])
            context["unresolved_ingredient_names"] = unresolved
        return ReviewItemDetail(review_item=review_item, import_record=import_record, context=context)

    async def map_ingredient(
        self, review_item_id: UUID, *, ingredient_id: UUID, actor: str,
    ) -> Dict[str, Any]:
        """Section 6/17: maps an unresolved raw string to an EXISTING
        canonical ingredient by creating a durable alias. Fails closed
        (AliasConflictError) if that normalized string already means
        something else -- never overwrites an existing mapping. The
        raw string itself is never caller-supplied -- see
        `_resolve_target_raw_name`'s own docstring."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id)
                _, raw_name = await self._resolve_target_raw_name(conn, review_item)

                conflict = await repo.find_alias_conflict(conn, raw_name)
                if conflict is not None and conflict["ingredient_id"] != ingredient_id:
                    raise AliasConflictError(
                        f"{raw_name!r} already resolves to a different ingredient ({conflict['ingredient_name']!r})",
                        conflict=conflict,
                    )
                if conflict is None:
                    alias_id = await repo.create_ingredient_alias(conn, ingredient_id=ingredient_id, alias=raw_name)
                    await repo.write_audit(
                        conn, action="ADD_ALIAS", entity_type="ingredient", entity_id=ingredient_id,
                        import_record_id=review_item["import_record_id"], actor=actor,
                        after_metadata={"alias": raw_name, "alias_id": str(alias_id)},
                    )

                resolved = await repo.resolve_review_item(
                    conn, review_item_id, status="RESOLVED", resolution="MAPPED_TO_EXISTING_INGREDIENT",
                    reviewed_by=actor,
                )
                await self._revalidate(conn, review_item["import_record_id"])
                return resolved

    async def create_ingredient(
        self, review_item_id: UUID, *, canonical_name: str, actor: str,
        ingredient_type: Optional[str] = None, inci_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Section 6: the raw string represents a genuinely new
        canonical ingredient this catalog has never seen. Creates it,
        then -- only if the raw source string differs from the new
        canonical name -- also creates an alias so this exact raw
        string resolves automatically on any future import. The raw
        string itself is never caller-supplied -- see
        `_resolve_target_raw_name`'s own docstring."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id)
                _, raw_name = await self._resolve_target_raw_name(conn, review_item)

                if await resolve_ingredient(conn, canonical_name) is not None:
                    raise ReviewError(
                        f"{canonical_name!r} already resolves to an existing ingredient -- use map_ingredient instead",
                        code="INGREDIENT_ALREADY_EXISTS",
                    )

                ingredient_id = await repo.create_canonical_ingredient(
                    conn, canonical_name=canonical_name, ingredient_type=ingredient_type, inci_name=inci_name,
                )
                await repo.write_audit(
                    conn, action="CREATE_INGREDIENT", entity_type="ingredient", entity_id=ingredient_id,
                    import_record_id=review_item["import_record_id"], actor=actor,
                    after_metadata={"canonical_name": canonical_name},
                )

                if " ".join(raw_name.split()).strip().lower() != " ".join(canonical_name.split()).strip().lower():
                    conflict = await repo.find_alias_conflict(conn, raw_name)
                    if conflict is not None:
                        raise AliasConflictError(
                            f"{raw_name!r} already resolves to a different ingredient ({conflict['ingredient_name']!r})",
                            conflict=conflict,
                        )
                    alias_id = await repo.create_ingredient_alias(conn, ingredient_id=ingredient_id, alias=raw_name)
                    await repo.write_audit(
                        conn, action="ADD_ALIAS", entity_type="ingredient", entity_id=ingredient_id,
                        import_record_id=review_item["import_record_id"], actor=actor,
                        after_metadata={"alias": raw_name, "alias_id": str(alias_id)},
                    )

                resolved = await repo.resolve_review_item(
                    conn, review_item_id, status="RESOLVED", resolution="CREATED_NEW_INGREDIENT",
                    reviewed_by=actor,
                )
                await self._revalidate(conn, review_item["import_record_id"])
                return resolved

    async def reject_import_record(
        self, review_item_id: UUID, *, reason: str, actor: str,
    ) -> Dict[str, Any]:
        """Terminal: the source value itself is rejected -- the whole
        import record becomes REJECTED (can never be published), not
        merely this one review item."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id)
                resolved = await repo.resolve_review_item(
                    conn, review_item_id, status="RESOLVED", resolution="REJECTED_SOURCE_VALUE",
                    reviewed_by=actor, resolution_notes=reason,
                )
                await repo.update_import_record(conn, review_item["import_record_id"], status="REJECTED")
                await repo.write_audit(
                    conn, action="REJECT", entity_type="catalog_import_record",
                    entity_id=review_item["import_record_id"], import_record_id=review_item["import_record_id"],
                    actor=actor, reason=reason,
                )
                return resolved

    async def dismiss(
        self, review_item_id: UUID, *, resolution: str, actor: str, notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generic manual resolution for reason codes that aren't
        ingredient-identity decisions (PRODUCT_IDENTITY_AMBIGUOUS,
        SKU_CONFLICT, INVALID_MARKET, etc.) -- a human confirms the
        record is fine as-is (e.g. `IDENTITY_CONFIRMED`) or overrides
        it (`MANUAL_OVERRIDE`), recorded with an explicit resolution
        code, never bare prose as the only record of the decision."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id)
                if review_item["reason_code"] == "UNKNOWN_INGREDIENT":
                    # Hotfix (independent review): ingredient identity
                    # must always go through map_ingredient()/
                    # create_ingredient() (or reject_import_record() to
                    # kill the whole record outright) -- never a
                    # generic dismissal, which has no notion of the
                    # review item's own identity_key and could
                    # otherwise let MANUAL_OVERRIDE silently convert an
                    # unresolved ingredient into VALIDATED state with
                    # no ingredient/alias ever actually created.
                    raise ReviewError(
                        "UNKNOWN_INGREDIENT review items must be resolved via map_ingredient(), "
                        "create_ingredient(), or reject_import_record() -- never a generic dismissal",
                        code="INGREDIENT_REVIEW_REQUIRES_EXPLICIT_RESOLUTION",
                    )
                resolved = await repo.resolve_review_item(
                    conn, review_item_id, status="RESOLVED", resolution=resolution,
                    reviewed_by=actor, resolution_notes=notes,
                )
                if resolution == "IDENTITY_REJECTED":
                    await repo.update_import_record(conn, review_item["import_record_id"], status="REJECTED")
                    await repo.write_audit(
                        conn, action="REJECT", entity_type="catalog_import_record",
                        entity_id=review_item["import_record_id"], import_record_id=review_item["import_record_id"],
                        actor=actor, reason=notes,
                    )
                else:
                    await self._revalidate(conn, review_item["import_record_id"])
                    await repo.write_audit(
                        conn, action="APPROVE", entity_type="catalog_review_item", entity_id=review_item_id,
                        import_record_id=review_item["import_record_id"], actor=actor,
                        after_metadata={"resolution": resolution}, reason=notes,
                    )
                return resolved

    async def _require_open_review_item(
        self, conn: asyncpg.Connection, review_item_id: UUID,
    ) -> Dict[str, Any]:
        row = await conn.fetchrow("SELECT * FROM catalog_review_items WHERE id = $1 AND status = 'OPEN'", review_item_id)
        if row is None:
            raise ReviewError("review item not found or already resolved", code="REVIEW_ITEM_NOT_OPEN")
        return dict(row)

    async def _resolve_target_raw_name(
        self, conn: asyncpg.Connection, review_item: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        """The ONLY place map_ingredient()/create_ingredient() learn
        which raw source string they're operating on -- derived from
        the review item's own `identity_key`, cross-referenced against
        the import record's current `normalized_payload`, never from a
        caller-supplied argument (Hotfix, independent review: an
        operator could previously pass any `raw_name` at all, resolving
        review item A's row while creating an alias for a completely
        unrelated string B, leaving A's actual problem silently marked
        RESOLVED without ever being fixed).

        `normalize_record()`'s own duplicate-raw-name validator
        (app/domain/catalog_normalization.py) already guarantees no two
        ingredients in one record share a normalized raw_name, so
        "exactly one match" is the normal case by construction -- the
        explicit count check here is defense in depth against that
        invariant somehow not holding (e.g. a future normalization
        change), never trusted to hold silently.

        Also requires the target to still be genuinely unresolved --
        acting on stale state (e.g. a concurrent alias already created
        for it) is refused rather than silently no-op'd."""
        if review_item["reason_code"] != "UNKNOWN_INGREDIENT":
            raise ReviewError(
                f"review item reason_code is {review_item['reason_code']!r}, expected 'UNKNOWN_INGREDIENT'",
                code="WRONG_REASON_CODE",
            )
        identity_key = review_item.get("identity_key")
        if not identity_key:
            raise ReviewError(
                "UNKNOWN_INGREDIENT review item has no identity_key -- cannot determine which "
                "ingredient it represents",
                code="MISSING_IDENTITY_KEY",
            )

        import_record = await repo.get_import_record(conn, review_item["import_record_id"])
        if import_record is None or not import_record.get("normalized_payload"):
            raise ReviewError(
                "import record or its normalized payload is missing", code="IMPORT_RECORD_NOT_FOUND",
            )

        matches = [
            ingredient for ingredient in import_record["normalized_payload"]["ingredients"]
            if normalize_identity(ingredient["raw_name"]) == identity_key
        ]
        if len(matches) != 1:
            raise ReviewError(
                f"expected exactly one ingredient in the record matching identity_key {identity_key!r}, "
                f"found {len(matches)}",
                code="IDENTITY_KEY_AMBIGUOUS" if len(matches) > 1 else "IDENTITY_KEY_NOT_FOUND",
            )
        raw_name = matches[0]["raw_name"]

        if await resolve_ingredient(conn, raw_name) is not None:
            raise ReviewError(
                f"{raw_name!r} already resolves to an existing ingredient -- nothing to resolve",
                code="ALREADY_RESOLVED",
            )

        return import_record, raw_name

    async def _revalidate(self, conn: asyncpg.Connection, import_record_id: UUID) -> None:
        """Blocker 3 (independent review): never infers `VALIDATED`
        merely from `count(open review items) == 0` -- that was wrong
        the moment a record had more than one independent problem of
        the same reason code (e.g. two unresolved ingredients),
        because resolving just one already brought that count to zero
        while the other remained genuinely unresolved. Delegates to
        the exact same canonical validation
        (app/domain/catalog_validation.py::reconcile_review_state)
        `CatalogIngestionService.validate_batch()` itself uses -- one
        rule set, never two copies that could drift apart. This
        re-runs the full check fresh (not just "are there still open
        rows") and creates a review item for the FIRST time it ever
        sees a still-unresolved problem (never re-opening or
        duplicating one a human already resolved -- see
        `reconcile_review_state`'s own docstring)."""
        import_record = await repo.get_import_record(conn, import_record_id)
        if import_record is None or import_record.get("normalized_payload") is None:
            return
        await reconcile_review_state(conn, import_record)
