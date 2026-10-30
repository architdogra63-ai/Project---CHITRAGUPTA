#!/usr/bin/env python3
"""Tamper demonstration script for Chitragupta.

Demonstrates two tampering scenarios:
- flip: Flips one byte in the middle of the stored .enc file (expected: decryption_failed)
- edit: Decrypts, changes one byte of plaintext, re-encrypts with same key and file_id (expected: hash_mismatch)
"""

import sys
from pathlib import Path

# Ensure backend is in path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.db import get_connection
from app.crypto import encrypt_file, decrypt_file_to_bytes, DecryptionError
from app.storage import stored_path_for


def flip_byte(file_id: str) -> None:
    """Flip one byte in the middle of the stored .enc file."""
    settings = get_settings()
    enc_path = stored_path_for(file_id)
    
    if not enc_path.exists():
        print(f"Error: Encrypted file not found for file_id={file_id}", file=sys.stderr)
        sys.exit(1)
    
    data = enc_path.read_bytes()
    if len(data) < 13:  # Need at least nonce (12) + 1 byte of ciphertext
        print(f"Error: Encrypted file too small to tamper", file=sys.stderr)
        sys.exit(1)
    
    # Flip a byte in the ciphertext portion (after the 12-byte nonce)
    # Pick a byte in the middle of the ciphertext
    ciphertext_start = 12
    middle_offset = ciphertext_start + (len(data) - ciphertext_start) // 2
    tampered = bytearray(data)
    tampered[middle_offset] ^= 0xFF  # Flip all bits
    
    enc_path.write_bytes(tampered)
    print(f"flip: Flipped byte at offset {middle_offset} in {enc_path}")
    print("Expected result on decrypt: decryption_failed (InvalidTag)")


def edit_byte(file_id: str) -> None:
    """Decrypt, change one byte of plaintext, re-encrypt with same key and file_id."""
    settings = get_settings()
    enc_path = stored_path_for(file_id)
    
    if not enc_path.exists():
        print(f"Error: Encrypted file not found for file_id={file_id}", file=sys.stderr)
        sys.exit(1)
    
    # Decrypt the file
    try:
        plaintext = decrypt_file_to_bytes(enc_path, settings.master_key, file_id)
    except DecryptionError as e:
        print(f"Error: Failed to decrypt file: {e}", file=sys.stderr)
        sys.exit(1)
    
    if len(plaintext) == 0:
        print(f"Error: Plaintext is empty, nothing to edit", file=sys.stderr)
        sys.exit(1)
    
    # Change one byte in the middle of the plaintext
    middle = len(plaintext) // 2
    tampered_plaintext = bytearray(plaintext)
    tampered_plaintext[middle] ^= 0xFF  # Flip all bits
    
    # Write tampered plaintext to a temporary file
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(tampered_plaintext)
        tmp_path = Path(tmp.name)
    
    try:
        # Re-encrypt with the same key and file_id (overwrites the original .enc)
        encrypt_file(tmp_path, enc_path, settings.master_key, file_id)
    finally:
        tmp_path.unlink(missing_ok=True)
    
    print(f"edit: Changed byte at offset {middle} in plaintext, re-encrypted {enc_path}")
    print("Expected result on verify: hash_mismatch (SHA-256 of plaintext changed)")


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python tamper_demo.py <file_id> <flip|edit>", file=sys.stderr)
        sys.exit(1)
    
    file_id = sys.argv[1]
    operation = sys.argv[2].lower()
    
    if operation == "flip":
        flip_byte(file_id)
    elif operation == "edit":
        edit_byte(file_id)
    else:
        print(f"Error: Unknown operation '{operation}'. Use 'flip' or 'edit'.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()