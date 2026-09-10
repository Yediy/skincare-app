"""app.workers.image_cleanup -- Part IV, Phase 22's cross-user safety
-net sweeper, through the real restricted skincare_app role and the
real find_overdue_ephemeral_images()/clear_image_reference() SECURITY
DEFINER functions (migration 071fab81f0ac).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import analysis_repository
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore
from app.workers.image_cleanup import run_cleanup_sweep
from tests.storage.test_ephemeral_image_store import FakeObjectStorage


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def _queued_request(app_db_pool, user_id, request_id, *, expires_at, object_key="ephemeral-analysis/global/2026/01/01/x"):
    req = await analysis_repository.create_request(app_db_pool, user_id, request_id)
    await analysis_repository.mark_queued(app_db_pool, user_id, req["id"], image_object_key=object_key, image_expires_at=expires_at)
    return req


async def test_sweep_deletes_overdue_image_and_clears_reference(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "cleanup-overdue@test.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    req = await _queued_request(app_db_pool, user_id, "cleanup-req-1", expires_at=past)

    fake_storage = FakeObjectStorage()
    fake_storage.objects["ephemeral-analysis/global/2026/01/01/x"] = b"stale-image"
    image_store = EphemeralAnalysisImageStore(fake_storage)

    result = await run_cleanup_sweep(app_db_pool, image_store)

    assert result.attempted == 1
    assert result.deleted == 1
    assert result.failed == 0
    assert "ephemeral-analysis/global/2026/01/01/x" not in fake_storage.objects

    row = await db_pool.fetchrow("SELECT image_object_key, image_expires_at FROM analysis_requests WHERE id = $1", req["id"])
    assert row["image_object_key"] is None
    assert row["image_expires_at"] is None


async def test_sweep_ignores_not_yet_expired_images(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "cleanup-future@test.com")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await _queued_request(app_db_pool, user_id, "cleanup-req-2", expires_at=future)

    image_store = EphemeralAnalysisImageStore(FakeObjectStorage())
    result = await run_cleanup_sweep(app_db_pool, image_store)

    assert result.attempted == 0


async def test_sweep_tracks_failure_and_leaves_reference_for_retry(db_pool, app_db_pool):
    """Phase 22's explicit requirement: a storage failure must not be
    silently dropped -- the reference stays so a later sweep retries."""
    user_id = await _create_user(db_pool, "cleanup-fail@test.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    req = await _queued_request(app_db_pool, user_id, "cleanup-req-3", expires_at=past)

    fake_storage = FakeObjectStorage()
    fake_storage.unavailable = True
    image_store = EphemeralAnalysisImageStore(fake_storage)

    result = await run_cleanup_sweep(app_db_pool, image_store)

    assert result.attempted == 1
    assert result.deleted == 0
    assert result.failed == 1
    assert str(req["id"]) in result.failed_request_ids

    row = await db_pool.fetchrow("SELECT image_object_key FROM analysis_requests WHERE id = $1", req["id"])
    assert row["image_object_key"] is not None  # reference retained for retry


async def test_sweep_is_cross_user(db_pool, app_db_pool):
    """The sweeper must find overdue images regardless of which user
    owns them -- proving find_overdue_ephemeral_images() genuinely
    operates cross-user, not scoped to whichever app.current_user_id
    happened to be set last on this connection."""
    user_a = await _create_user(db_pool, "cleanup-cross-a@test.com")
    user_b = await _create_user(db_pool, "cleanup-cross-b@test.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    await _queued_request(app_db_pool, user_a, "cleanup-cross-req-a", expires_at=past, object_key="ephemeral-analysis/global/2026/01/01/a")
    await _queued_request(app_db_pool, user_b, "cleanup-cross-req-b", expires_at=past, object_key="ephemeral-analysis/global/2026/01/01/b")

    fake_storage = FakeObjectStorage()
    fake_storage.objects["ephemeral-analysis/global/2026/01/01/a"] = b"a"
    fake_storage.objects["ephemeral-analysis/global/2026/01/01/b"] = b"b"
    image_store = EphemeralAnalysisImageStore(fake_storage)

    result = await run_cleanup_sweep(app_db_pool, image_store)
    assert result.attempted == 2
    assert result.deleted == 2
