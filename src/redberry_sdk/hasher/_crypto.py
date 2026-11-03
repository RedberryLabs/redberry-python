"""SHA-256, VALP key ids and SUITE-1 Ed25519 signatures (``@redberrylabs/otel`` SPEC.md §6).

SUITE-1 (``valp_core.hashing.signing_message``): Ed25519 signs the 33 bytes ``0x05 || SHA-256(raw body)``,
never the body itself. ``key_id`` (``valp_core.hashing.key_id``): lowercase hex of the first 16 bytes of
SHA-256 over the raw 32-byte public key.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field

__all__ = ["Signer", "decode_signing_key", "key_id_of", "load_signer", "signing_message", "verify"]

SIG_CONTEXT = 0x05
#: DER prefix of a PKCS8 Ed25519 private key; the 32-byte seed follows (RFC 8410).
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def key_id_of(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("an Ed25519 public key is 32 raw bytes")
    return hashlib.sha256(public_key).digest()[:16].hex()


def signing_message(body: bytes) -> bytes:
    """The 33 bytes Ed25519 signs: ``0x05 || SHA-256(body)``."""
    return bytes([SIG_CONTEXT]) + hashlib.sha256(body).digest()


def _unb64(value: str) -> bytes:
    """Standard or URL-safe base64, padded or not."""
    t = re.sub(r"\s+", "", value).replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t, validate=True)


def decode_signing_key(value: str) -> bytes:
    """The 32-byte Ed25519 seed from ``REDBERRY_SIGNING_KEY``: base64 PKCS8 DER (what ``redberry connect``
    writes), a PEM ``PRIVATE KEY`` block, or a bare base64 32-byte seed."""
    body = re.sub(r"-----(BEGIN|END) PRIVATE KEY-----", "", value or "").strip()
    try:
        der = _unb64(body)
    except (binascii.Error, ValueError):
        der = b""
    if len(der) == 32:
        return der
    if len(der) != 48 or not der.startswith(PKCS8_PREFIX):
        raise ValueError("REDBERRY_SIGNING_KEY is not a base64 PKCS8 Ed25519 private key")
    return der[len(PKCS8_PREFIX):]


@dataclass(frozen=True)
class Signer:
    key_id: str
    public_key: bytes
    _key: object = field(repr=False)

    def sign(self, body: bytes) -> bytes:
        """The 64-byte SUITE-1 signature over ``body`` exactly as it will be sent."""
        return self._key.sign(signing_message(body))  # type: ignore[attr-defined]


def load_signer(signing_key: str, key_id: str | None = None) -> Signer:
    """A signer for ``REDBERRY_SIGNING_KEY``; ``key_id`` (``REDBERRY_KEY_ID``), when given, must be its id."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    sk = Ed25519PrivateKey.from_private_bytes(decode_signing_key(signing_key))
    public = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    derived = key_id_of(public)
    if key_id and key_id != derived:
        raise ValueError(f"REDBERRY_KEY_ID {key_id} is not the id of REDBERRY_SIGNING_KEY ({derived})")
    return Signer(key_id=derived, public_key=public, _key=sk)


def verify(public_key: bytes, body: bytes, signature: bytes) -> bool:
    """Check a SUITE-1 signature (tests, and anyone checking a stored report)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_message(body))
        return True
    except (InvalidSignature, ValueError):
        return False
