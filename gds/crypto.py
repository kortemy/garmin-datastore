"""AES-256-GCM encryption for the Garmin token at rest (NFR-2)."""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD = b"gds-garmin-token-v1"


def new_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


def _key(b64: str) -> bytes:
    key = base64.urlsafe_b64decode(b64)
    if len(key) != 32:
        raise ValueError("TOKEN_ENC_KEY must decode to 32 bytes")
    return key


def encrypt(plaintext: str, key_b64: str, aad: bytes = _AAD) -> tuple[bytes, bytes]:
    """`aad` binds a ciphertext to its use (Garmin vs Strava token), so one can't stand in for the other."""
    nonce = os.urandom(12)
    return AESGCM(_key(key_b64)).encrypt(nonce, plaintext.encode(), aad), nonce


def decrypt(ciphertext: bytes, nonce: bytes, key_b64: str, aad: bytes = _AAD) -> str:
    return AESGCM(_key(key_b64)).decrypt(nonce, ciphertext, aad).decode()
