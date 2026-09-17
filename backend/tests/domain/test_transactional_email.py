"""app.domain.transactional_email -- the provider-neutral boundary
domain/auth code depends on (V1 account recovery pass, Part 3).
ResendTransactionalEmailService is exercised against httpx.MockTransport
(no real network access is used or required, the same pattern
tests/domain/test_revenuecat_reconciliation_http_contract.py already
establishes for this codebase's other outbound HTTP integration) --
never against the real Resend API.
"""
import httpx
import pytest

from app.domain.transactional_email import (
    InMemoryTransactionalEmailService,
    NullTransactionalEmailService,
    ResendTransactionalEmailService,
    TransactionalEmailError,
    build_transactional_email_service,
)


def _resend_client(handler) -> ResendTransactionalEmailService:
    transport = httpx.MockTransport(handler)
    return ResendTransactionalEmailService(
        api_key="test-resend-key", from_email="noreply@test.invalid", transport=transport,
    )


async def test_resend_sends_the_documented_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "email-id-123"})

    client = _resend_client(handler)
    await client.send_password_reset(to_email="user@test.invalid", reset_url="https://app.test.invalid/reset?token=abc")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["authorization"] == "Bearer test-resend-key"
    import json as _json
    payload = _json.loads(captured["body"])
    assert payload["to"] == ["user@test.invalid"]
    assert payload["from"] == "noreply@test.invalid"
    assert "https://app.test.invalid/reset?token=abc" in payload["text"]


async def test_resend_raises_transactional_email_error_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "invalid recipient"})

    client = _resend_client(handler)
    with pytest.raises(TransactionalEmailError):
        await client.send_password_reset(to_email="user@test.invalid", reset_url="https://app.test.invalid/reset?token=abc")


async def test_resend_raises_transactional_email_error_on_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _resend_client(handler)
    with pytest.raises(TransactionalEmailError):
        await client.send_password_reset(to_email="user@test.invalid", reset_url="https://app.test.invalid/reset?token=abc")


async def test_resend_error_message_never_contains_recipient_or_url():
    """Defense in depth for the "never log the reset URL/email" rule --
    even the exception string this raises on failure must not leak
    them, since a caller's error-logging path could otherwise do it
    unintentionally."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _resend_client(handler)
    with pytest.raises(TransactionalEmailError) as exc_info:
        await client.send_password_reset(
            to_email="super-secret-user@test.invalid", reset_url="https://app.test.invalid/reset?token=super-secret-token",
        )
    message = str(exc_info.value)
    assert "super-secret-user" not in message
    assert "super-secret-token" not in message


async def test_null_service_does_not_raise_and_performs_no_network_io():
    service = NullTransactionalEmailService()
    await service.send_password_reset(to_email="user@test.invalid", reset_url="https://app.test.invalid/reset?token=abc")


async def test_in_memory_service_records_calls_without_network_io():
    service = InMemoryTransactionalEmailService()
    await service.send_password_reset(to_email="user@test.invalid", reset_url="https://app.test.invalid/reset?token=abc")
    assert service.sent == [{"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=abc"}]


def test_factory_returns_null_service_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "email_provider", "none")
    assert isinstance(build_transactional_email_service(), NullTransactionalEmailService)


def test_factory_returns_resend_service_when_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "email_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "a-real-key")
    monkeypatch.setattr(settings, "password_reset_from_email", "noreply@test.invalid")
    assert isinstance(build_transactional_email_service(), ResendTransactionalEmailService)


def test_factory_never_returns_the_test_only_in_memory_fake(monkeypatch):
    """InMemoryTransactionalEmailService is a test fixture, not a
    fallback -- no EMAIL_PROVIDER value should ever cause the
    composition-root factory to construct it."""
    from app.config import settings

    for provider in ("none", "resend", "anything-unrecognized"):
        monkeypatch.setattr(settings, "email_provider", provider)
        monkeypatch.setattr(settings, "resend_api_key", "a-real-key")
        monkeypatch.setattr(settings, "password_reset_from_email", "noreply@test.invalid")
        assert not isinstance(build_transactional_email_service(), InMemoryTransactionalEmailService)
