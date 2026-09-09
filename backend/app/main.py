import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Literal

from asyncpg.exceptions import UniqueViolationError
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.cv.pipeline import FacialAnalysisPipeline, NoFaceDetectedError, CaptureQualityFailedError
from app.ml.scorer import FacialScorer
from app.services.plan_service import PlanService
from app.db.connection import init_db_pool, close_db_pool
from app.redis_client import init_redis, close_redis, get_redis
from app.security.passwords import hash_password, verify_password
from app.security.auth import get_current_user
from app.security.tokens import create_access_token, generate_refresh_token, hash_refresh_token

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Skincare Priority Engine API",
    version="0.1.0",
    # ENABLE_DOCS=false (the required posture in production, enforced
    # by Settings itself) removes /docs, /redoc, and the raw OpenAPI
    # schema entirely, rather than just hiding a link to them.
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    # Auth here is a Bearer token in the Authorization header (set
    # explicitly by the client), never a cookie -- CORS credentials
    # mode governs cookies/TLS-client-certs/HTTP-auth-prompts, not
    # explicit headers, so it's correctly left off. Leaving it on
    # would also conflict with a wildcard origin, which browsers
    # reject for credentialed requests anyway.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/health/live")
async def liveness():
    """Is the application process alive? Deliberately has no
    dependency on Postgres/Redis/anything external -- a liveness probe
    that can fail because a downstream dependency is briefly down
    causes an orchestrator to restart-loop a perfectly healthy process
    instead of just holding it out of rotation, which is what
    readiness is for."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    """Can this instance safely serve traffic right now? Checks the
    two hard dependencies every real request needs. Either check
    failing fails the whole probe -- a half-working instance should be
    taken out of rotation, not left serving requests doomed to 503."""
    from app.db.connection import get_db_pool

    checks: dict[str, str] = {}
    healthy = True

    try:
        pool = get_db_pool()
        await pool.fetchval("SELECT 1")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"unavailable: {e.__class__.__name__}"
        healthy = False

    try:
        r = get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"unavailable: {e.__class__.__name__}"
        healthy = False

    if not healthy:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


class AnalyzeRequest(BaseModel):
    image_base64: str


@app.post("/analyze")
async def analyze(request: AnalyzeRequest, user_id: str = Depends(get_current_user)):
    """
    A thin HTTP adapter over app.domain.analysis_service.perform_analysis
    (Phase 14) -- this route no longer contains the analysis logic
    itself, only the translation from that domain function's plain
    exceptions/result to HTTP status codes/bodies. The domain function
    is what a future background worker would call too, unchanged.
    """
    from app.db.connection import get_db_pool
    from app.domain.analysis_service import (
        AnalysisRequest,
        ConsentRequiredError,
        InvalidImageError,
        perform_analysis,
    )

    try:
        result = await perform_analysis(
            get_db_pool(),
            AnalysisRequest(user_id=user_id, image_base64=request.image_base64),
            pipeline=pipeline,
            scorer=scorer,
            plan_service=plan_service,
        )
    except ConsentRequiredError as e:
        raise HTTPException(
            status_code=403,
            detail=f"Consent required for facial analysis (policy version {e.required_policy_version}). "
                   f"Grant it via POST /consent before calling /analyze.",
        )
    except InvalidImageError:
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64")
    except NoFaceDetectedError:
        raise HTTPException(status_code=422, detail="No face detected. Please retake the photo.")
    except CaptureQualityFailedError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Capture quality too low for analysis. Please retake the photo.",
                "capture_assessment": e.capture_assessment.to_dict(),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid image: {e}")

    return {
        "plan": result.plan,
        "scores": result.scores,
        "metric_results": result.metric_results,
        "capture_assessment": result.capture_assessment,
        "eligible_for_longitudinal_comparison": result.eligible_for_longitudinal_comparison,
    }


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


@app.post("/signup")
async def signup(request: SignupRequest):
    """
    The new user's id is generated here in Python (not left to the
    table's default gen_random_uuid()) so that app.current_user_id can
    be set to it *before* the INSERT, in the same transaction. That's
    required, not cosmetic: Postgres RLS applies the table's
    SELECT-applicable policy to `RETURNING` too, so an INSERT whose
    row doesn't satisfy that policy fails on RETURNING even though
    WITH CHECK already passed -- a server-generated id could never be
    known in advance to pre-satisfy it.
    """
    from app.db.connection import get_db_pool
    new_id = uuid.uuid4()
    password_hash = hash_password(request.password)
    pool = get_db_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(new_id))
                row = await conn.fetchrow(
                    "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3) RETURNING id, email, created_at",
                    new_id,
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
    """
    The email lookup below goes through `login_lookup_by_email`, a
    SECURITY DEFINER function (migration feb038fd05bd), not a direct
    SELECT -- at this point in the flow no user_id/session context
    exists yet for RLS to scope a direct table read by, and email
    itself is caller-supplied rather than a secret, so it can't be
    used as an RLS session-context key without that being no
    restriction at all. Once the row is found, the rest of this
    function knows a real user_id and sets app.current_user_id before
    the refresh_tokens insert, same pattern as everywhere else.
    """
    from app.db.connection import get_db_pool
    pool = get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, password_hash FROM login_lookup_by_email($1)",
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
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user["id"]))
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

    refresh_tokens has row-level security (migration feb038fd05bd).
    Both lookups by raw token below are genuinely pre-identity -- the
    caller has proven nothing but possession of that one hash -- so
    each sets app.current_token_hash (not app.current_user_id) as the
    transaction's first statement, matching the token_hash-scoped
    policy. Once a row is found, the owning user_id is known, so
    app.current_user_id is set from it before any subsequent write
    that touches other rows in the same family (which don't share that
    one hash and need the user_id-scoped policy instead).
    """
    from app.db.connection import get_db_pool
    token_hash = hash_refresh_token(request.refresh_token)
    pool = get_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", token_hash)
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

                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
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
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", token_hash)
            existing = await conn.fetchrow(
                "SELECT user_id, family_id, used_at, revoked_at FROM refresh_tokens WHERE token_hash = $1",
                token_hash,
            )
            if existing is not None and existing["used_at"] is not None and existing["revoked_at"] is None:
                family_id = existing["family_id"]
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(existing["user_id"]))
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
    """
    Both statements now run inside one explicit transaction (not two
    separate implicit ones) so that app.current_token_hash, set before
    the lookup, and app.current_user_id, set from its result before
    the family-wide revocation, are both still in scope for the
    UPDATE -- see the /refresh docstring for why refresh_tokens needs
    two different session GUCs.
    """
    from app.db.connection import get_db_pool
    token_hash = hash_refresh_token(request.refresh_token)
    pool = get_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", token_hash)
            row = await conn.fetchrow("SELECT user_id, family_id FROM refresh_tokens WHERE token_hash = $1", token_hash)
            if row is None:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            family_id = row["family_id"]
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(row["user_id"]))
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
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
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

    Both tables' RLS policies key off the same app.current_user_id GUC,
    so one set_config() at the top of one explicit transaction covers
    both statements.
    """
    from app.db.connection import get_db_pool
    pool = get_db_pool()
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
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


class ProfileUpdateRequest(BaseModel):
    has_sensitive_skin: bool = False
    experience_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    max_routine_steps: int = Field(default=10, ge=1, le=20)
    is_pregnant: bool = False
    is_nursing: bool = False
    allergies: List[str] = []
    avoid_ingredients: List[str] = []


@app.get("/profile")
async def get_profile_route(user_id: str = Depends(get_current_user)):
    from app.db.connection import get_db_pool
    from app.db.profile_repository import get_profile
    return await get_profile(get_db_pool(), uuid.UUID(user_id))


@app.put("/profile")
async def update_profile_route(request: ProfileUpdateRequest, user_id: str = Depends(get_current_user)):
    from app.db.connection import get_db_pool
    from app.db.profile_repository import upsert_profile
    await upsert_profile(get_db_pool(), uuid.UUID(user_id), request.model_dump())
    return {"detail": "Profile updated"}


class ConsentGrantRequest(BaseModel):
    consent_type: str = "facial_analysis"
    policy_version: str
    purpose: str
    jurisdiction: str | None = None
    app_version: str | None = None
    platform: str | None = None


@app.post("/consent")
async def grant_consent(request: ConsentGrantRequest, user_id: str = Depends(get_current_user)):
    from app.db.connection import get_db_pool
    from app.db.consent_repository import record_consent
    result = await record_consent(
        get_db_pool(), uuid.UUID(user_id), request.consent_type, request.policy_version,
        request.purpose, request.jurisdiction, request.app_version, request.platform,
    )
    return result


class ConsentWithdrawRequest(BaseModel):
    consent_type: str = "facial_analysis"


@app.post("/consent/withdraw")
async def withdraw_consent_route(request: ConsentWithdrawRequest, user_id: str = Depends(get_current_user)):
    from app.db.connection import get_db_pool
    from app.db.consent_repository import withdraw_consent
    withdrew = await withdraw_consent(get_db_pool(), uuid.UUID(user_id), request.consent_type)
    if not withdrew:
        raise HTTPException(status_code=404, detail="No active consent of this type to withdraw")
    return {"detail": "Consent withdrawn"}
