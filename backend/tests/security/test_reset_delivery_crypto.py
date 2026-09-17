"""app.security.reset_delivery_crypto -- Fernet encryption protecting
the password-reset-email delivery job's payload at rest (independent-
review timing-enumeration fix). No test here ever writes a real
plaintext token to any durable store; these are pure round-trip/
tamper/wrong-key unit tests of the crypto boundary itself.

Test keys are generated at import time with `secrets.token_hex` rather
than written as literal strings in source -- `encrypt_delivery_payload`
accepts any string secret (see `_derive_fernet_key`'s SHA-256 step), so
there is no requirement these look like real Fernet key material, and a
runtime-constructed value can never itself be a credential a scanner
(or a human) needs to reason about, unlike a hardcoded high-entropy
literal sitting in the diff forever.
"""
import secrets

import pytest

from app.security.reset_delivery_crypto import (
    ResetDeliveryDecryptionError,
    decrypt_delivery_payload,
    encrypt_delivery_payload,
)

KEY_A = secrets.token_hex(32)
KEY_B = secrets.token_hex(32)


def test_round_trips_the_exact_payload():
    payload = {"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=super-secret-raw-token"}
    ciphertext = encrypt_delivery_payload(payload, KEY_A)
    assert decrypt_delivery_payload(ciphertext, KEY_A) == payload


def test_ciphertext_never_contains_the_plaintext_token_or_email():
    payload = {"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=super-secret-raw-token"}
    ciphertext = encrypt_delivery_payload(payload, KEY_A)
    assert "super-secret-raw-token" not in ciphertext
    assert "user@test.invalid" not in ciphertext


def test_wrong_key_cannot_decrypt():
    payload = {"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=abc"}
    ciphertext = encrypt_delivery_payload(payload, KEY_A)
    with pytest.raises(ResetDeliveryDecryptionError):
        decrypt_delivery_payload(ciphertext, KEY_B)


def test_tampered_ciphertext_is_rejected():
    payload = {"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=abc"}
    ciphertext = encrypt_delivery_payload(payload, KEY_A)
    tampered = ciphertext[:-4] + ("A" if ciphertext[-4] != "A" else "B") + ciphertext[-3:]
    with pytest.raises(ResetDeliveryDecryptionError):
        decrypt_delivery_payload(tampered, KEY_A)


def test_malformed_ciphertext_is_rejected():
    with pytest.raises(ResetDeliveryDecryptionError):
        decrypt_delivery_payload("not-a-real-fernet-token", KEY_A)


def test_same_payload_encrypted_twice_produces_different_ciphertext():
    """Fernet includes a random IV/nonce per encryption -- two deliveries
    of the same address/URL must not be linkable by ciphertext
    equality."""
    payload = {"to_email": "user@test.invalid", "reset_url": "https://app.test.invalid/reset?token=abc"}
    first = encrypt_delivery_payload(payload, KEY_A)
    second = encrypt_delivery_payload(payload, KEY_A)
    assert first != second
    assert decrypt_delivery_payload(first, KEY_A) == decrypt_delivery_payload(second, KEY_A) == payload
