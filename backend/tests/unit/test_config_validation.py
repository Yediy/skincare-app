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
    # Password recovery is always-on production surface (V1 account
    # recovery pass) -- every _SAFE_PROD_KWARGS-based test needs a
    # valid email-provider configuration or it would trip the new
    # EMAIL_PROVIDER validation for reasons unrelated to what it's
    # actually testing. See the dedicated "Password recovery /
    # transactional email" section below for tests of this validation
    # itself.
    email_provider="resend",
    resend_api_key="a-real-configured-resend-api-key",
    password_reset_from_email="noreply@app.example.com",
    password_reset_url_base="https://app.skincare-launch.internal/reset-password",
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


def test_production_rejects_revenuecat_billing_enabled_without_secrets():
    with pytest.raises(ValueError):
        Settings(_env_file=None, **_SAFE_PROD_KWARGS, revenuecat_billing_enabled=True)


_REVENUECAT_SAFE_KWARGS = dict(
    revenuecat_billing_enabled=True,
    revenuecat_webhook_auth="a-real-configured-auth-value",
    revenuecat_webhook_signing_secret="a-real-configured-signing-secret",
    revenuecat_api_key="a-real-configured-api-key",
    revenuecat_project_id="a-real-configured-project-id",
    revenuecat_billing_database_url="postgresql://skincare_billing_runtime:REDACTED@db.internal.example.com:5432/skincare",
)


def test_production_accepts_revenuecat_billing_enabled_with_all_secrets_configured():
    settings = Settings(_env_file=None, **_SAFE_PROD_KWARGS, **_REVENUECAT_SAFE_KWARGS)
    assert settings.revenuecat_billing_enabled is True


def test_production_rejects_revenuecat_billing_enabled_with_only_some_secrets_configured():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **_SAFE_PROD_KWARGS,
            revenuecat_billing_enabled=True,
            revenuecat_webhook_auth="a-real-configured-auth-value",
            revenuecat_webhook_signing_secret="a-real-configured-signing-secret",
            revenuecat_billing_database_url=_REVENUECAT_SAFE_KWARGS["revenuecat_billing_database_url"],
            # api_key/project_id left unset
        )


def test_production_rejects_revenuecat_billing_enabled_without_billing_database_url():
    """Billing mutation must not silently fall back to the ordinary
    runtime DATABASE_URL -- see migration 9815eb266923."""
    kwargs = {**_REVENUECAT_SAFE_KWARGS}
    kwargs.pop("revenuecat_billing_database_url")
    with pytest.raises(ValueError):
        Settings(_env_file=None, **_SAFE_PROD_KWARGS, **kwargs)


def test_production_rejects_revenuecat_billing_database_url_identical_to_database_url():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **{**_SAFE_PROD_KWARGS, **_REVENUECAT_SAFE_KWARGS,
               "revenuecat_billing_database_url": _SAFE_PROD_KWARGS["database_url"]},
        )


def test_production_rejects_revenuecat_billing_database_url_with_dev_marker():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **{**_SAFE_PROD_KWARGS, **_REVENUECAT_SAFE_KWARGS,
               "revenuecat_billing_database_url": "postgresql://skincare_billing_runtime:skincare_billing_dev_only@localhost:5432/skincare"},
        )


def test_production_rejects_known_dev_billing_credential_even_off_localhost():
    """`skincare_billing_dev_only` is rejected as its own marker, not
    merely caught incidentally by the `localhost` check above --
    proven here against a real-looking production host, since
    `skincare_billing` itself being NOLOGIN at head (migration
    1367b870bdcd) means this exact literal could otherwise slip through as
    plausible-looking runtime-login copy-paste."""
    with pytest.raises(ValueError) as exc_info:
        Settings(
            _env_file=None,
            **{**_SAFE_PROD_KWARGS, **_REVENUECAT_SAFE_KWARGS,
               "revenuecat_billing_database_url":
                   "postgresql://skincare_billing_runtime:skincare_billing_dev_only@db.internal.example.com:5432/skincare"},
        )
    assert "skincare_billing_dev_only" in str(exc_info.value)


def test_development_config_unaffected_by_revenuecat_billing_flag():
    """Same posture as the rest of this validator -- development stays
    unmodified even with billing "enabled" but unconfigured."""
    settings = Settings(
        _env_file=None,
        environment="development",
        jwt_secret="",
        database_url="postgresql://postgres:postgres@localhost:5432/skincare",
        redis_url="redis://localhost:6379/0",
        enable_docs=True,
        allowed_origins=["*"],
        revenuecat_billing_enabled=True,
    )
    assert not settings.is_production


# ---------------------------------------------------------------------------
# Catalog ingestion/administration CLI (CATALOG_INGESTION_ARCHITECTURE.md) --
# same validation shape as revenuecat_billing above, one dedicated DSN.
# ---------------------------------------------------------------------------


def test_production_rejects_catalog_admin_enabled_without_database_url():
    with pytest.raises(ValueError) as exc_info:
        Settings(_env_file=None, **_SAFE_PROD_KWARGS, catalog_admin_enabled=True)
    assert "CATALOG_ADMIN_DATABASE_URL" in str(exc_info.value)


def test_production_accepts_catalog_admin_enabled_with_database_url_configured():
    settings = Settings(
        _env_file=None, **_SAFE_PROD_KWARGS, catalog_admin_enabled=True,
        catalog_admin_database_url="postgresql://skincare_catalog_runtime:REDACTED@db.internal.example.com:5432/skincare",
    )
    assert settings.catalog_admin_enabled is True


def test_production_rejects_catalog_admin_database_url_identical_to_database_url():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None, **_SAFE_PROD_KWARGS, catalog_admin_enabled=True,
            catalog_admin_database_url=_SAFE_PROD_KWARGS["database_url"],
        )


def test_production_rejects_catalog_admin_database_url_with_dev_marker():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None, **_SAFE_PROD_KWARGS, catalog_admin_enabled=True,
            catalog_admin_database_url="postgresql://skincare_catalog_runtime:skincare_catalog_dev_only@localhost:5432/skincare",
        )


def test_production_rejects_known_dev_catalog_credential_even_off_localhost():
    with pytest.raises(ValueError) as exc_info:
        Settings(
            _env_file=None, **_SAFE_PROD_KWARGS, catalog_admin_enabled=True,
            catalog_admin_database_url=(
                "postgresql://skincare_catalog_runtime:skincare_catalog_dev_only@db.internal.example.com:5432/skincare"
            ),
        )
    assert "skincare_catalog_dev_only" in str(exc_info.value)


def test_development_config_unaffected_by_catalog_admin_flag():
    settings = Settings(
        _env_file=None,
        environment="development",
        jwt_secret="",
        database_url="postgresql://postgres:postgres@localhost:5432/skincare",
        redis_url="redis://localhost:6379/0",
        enable_docs=True,
        allowed_origins=["*"],
        catalog_admin_enabled=True,
    )
    assert not settings.is_production


# ---------------------------------------------------------------------------
# Password recovery / transactional email (V1 account recovery pass, Part
# 3/8) -- unlike revenuecat_billing_enabled/catalog_admin_enabled there is no
# opt-in flag: password reset is always-on production surface, so production
# always requires a real (non-"none") EMAIL_PROVIDER, not just when some flag
# is set.
# ---------------------------------------------------------------------------


def test_production_rejects_default_email_provider_none():
    kwargs = {**_SAFE_PROD_KWARGS}
    kwargs["email_provider"] = "none"
    with pytest.raises(ValueError) as exc_info:
        Settings(_env_file=None, **kwargs)
    assert "EMAIL_PROVIDER" in str(exc_info.value)


def test_production_rejects_unrecognized_email_provider():
    kwargs = {**_SAFE_PROD_KWARGS}
    kwargs["email_provider"] = "sendgrid"
    with pytest.raises(ValueError) as exc_info:
        Settings(_env_file=None, **kwargs)
    assert "EMAIL_PROVIDER" in str(exc_info.value)


@pytest.mark.parametrize("missing_field", ["resend_api_key", "password_reset_from_email"])
def test_production_rejects_resend_enabled_with_missing_field(missing_field):
    kwargs = {**_SAFE_PROD_KWARGS}
    kwargs[missing_field] = None
    with pytest.raises(ValueError):
        Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("placeholder", ["", "changeme", "placeholder"])
def test_production_rejects_resend_api_key_placeholder(placeholder):
    kwargs = {**_SAFE_PROD_KWARGS, "resend_api_key": placeholder}
    with pytest.raises(ValueError):
        Settings(_env_file=None, **kwargs)


def test_production_rejects_dev_scheme_password_reset_url_base():
    kwargs = {**_SAFE_PROD_KWARGS, "password_reset_url_base": "skincare://reset-password"}
    with pytest.raises(ValueError) as exc_info:
        Settings(_env_file=None, **kwargs)
    assert "PASSWORD_RESET_URL_BASE" in str(exc_info.value)


@pytest.mark.parametrize(
    "placeholder_url",
    [
        "https://example.com/reset-password",
        "https://localhost/reset-password",
    ],
)
def test_production_rejects_placeholder_password_reset_url_base(placeholder_url):
    kwargs = {**_SAFE_PROD_KWARGS, "password_reset_url_base": placeholder_url}
    with pytest.raises(ValueError):
        Settings(_env_file=None, **kwargs)


def test_production_rejects_non_positive_token_ttl():
    kwargs = {**_SAFE_PROD_KWARGS, "password_reset_token_ttl_minutes": 0}
    with pytest.raises(ValueError):
        Settings(_env_file=None, **kwargs)


def test_production_accepts_fully_configured_resend():
    settings = Settings(_env_file=None, **_SAFE_PROD_KWARGS)
    assert settings.email_provider == "resend"
    assert settings.password_reset_url_base.startswith("https://")


def test_development_defaults_to_no_op_email_provider_unaffected_by_validation():
    """Development's default (EMAIL_PROVIDER unset -> "none",
    PASSWORD_RESET_URL_BASE unset -> the skincare:// dev scheme) must
    keep working unmodified -- these production-only rules never run
    outside ENVIRONMENT=production."""
    settings = Settings(
        _env_file=None,
        environment="development",
        jwt_secret="",
        database_url="postgresql://postgres:postgres@localhost:5432/skincare",
        redis_url="redis://localhost:6379/0",
        enable_docs=True,
        allowed_origins=["*"],
    )
    assert settings.email_provider == "none"
    assert settings.password_reset_url_base == "skincare://reset-password"
