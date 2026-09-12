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
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.db.catalog_repository import resolve_ingredient
from app.domain.catalog_validation import reconcile_review_state

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
        self, review_item_id: UUID, *, raw_name: str, ingredient_id: UUID, actor: str,
    ) -> Dict[str, Any]:
        """Section 6/17: maps an unresolved raw string to an EXISTING
        canonical ingredient by creating a durable alias. Fails closed
        (AliasConflictError) if that normalized string already means
        something else -- never overwrites an existing mapping."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id, "UNKNOWN_INGREDIENT")

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
        self, review_item_id: UUID, *, raw_name: str, canonical_name: str, actor: str,
        ingredient_type: Optional[str] = None, inci_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Section 6: the raw string represents a genuinely new
        canonical ingredient this catalog has never seen. Creates it,
        then -- only if the raw source string differs from the new
        canonical name -- also creates an alias so this exact raw
        string resolves automatically on any future import."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                review_item = await self._require_open_review_item(conn, review_item_id, "UNKNOWN_INGREDIENT")

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
        self, conn: asyncpg.Connection, review_item_id: UUID, expected_reason_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = await conn.fetchrow("SELECT * FROM catalog_review_items WHERE id = $1 AND status = 'OPEN'", review_item_id)
        if row is None:
            raise ReviewError("review item not found or already resolved", code="REVIEW_ITEM_NOT_OPEN")
        review_item = dict(row)
        if expected_reason_code is not None and review_item["reason_code"] != expected_reason_code:
            raise ReviewError(
                f"review item reason_code is {review_item['reason_code']!r}, expected {expected_reason_code!r}",
                code="WRONG_REASON_CODE",
            )
        return review_item

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
