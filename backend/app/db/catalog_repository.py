"""Deterministic lookup and ingredient-resolution repository for the
normalized product catalog (migrations d70e5fc90775, 16b82dde6e7d).

Every table this module reads is global reference data with a
SELECT-only grant to skincare_app (no RLS -- there is no per-row
owner to scope by; see those migrations' docstrings) -- this module
never writes to any of them.

Ingredient resolution is exact-match against a normalized form, never
substring/LIKE matching: normalize_name() is the single place that
normalization happens, reused for every lookup (brands, products,
ingredients, aliases) so two different callers can never disagree on
whether "Vitamin C" and "vitamin   c" are the same string.
"""
import json
import re
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

import asyncpg

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """The one, single normalization rule every catalog lookup in this
    module uses: lowercase, trimmed, internal whitespace collapsed to
    a single space. Deliberately not stripping punctuation/accents --
    that is a real, separate i18n/data-quality decision this pass does
    not make casually; adding it later only makes matching more
    permissive, never less, so deferring it is safe."""
    return _WHITESPACE_RE.sub(" ", name.strip().lower())


def canonical_ingredient_pair(ingredient_a_id: UUID, ingredient_b_id: UUID) -> tuple[UUID, UUID]:
    """Matches the DB-level CHECK constraint
    (ingredient_interactions_canonical_order) exactly: string
    comparison of two UUIDs in their canonical (lowercase, hyphenated)
    form orders identically to Postgres's own `<` on the `uuid` type,
    which compares the 16-byte binary form -- so this is not an
    approximation of the DB's ordering, it is the same ordering."""
    a, b = str(ingredient_a_id), str(ingredient_b_id)
    return (ingredient_a_id, ingredient_b_id) if a < b else (ingredient_b_id, ingredient_a_id)


async def get_formulation_by_id(pool: asyncpg.Pool, formulation_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        """
        SELECT f.id, f.product_id, f.version, f.market_or_region, f.is_current,
               f.effective_from, f.effective_to, f.source_type, f.source_reference, f.verified_at,
               f.ingredient_data_status,
               p.name AS product_name, p.category AS product_category, p.brand_id, p.status AS product_status
        FROM product_formulations f JOIN products p ON p.id = f.product_id
        WHERE f.id = $1
        """,
        formulation_id,
    )
    return dict(row) if row is not None else None


async def get_current_formulation_for_product(
    pool: asyncpg.Pool, product_id: UUID, market_or_region: str = "global"
) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        """
        SELECT f.id, f.product_id, f.version, f.market_or_region, f.is_current,
               f.effective_from, f.effective_to, f.source_type, f.source_reference, f.verified_at,
               f.ingredient_data_status,
               p.name AS product_name, p.category AS product_category, p.brand_id, p.status AS product_status
        FROM product_formulations f JOIN products p ON p.id = f.product_id
        WHERE f.product_id = $1 AND f.market_or_region = $2 AND f.is_current = true
        """,
        product_id, market_or_region,
    )
    return dict(row) if row is not None else None


async def get_formulation_by_sku(pool: asyncpg.Pool, sku: str, product_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    """Deterministic lookup by SKU. If product_id is not given, the
    SKU must be unambiguous across the whole catalog (sku is only
    unique per-product at the schema level, not globally) -- callers
    that already know the product_id should pass it."""
    if product_id is not None:
        row = await pool.fetchrow(
            "SELECT formulation_id FROM product_skus WHERE product_id = $1 AND sku = $2",
            product_id, sku,
        )
    else:
        rows = await pool.fetch("SELECT formulation_id FROM product_skus WHERE sku = $1", sku)
        row = rows[0] if len(rows) == 1 else None
    if row is None:
        return None
    return await get_formulation_by_id(pool, row["formulation_id"])


async def list_current_active_formulations_by_category(
    pool: asyncpg.Pool, category: str, market_or_region: str
) -> List[Dict[str, Any]]:
    """Candidate lookup for ProductMatchingService (Part I, Phase 9).
    Only ever returns: active products, current formulations, exactly
    the requested market_or_region (never a different specific region
    -- global fallback is the caller's own explicit second call with
    market_or_region='global', never silently substituted here), and
    COMPLETE ingredient data (filtered here as an efficiency measure --
    evaluate_product_formulation() independently re-enforces this same
    rule regardless, so this is defense in depth, not the only
    mechanism). Ordered by verification freshness, most recent first,
    with product_id as a final deterministic tie-break for anything
    with the same (or no) verified_at."""
    rows = await pool.fetch(
        """
        SELECT f.id, f.product_id, f.market_or_region, f.verified_at, f.ingredient_data_status,
               p.name AS product_name, p.category AS product_category, b.id AS brand_id, b.name AS brand_name
        FROM product_formulations f
        JOIN products p ON p.id = f.product_id
        JOIN brands b ON b.id = p.brand_id
        WHERE p.category = $1
          AND p.status = 'active'
          AND f.is_current = true
          AND f.market_or_region = $2
          AND f.ingredient_data_status = 'COMPLETE'
        ORDER BY f.verified_at DESC NULLS LAST, f.product_id
        """,
        category, market_or_region,
    )
    return [dict(r) for r in rows]


async def get_representative_sku_for_formulation(pool: asyncpg.Pool, formulation_id: UUID) -> Optional[Dict[str, Any]]:
    """A formulation may back several SKUs (different sizes/packaging).
    Returns one deterministically (lowest sku string) for display
    purposes -- ProductMatch doesn't need to enumerate every SKU, just
    a representative one to reference."""
    row = await pool.fetchrow(
        "SELECT id, sku FROM product_skus WHERE formulation_id = $1 AND active = true ORDER BY sku LIMIT 1",
        formulation_id,
    )
    return dict(row) if row is not None else None


async def get_product_by_id(pool: asyncpg.Pool, product_id: UUID) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        """
        SELECT p.id, p.brand_id, p.name, p.category, p.description, p.status, b.name AS brand_name
        FROM products p JOIN brands b ON b.id = p.brand_id
        WHERE p.id = $1
        """,
        product_id,
    )
    return dict(row) if row is not None else None


async def resolve_ingredient(pool: asyncpg.Pool, name: str) -> Optional[Dict[str, Any]]:
    """Exact match against the canonical name first, then aliases --
    never a substring/LIKE match. Returns None (not a fabricated
    match) when nothing resolves; callers must treat that as "unknown
    ingredient", not "no restrictions apply"."""
    normalized = normalize_name(name)
    row = await pool.fetchrow(
        "SELECT id, canonical_name, normalized_name, inci_name, ingredient_type "
        "FROM ingredients WHERE normalized_name = $1",
        normalized,
    )
    if row is not None:
        return dict(row)

    row = await pool.fetchrow(
        """
        SELECT i.id, i.canonical_name, i.normalized_name, i.inci_name, i.ingredient_type
        FROM ingredient_aliases a JOIN ingredients i ON i.id = a.ingredient_id
        WHERE a.normalized_alias = $1
        """,
        normalized,
    )
    return dict(row) if row is not None else None


async def get_formulation_ingredients(pool: asyncpg.Pool, formulation_id: UUID) -> List[Dict[str, Any]]:
    """Ordered by declared position -- ingredient order is a real,
    preserved property of a formulation, not incidental."""
    rows = await pool.fetch(
        """
        SELECT fi.ingredient_id, i.canonical_name, i.normalized_name, i.inci_name, i.ingredient_type,
               fi.position, fi.declared_concentration, fi.concentration_unit, fi.notes
        FROM formulation_ingredients fi JOIN ingredients i ON i.id = fi.ingredient_id
        WHERE fi.formulation_id = $1
        ORDER BY fi.position
        """,
        formulation_id,
    )
    return [dict(r) for r in rows]


async def get_active_rules_for_ingredients(pool: asyncpg.Pool, ingredient_ids: Sequence[UUID]) -> List[Dict[str, Any]]:
    if not ingredient_ids:
        return []
    rows = await pool.fetch(
        """
        SELECT id, ingredient_id, rule_type, severity, action, reason_code,
               evidence_grade, source_reference, rules_version, parameters
        FROM ingredient_rules
        WHERE ingredient_id = ANY($1::uuid[]) AND active = true
        """,
        list(ingredient_ids),
    )
    results = []
    for r in rows:
        rule = dict(r)
        # asyncpg returns jsonb as raw text unless a type codec is
        # registered (none is, in this codebase -- see
        # app/queue/postgres_queue.py's identical pattern for `jobs.payload`).
        params = rule["parameters"]
        rule["parameters"] = json.loads(params) if isinstance(params, str) else (params or {})
        results.append(rule)
    return results


async def get_interactions_within(pool: asyncpg.Pool, ingredient_ids: Sequence[UUID]) -> List[Dict[str, Any]]:
    """Interactions where *both* members of the pair are present in
    ingredient_ids. Deliberately not scoped to one formulation's own
    ingredient list -- the same query is reused for routine-level,
    cross-product interaction evaluation (Part I, Phase 8) by passing
    the union of ingredient IDs across every formulation in a proposed
    routine, not just one formulation's own set."""
    if len(ingredient_ids) < 2:
        return []
    id_list = list(ingredient_ids)
    rows = await pool.fetch(
        """
        SELECT id, ingredient_a_id, ingredient_b_id, interaction_type, severity,
               reason_code, recommendation, recommended_action, rules_version
        FROM ingredient_interactions
        WHERE ingredient_a_id = ANY($1::uuid[]) AND ingredient_b_id = ANY($1::uuid[])
        """,
        id_list,
    )
    return [dict(r) for r in rows]
