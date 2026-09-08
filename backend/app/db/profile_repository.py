"""Real user profile persistence, replacing the hardcoded placeholder
values that used to be synthesized directly inside /analyze.

Allergies/avoid_ingredients are stored as plain text arrays for now --
a deliberate, documented placeholder until a normalized
ingredient/allergen entity model exists (tracked as a known follow-up
in OPEN_ENGINEERING_ITEMS.md), not treated as a finished representation.

user_profiles has row-level security enabled (see the
7b38b717546e migration): every query here runs inside an explicit
transaction that sets `app.current_user_id` via set_config() (not a
plain `SET LOCAL ...`, which doesn't accept bind parameters) as the
*first* statement, so RLS can actually scope the query -- forgetting
this would make every row invisible (fail closed), not leak everyone
else's.
"""
from typing import Any, Dict
from uuid import UUID

import asyncpg

# Documented default profile, per Phase 4's explicit requirement: if a
# user has never set a profile, /analyze must use these named defaults
# rather than silently pretending every user has no constraints for
# some other, unstated reason.
DEFAULT_PROFILE: Dict[str, Any] = {
    "has_sensitive_skin": False,
    "experience_level": "beginner",
    "max_routine_steps": 10,
    "is_pregnant": False,
    "is_nursing": False,
    "allergies": [],
    "avoid_ingredients": [],
}


async def get_profile(pool: asyncpg.Pool, user_id: UUID) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                """
                SELECT has_sensitive_skin, experience_level, max_routine_steps,
                       is_pregnant, is_nursing, allergies, avoid_ingredients
                FROM user_profiles WHERE user_id = $1
                """,
                user_id,
            )
    if row is None:
        return dict(DEFAULT_PROFILE)
    return {
        "has_sensitive_skin": row["has_sensitive_skin"],
        "experience_level": row["experience_level"],
        "max_routine_steps": row["max_routine_steps"],
        "is_pregnant": row["is_pregnant"],
        "is_nursing": row["is_nursing"],
        "allergies": list(row["allergies"]),
        "avoid_ingredients": list(row["avoid_ingredients"]),
    }


async def upsert_profile(pool: asyncpg.Pool, user_id: UUID, profile: Dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                """
                INSERT INTO user_profiles (
                    user_id, has_sensitive_skin, experience_level, max_routine_steps,
                    is_pregnant, is_nursing, allergies, avoid_ingredients, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    has_sensitive_skin = EXCLUDED.has_sensitive_skin,
                    experience_level = EXCLUDED.experience_level,
                    max_routine_steps = EXCLUDED.max_routine_steps,
                    is_pregnant = EXCLUDED.is_pregnant,
                    is_nursing = EXCLUDED.is_nursing,
                    allergies = EXCLUDED.allergies,
                    avoid_ingredients = EXCLUDED.avoid_ingredients,
                    updated_at = now()
                """,
                user_id,
                profile["has_sensitive_skin"],
                profile["experience_level"],
                profile["max_routine_steps"],
                profile["is_pregnant"],
                profile["is_nursing"],
                profile["allergies"],
                profile["avoid_ingredients"],
            )
