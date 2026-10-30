"""Ledger operations for tamper-evident evidence chain."""

import hashlib
import re
import sqlite3

from .db import transaction
from .models import ChainStatus
from .timeutil import utc_now_iso

# Validation patterns
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Genesis previous hash
GENESIS_PREV = "0" * 64


def compute_entry_hash(
    entry_no: int,
    file_id: str,
    case_id: str,
    file_sha256: str,
    uploader_id: str,
    created_at: str,
    prev_entry_hash: str,
) -> str:
    """Compute SHA-256 hash of canonical ledger entry string."""
    canonical = "|".join([
        str(entry_no),
        file_id,
        case_id,
        file_sha256,
        uploader_id,
        created_at,
        prev_entry_hash,
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_inputs(file_id: str, case_id: str, file_sha256: str, uploader_id: str) -> None:
    """Validate input identifiers and hash format."""
    if not _ID_RE.fullmatch(file_id):
        raise ValueError("invalid file_id")
    if not _ID_RE.fullmatch(case_id):
        raise ValueError("invalid case_id")
    if not _ID_RE.fullmatch(uploader_id):
        raise ValueError("invalid uploader_id")
    if not _SHA256_RE.fullmatch(file_sha256):
        raise ValueError("invalid file_sha256")


def append_entry(
    conn: sqlite3.Connection,
    file_id: str,
    case_id: str,
    file_sha256: str,
    uploader_id: str,
) -> dict:
    """Append a new entry to the ledger and return it as a dict."""
    _validate_inputs(file_id, case_id, file_sha256, uploader_id)

    with transaction(conn):
        # Get last entry
        row = conn.execute(
            "SELECT entry_no, entry_hash FROM ledger_entries ORDER BY entry_no DESC LIMIT 1"
        ).fetchone()

        if row is None:
            entry_no = 1
            prev_entry_hash = GENESIS_PREV
        else:
            entry_no = row["entry_no"] + 1
            prev_entry_hash = row["entry_hash"]

        created_at = utc_now_iso()
        entry_hash = compute_entry_hash(
            entry_no, file_id, case_id, file_sha256, uploader_id, created_at, prev_entry_hash
        )

        conn.execute(
            """
            INSERT INTO ledger_entries (entry_no, file_id, case_id, file_sha256, uploader_id, created_at, prev_entry_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (entry_no, file_id, case_id, file_sha256, uploader_id, created_at, prev_entry_hash, entry_hash),
        )

        return {
            "entry_no": entry_no,
            "file_id": file_id,
            "case_id": case_id,
            "file_sha256": file_sha256,
            "uploader_id": uploader_id,
            "created_at": created_at,
            "prev_entry_hash": prev_entry_hash,
            "entry_hash": entry_hash,
        }


def get_entry(conn: sqlite3.Connection, entry_no: int) -> dict | None:
    """Retrieve a ledger entry by entry number."""
    row = conn.execute(
        "SELECT * FROM ledger_entries WHERE entry_no = ?", (entry_no,)
    ).fetchone()
    return dict(row) if row else None


def list_entries(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[dict]:
    """List ledger entries with pagination."""
    rows = conn.execute(
        "SELECT * FROM ledger_entries ORDER BY entry_no LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [dict(row) for row in rows]


def verify_chain(conn: sqlite3.Connection) -> ChainStatus:
    """Verify the integrity of the entire ledger chain."""
    rows = conn.execute("SELECT * FROM ledger_entries ORDER BY entry_no").fetchall()

    if not rows:
        return ChainStatus(ok=True, entries_checked=0)

    expected_no = 1
    expected_prev = GENESIS_PREV
    checked = 0

    for row in rows:
        # Check entry_no sequence
        if row["entry_no"] != expected_no:
            return ChainStatus(
                ok=False,
                entries_checked=checked,
                broken_at=row["entry_no"],
                reason="entry_no_gap",
            )

        # Check prev_entry_hash linkage
        if row["prev_entry_hash"] != expected_prev:
            reason = "first_prev_not_genesis" if expected_no == 1 else "prev_hash_mismatch"
            return ChainStatus(
                ok=False,
                entries_checked=checked,
                broken_at=row["entry_no"],
                reason=reason,
            )

        # Recompute and verify entry_hash
        recomputed = compute_entry_hash(
            row["entry_no"],
            row["file_id"],
            row["case_id"],
            row["file_sha256"],
            row["uploader_id"],
            row["created_at"],
            row["prev_entry_hash"],
        )
        if recomputed != row["entry_hash"]:
            return ChainStatus(
                ok=False,
                entries_checked=checked,
                broken_at=row["entry_no"],
                reason="entry_hash_mismatch",
            )

        checked += 1
        expected_no += 1
        expected_prev = row["entry_hash"]

    return ChainStatus(ok=True, entries_checked=checked)


# *tail truncation (deleting the newest entries) is not detectable by the chain alone; Phase 7 may add an external anchor.*