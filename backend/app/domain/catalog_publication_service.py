"""CatalogPublicationService: the final two pipeline stages
(CATALOG_INGESTION_ARCHITECTURE.md) -- atomic publication of a
verified staging record into the existing production catalog
(brands/products/product_formulations/product_skus/
formulation_ingredients) that ProductMatchingService/SafetyEngine
actually query.

Core invariant this module exists to uphold (the pass's own brief,
verbatim): INGESTED != VERIFIED != PUBLISHED != SAFE FOR EVERY USER.
`publish()` never marks anything user-specific-safe -- it marks a
formulation `publication_status = 'PUBLISHED'`, meaning "trusted
enough to be evaluated by SafetyEngine", nothing more.

Runs in exactly one transaction per call (Section 15: "Publication
must run in one transaction where practical"). Every failure path
inside that transaction raises rather than partially committing --
asyncpg's `transaction()` context manager rolls back on any exception,
so "no half-created product, no product with half an ingredient list,
no is_current formulation missing its own ingredient rows" is enforced
by the database transaction boundary itself, not by careful manual
bookkeeping.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.db.catalog_repository import resolve_ingredient

logger = logging.getLogger(__name__)

# Structural safety-backstop reason codes (Section 16) -- a closed,
# deliberately-named set, never a raw string invented at a call site.
STRUCTURAL_POSITIONS_INVALID = "STRUCTURAL_POSITIONS_INVALID"
STRUCTURAL_DUPLICATE_INGREDIENT = "STRUCTURAL_DUPLICATE_INGREDIENT"
STRUCTURAL_UNRESOLVED_INGREDIENT = "STRUCTURAL_UNRESOLVED_INGREDIENT"
STRUCTURAL_COMPLETE_CLAIM_UNJUSTIFIED = "STRUCTURAL_COMPLETE_CLAIM_UNJUSTIFIED"
STRUCTURAL_MARKET_MISSING = "STRUCTURAL_MARKET_MISSING"
STRUCTURAL_SOURCE_TYPE_INVALID = "STRUCTURAL_SOURCE_TYPE_INVALID"
STRUCTURAL_PRODUCT_IDENTITY_AMBIGUOUS = "STRUCTURAL_PRODUCT_IDENTITY_AMBIGUOUS"
STRUCTURAL_PROVENANCE_MISSING = "STRUCTURAL_PROVENANCE_MISSING"

_VALID_FORMULATION_SOURCE_TYPES = frozenset({
    "manufacturer_label", "manufacturer_disclosure", "regulatory_filing",
    "third_party_verified", "user_submitted_unverified",
})

# ingredient_data_status -- reused verbatim from migration ac641537d224,
# never a parallel vocabulary. See _compute_ingredient_data_status().
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
UNKNOWN = "UNKNOWN"


class PublicationError(Exception):
    """Raised whenever publish() cannot proceed -- never partially.
    `code` is a closed classification (see the module's own constants
    plus NOT_PUBLISHABLE_STATUS/IMPORT_RECORD_NOT_FOUND/
    FORMULATION_VERSION_CONFLICT/UNRESOLVED_INGREDIENTS below), never a
    raw message string a caller would have to parse."""

    def __init__(self, message: str, *, code: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class PublicationOutcome:
    import_record_id: UUID
    formulation_id: UUID
    product_id: UUID
    brand_id: UUID
    is_reformulation: bool
    superseded_formulation_id: Optional[UUID]
    ingredient_data_status: str
    reused_existing_formulation: bool
    dry_run: bool = False


def _normalize(name: str) -> str:
    return " ".join(name.split()).strip().lower()


def _run_structural_backstop(normalized: Dict[str, Any], resolved_ingredient_ids: List[Optional[UUID]]) -> List[str]:
    """Section 16, run immediately before a formulation may become
    PUBLISHED -- independent of, and in addition to, the review-time
    checks in CatalogIngestionService.validate_batch (which flag for
    HUMAN review; this raises and blocks PUBLICATION outright,
    defense in depth against staging state having changed since
    validation, e.g. an alias removed after this record was
    validated)."""
    violations: List[str] = []

    ingredients = normalized["ingredients"]
    if ingredients:
        positions = sorted(i["position"] for i in ingredients)
        if positions != list(range(1, len(positions) + 1)):
            violations.append(STRUCTURAL_POSITIONS_INVALID)
        seen_names = set()
        for ing in ingredients:
            key = _normalize(ing["raw_name"])
            if key in seen_names:
                violations.append(STRUCTURAL_DUPLICATE_INGREDIENT)
                break
            seen_names.add(key)

    if any(rid is None for rid in resolved_ingredient_ids):
        violations.append(STRUCTURAL_UNRESOLVED_INGREDIENT)
    else:
        # Two DIFFERENT raw names resolving to the SAME canonical
        # ingredient is the duplicate normalization-time checks cannot
        # catch (they only compare raw strings) -- caught here, after
        # resolution, against the real resolved ID set.
        if len(set(resolved_ingredient_ids)) != len(resolved_ingredient_ids):
            if STRUCTURAL_DUPLICATE_INGREDIENT not in violations:
                violations.append(STRUCTURAL_DUPLICATE_INGREDIENT)

    if normalized["ingredient_list_complete"] and not ingredients:
        violations.append(STRUCTURAL_COMPLETE_CLAIM_UNJUSTIFIED)

    if not (normalized.get("market_or_region") or "").strip():
        violations.append(STRUCTURAL_MARKET_MISSING)

    if normalized["source_type"] not in _VALID_FORMULATION_SOURCE_TYPES:
        violations.append(STRUCTURAL_SOURCE_TYPE_INVALID)

    return violations


def _compute_ingredient_data_status(normalized: Dict[str, Any], all_resolved: bool) -> str:
    """Section 5's fail-closed rule, applied at the one place that
    actually promotes a formulation: full label + all entries parsed +
    all entries resolved + source explicitly represents completeness
    -- ALL FOUR, never inferred from count or "the last ingredient
    looks minor". Reusing product_formulations.ingredient_data_status'
    own existing three-value vocabulary (migration ac641537d224), never
    a parallel one."""
    ingredients = normalized["ingredients"]
    if not ingredients:
        return UNKNOWN
    if normalized["ingredient_list_complete"] and all_resolved:
        return COMPLETE
    return PARTIAL


class CatalogPublicationService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def publish(
        self, import_record_id: UUID, *, actor: str, dry_run: bool = False,
    ) -> PublicationOutcome:
        async with self._pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                outcome = await self._publish_within_transaction(conn, import_record_id, actor=actor)
            except Exception:
                await tx.rollback()
                raise
            if dry_run:
                await tx.rollback()
                outcome.dry_run = True
            else:
                await tx.commit()
            return outcome

    async def _publish_within_transaction(
        self, conn: asyncpg.Connection, import_record_id: UUID, *, actor: str,
    ) -> PublicationOutcome:
        # Locked FIRST, before any status/idempotency decision (Blocker
        # 4, independent review) -- see get_import_record_for_update's
        # own docstring for why a plain unlocked SELECT here let two
        # concurrent publish() calls for the same import_record_id both
        # observe VALIDATED and both proceed.
        record = await repo.get_import_record_for_update(conn, import_record_id)
        if record is None:
            raise PublicationError("import record not found", code="IMPORT_RECORD_NOT_FOUND")

        if record["status"] == "PUBLISHED" and record.get("formulation_id"):
            # Idempotent re-publish of an already-published record --
            # no-op, not an error (Section 9's idempotency contract
            # extended to the publish step itself).
            formulation = await conn.fetchrow(
                "SELECT product_id, publication_status, ingredient_data_status FROM product_formulations WHERE id = $1",
                record["formulation_id"],
            )
            product = await conn.fetchrow("SELECT brand_id FROM products WHERE id = $1", formulation["product_id"])
            return PublicationOutcome(
                import_record_id=import_record_id, formulation_id=record["formulation_id"],
                product_id=formulation["product_id"], brand_id=product["brand_id"],
                is_reformulation=False, superseded_formulation_id=record.get("superseded_formulation_id"),
                ingredient_data_status=formulation["ingredient_data_status"],
                reused_existing_formulation=True,
            )

        if record["status"] != "VALIDATED":
            raise PublicationError(
                f"import record status is {record['status']!r}, must be VALIDATED to publish",
                code="NOT_PUBLISHABLE_STATUS", details={"status": record["status"]},
            )

        normalized = record["normalized_payload"]

        # Step 2/3: resolve/create brand and product deterministically
        # (Section 10 -- exact normalized match only, never fuzzy).
        # Both concurrency-safe against two DIFFERENT import records
        # racing to introduce the same previously-unseen brand/product
        # (Blocker 4, independent review) -- see resolve_or_create_
        # brand/resolve_or_create_product's own docstrings.
        brand_id, _ = await repo.resolve_or_create_brand(conn, normalized["brand_name"])
        product_id, _ = await repo.resolve_or_create_product(
            conn, brand_id=brand_id, name=normalized["product_name"], category=normalized["category"],
            description=normalized.get("description"),
        )

        # Ingredient resolution, re-verified fresh at publish time
        # (never trusted stale from an earlier validate pass -- the
        # ingredient tables could have changed since).
        resolved_ids: List[Optional[UUID]] = []
        resolved_items: List[Dict[str, Any]] = []
        for ingredient in normalized["ingredients"]:
            resolved = await resolve_ingredient(conn, ingredient["raw_name"])
            resolved_ids.append(resolved["id"] if resolved else None)
            if resolved is not None:
                resolved_items.append({
                    "ingredient_id": resolved["id"], "position": ingredient["position"],
                    "declared_concentration": ingredient.get("declared_concentration"),
                    "concentration_unit": ingredient.get("concentration_unit"),
                })

        violations = _run_structural_backstop(normalized, resolved_ids)
        if violations:
            # Deliberately does NOT persist any state change here (e.g.
            # flipping the record back to NEEDS_REVIEW) -- this whole
            # method runs in one transaction that the caller rolls back
            # on any exception (Section 15: "if any required step
            # fails: ROLLBACK", no partial effects survive a failed
            # publish). The record is left exactly as it was
            # (VALIDATED); an operator re-runs validate_batch to
            # re-flag it for review with a real review item, rather
            # than this method reaching outside its own atomic
            # boundary to leave a side effect a rolled-back transaction
            # would otherwise erase anyway.
            raise PublicationError(
                f"structural safety backstop failed: {violations}",
                code="STRUCTURAL_VALIDATION_FAILED", details={"violations": violations},
            )

        market_or_region = normalized["market_or_region"]
        sku_values = [s["sku"] for s in normalized["skus"]]
        conflicts = await repo.find_conflicting_skus(conn, sku_values, exclude_product_id=product_id)
        if conflicts:
            raise PublicationError(
                "one or more SKUs already belong to a different product",
                code="SKU_CONFLICT", details={"conflicts": [c["sku"] for c in conflicts]},
            )

        all_resolved = all(rid is not None for rid in resolved_ids)
        ingredient_data_status = _compute_ingredient_data_status(normalized, all_resolved)

        current = await repo.get_current_formulation_for_update(conn, product_id, market_or_region)

        reused_existing = False
        superseded_id: Optional[UUID] = None
        is_reformulation = False

        if current is not None:
            current_provenance = await repo.get_provenance_for_formulation(conn, current["id"])
            current_import_record = (
                await repo.get_import_record(conn, current_provenance["import_record_id"])
                if current_provenance is not None else None
            )
            if current_import_record is not None and current_import_record["payload_sha256"] == record["payload_sha256"]:
                # Identical content re-imported through a new file/
                # batch -- Section 9's "same content -> same logical
                # import, no duplicate formulation" satisfied even
                # across separate import records.
                formulation_id = current["id"]
                reused_existing = True
            else:
                if current["version"] == normalized["formulation_version"]:
                    raise PublicationError(
                        "incoming formulation_version collides with the currently-published version "
                        "for this product/market but the content differs",
                        code="FORMULATION_VERSION_CONFLICT",
                    )
                is_reformulation = True
                superseded_id = current["id"]
                # Order matters: supersede the OLD row (is_current ->
                # false) BEFORE the new row can ever be set
                # is_current=true -- idx_product_formulations_one_
                # current_per_market (migration d70e5fc90775) permits
                # at most one is_current=true row per (product_id,
                # market_or_region); violating that ordering would
                # abort this entire transaction rather than silently
                # allow two.
                await repo.supersede_formulation(conn, current["id"])
                formulation_id = await repo.create_draft_formulation(
                    conn, product_id=product_id, version=normalized["formulation_version"],
                    market_or_region=market_or_region, source_type=normalized["source_type"],
                    source_reference=normalized.get("source_reference"),
                    effective_from=normalized.get("effective_from"),
                )
        else:
            formulation_id = await repo.create_draft_formulation(
                conn, product_id=product_id, version=normalized["formulation_version"],
                market_or_region=market_or_region, source_type=normalized["source_type"],
                source_reference=normalized.get("source_reference"),
                effective_from=normalized.get("effective_from"),
            )

        if not reused_existing:
            await repo.attach_formulation_ingredients(conn, formulation_id, resolved_items)
            for sku in normalized["skus"]:
                await repo.upsert_sku_for_formulation(
                    conn, product_id=product_id, formulation_id=formulation_id, sku=sku["sku"],
                    upc_or_ean=sku.get("upc_or_ean"), size_value=sku.get("size_value"),
                    size_unit=sku.get("size_unit"), market_or_region=sku.get("market_or_region", market_or_region),
                )

            verified_at = datetime.now(timezone.utc)
            await repo.set_formulation_publication_state(
                conn, formulation_id, publication_status="PUBLISHED",
                ingredient_data_status=ingredient_data_status, is_current=True, verified_at=verified_at,
            )

            existing_provenance = await repo.get_provenance_for_formulation(conn, formulation_id)
            if existing_provenance is None:
                await repo.create_provenance(
                    conn, formulation_id=formulation_id, import_record_id=import_record_id,
                    source_reference=normalized.get("source_reference"), verified_at=verified_at,
                    verification_actor=actor, superseded_formulation_id=superseded_id,
                )

            await repo.write_audit(
                conn, action="PUBLISH", entity_type="product_formulation", entity_id=formulation_id,
                import_record_id=import_record_id, actor=actor,
                before_metadata={"publication_status": "DRAFT"},
                after_metadata={"publication_status": "PUBLISHED", "ingredient_data_status": ingredient_data_status},
            )
            if is_reformulation:
                await repo.write_audit(
                    conn, action="SUPERSEDE", entity_type="product_formulation", entity_id=superseded_id,
                    import_record_id=import_record_id, actor=actor,
                    before_metadata={"is_current": True}, after_metadata={"is_current": False},
                    reason=f"superseded by formulation {formulation_id}",
                )
        else:
            ingredient_data_status = current["ingredient_data_status"]

        await repo.update_import_record(
            conn, import_record_id, status="PUBLISHED", formulation_id=formulation_id,
            superseded_formulation_id=superseded_id,
        )
        await self._recompute_batch_status(conn, record["batch_id"])

        return PublicationOutcome(
            import_record_id=import_record_id, formulation_id=formulation_id, product_id=product_id,
            brand_id=brand_id, is_reformulation=is_reformulation, superseded_formulation_id=superseded_id,
            ingredient_data_status=ingredient_data_status, reused_existing_formulation=reused_existing,
        )

    async def _recompute_batch_status(self, conn: asyncpg.Connection, batch_id: UUID) -> None:
        counts = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'PUBLISHED') AS published,
                count(*) AS total
            FROM catalog_import_records WHERE batch_id = $1
            """,
            batch_id,
        )
        status = "PUBLISHED" if counts["published"] == counts["total"] else "PARTIALLY_PUBLISHED"
        await repo.update_batch(conn, batch_id, status=status, records_published=counts["published"], completed=(status == "PUBLISHED"))
