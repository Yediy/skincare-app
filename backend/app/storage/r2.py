"""Cloudflare R2 implementation of the ObjectStorage contract
(app/storage/base.py). R2 is S3-API-compatible, so this is a thin
boto3 S3 client pointed at R2's endpoint -- boto3 itself is the only
part of this module aware that the provider happens to be R2 and not
S3 proper.

boto3 is synchronous. Every call here runs the blocking boto3 call in
a thread via asyncio.to_thread rather than calling it directly on the
event loop -- otherwise an object-storage round trip would stall every
other coroutine on this worker for its duration, same class of problem
Phase 1's Redis timeout hardening exists to avoid.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from app.storage.base import (
    DownloadAuthorization,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageUnavailableError,
    UploadAuthorization,
)

logger = logging.getLogger(__name__)


class CloudflareR2ObjectStorage(ObjectStorage):
    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        endpoint_url: str | None = None,
    ):
        self._bucket = bucket
        # boto3 client construction does not itself make a network
        # call, so this is safe to do synchronously in __init__.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # R2 only supports SigV4, and region is a required-but-
            # meaningless field for an R2 endpoint.
            config=BotoConfig(signature_version="s3v4", region_name="auto"),
        )

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        kwargs = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        await self._run(self._client.put_object, **kwargs)

    async def get(self, key: str) -> bytes:
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        return response["Body"].read()

    async def delete(self, key: str) -> None:
        if not await self.exists(key):
            raise ObjectNotFoundError(key)
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        try:
            await self._run(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except ObjectNotFoundError:
            return False

    async def create_upload_authorization(
        self, key: str, *, content_type: str | None = None, expires_in_seconds: int = 900
    ) -> UploadAuthorization:
        params = {"Bucket": self._bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        url = await self._run(
            self._client.generate_presigned_url,
            "put_object",
            Params=params,
            ExpiresIn=expires_in_seconds,
        )
        headers = {"Content-Type": content_type} if content_type else {}
        return UploadAuthorization(
            url=url,
            method="PUT",
            headers=headers,
            object_key=key,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        )

    async def create_download_authorization(
        self, key: str, *, expires_in_seconds: int = 900
    ) -> DownloadAuthorization:
        url = await self._run(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )
        return DownloadAuthorization(
            url=url,
            object_key=key,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        )

    async def _run(self, fn, *args, **kwargs):
        """Runs one blocking boto3 call off the event loop and maps
        botocore's exceptions onto this module's provider-neutral
        ones. Never logs `args`/`kwargs` -- for put(), that would be
        the raw object bytes; for the client itself, credentials are
        held internally by boto3 and never passed as call arguments,
        so they can't leak through this path either way."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFoundError(kwargs.get("Key", "")) from e
            logger.error("R2 request failed (code=%s)", code)
            raise ObjectStorageUnavailableError(code) from e
        except EndpointConnectionError as e:
            logger.error("R2 endpoint unreachable")
            raise ObjectStorageUnavailableError("endpoint unreachable") from e
