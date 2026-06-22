"""Symmetric encryption helpers for credentials stored at rest.

Used for fields like ProxmoxIntegration.token_secret_enc / password_enc so
the SQLite file isn't a plaintext-credentials dump if it leaks.

The Fernet key is derived from settings.secret_key (already required for JWT)
via HKDF — no new env var to configure. Rotating SECRET_KEY invalidates all
encrypted values; users must re-enter credentials after rotation.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    # Domain-separate from JWT signing by hashing with a constant salt.
    digest = hashlib.sha256(b"homelable.proxmox.v1|" + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string; returns base64 token suitable for DB storage."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt(); raises ValueError if tampered/rotated."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as err:
        raise ValueError(
            "Failed to decrypt stored secret — SECRET_KEY may have rotated. "
            "Re-enter the credential to repair."
        ) from err
