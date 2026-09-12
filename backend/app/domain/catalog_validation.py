"""Single source of truth for what makes an already-normalized catalog
import record ready to publish (`VALIDATED`) versus needing human
review (`NEEDS_REVIEW`).

Independent review's third blocker on this branch: the original design
created review items once, then decided `VALIDATED` purely from
`count(open review items) == 0` -- which is wrong the moment a record
has more than one independent problem of the same reason code
(multiple unresolved ingredients), because resolving just one of them
already brings that count to zero. `reconcile_review_state()` below is
called by BOTH `CatalogIngestionService.validate_batch()` (the initial
pass) and `CatalogReviewService`'s post-resolution revalidation, so
there is exactly one place that decides "is this record actually
fine right now" -- never two copies of the rules that could drift out
of sync, and never a status derived from stale review-item bookkeeping
instead of the current data.

`compute_validation_findings()` is deliberately read-only against
production catalog tables (Section 10: identity/ingredient resolution
here never creates a brand/product/ingredient -- that is
`CatalogPublicationService`'s job, at publish time) and exact-match
only (`app/db/catalog_repository.resolve_ingredient` -- never fuzzy).
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from app.db import catalog_admin_repository as repo
from app.db.catalog_repository import resolve_ingredient

_UNVERIFIED_SOURCE_TYPE = "user_submitted_unverified"


def normalize_identity(raw_name: str) -> str:
    """Same normalization every other catalog lookup in this codebase
    uses (lowercase, trimmed, internal whitespace collapsed) -- see
    app/db/catalog_repository.py::normalize_name. Reimplemented here
    (rather than imported) only to avoid a needless cross-module
    dependency for a two-line pure function; the rule itself is
    identical."""
    return " ".join(raw_name.split()).strip().lower()


@dataclass(frozen=True)
class ValidationFinding:
    """One currently-detected problem. `identity_key` distinguishes
    multiple independent instances of the same `reason_code` on the
    same record (only `UNKNOWN_INGREDIENT` needs this today -- see
    migration 154080b29153); every other reason code is a single
    instance per record (`identity_key=None`)."""

    reason_code: str
    identity_key: Optional[str] = None


async def compute_validation_findings(
    conn: asyncpg.Connection, normalized: Dict[str, Any],
) -> List[ValidationFinding]:
    """Re-checks everything `CatalogIngestionService.validate_batch()`
    originally checked, fresh, against whatever the catalog tables say
    *right now* -- never a cached judgment. Safe to call repeatedly
    (idempotent; has no side effects of its own)."""
    findings: List[ValidationFinding] = []

    market = (normalized.get("market_or_region") or "").strip()
    if not market:
        findings.append(ValidationFinding("INVALID_MARKET"))

    if normalized["source_type"] == _UNVERIFIED_SOURCE_TYPE and normalized["ingredient_list_complete"]:
        findings.append(ValidationFinding("SOURCE_INSUFFICIENT"))

    for ingredient in normalized["ingredients"]:
        resolved = await resolve_ingredient(conn, ingredient["raw_name"])
        if resolved is None:
            findings.append(
                ValidationFinding("UNKNOWN_INGREDIENT", identity_key=normalize_identity(ingredient["raw_name"]))
            )

    if normalized["ingredient_list_complete"] and not normalized["ingredients"]:
        findings.append(ValidationFinding("INGREDIENT_LIST_INCOMPLETE"))

    brand_id = await conn.fetchval(
        "SELECT id FROM brands WHERE normalized_name = $1", normalize_identity(normalized["brand_name"]),
    )
    existing_product_id = None
    if brand_id is not None:
        existing_product_id = await conn.fetchval(
            "SELECT id FROM products WHERE brand_id = $1 AND normalized_name = $2",
            brand_id, normalize_identity(normalized["product_name"]),
        )

    sku_values = [s["sku"] for s in normalized["skus"]]
    conflicts = await repo.find_conflicting_skus(conn, sku_values, exclude_product_id=existing_product_id)
    if conflicts:
        findings.append(ValidationFinding("SKU_CONFLICT"))

    upc_values = [s["upc_or_ean"] for s in normalized["skus"] if s.get("upc_or_ean")]
    if upc_values:
        if existing_product_id is not None:
            upc_conflicts = await conn.fetch(
                "SELECT * FROM product_skus WHERE upc_or_ean = ANY($1::varchar[]) AND product_id <> $2",
                upc_values, existing_product_id,
            )
        else:
            upc_conflicts = await conn.fetch(
                "SELECT * FROM product_skus WHERE upc_or_ean = ANY($1::varchar[])", upc_values,
            )
        if upc_conflicts:
            findings.append(ValidationFinding("UPC_CONFLICT"))

    return findings


async def reconcile_review_state(conn: asyncpg.Connection, import_record: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures a review item exists for every CURRENTLY-detected
    finding (creating one only the first time this exact problem --
    (reason_code, identity_key) -- has ever been seen for this record;
    never duplicated on repeated calls), then derives the record's
    status purely from whether any OPEN review item remains.

    An item a human already resolved (however: mapped, created,
    confirmed, overridden) is never recreated even if the underlying
    structural condition it was about is technically still true (e.g.
    a confirmed SKU_CONFLICT stays confirmed) -- `find_review_item`
    matches regardless of status. A genuinely new or still-unresolved
    distinct problem (a second unknown ingredient never addressed)
    always keeps or moves the record to NEEDS_REVIEW. This is what
    makes `VALIDATED` an honest claim about the record's current data,
    never an artifact of "zero open rows happened to remain"."""
    normalized = import_record["normalized_payload"]
    findings = await compute_validation_findings(conn, normalized)

    for finding in findings:
        existing = await repo.find_review_item(
            conn, import_record["id"], finding.reason_code, finding.identity_key,
        )
        if existing is None:
            await repo.create_review_item(
                conn, import_record_id=import_record["id"], reason_code=finding.reason_code,
                identity_key=finding.identity_key,
            )

    open_count = await repo.count_open_review_items(conn, import_record["id"])
    reason_codes = sorted({f.reason_code for f in findings})
    status = "NEEDS_REVIEW" if open_count > 0 else "VALIDATED"

    await repo.update_import_record(conn, import_record["id"], status=status, review_reason_codes=reason_codes)
    return {"status": status, "review_reason_codes": reason_codes}
