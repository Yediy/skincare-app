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

    # Object storage (Phase 6/7). All optional: nothing in the
    # application calls into R2 yet (see PRODUCTION_ARCHITECTURE.md --
    # face images are deliberately not persisted there), so an
    # unconfigured deployment must keep working unmodified. Once a
    # real feature depends on R2, these become required the same way
    # jwt_secret/database_url already are.
    r2_account_id: Optional[str] = None
    r2_access_key_id: Optional[str] = None
    r2_secret_access_key: Optional[str] = None
    r2_bucket: Optional[str] = None
    r2_endpoint: Optional[str] = None

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

        if errors:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production and unsafe configuration:\n  - "
                + "\n  - ".join(errors)
            )
        return self


settings = Settings()
