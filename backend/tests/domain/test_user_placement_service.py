"""app.domain.user_placement_service.UserPlacementService -- P0-1
correction: `users` genuinely has row-level security (migration
feb038fd05bd), so get_placement() must go through the same
set_config('app.current_user_id', ...)-scoped-transaction pattern
every other RLS-protected repository in this codebase uses (see
app/db/profile_repository.py). Run against the real restricted
app_db_pool (the skincare_app role), never mocked, so a regression
back to a direct pool.fetchrow() UPDATE -- which users_identity_scope
silently turns into a zero-row update under RLS -- shows up here as a
real, durable persistence failure, not just a wrong return value.
"""
import uuid

import pytest

from app.config import settings
from app.domain.user_placement_service import UserPlacement, UserPlacementNotFoundError, UserPlacementService


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )
    return row["id"]


async def test_new_user_is_assigned_launch_defaults_and_it_actually_persists(db_pool, app_db_pool, monkeypatch):
    """The core RLS regression this pass fixes: get_placement() must
    actually write home_region/cell_id onto the users row (visible to
    a superuser assertion connection), not merely return the launch
    defaults in memory while leaving the row's columns untouched."""
    monkeypatch.setattr(settings, "launch_home_region", "us-east")
    monkeypatch.setattr(settings, "launch_cell_id", "use1-001")

    user_id = await _create_user(db_pool, "placement-new@test.com")
    row = await db_pool.fetchrow("SELECT home_region, cell_id FROM users WHERE id = $1", user_id)
    assert row["home_region"] is None
    assert row["cell_id"] is None

    service = UserPlacementService(app_db_pool)
    placement = await service.get_placement(user_id)

    assert placement == UserPlacement(home_region="us-east", cell_id="use1-001")

    # Durable, superuser-verified -- not just the in-memory return value.
    persisted = await db_pool.fetchrow("SELECT home_region, cell_id FROM users WHERE id = $1", user_id)
    assert persisted["home_region"] == "us-east"
    assert persisted["cell_id"] == "use1-001"


async def test_existing_placement_survives_a_later_launch_default_change(db_pool, app_db_pool, monkeypatch):
    """Proves real durable assignment, not re-derivation per call: once
    a user has a placement, changing the deployment's configured launch
    defaults afterward must not move that user -- get_placement() must
    keep returning (and leave persisted) their original assignment."""
    monkeypatch.setattr(settings, "launch_home_region", "us-east")
    monkeypatch.setattr(settings, "launch_cell_id", "use1-001")

    user_id = await _create_user(db_pool, "placement-sticky@test.com")
    service = UserPlacementService(app_db_pool)

    first = await service.get_placement(user_id)
    assert first == UserPlacement(home_region="us-east", cell_id="use1-001")

    # Deployment reconfigured to a different launch cell/region.
    monkeypatch.setattr(settings, "launch_home_region", "eu-west")
    monkeypatch.setattr(settings, "launch_cell_id", "euw1-001")

    second = await service.get_placement(user_id)
    assert second == UserPlacement(home_region="us-east", cell_id="use1-001")  # unchanged

    persisted = await db_pool.fetchrow("SELECT home_region, cell_id FROM users WHERE id = $1", user_id)
    assert persisted["home_region"] == "us-east"
    assert persisted["cell_id"] == "use1-001"


async def test_missing_authenticated_user_row_fails_closed(app_db_pool):
    """get_placement() is only ever called after authentication --
    reaching an absent row here must raise a real domain error, never
    silently fabricate/return the launch defaults as if they had been
    durably assigned to nobody's row."""
    service = UserPlacementService(app_db_pool)
    with pytest.raises(UserPlacementNotFoundError):
        await service.get_placement(uuid.uuid4())


async def test_user_a_cannot_assign_or_read_user_bs_placement_via_the_service(db_pool, app_db_pool):
    """The service itself only ever scopes app.current_user_id to the
    exact user_id passed in -- there is no "assign someone else's
    placement" call shape here, but this proves the underlying RLS
    policy (not just the service's own argument plumbing) is what
    actually enforces that: setting app.current_user_id to A and then
    directly attempting the same UPDATE shape against B's row (the
    lower-level path a bug in this service could regress to) affects
    zero rows and leaves B's placement untouched."""
    user_a = await _create_user(db_pool, "placement-a@test.com")
    user_b = await _create_user(db_pool, "placement-b@test.com")

    # B already has a real, durably-assigned placement.
    await UserPlacementService(app_db_pool).get_placement(user_b)
    b_before = await db_pool.fetchrow("SELECT home_region, cell_id FROM users WHERE id = $1", user_b)
    assert b_before["home_region"] is not None

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                """
                UPDATE users SET home_region = 'attacker-region', cell_id = 'attacker-cell'
                WHERE id = $1
                """,
                user_b,
            )

    assert result.endswith(" 0")  # RLS blocked it: zero rows affected

    b_after = await db_pool.fetchrow("SELECT home_region, cell_id FROM users WHERE id = $1", user_b)
    assert b_after["home_region"] == b_before["home_region"]
    assert b_after["cell_id"] == b_before["cell_id"]
