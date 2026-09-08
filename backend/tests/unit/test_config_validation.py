"""Production config validation (Phase 4). Every test constructs
Settings directly with `_env_file=None` so it never reads the real
.env -- these are pure unit tests of the validator, not integration
tests against this sandbox's actual environment."""
import pytest

from app.config import Settings

_SAFE_PROD_KWARGS = dict(
    environment="production",
    jwt_secret="a" * 40,
    database_url="postgresql://skincare_app:REDACTED@db.internal.example.com:5432/skincare",
    redis_url="redis://redis.internal.example.com:6379/0",
    enable_docs=False,
    allowed_origins=["https://app.example.com"],
)


def test_safe_production_config_is_accepted():
    settings = Settings(_env_file=None, **_SAFE_PROD_KWARGS)
    assert settings.is_production


def test_development_config_is_never_validated_against_production_rules():
    """The whole point of these rules is that they're production-only
    -- a developer's default .env (localhost DB, wildcard CORS, docs
    on) must keep working unmodified."""
    settings = Settings(
        _env_file=None,
        environment="development",
        jwt_secret="",
        database_url="postgresql://postgres:postgres@localhost:5432/skincare",
        redis_url="redis://localhost:6379/0",
        enable_docs=True,
        allowed_origins=["*"],
    )
    assert not settings.is_production


@pytest.mark.parametrize(
    "override,bad_field",
    [
        ({"jwt_secret": ""}, "jwt_secret"),
        ({"jwt_secret": "changeme"}, "jwt_secret"),
        ({"jwt_secret": "short"}, "jwt_secret"),
        ({"database_url": "postgresql://postgres:postgres@localhost:5432/skincare"}, "database_url"),
        (
            {"database_url": "postgresql://skincare_app:skincare_app_dev_only@db.example.com:5432/skincare"},
            "database_url",
        ),
        ({"redis_url": "redis://localhost:6379/0"}, "redis_url"),
        ({"enable_docs": True}, "enable_docs"),
        ({"allowed_origins": ["*"]}, "allowed_origins"),
    ],
)
def test_production_rejects_each_unsafe_default(override, bad_field):
    kwargs = {**_SAFE_PROD_KWARGS, **override}
    with pytest.raises(ValueError):
        Settings(_env_file=None, **kwargs)
