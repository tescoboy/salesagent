"""Encryption utilities for sensitive data."""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _get_encryption_key() -> bytes:
    """Get encryption key from environment variable.

    Returns:
        Encryption key as bytes.

    Raises:
        ValueError: If ENCRYPTION_KEY environment variable is not set.
    """
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        raise ValueError(
            "ENCRYPTION_KEY environment variable not set. "
            "Generate a key with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return key.encode()


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt API key for storage.

    Args:
        plaintext: API key in plaintext.

    Returns:
        Encrypted API key as base64-encoded string.

    Raises:
        ValueError: If ENCRYPTION_KEY is not set or plaintext is empty.
    """
    if not plaintext:
        raise ValueError("Cannot encrypt empty string")

    key = _get_encryption_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(plaintext.encode())
    return encrypted.decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt API key for use.

    Args:
        ciphertext: Encrypted API key as base64-encoded string.

    Returns:
        Decrypted API key in plaintext.

    Raises:
        ValueError: If ENCRYPTION_KEY is not set, ciphertext is empty, or decryption fails.
    """
    if not ciphertext:
        raise ValueError("Cannot decrypt empty string")

    try:
        key = _get_encryption_key()
        fernet = Fernet(key)
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except InvalidToken:
        logger.error("Failed to decrypt API key - invalid token or wrong encryption key")
        raise ValueError("Invalid encrypted data or wrong encryption key")
    except Exception as e:
        logger.error(f"Unexpected error during decryption: {e}")
        raise ValueError(f"Decryption failed: {e}")


def is_encrypted(value: str | None) -> bool:
    """Check if a value is a Fernet ciphertext we can decrypt.

    Two-stage:

    1. **Structural check.** Fernet tokens are URL-safe-base64 of a fixed-format
       payload that begins with version byte ``0x80`` — the resulting base64
       always starts with ``gAAAAA``. Plaintext credentials that happen to
       successfully decrypt-to-something would be bizarre, but the prefix check
       short-circuits before we touch the key.
    2. **Decryption check.** Only after the structural match do we attempt
       ``decrypt_api_key()``. ``InvalidToken`` (wrong key, tampered ciphertext,
       expired TTL) returns False — caller treats the value as plaintext.
       Other exceptions (e.g. ``ENCRYPTION_KEY`` unset) propagate so a
       misconfigured deployment fails loud rather than silently re-encrypting
       ciphertext as if it were plaintext on the next save.

    Args:
        value: String to check, or None.

    Returns:
        True if value is a valid Fernet ciphertext under the current key.
        False if value is plaintext / structurally not a Fernet token / token
        is invalid for the current key.

    Raises:
        ValueError: If ``ENCRYPTION_KEY`` is not set when a structural match
            triggers a decryption attempt.
    """
    if not value or not value.startswith("gAAAAA"):
        return False
    try:
        decrypt_api_key(value)
        return True
    except ValueError as exc:
        # decrypt_api_key wraps InvalidToken (and "missing key") as ValueError.
        # An InvalidToken means the structural prefix matched but the value
        # isn't decryptable under the current key — treat as not-encrypted so
        # the caller can fall through to plaintext handling. A missing-key
        # ValueError has a distinct message and re-raises so the deployment
        # surfaces the misconfiguration instead of corrupting data.
        if "ENCRYPTION_KEY environment variable not set" in str(exc):
            raise
        return False


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key.

    Returns:
        New encryption key as string.
    """
    return Fernet.generate_key().decode()
