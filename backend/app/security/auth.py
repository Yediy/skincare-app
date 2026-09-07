from typing import Optional

from fastapi import Header, HTTPException
from jose import JWTError
from redis.exceptions import RedisError

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

    return user_id
