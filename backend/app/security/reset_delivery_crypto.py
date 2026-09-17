"""Encryption for the password-reset-email delivery outbox
(independent-review timing-enumeration fix on top of the V1 account
recovery pass).

POST /password/forgot must not perform outbound provider network I/O
in its own request path (that latency is itself an account-enumeration
side channel -- see app/domain/timing_normalization.py). Email delivery
is instead handed off to a durable Postgres job (app/workers/
password_reset_email_worker.py) processed out of band. But the raw
reset token (and the reset URL built from it) is exactly the secret
this whole system exists to protect -- password_reset_repository.py
never persists it, and that invariant must not be weakened just to make
the job payload convenient to construct.

This module is the one place that may turn `{to_email, reset_url}` into
something safe to put in the ordinary, non-RLS-restricted `jobs.payload`
JSONB column: Fernet (AES-128-CBC + HMAC-SHA256, authenticated symmetric
encryption) under a key derived from `settings.
password_reset_email_delivery_key` -- a secret configured only in
application environment, never written to Postgres itself, so an
ordinary database reader (anyone with a `SELECT` on `jobs`, e.g. an
operator debugging the queue) sees only opaque ciphertext, never the
plaintext token or email address. The worker decrypts it only in
process memory, immediately before calling the email provider, and the
caller redacts the row's payload once delivery is no longer pending
(see the worker's own `_redact_payload`).

Fernet also gives this an expiring-token property essentially for free
(`Fernet(key).decrypt(token, ttl=...)`), but this module does not use
that -- the payload's own relevant expiry is `password_reset_tokens.
expires_at`, checked independently by the worker before it ever
decrypts, so there is no need for a second, separate TTL concept here.
"""
import base64
import hashlib
import json
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken


class ResetDeliveryDecryptionError(Exception):
    """Raised when a stored encrypted delivery payload cannot be
    decrypted -- a wrong/rotated key, or (extremely unlikely, since
    Fernet is authenticated) corrupted ciphertext. Distinct from
    InvalidToken so callers depend on this module's own exception
    hierarchy, not `cryptography`'s directly."""


def _derive_fernet_key(secret: str) -> bytes:
    """Fernet requires an exact 32-byte, urlsafe-base64-encoded key --
    not an arbitrary operator-chosen string, unlike JWT_SECRET (raw
    HMAC accepts any byte string). SHA-256 over the configured secret
    deterministically produces exactly 32 bytes, so
    PASSWORD_RESET_EMAIL_DELIVERY_KEY can be an ordinary
    human-generated high-entropy secret string -- consistent with every
    other secret in app/config.py -- rather than requiring an operator
    to generate and paste a literal Fernet key."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_delivery_payload(payload: Dict[str, Any], secret: str) -> str:
    """`payload` is typically `{"to_email": ..., "reset_url": ...}` --
    the exact, and only, sensitive content this module ever handles.
    Returns an opaque ascii token string safe to store in a JSONB
    column."""
    fernet = Fernet(_derive_fernet_key(secret))
    plaintext = json.dumps(payload).encode("utf-8")
    return fernet.encrypt(plaintext).decode("ascii")


def decrypt_delivery_payload(ciphertext: str, secret: str) -> Dict[str, Any]:
    fernet = Fernet(_derive_fernet_key(secret))
    try:
        plaintext = fernet.decrypt(ciphertext.encode("ascii"))
    except InvalidToken as e:
        raise ResetDeliveryDecryptionError("could not decrypt password-reset delivery payload") from e
    return json.loads(plaintext.decode("utf-8"))
