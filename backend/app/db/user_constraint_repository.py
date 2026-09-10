"""Normalized user ingredient constraints (migration 0f5178eef3fe),
replacing user_profiles.allergies/avoid_ingredients' permanent
free-text-array matching for anything that needs to know *which
canonical ingredient* a user's constraint actually refers to.

user_ingredient_constraints has row-level security (same
app.current_user_id pattern as user_profiles/consent_events/
analysis_usage): every query here sets it as the first statement of
its transaction.

Dual-write, documented (see app/db/profile_repository.py and
PRODUCT_CATALOG_ARCHITECTURE.md): app/db/profile_repository.py's
upsert_profile() continues writing the legacy user_profiles.allergies/
avoid_ingredients arrays (still what PlanService's category-level
evaluate_offer() reads via _extract_user_constraints) *and* now also
calls replace_constraints() here to keep the normalized table in sync.
The normalized table is the source of truth for anything doing real
per-ingredient resolution (formulation-level safety evaluation,
Part I Phase 4's unresolved-constraint gate); the legacy arrays remain
the source of truth for the pre-existing category-level path.
"""
from typing import Any, Dict, List, Sequence
from uuid import UUID

import asyncpg

from app.db.catalog_repository import normalize_name, resolve_ingredient

ALLERGY = "ALLERGY"
AVOID = "AVOID"

RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"


async def replace_constraints(
    pool: asyncpg.Pool, user_id: UUID, constraint_type: str, raw_texts: Sequence[str]
) -> List[Dict[str, Any]]:
    """Full-replace semantics, matching how PUT /profile already fully
    replaces the profile: every existing row of this constraint_type
    for this user is deleted and replaced with freshly-resolved rows
    for the given raw_texts. Each raw_text is resolved through the
    exact same catalog_repository.resolve_ingredient() path
    formulation-safety evaluation itself uses -- an unresolvable entry
    is stored as UNRESOLVED (ingredient_id NULL), never guessed."""
    entries: List[Dict[str, Any]] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "DELETE FROM user_ingredient_constraints WHERE user_id = $1 AND constraint_type = $2",
                user_id, constraint_type,
            )
            for raw_text in raw_texts:
                if not raw_text.strip():
                    continue
                resolved = await resolve_ingredient(conn, raw_text)
                ingredient_id = resolved["id"] if resolved is not None else None
                resolution_status = RESOLVED if resolved is not None else UNRESOLVED
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_ingredient_constraints
                        (user_id, constraint_type, raw_text, normalized_raw_text, ingredient_id, resolution_status)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (user_id, constraint_type, normalized_raw_text) DO UPDATE SET
                        raw_text = EXCLUDED.raw_text,
                        ingredient_id = EXCLUDED.ingredient_id,
                        resolution_status = EXCLUDED.resolution_status,
                        updated_at = now()
                    RETURNING id, raw_text, ingredient_id, resolution_status
                    """,
                    user_id, constraint_type, raw_text, normalize_name(raw_text), ingredient_id, resolution_status,
                )
                entries.append(dict(row))
    return entries


async def get_constraints(pool: asyncpg.Pool, user_id: UUID, constraint_type: str = None) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            if constraint_type is not None:
                rows = await conn.fetch(
                    "SELECT id, constraint_type, raw_text, ingredient_id, resolution_status "
                    "FROM user_ingredient_constraints WHERE user_id = $1 AND constraint_type = $2",
                    user_id, constraint_type,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, constraint_type, raw_text, ingredient_id, resolution_status "
                    "FROM user_ingredient_constraints WHERE user_id = $1",
                    user_id,
                )
    return [dict(r) for r in rows]


async def has_unresolved_constraints(pool: asyncpg.Pool, user_id: UUID) -> bool:
    """Part I Phase 4's gate: an unresolved allergy/avoid entry must
    block specific-product recommendation, not be silently treated as
    though it didn't exist."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchval(
                "SELECT 1 FROM user_ingredient_constraints WHERE user_id = $1 AND resolution_status = $2 LIMIT 1",
                user_id, UNRESOLVED,
            )
    return row is not None
