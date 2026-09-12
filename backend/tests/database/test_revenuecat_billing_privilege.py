"""Migration 9815eb266923's core claim: the ORDINARY application
runtime role (`skincare_app` -- what every HTTP request in this
application actually connects as) cannot manufacture billing truth,
full stop -- not merely "cannot manufacture *another user's* billing
truth" (RLS's own, narrower guarantee, covered by
tests/database/test_revenuecat_rls.py). Every test below runs through
the real restricted roles, not the superuser, and not mocked.

Structure: first, everything the ordinary skincare_app role must NOT
be able to do, including the specific bypasses an unrestricted RLS-
only design would have missed (writing a NULL source_event_id row
attributed to *itself*, i.e. app.current_user_id set to its own
victim's id -- RLS's WITH CHECK is satisfied by that, so only a grant-
level write restriction stops it). Then, the dedicated skincare_billing
role can do exactly what it needs to.
"""
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


# ---------------------------------------------------------------------------
# Ordinary skincare_app role: no write path to billing truth at all.
# ---------------------------------------------------------------------------


async def test_ordinary_role_cannot_insert_a_webhook_event_row(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "priv-webhook-insert@test.com")

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO revenuecat_webhook_events
                    (revenuecat_event_id, event_type, app_user_id, environment, event_timestamp, payload_json)
                VALUES ($1, 'INITIAL_PURCHASE', $2, 'PRODUCTION', now(), '{}'::jsonb)
                """,
                "forged-event-id", str(user_a),
            )


async def test_ordinary_role_cannot_update_a_webhook_event_row(db_pool, billing_db_pool, app_db_pool):
    """A legitimate row exists (inserted by the billing role) --
    skincare_app still cannot flip its processing_status."""
    async with billing_db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO revenuecat_webhook_events
                (revenuecat_event_id, event_type, environment, event_timestamp, payload_json)
            VALUES ('priv-update-event', 'INITIAL_PURCHASE', 'PRODUCTION', now(), '{}'::jsonb)
            """
        )

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE revenuecat_webhook_events SET processing_status = 'PROCESSED' "
                "WHERE revenuecat_event_id = 'priv-update-event'"
            )


async def test_ordinary_role_cannot_insert_its_own_active_entitlement_even_with_null_source_event(
    db_pool, app_db_pool,
):
    """The scenario an RLS-only design would miss: app.current_user_id
    is set to the victim's OWN id (so WITH CHECK is satisfied) and
    source_event_id is NULL (so there's no FK to fail on) -- the only
    thing left to stop this is the grant itself. It does."""
    user_a = await _create_user(db_pool, "priv-self-grant@test.com")

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute(
                    """
                    INSERT INTO user_entitlements
                        (user_id, entitlement_identifier, provider, status, effective_at,
                         environment, provider_customer_id, last_provider_event_at, source_event_id)
                    VALUES ($1, 'premium', 'revenuecat', 'ACTIVE', now(), 'PRODUCTION', $2, now(), NULL)
                    """,
                    user_a, str(user_a),
                )


async def test_ordinary_role_cannot_update_its_own_entitlement_to_active(db_pool, app_db_pool, billing_db_pool):
    """A real EXPIRED row already exists (legitimately written by the
    billing role) -- skincare_app cannot flip it back to ACTIVE for
    itself, even scoped to its own user_id under RLS."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-self-update@test.com")
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="EXPIRED", effective_at=T0, expires_at=T0, will_renew=False, environment="PRODUCTION",
        source_event_id=None, provider_customer_id=str(user_a), last_provider_event_at=T0,
    )

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute(
                    "UPDATE user_entitlements SET status = 'ACTIVE' WHERE user_id = $1", user_a,
                )


async def test_ordinary_role_cannot_delete_its_own_entitlement(db_pool, app_db_pool, billing_db_pool):
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-self-delete@test.com")
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=None, will_renew=True, environment="PRODUCTION",
        source_event_id=None, provider_customer_id=str(user_a), last_provider_event_at=T0,
    )

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute("DELETE FROM user_entitlements WHERE user_id = $1", user_a)


async def test_ordinary_role_can_still_read_its_own_entitlement(db_pool, app_db_pool, billing_db_pool):
    """The read path (RevenueCatEntitlementService) must keep working
    through the ordinary role -- only writes are taken away."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-self-read@test.com")
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=None, will_renew=True, environment="PRODUCTION",
        source_event_id=None, provider_customer_id=str(user_a), last_provider_event_at=T0,
    )

    row = await revenuecat_repository.get_entitlement(app_db_pool, user_a, "premium", "revenuecat", "PRODUCTION")
    assert row is not None
    assert row["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Dedicated skincare_billing role: least privilege, but sufficient for
# every real billing-mutation path.
# ---------------------------------------------------------------------------


async def test_billing_role_can_persist_a_verified_webhook_event(db_pool, billing_db_pool):
    """Mirrors what app/api/v2/webhooks.py actually does (record_event
    through the billing pool)."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-billing-record@test.com")
    recorded = await revenuecat_repository.record_event(
        billing_db_pool,
        revenuecat_event_id="priv-billing-record",
        event_type="INITIAL_PURCHASE",
        app_user_id=str(user_a),
        environment="PRODUCTION",
        event_timestamp=T0,
        payload_json={"api_version": "1.0", "event": {"id": "priv-billing-record"}},
    )
    assert recorded.is_new is True

    row = await revenuecat_repository.get_event(db_pool, recorded.id)
    assert row is not None
    assert row["processing_status"] == "PENDING"


async def test_billing_role_can_project_an_entitlement(db_pool, billing_db_pool):
    """Mirrors what the RevenueCat worker actually does (apply_entitlement_projection
    through the billing pool)."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-billing-project@test.com")
    result = await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=T0 + timedelta(days=30), will_renew=True,
        environment="PRODUCTION", source_event_id=None, provider_customer_id=str(user_a),
        last_provider_event_at=T0,
    )
    assert result.applied is True
    assert result.status == "ACTIVE"


async def test_billing_role_can_correct_an_entitlement_via_reconciliation(db_pool, billing_db_pool):
    """Mirrors what RevenueCatReconciliationService actually does --
    correcting a stale local row, through the billing pool."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "priv-billing-reconcile@test.com")
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=T0 + timedelta(days=30), will_renew=True,
        environment="PRODUCTION", source_event_id=None, provider_customer_id=str(user_a),
        last_provider_event_at=T0,
    )

    now = datetime.now(timezone.utc)
    corrected = await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="EXPIRED", effective_at=now, expires_at=now, will_renew=False,
        environment="PRODUCTION", source_event_id=None, provider_customer_id=str(user_a),
        last_provider_event_at=now,
    )
    assert corrected.applied is True
    assert corrected.status == "EXPIRED"


async def test_billing_role_is_not_a_superuser_or_rls_bypass(db_pool):
    row = await db_pool.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = 'skincare_billing'"
    )
    assert row is not None
    # NOLOGIN at head (migration 1367b870bdcd, after 9815eb266923
    # originally created it LOGIN) -- see
    # tests/database/test_billing_role_migration_lineage.py for the
    # migration-lineage regression guard behind this.
    assert row["rolcanlogin"] is False
    assert row["rolsuper"] is False
    assert row["rolcreatedb"] is False
    assert row["rolcreaterole"] is False
    assert row["rolbypassrls"] is False
