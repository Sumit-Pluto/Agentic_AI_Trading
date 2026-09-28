"""Fernet encryption/decryption helpers for credentials at rest."""

import json
import os
from cryptography.fernet import Fernet
from typing import Any


# Initialize Fernet cipher with key from FERNET_KEY env var.
# Fails at startup if FERNET_KEY is missing — that's intentional (loud failure).
_fernet_key = os.environ.get("FERNET_KEY")
if not _fernet_key:
    raise RuntimeError(
        "FERNET_KEY environment variable is not set. "
        "Generate one with: python -c 'from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())' "
        "and add it to your .env file."
    )

_fernet = Fernet(_fernet_key.encode())


def encrypt_json(data: dict[str, Any]) -> str:
    """
    Serialize dict to compact JSON and encrypt with Fernet.

    Args:
        data: Dictionary to encrypt

    Returns:
        URL-safe base64 encrypted string
    """
    json_bytes = json.dumps(data, separators=(",", ":")).encode()
    encrypted_bytes = _fernet.encrypt(json_bytes)
    return encrypted_bytes.decode()


def decrypt_json(blob: str) -> dict[str, Any]:
    """
    Decrypt blob and deserialize JSON back to dict.

    Args:
        blob: Encrypted string (from encrypt_json)

    Returns:
        Decrypted dictionary

    Raises:
        cryptography.fernet.InvalidToken: If blob is corrupted or key is wrong
    """
    decrypted_bytes = _fernet.decrypt(blob.encode())
    return json.loads(decrypted_bytes.decode())


def generate_key() -> str:
    """
    Generate a new Fernet key (for one-time setup).

    Usage:
        python -c "from crypto import generate_key; print(generate_key())"

    Returns:
        Base64-encoded key string suitable for FERNET_KEY env var
    """
    return Fernet.generate_key().decode()
