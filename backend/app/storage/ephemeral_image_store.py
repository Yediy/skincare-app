"""EphemeralAnalysisImageStore (Part IV, Phase 20) -- a dedicated
abstraction on top of ObjectStorage (app/storage/base.py) for exactly
one purpose: transient raw-face-image transport between the API
process and a CV worker. See RAW_IMAGE_LIFECYCLE.md for the full
policy this implements.

This does NOT turn object storage into a normal user-photo archive.
Every object this store writes:
  - lives under a fixed `ephemeral-analysis/<region>/<date>/<uuid>`
    key that contains no email/username/user_id or any other
    identifying text (Phase 20's explicit requirement) -- the
    *reference* linking an object back to a user lives only in
    analysis_requests (RLS-protected, DB-side), never in the object
    key or the bucket itself;
  - is private (no public URL is ever generated -- store()/retrieve()
    only use put/get, never create_upload_authorization/
    create_download_authorization, which this store deliberately never
    calls);
  - carries a short retention window (Phase 22 -- default 1 hour, not
    months) and is deleted by the worker immediately after terminal
    processing (Phase 20's primary deletion path);
  - has a database-tracked expiry (analysis_requests.image_expires_at)
    as the safety net a crashed/killed worker can't skip -- see
    find_overdue_ephemeral_images() (migration 071fab81f0ac) and
    app/workers/image_cleanup.py for the sweeper that acts on it.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError

# "minutes/hours, not months" -- Phase 22's own phrasing. One hour is
# generous for a CV pipeline run (seconds, in practice) while still
# being a real, short-lived window, not a de facto permanent archive.
DEFAULT_RETENTION = timedelta(hours=1)


@dataclass(frozen=True)
class StoredImage:
    object_key: str
    expires_at: datetime


class EphemeralAnalysisImageStore:
    def __init__(self, object_storage: ObjectStorage, *, retention: timedelta = DEFAULT_RETENTION):
        self._storage = object_storage
        self._retention = retention

    def _generate_key(self, region: str) -> str:
        now = datetime.now(timezone.utc)
        random_id = uuid.uuid4()
        # No email/username/user_id anywhere in this string -- see
        # module docstring. `region` is a deployment-topology label
        # (Part VII's home_region), not anything user-identifying.
        return f"ephemeral-analysis/{region}/{now.year:04d}/{now.month:02d}/{now.day:02d}/{random_id}"

    async def store(self, image_bytes: bytes, *, region: str = "global", content_type: str = "image/jpeg") -> StoredImage:
        key = self._generate_key(region)
        await self._storage.put(key, image_bytes, content_type=content_type)
        expires_at = datetime.now(timezone.utc) + self._retention
        return StoredImage(object_key=key, expires_at=expires_at)

    async def retrieve(self, object_key: str) -> bytes:
        return await self._storage.get(object_key)

    async def delete(self, object_key: str) -> None:
        """Raises ObjectNotFoundError/ObjectStorageUnavailableError --
        callers on the primary deletion path (Part VI's worker) should
        prefer delete_if_confirmed() below, which treats both as
        non-fatal, tracked outcomes rather than letting a storage
        hiccup break an otherwise-successful analysis."""
        await self._storage.delete(object_key)

    async def delete_if_confirmed(self, object_key: str) -> bool:
        """Best-effort delete for the primary cleanup path (Phase 20)
        and the cleanup sweeper (Phase 22) alike: returns True if the
        object is now confirmed gone (either this call deleted it, or
        it was already gone), False if the storage backend couldn't be
        reached right now -- callers must track a False return (Phase
        22's "track cleanup failures") and leave the DB-side expiry
        reference in place so a later sweep retries it, rather than
        silently giving up."""
        try:
            await self._storage.delete(object_key)
            return True
        except ObjectNotFoundError:
            return True
        except ObjectStorageUnavailableError:
            return False
