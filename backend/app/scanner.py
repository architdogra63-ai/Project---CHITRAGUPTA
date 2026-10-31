"""File scanner for content validation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class ScanVerdict:
    """Result of scanning a file."""
    decision: Literal["accept", "quarantine", "reject"]
    file_type: str | None
    reason: str | None


ALLOWED = {
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".pdf": "pdf",
    ".mp4": "mp4",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


def scan_file(path: Path, original_filename: str) -> ScanVerdict:
    """Scan a file and return a verdict based on extension and magic bytes."""
    # Read first 4096 bytes
    sample = path.read_bytes()[:4096]

    # 1. Extension not in ALLOWED -> reject
    suffix = Path(original_filename).suffix.lower()
    if suffix not in ALLOWED:
        return ScanVerdict(decision="reject", file_type=None, reason="unsupported file type")

    # 2. Executable content detection
    if sample.startswith(b"MZ") or sample.startswith(b"\x7fELF") or sample.startswith(b"#!") :
        return ScanVerdict(decision="quarantine", file_type=None, reason="executable content")

    # 3. Magic check by extension
    if suffix == ".pdf":
        if not sample.startswith(b"%PDF"):
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")
    elif suffix == ".png":
        if not sample.startswith(b"\x89PNG"):
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")
    elif suffix in (".jpg", ".jpeg"):
        if not sample.startswith(b"\xff\xd8\xff"):
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")
    elif suffix == ".mp4":
        if len(sample) < 8 or sample[4:8] != b"ftyp":
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")
    elif suffix == ".xlsx":
        if not sample.startswith(b"PK\x03\x04"):
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")
    elif suffix == ".csv":
        if b"\x00" in sample:
            return ScanVerdict(decision="quarantine", file_type=None, reason="content does not match extension")

    # 4. Accept with mapped file type
    # TODO Phase 2+: call ClamAV (clamd INSTREAM) here
    return ScanVerdict(decision="accept", file_type=ALLOWED[suffix], reason=None)