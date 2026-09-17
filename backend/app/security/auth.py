import uuid
from typing import Optional

import asyncpg
from fastapi import Header, HTTPException
from jwt import InvalidTokenError as JWTError
from redis.exceptions import RedisError

from app.db.connection import get_db_pool
from app.redis_client import get_redis
from app.security.tokens import decode_access_token


async def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    """System Integrity Gate V1, section 4: Postgres is the durable
    revocation authority, not Redis. Redis remains a fast-deny cache
    layer only -- when it's reachable AND says revoked, that's checked
    first purely to skip a DB round trip for the (common, cheap) known-
    revoked case. When Redis is unreachable, this now falls through to
    the authoritative Postgres check below instead of failing the
    request with a 503 -- a durably valid session must keep working
    through a Redis outage, and a durably revoked one must never become
    usable merely because the cache that would have said so is down.

    The single Postgres transaction below is the actual authority: it
    re-derives "has this session been revoked" from the same durable
    refresh_tokens rows /logout, /logout-all, refresh-replay-detection,
    and account deletion already write to (see app/main.py) --
    `family_id` must belong to this user AND that family must still
    have at least one non-revoked refresh_tokens row (an unrevoked
    durable session representation) -- rather than introducing a
    second, separately-maintained revocation table that could drift
    from what those endpoints actually commit."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[len("Bearer "):]

    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload["sub"]
    family_id = payload["family_id"]
    iat = payload["iat"]

    try:
        r = get_redis()
        if await r.get(f"revoked_family:{family_id}"):
            raise HTTPException(status_code=401, detail="Token has been revoked")
        invalid_before = await r.get(f"user_tokens_invalid_before:{user_id}")
        if invalid_before and iat < float(invalid_before):
            raise HTTPException(status_code=401, detail="Token has been revoked")
    except RedisError:
        # Fall through to the authoritative Postgres check below --
        # Redis being unreachable is not itself proof of anything about
        # this session, and must never turn into a fail-closed 503 for
        # a session Postgres can still validate directly.
        pass

    # users/refresh_tokens both have row-level security (migration
    # feb038fd05bd): this is the self-lookup-by-id path, so
    # app.current_user_id is set to the JWT's own claimed subject as
    # the first statement of an explicit transaction, same pattern as
    # profile_repository.py. The JWT signature is already verified
    # above -- RLS here is defense in depth against a query bug reading
    # another user's row, not the authentication step itself.
    try:
        pool = get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
                user = await conn.fetchrow(
                    "SELECT is_active, deleted_at FROM users WHERE id = $1",
                    uuid.UUID(user_id),
                )
                if user is not None and user["is_active"] and user["deleted_at"] is None:
                    # refresh_tokens' own RLS policy (refresh_tokens_by_user)
                    # already scopes this SELECT to user_id =
                    # app.current_user_id -- the explicit `AND user_id = $2`
                    # below is the same defense-in-depth belt-and-suspenders
                    # every other query in this module already applies, not
                    # the only thing standing between this query and another
                    # user's rows.
                    family_active = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM refresh_tokens
                            WHERE family_id = $1 AND user_id = $2 AND revoked_at IS NULL
                        )
                        """,
                        uuid.UUID(family_id), uuid.UUID(user_id),
                    )
                else:
                    family_active = False
    except (asyncpg.PostgresError, OSError):
        raise HTTPException(status_code=503, detail="Authentication service temporarily unavailable")

    if user is None or not user["is_active"] or user["deleted_at"] is not None:
        raise HTTPException(status_code=401, detail="Account is disabled or no longer exists")

    if not family_active:
        raise HTTPException(status_code=401, detail="Token has been revoked")

    return user_id
