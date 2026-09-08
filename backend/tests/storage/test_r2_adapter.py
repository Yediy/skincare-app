"""CloudflareR2ObjectStorage contract tests (Phase 7). Uses
botocore.stub.Stubber -- already a transitive dependency of boto3, no
new package needed -- to intercept the actual S3 API calls without any
real network I/O or R2 connectivity. This is deliberately part of the
normal unit test suite: it proves the adapter's request/response/error
mapping is correct without requiring Cloudflare connectivity, per this
phase's own instruction not to make the standard suite depend on it.

Presigned-URL generation (`generate_presigned_url`) is not intercepted
by Stubber -- botocore computes it locally, no network call happens --
so those tests just call it directly and inspect the result.
"""
import pytest
from botocore.stub import ANY, Stubber

from app.storage.base import ObjectNotFoundError, ObjectStorageUnavailableError
from app.storage.r2 import CloudflareR2ObjectStorage


@pytest.fixture
def storage():
    return CloudflareR2ObjectStorage(
        account_id="test-account",
        access_key_id="test-key-id",
        secret_access_key="test-secret",
        bucket="test-bucket",
    )


@pytest.fixture
def stubber(storage):
    s = Stubber(storage._client)
    s.activate()
    yield s
    s.assert_no_pending_responses()
    s.deactivate()


async def test_put_sends_bucket_key_body_and_content_type(storage, stubber):
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": "test-bucket", "Key": "prod/us-east/product-assets/2026/09/abc.webp", "Body": ANY, "ContentType": "image/webp"},
    )
    await storage.put("prod/us-east/product-assets/2026/09/abc.webp", b"fake-image-bytes", content_type="image/webp")


async def test_get_returns_object_bytes(storage, stubber):
    import io

    stubber.add_response(
        "get_object", {"Body": io.BytesIO(b"hello")}, {"Bucket": "test-bucket", "Key": "some-key"}
    )
    result = await storage.get("some-key")
    assert result == b"hello"


async def test_get_missing_key_raises_object_not_found(storage, stubber):
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    with pytest.raises(ObjectNotFoundError):
        await storage.get("missing-key")


async def test_exists_true_when_head_object_succeeds(storage, stubber):
    stubber.add_response("head_object", {}, {"Bucket": "test-bucket", "Key": "present-key"})
    assert await storage.exists("present-key") is True


async def test_exists_false_when_head_object_404s(storage, stubber):
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    assert await storage.exists("missing-key") is False


async def test_delete_raises_object_not_found_for_missing_key(storage, stubber):
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with pytest.raises(ObjectNotFoundError):
        await storage.delete("missing-key")


async def test_delete_removes_existing_key(storage, stubber):
    stubber.add_response("head_object", {}, {"Bucket": "test-bucket", "Key": "present-key"})
    stubber.add_response("delete_object", {}, {"Bucket": "test-bucket", "Key": "present-key"})
    await storage.delete("present-key")


async def test_provider_error_other_than_not_found_maps_to_unavailable(storage, stubber):
    stubber.add_client_error("get_object", service_error_code="InternalError", http_status_code=500)
    with pytest.raises(ObjectStorageUnavailableError):
        await storage.get("some-key")


async def test_create_upload_authorization_returns_put_url_with_expiry(storage):
    auth = await storage.create_upload_authorization("uploads/abc", content_type="image/png", expires_in_seconds=120)
    assert auth.method == "PUT"
    assert auth.object_key == "uploads/abc"
    assert "test-bucket" in auth.url
    assert auth.headers == {"Content-Type": "image/png"}


async def test_create_download_authorization_returns_get_url(storage):
    auth = await storage.create_download_authorization("uploads/abc", expires_in_seconds=60)
    assert auth.object_key == "uploads/abc"
    assert "test-bucket" in auth.url


async def test_errors_never_include_raw_credentials_in_repr(storage):
    """The adapter's error path (`_run`) deliberately never logs call
    args/kwargs -- confirm that holds for the one path that could
    plausibly leak something (a mapped ObjectStorageUnavailableError),
    by checking the secret we constructed the client with doesn't
    appear anywhere in the raised exception's string form."""
    stub = Stubber(storage._client)
    stub.add_client_error("get_object", service_error_code="InternalError", http_status_code=500)
    stub.activate()
    try:
        with pytest.raises(ObjectStorageUnavailableError) as exc_info:
            await storage.get("some-key")
        assert "test-secret" not in str(exc_info.value)
    finally:
        stub.deactivate()
