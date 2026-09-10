"""EphemeralAnalysisImageStore (Part IV, Phase 20) -- tested against an
in-memory fake ObjectStorage (the underlying provider mechanics are
already covered by test_r2_adapter.py's real botocore Stubber tests;
this file is about the store's own key-generation/retention/delete
-tracking logic, which is provider-independent).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import DEFAULT_RETENTION, EphemeralAnalysisImageStore


class FakeObjectStorage(ObjectStorage):
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.unavailable = False

    async def put(self, key, data, *, content_type=None):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        self.objects[key] = data

    async def get(self, key):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        del self.objects[key]

    async def exists(self, key):
        return key in self.objects

    async def create_upload_authorization(self, key, *, content_type=None, expires_in_seconds=900):
        raise NotImplementedError("EphemeralAnalysisImageStore never calls this")

    async def create_download_authorization(self, key, *, expires_in_seconds=900):
        raise NotImplementedError("EphemeralAnalysisImageStore never calls this")


@pytest.fixture
def fake_storage():
    return FakeObjectStorage()


@pytest.fixture
def store(fake_storage):
    return EphemeralAnalysisImageStore(fake_storage)


async def test_store_and_retrieve_round_trip(store):
    stored = await store.store(b"fake-jpeg-bytes")
    retrieved = await store.retrieve(stored.object_key)
    assert retrieved == b"fake-jpeg-bytes"


async def test_object_key_contains_no_identifying_text(store):
    """Phase 20's explicit requirement: no email/username/user_id in
    the object key."""
    stored = await store.store(b"x", region="us-east")
    assert "@" not in stored.object_key  # no email
    assert stored.object_key.startswith("ephemeral-analysis/us-east/")
    parts = stored.object_key.split("/")
    assert len(parts) == 6  # ephemeral-analysis / region / YYYY / MM / DD / uuid


async def test_object_key_is_immutable_random_uuid_not_derived_from_user(store):
    a = await store.store(b"x")
    b = await store.store(b"x")
    assert a.object_key != b.object_key  # never colliding, never user-derived


async def test_expiry_reflects_configured_retention(fake_storage):
    short_store = EphemeralAnalysisImageStore(fake_storage, retention=timedelta(minutes=5))
    before = datetime.now(timezone.utc)
    stored = await short_store.store(b"x")
    after = datetime.now(timezone.utc)
    assert before + timedelta(minutes=5) <= stored.expires_at <= after + timedelta(minutes=5)


async def test_default_retention_is_minutes_or_hours_not_months():
    assert DEFAULT_RETENTION <= timedelta(hours=24)


async def test_delete_if_confirmed_returns_true_when_object_existed(store):
    stored = await store.store(b"x")
    assert await store.delete_if_confirmed(stored.object_key) is True
    with pytest.raises(ObjectNotFoundError):
        await store.retrieve(stored.object_key)


async def test_delete_if_confirmed_returns_true_when_already_gone(store):
    """Idempotent by design -- a retry after a successful delete (e.g.
    a worker crash between delete and clearing the DB reference) must
    not raise or be treated as a failure."""
    assert await store.delete_if_confirmed("ephemeral-analysis/global/2026/01/01/never-existed") is True


async def test_delete_if_confirmed_returns_false_when_storage_unavailable(store, fake_storage):
    stored = await store.store(b"x")
    fake_storage.unavailable = True
    result = await store.delete_if_confirmed(stored.object_key)
    assert result is False  # caller must track this and retry later, not assume success


async def test_never_creates_upload_or_download_authorizations():
    """Structural proof of "no public URL, no CDN exposure" -- this
    store's own methods never call create_upload_authorization/
    create_download_authorization at all."""
    import inspect

    source = inspect.getsource(EphemeralAnalysisImageStore)
    assert "create_upload_authorization" not in source
    assert "create_download_authorization" not in source
