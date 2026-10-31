"""Cryptographic utilities for file encryption/decryption."""

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionError(Exception):
    """Raised when file decryption fails due to invalid tag or corrupted data."""


def encrypt_file(src_path: Path, dst_path: Path, key: bytes, file_id: str) -> None:
    """Encrypt a file using AES-GCM with file_id as associated data."""
    data = src_path.read_bytes()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, data, file_id.encode())
    dst_path.write_bytes(nonce + ciphertext)


def decrypt_file_to_bytes(path: Path, key: bytes, file_id: str) -> bytes:
    """Decrypt a file encrypted with encrypt_file, returning plaintext bytes."""
    data = path.read_bytes()
    if len(data) < 28:  # 12 bytes nonce + 16 bytes minimum tag
        raise DecryptionError("File too short to contain valid ciphertext")
    nonce, ciphertext = data[:12], data[12:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, file_id.encode())
    except InvalidTag as exc:
        raise DecryptionError("Invalid authentication tag") from exc