"""File storage utilities."""

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Tuple

from fastapi import UploadFile

from .config import get_settings


class UploadTooLargeError(Exception):
    """Raised when uploaded file exceeds maximum allowed size."""


def ensure_dirs() -> None:
    """Create tmp/, files/, quarantine/ directories under storage_dir."""
    settings = get_settings()
    for subdir in ("tmp", "files", "quarantine"):
        (settings.storage_dir / subdir).mkdir(parents=True, exist_ok=True)


def save_upload_to_tmp(upload: UploadFile, max_bytes: int) -> Tuple[Path, str, int]:
    """Stream upload to tmp/<uuid4>.part in 1 MB chunks, return (path, sha256_hex, size)."""
    ensure_dirs()
    settings = get_settings()
    tmp_path = settings.storage_dir / "tmp" / f"{uuid.uuid4()}.part"
    sha256 = hashlib.sha256()
    size = 0
    chunk_size = 1024 * 1024  # 1 MB

    try:
        # Use upload.file for synchronous reading (SpooledTemporaryFile)
        while True:
            chunk = upload.file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLargeError(f"File size {size} exceeds limit {max_bytes}")
            sha256.update(chunk)
            with tmp_path.open("ab") as f:
                f.write(chunk)
    except UploadTooLargeError:
        safe_delete(tmp_path)
        raise
    except Exception:
        safe_delete(tmp_path)
        raise

    return tmp_path, sha256.hexdigest(), size


def stored_path_for(file_id: str) -> Path:
    """Return Path to encrypted file: files/<file_id>.enc."""
    settings = get_settings()
    return settings.storage_dir / "files" / f"{file_id}.enc"


def quarantine_file(tmp_path: Path, file_id: str) -> Path:
    """Move file to quarantine/<file_id>.bin and return new path."""
    ensure_dirs()
    settings = get_settings()
    dest = settings.storage_dir / "quarantine" / f"{file_id}.bin"
    shutil.move(str(tmp_path), str(dest))
    return dest


def safe_delete(path: Path) -> None:
    """Delete file at path, ignore if missing."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass