import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone

from asyncpg.exceptions import UniqueViolationError
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.cv.pipeline import FacialAnalysisPipeline, NoFaceDetectedError, LowQualityCaptureError
from app.ml.scorer import FacialScorer
from app.services.plan_service import PlanService
from app.db.connection import init_db_pool, close_db_pool
from app.redis_client import init_redis, close_redis, get_redis
from app.security.passwords import hash_password, verify_password
from app.security.auth import get_current_user
from app.security.tokens import create_access_token, generate_refresh_token, hash_refresh_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Skincare Priority Engine API", version="0.1.0")


@app.on_event("startup")
async def startup_event():
    await init_db_pool()
    await init_redis()


@app.on_event("shutdown")
async def shutdown_event():
    await close_db_pool()
    await close_redis()


# Constructed once at import time, not per-request -- the MediaPipe model
# is expensive to load, so this must be a singleton, not rebuilt on every call.
pipeline = FacialAnalysisPipeline()
scorer = FacialScorer()
plan_service = PlanService()


@app.get("/health")
async def health_check():
    return {"status": "ok"}


class AnalyzeRequest(BaseModel):
    image_base64: str


@app.post("/analyze")
async def analyze(request: AnalyzeRequest, user_id: str = Depends(get_current_user)):
    try:
        image_bytes = base64.b64decode(request.image_base64)
    except Exception:
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64")
    try:
        extraction_result = pipeline.analyze(image_bytes)
    except NoFaceDetectedError:
        raise HTTPException(status_code=422, detail="No face detected. Please retake the photo.")
    except LowQualityCaptureError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Image quality too low (score: {e.quality_score:.2f}). Try better lighting."
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid image: {e}")

    metrics = extraction_result["metrics"]
    capture_quality = extraction_result["capture_quality"]

    # user_id is now real, from a verified access token. The other four
    # fields are STILL placeholders -- no DB column backs any of them yet.
    # Real profile storage is tracked separately (OPEN_ENGINEERING_ITEMS.md,
    # Tier 2), not silently built here.
    user_profile = {
        "user_id": user_id,
        "is_pregnant": False,           # PLACEHOLDER -- no DB column
        "is_nursing": False,            # PLACEHOLDER -- no DB column
        "has_sensitive_skin": False,    # PLACEHOLDER -- no DB column
        "experience_level": "beginner", # PLACEHOLDER -- no DB column
    }
    analysis = scorer.compute_scores(metrics, capture_quality=capture_quality)
    plan = plan_service.generate_plan(analysis["scores"], analysis["insights"], user_profile, capture_quality)
    return {"plan": plan, "scores": analysis["scores"]}


@app.get("/db-check")
async def db_check():
    from app.db.connection import get_db_pool
    pool = get_db_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
    return {"user_count": count}


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


@app.post("/signup")
async def signup(request: SignupRequest):
    from app.db.connection import get_db_pool
    password_hash = hash_password(request.password)
    pool = get_db_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id, email, created_at",
                request.email,
                password_hash,
            )
    except UniqueViolationError:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    return {"id": str(row["id"]), "email": row["email"], "created_at": row["created_at"].isoformat()}


# Precompute once at module load, not per-request -- used to burn
# equivalent bcrypt time on the nonexistent-email path so it isn't
# distinguishable by timing from the wrong-password path.
_DUMMY_HASH = hash_password("dummy-password-for-timing-safety-only")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@app.post("/login")
async def login(request: LoginRequest):
    from app.db.connection import get_db_pool
    pool = get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1",
            request.email,
        )

    if user is None:
        verify_password(request.password, _DUMMY_HASH)  # burn the time cost
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    family_id = str(uuid.uuid4())
    access_token = create_access_token(str(user["id"]), family_id)
    refresh_raw, refresh_hash = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at) VALUES ($1, $2, $3, $4)",
            user["id"], family_id, refresh_hash, expires_at,
        )

    return {"access_token": access_token, "refresh_token": refresh_raw, "token_type": "bearer"}


class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/refresh")
async def refresh(request: RefreshRequest):
    """
    The old-token consumption and successor creation happen inside one
    explicit transaction. If successor insertion fails for any reason,
    the whole transaction rolls back, so the old token's `used_at` reverts
    to NULL -- the caller can safely retry with the same original token
    instead of being left with no usable refresh token at all.

    The replay-detection path (row is None because the token was already
    used) is a separate transaction, deliberately opened after the first
    one has closed -- it must commit its family-wide revocation even
    though this request is about to fail with a 401.
    """
    from app.db.connection import get_db_pool
    token_hash = hash_refresh_token(request.refresh_token)
    pool = get_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE refresh_tokens
                SET used_at = now()
                WHERE token_hash = $1 AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
                RETURNING user_id, family_id
                """,
                token_hash,
            )

            if row is not None:
                user_id, family_id = row["user_id"], row["family_id"]
                new_access_token = create_access_token(str(user_id), str(family_id))
                new_refresh_raw, new_refresh_hash = generate_refresh_token()
                new_expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)

                await conn.execute(
                    "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at) VALUES ($1, $2, $3, $4)",
                    user_id, family_id, new_refresh_hash, new_expires_at,
                )
                return {"access_token": new_access_token, "refresh_token": new_refresh_raw, "token_type": "bearer"}

        # row was None: token never existed, is expired/revoked, or --
        # the interesting case -- was already consumed (replay). This
        # runs in its own transaction so the revocation below commits
        # even though we raise immediately after.
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT family_id, used_at, revoked_at FROM refresh_tokens WHERE token_hash = $1",
                token_hash,
            )
            if existing is not None and existing["used_at"] is not None and existing["revoked_at"] is None:
                family_id = existing["family_id"]
                await conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL",
                    family_id,
                )
                r = get_redis()
                await r.set(f"revoked_family:{family_id}", "1", ex=settings.access_token_expire_minutes * 60)

    raise HTTPException(status_code=401, detail="Invalid or expired refresh token")


class LogoutRequest(BaseModel):
    refresh_token: str


@app.post("/logout")
async def logout(request: LogoutRequest):
    from app.db.connection import get_db_pool
    token_hash = hash_refresh_token(request.refresh_token)
    pool = get_db_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT family_id FROM refresh_tokens WHERE token_hash = $1", token_hash)
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        family_id = row["family_id"]
        await conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL",
            family_id,
        )

    r = get_redis()
    await r.set(f"revoked_family:{family_id}", "1", ex=settings.access_token_expire_minutes * 60)

    return {"detail": "Logged out"}


@app.get("/me")
async def get_me(user_id: str = Depends(get_current_user)):
    return {"user_id": user_id}


@app.post("/logout-all")
async def logout_all(user_id: str = Depends(get_current_user)):
    from app.db.connection import get_db_pool
    r = get_redis()
    now_ts = datetime.now(timezone.utc).timestamp()
    await r.set(
        f"user_tokens_invalid_before:{user_id}",
        str(now_ts),
        ex=settings.access_token_expire_minutes * 60,
    )

    pool = get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL",
            uuid.UUID(user_id),
        )

    return {"detail": "Logged out of all sessions"}


@app.delete("/me")
async def delete_account(user_id: str = Depends(get_current_user)):
    """
    Soft-deletes the authenticated user's own account. The primary
    invalidation guarantee is the `is_active`/`deleted_at` check in
    get_current_user, which reads Postgres directly on every request --
    it does not depend on any Redis TTL. Refresh tokens are also
    revoked and the Redis family/user markers are set as defense in
    depth, but a stale/expired Redis key can never leave a deleted
    account's existing access tokens usable, unlike the reverse.
    """
    from app.db.connection import get_db_pool
    pool = get_db_pool()
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_active = false, deleted_at = now() WHERE id = $1",
            user_uuid,
        )
        await conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL",
            user_uuid,
        )

    r = get_redis()
    now_ts = datetime.now(timezone.utc).timestamp()
    await r.set(
        f"user_tokens_invalid_before:{user_id}",
        str(now_ts),
        ex=settings.access_token_expire_minutes * 60,
    )

    return {"detail": "Account deleted"}
