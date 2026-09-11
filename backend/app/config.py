from typing import List, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that must never appear in a production DATABASE_URL -- every
# one of these is a real dev/CI-only credential or host that exists
# elsewhere in this repository (.env, docker-compose.yml, this
# session's own migration for the skincare_app role, and CI's service
# containers), not a hypothetical.
_DEV_ONLY_DATABASE_URL_MARKERS = (
    "localhost",
    "127.0.0.1",
    "postgres:postgres@",
    "skincare_app_dev_only",
)
_PLACEHOLDER_SECRET_VALUES = {
    "",
    "changeme",
    "change-me",
    "secret",
    "placeholder",
    "test-only-secret-never-used-outside-pytest-0123456789abcdef",
    "ci-only-secret-never-used-outside-github-actions-0123456789ab",
}
_PLACEHOLDER_R2_MARKERS = ("changeme", "your-", "placeholder", "example")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    jwt_secret: str
    redis_url: str
    database_url: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    redis_connect_timeout_seconds: float = 5.0
    redis_socket_timeout_seconds: float = 5.0

    enable_docs: bool = True
    allowed_origins: List[str] = ["*"]
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Database connection-pool sizing (Phase 11). Defaults match the
    # values already proven in app/db/connection.py; production
    # deployments can widen/narrow via env without a code change.
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_pool_connect_timeout_seconds: float = 10.0
    db_pool_command_timeout_seconds: float = 30.0

    # Atomic Redis rate limiting (see app/middleware/rate_limiter.py).
    # Three policies, each independently configurable: auth endpoints
    # (signup/login/refresh, keyed by client IP -- no session exists
    # yet), analysis submission (keyed by user_id, the expensive CV
    # path), general authenticated API (keyed by user_id). Fail-open
    # vs fail-closed on a Redis outage is a deliberate per-policy
    # choice, not a single blanket one -- see
    # USAGE_AND_RATE_LIMIT_ARCHITECTURE.md's "Rate limit failure
    # policy" section for why auth/analysis fail closed and general
    # reads fail open.
    rate_limit_auth_max: int = 10
    rate_limit_auth_window_seconds: int = 60
    rate_limit_analysis_max: int = 20
    rate_limit_analysis_window_seconds: int = 3600
    rate_limit_general_max: int = 120
    rate_limit_general_window_seconds: int = 60

    # Only X-Forwarded-For values relayed by a listed, trusted
    # reverse-proxy peer IP are honored for IP-based rate-limit keying
    # -- otherwise any client could simply forge the header to any
    # value (including another real user's IP) and evade IP-based
    # limiting entirely. Empty by default: an unconfigured deployment
    # (no reverse proxy declared as trusted) uses the direct TCP peer
    # IP only, which is always correct even if less useful behind an
    # undeclared proxy.
    trusted_proxies: List[str] = []

    # Object storage (Phase 6/7). All optional: an unconfigured
    # deployment must keep working unmodified for as long as
    # async_image_storage_enabled stays False below (the default).
    r2_account_id: Optional[str] = None
    r2_access_key_id: Optional[str] = None
    r2_secret_access_key: Optional[str] = None
    r2_bucket: Optional[str] = None
    r2_endpoint: Optional[str] = None

    # Async analysis submission (production recommendation pass, Part
    # IV/V). Defaults False -- see CONSENT_ASYNC_PROCESSING_REVIEW.md:
    # this pass builds the full async image-transport pipeline
    # (EphemeralAnalysisImageStore on top of R2, a real CV worker), but
    # it must not run against real user traffic until a real legal/
    # product review confirms the consent language covers transient
    # third-party cloud storage during processing, which this
    # engineering pass has no authority to assert on its own.
    # AnalysisSubmissionService checks this and refuses to enqueue
    # (503) when False, rather than silently proceeding.
    async_image_storage_enabled: bool = False

    # Minimal cell-readiness (Part VII): the launch defaults a new/
    # unplaced user is assigned by UserPlacementService. Configurable
    # per deployment via env, never hardcoded at any call site --
    # these two settings are the *only* place a literal region/cell
    # value should ever appear. This is metadata/contract readiness
    # only; it does not stand up any actual multi-region
    # infrastructure.
    launch_home_region: str = "us-east"
    launch_cell_id: str = "use1-001"

    # RevenueCat billing synchronization (see BILLING_ARCHITECTURE.md).
    # Defaults False/unset -- an unconfigured deployment keeps using
    # FreeTierEntitlementService unmodified (see
    # app/domain/entitlement.py's build_entitlement_service), same
    # "off by default, explicit opt-in" posture as
    # async_image_storage_enabled above. Enabling this without the
    # three secrets configured is refused outright in production (see
    # _reject_unsafe_production_config below) -- there is no silent
    # partial-billing mode.
    revenuecat_billing_enabled: bool = False
    revenuecat_webhook_auth: Optional[str] = None
    revenuecat_webhook_signing_secret: Optional[str] = None
    revenuecat_api_key: Optional[str] = None
    revenuecat_project_id: Optional[str] = None
    # This app currently models exactly one paid entitlement -- see
    # RevenueCatEntitlementService's own docstring for why every event
    # is projected under this single configured identifier rather than
    # whatever entitlement_ids a given event happens to carry.
    revenuecat_entitlement_id: str = "premium"
    # RevenueCat's own docs suggest ~5 minutes as a reasonable replay
    # tolerance for HMAC signature timestamps -- not a fabricated
    # number, see REVENUECAT_INTEGRATION_NOTES.md section 1.
    revenuecat_webhook_signature_tolerance_seconds: int = 300
    # Business allowances, not hardcoded anywhere in domain code --
    # see RevenueCatEntitlementService. free matches
    # FreeTierEntitlementService's own existing default so enabling
    # RevenueCat billing with no paying users yet changes nothing.
    revenuecat_free_tier_allowance: int = 3
    revenuecat_paid_tier_allowance: int = 100

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @model_validator(mode="after")
    def _reject_unsafe_production_config(self) -> "Settings":
        """
        Fails application startup outright, not a warning log, when
        ENVIRONMENT=production carries configuration that is only ever
        correct for local dev/CI -- a default JWT secret, a
        dev-database host/credential, docs left open, or a wildcard
        CORS policy. Each marker below is a real value that exists
        somewhere else in this repository (.env, docker-compose.yml,
        conftest.py, ci.yml) -- this only rejects genuinely
        distinguishable dev/CI artifacts making it into a production
        deploy, not a guess at what "looks unsafe".
        """
        if not self.is_production:
            return self

        errors: List[str] = []

        if self.jwt_secret.strip().lower() in _PLACEHOLDER_SECRET_VALUES:
            errors.append("JWT_SECRET is blank or a known placeholder/dev/CI value")
        if len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET is shorter than 32 characters")

        lowered_db_url = self.database_url.lower()
        for marker in _DEV_ONLY_DATABASE_URL_MARKERS:
            if marker in lowered_db_url:
                errors.append(f"DATABASE_URL contains a dev/CI-only marker ({marker!r})")

        if "localhost" in self.redis_url.lower() or "127.0.0.1" in self.redis_url.lower():
            errors.append("REDIS_URL points at localhost")

        if self.enable_docs:
            errors.append("ENABLE_DOCS=true exposes /docs, /redoc, and the raw OpenAPI schema")

        if self.allowed_origins == ["*"]:
            errors.append("ALLOWED_ORIGINS is wildcard (\"*\") -- set explicit origins for production")

        for field_name, value in (
            ("R2_ACCOUNT_ID", self.r2_account_id),
            ("R2_ACCESS_KEY_ID", self.r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", self.r2_secret_access_key),
            ("R2_BUCKET", self.r2_bucket),
            ("R2_ENDPOINT", self.r2_endpoint),
        ):
            if value and any(marker in value.lower() for marker in _PLACEHOLDER_R2_MARKERS):
                # Deliberately not including `value` itself in the
                # message -- it may be the secret key.
                errors.append(f"{field_name} looks like a placeholder value")

        if self.revenuecat_billing_enabled:
            for field_name, value in (
                ("REVENUECAT_WEBHOOK_AUTH", self.revenuecat_webhook_auth),
                ("REVENUECAT_WEBHOOK_SIGNING_SECRET", self.revenuecat_webhook_signing_secret),
                ("REVENUECAT_API_KEY", self.revenuecat_api_key),
                ("REVENUECAT_PROJECT_ID", self.revenuecat_project_id),
            ):
                if not value or value.strip().lower() in _PLACEHOLDER_SECRET_VALUES:
                    errors.append(
                        f"REVENUECAT_BILLING_ENABLED=true but {field_name} is blank or a placeholder"
                    )

        if errors:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production and unsafe configuration:\n  - "
                + "\n  - ".join(errors)
            )
        return self


settings = Settings()
