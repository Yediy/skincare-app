"""Provider-neutral object storage contract (Phase 6). Business logic
must depend on `ObjectStorage`, never on a provider SDK directly --
`CloudflareR2ObjectStorage` (r2.py) is the only module in this
codebase allowed to import boto3/botocore for this purpose. A future
`S3ObjectStorage`/`GCSObjectStorage` would implement this same
interface without any caller needing to change.

Nothing in this module talks to a network. It is deliberately just
types and an abstract contract.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class ObjectStorageError(Exception):
    """Base class for every error this abstraction raises. Callers
    should be able to catch this without knowing which provider is
    behind it -- a provider adapter must translate its own SDK's
    exceptions into this hierarchy, not let them leak through."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised by `get`/`delete` when the key doesn't exist. `exists`
    does not raise this -- it returns False instead."""


class ObjectStorageUnavailableError(ObjectStorageError):
    """Raised when the provider itself couldn't be reached or
    authenticated against -- distinct from ObjectNotFoundError so
    callers can tell 'this object was never here' apart from 'we
    couldn't ask the provider right now'."""


@dataclass(frozen=True)
class UploadAuthorization:
    """A short-lived, single-object authorization a client can use to
    upload directly to the provider (Phase 10) without the API
    proxying the bytes."""

    url: str
    method: str
    headers: dict
    object_key: str
    expires_at: datetime


@dataclass(frozen=True)
class DownloadAuthorization:
    """A short-lived, single-object authorization a client can use to
    download directly from the provider without the API proxying the
    bytes."""

    url: str
    object_key: str
    expires_at: datetime


class ObjectStorage(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        """Writes `data` to `key`, overwriting any existing object at
        that key. Callers should prefer immutable keys (Phase 8) so
        this is never relied on as an update-in-place operation."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Raises ObjectNotFoundError if `key` doesn't exist."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Raises ObjectNotFoundError if `key` doesn't exist."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Never raises ObjectNotFoundError -- that's the point of
        this method existing separately from `get`."""

    @abstractmethod
    async def create_upload_authorization(
        self, key: str, *, content_type: str | None = None, expires_in_seconds: int = 900
    ) -> UploadAuthorization:
        """Does not itself write anything -- the caller (or their
        client) still has to perform the upload against the returned
        URL. The API remains responsible for deciding `key` and
        `content_type`, and for recording whatever metadata the
        eventual object represents; this method only hands out
        permission to write those exact bytes to that exact key."""

    @abstractmethod
    async def create_download_authorization(
        self, key: str, *, expires_in_seconds: int = 900
    ) -> DownloadAuthorization:
        """Does not check whether the object actually exists -- a
        presigned GET URL for a missing key simply 404s when used."""
