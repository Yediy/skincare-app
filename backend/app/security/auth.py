import uuid
from typing import Optional

import asyncpg
from fastapi import Header, HTTPException
from jose import JWTError
from redis.exceptions import RedisError

from app.db.connection import get_db_pool
from app.redis_client import get_redis
from app.security.tokens import decode_access_token


async def get_current_user(authorization: Optional[str] = Header(None)) -> str:
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
        raise HTTPException(status_code=503, detail="Authentication service temporarily unavailable")

    # Account-state check happens against Postgres directly, not a
    # Redis artifact -- a disabled/deleted account is rejected on every
    # request from the moment the DB row changes, with no dependency on
    # any TTL surviving long enough to cover the access token's own
    # remaining lifetime.
    #
    # users has row-level security (see migration feb038fd05bd): this
    # is the self-lookup-by-id path, so app.current_user_id is set to
    # the JWT's own claimed subject as the first statement of an
    # explicit transaction, same pattern as profile_repository.py. The
    # JWT signature is already verified above -- RLS here is defense
    # in depth against a query bug reading another user's row, not the
    # authentication step itself.
    try:
        pool = get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
                user = await conn.fetchrow(
                    "SELECT is_active, deleted_at FROM users WHERE id = $1",
                    uuid.UUID(user_id),
                )
    except (asyncpg.PostgresError, OSError):
        raise HTTPException(status_code=503, detail="Authentication service temporarily unavailable")

    if user is None or not user["is_active"] or user["deleted_at"] is not None:
        raise HTTPException(status_code=401, detail="Account is disabled or no longer exists")

    return user_id
