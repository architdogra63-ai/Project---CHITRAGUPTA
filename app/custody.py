"""Evidence custody operations: seal, verify, retrieve."""

import contextlib
import hashlib
import json
import tempfile
from pathlib import Path

import sqlite3

from . import config, db, models, timeutil, crypto, scanner, storage, audit, ledger


# Exceptions
class FileTamperedError(Exception):
    """Raised when a file fails verification (tampered)."""


class LedgerCompromisedError(Exception):
    """Raised when the ledger chain verification fails."""


class EvidenceNotFoundError(Exception):
    """Raised when evidence file is not found or not in a verifiable state."""


class UnsupportedFileError(Exception):
    """Raised when file type is not supported."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class QuarantinedError(Exception):
    """Raised when file is quarantined."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# Type alias for verify result (re-export from models for internal use)
VerifyResult = models.VerifyResult


def _verify_and_load(
    conn: sqlite3.Connection, file_id: str, user_id: str
) -> tuple[VerifyResult, bytes | None]:
    """Internal helper: verify file and return (result, plaintext_bytes or None)."""
    result = verify_file(conn, file_id, user_id)
    if result.status == "tampered":
        return result, None
    # Verified: decrypt and return plaintext
    row = conn.execute(
        "SELECT stored_path, sha256 FROM evidence_files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row or not row["stored_path"]:
        raise FileNotFoundError("Stored file path missing")
    settings = config.get_settings()
    try:
        plaintext = crypto.decrypt_file_to_bytes(Path(row["stored_path"]), settings.master_key, file_id)
    except crypto.DecryptionError:
        # This shouldn't happen since verify_file already checked, but handle gracefully
        raise FileTamperedError("Decryption failed after verification")
    return result, plaintext


def seal_file(
    conn: sqlite3.Connection,
    tmp_path: Path,
    sha256: str,
    size_bytes: int,
    original_filename: str,
    case_id: str,
    user_id: str,
) -> dict:
    """Seal an uploaded file: scan, allocate ID, encrypt, append ledger entry."""
    # 1. Scan file
    verdict = scanner.scan_file(tmp_path, original_filename)

    # 2. Reject
    if verdict.decision == "reject":
        storage.safe_delete(tmp_path)
        audit.write_audit(conn, user_id, "upload_rejected", "error", None, case_id, verdict.reason)
        raise UnsupportedFileError(verdict.reason or "unsupported file type")

    # 3. Allocate seq and file_id, insert pending row
    with db.transaction(conn):
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM evidence_files").fetchone()
        seq = row["next_seq"]
        file_id = f"EV-{seq:04d}"
        conn.execute(
            """
            INSERT INTO evidence_files (seq, file_id, case_id, original_filename, file_type, content_type,
                                        size_bytes, sha256, stored_path, status, ledger_entry_no,
                                        uploaded_by, uploaded_at, last_verified_at, tamper_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                file_id,
                case_id,
                original_filename,
                verdict.file_type or "unknown",
                None,  # content_type
                size_bytes,
                sha256,
                None,  # stored_path
                "pending",
                None,  # ledger_entry_no
                user_id,
                timeutil.utc_now_iso(),
                None,  # last_verified_at
                None,  # tamper_reason
            ),
        )

    # 4. Quarantine
    if verdict.decision == "quarantine":
        quarantine_path = storage.quarantine_file(tmp_path, file_id)
        with db.transaction(conn):
            conn.execute(
                "UPDATE evidence_files SET status = ?, stored_path = ? WHERE file_id = ?",
                ("quarantined", str(quarantine_path), file_id),
            )
            conn.execute(
                "INSERT INTO events (created_at, event_type, payload_json) VALUES (?, ?, ?)",
                (
                    timeutil.utc_now_iso(),
                    "FILE_QUARANTINED",
                    json.dumps({"file_id": file_id, "case_id": case_id}),
                ),
            )
        audit.write_audit(conn, user_id, "upload_quarantined", "ok", file_id, case_id, verdict.reason)
        raise QuarantinedError(verdict.reason or "quarantined")

    # 5. Accept: encrypt, ledger, finalize
    stored_path = storage.stored_path_for(file_id)
    settings = config.get_settings()

    try:
        # Encrypt file
        crypto.encrypt_file(tmp_path, stored_path, settings.master_key, file_id)

        # Append ledger entry
        entry = ledger.append_entry(conn, file_id, case_id, sha256, user_id)

        # Update row to verified
        with db.transaction(conn):
            conn.execute(
                """
                UPDATE evidence_files
                SET status = ?, ledger_entry_no = ?, stored_path = ?
                WHERE file_id = ?
                """,
                ("verified", entry["entry_no"], str(stored_path), file_id),
            )
            conn.execute(
                "INSERT INTO events (created_at, event_type, payload_json) VALUES (?, ?, ?)",
                (
                    timeutil.utc_now_iso(),
                    "FILE_SEALED",
                    json.dumps(
                        {
                            "file_id": file_id,
                            "case_id": case_id,
                            "sha256": sha256,
                            "file_type": verdict.file_type or "unknown",
                            "ledger_entry_no": entry["entry_no"],
                        }
                    ),
                ),
            )
        audit.write_audit(conn, user_id, "upload", "ok", file_id, case_id, None)

        return {
            "file_id": file_id,
            "case_id": case_id,
            "sha256": sha256,
            "ledger_entry_no": entry["entry_no"],
            "status": "verified",
        }

    except Exception:
        # On any error: delete encrypted file, mark row failed, audit, re-raise
        storage.safe_delete(stored_path)
        with db.transaction(conn):
            conn.execute("UPDATE evidence_files SET status = ? WHERE file_id = ?", ("failed", file_id))
        audit.write_audit(conn, user_id, "upload", "error", file_id, case_id, "encryption or ledger failed")
        raise
    finally:
        storage.safe_delete(tmp_path)


def _mark_tampered(
    conn: sqlite3.Connection, row: sqlite3.Row, user_id: str, reason: str, actual_sha256: str | None
) -> VerifyResult:
    """Mark file as tampered, create alert and event, audit, return tampered result."""
    file_id = row["file_id"]
    case_id = row["case_id"]
    with db.transaction(conn):
        conn.execute(
            "UPDATE evidence_files SET status = ?, tamper_reason = ? WHERE file_id = ?",
            ("tampered", reason, file_id),
        )
        conn.execute(
            "INSERT INTO alerts (created_at, severity, file_id, case_id, message) VALUES (?, ?, ?, ?, ?)",
            (
                timeutil.utc_now_iso(),
                "critical",
                file_id,
                case_id,
                f"File {file_id} failed verification: {reason}",
            ),
        )
        conn.execute(
            "INSERT INTO events (created_at, event_type, payload_json) VALUES (?, ?, ?)",
            (
                timeutil.utc_now_iso(),
                "FILE_TAMPERED",
                json.dumps({"file_id": file_id, "case_id": case_id, "reason": reason}),
            ),
        )
    audit.write_audit(conn, user_id, "tamper_detected", "tampered", file_id, case_id, reason)

    return VerifyResult(
        file_id=file_id,
        status="tampered",
        reason=reason,
        expected_sha256=row["sha256"],
        actual_sha256=actual_sha256,
        ledger_entry_no=row["ledger_entry_no"],
        chain_ok=True,  # chain check passed before this point
        checked_at=timeutil.utc_now_iso(),
    )


def verify_file(conn: sqlite3.Connection, file_id: str, user_id: str) -> VerifyResult:
    """Verify a sealed file's integrity against ledger and stored ciphertext."""
    # 1. Load evidence row
    row = conn.execute("SELECT * FROM evidence_files WHERE file_id = ?", (file_id,)).fetchone()
    if not row:
        raise EvidenceNotFoundError(f"Evidence file {file_id} not found")

    # 2. Sticky tampered
    if row["status"] == "tampered":
        return VerifyResult(
            file_id=file_id,
            status="tampered",
            reason=row["tamper_reason"],
            expected_sha256=row["sha256"],
            actual_sha256=None,
            ledger_entry_no=row["ledger_entry_no"],
            chain_ok=True,
            checked_at=timeutil.utc_now_iso(),
        )

    # 3. Must be verified
    if row["status"] != "verified":
        raise EvidenceNotFoundError(f"File {file_id} is not in verified state (status: {row['status']})")

    # 4. Verify ledger chain
    chain = ledger.verify_chain(conn)
    if not chain.ok:
        audit.write_audit(conn, user_id, "verify", "error", file_id, row["case_id"], chain.reason)
        raise LedgerCompromisedError(f"Ledger chain compromised: {chain.reason}")

    # 5. Check ledger entry matches row
    entry = ledger.get_entry(conn, row["ledger_entry_no"])
    if not entry:
        return _mark_tampered(conn, row, user_id, "ledger_record_missing", None)
    if entry["file_id"] != file_id:
        return _mark_tampered(conn, row, user_id, "ledger_record_mismatch", None)
    if entry["file_sha256"] != row["sha256"]:
        return _mark_tampered(conn, row, user_id, "ledger_record_mismatch", None)

    # 6. Decrypt
    stored_path = Path(row["stored_path"])
    settings = config.get_settings()
    try:
        plaintext = crypto.decrypt_file_to_bytes(stored_path, settings.master_key, file_id)
    except FileNotFoundError:
        return _mark_tampered(conn, row, user_id, "file_missing", None)
    except crypto.DecryptionError:
        return _mark_tampered(conn, row, user_id, "decryption_failed", None)

    # 7. Verify hash
    actual = hashlib.sha256(plaintext).hexdigest()
    if actual != entry["file_sha256"]:
        return _mark_tampered(conn, row, user_id, "hash_mismatch", actual)

    # 8. Verified: update last_verified_at, audit, return
    conn.execute(
        "UPDATE evidence_files SET last_verified_at = ? WHERE file_id = ?",
        (timeutil.utc_now_iso(), file_id),
    )
    audit.write_audit(conn, user_id, "verify", "ok", file_id, row["case_id"], None)

    return VerifyResult(
        file_id=file_id,
        status="verified",
        reason=None,
        expected_sha256=row["sha256"],
        actual_sha256=actual,
        ledger_entry_no=row["ledger_entry_no"],
        chain_ok=True,
        checked_at=timeutil.utc_now_iso(),
    )


def get_verified_bytes(conn: sqlite3.Connection, file_id: str, user_id: str) -> bytes:
    """Return verified plaintext bytes; raise FileTamperedError if tampered."""
    result, plaintext = _verify_and_load(conn, file_id, user_id)
    if result.status == "tampered":
        raise FileTamperedError(result.reason or "file tampered")
    return plaintext


@contextlib.contextmanager
def verified_tempfile(conn: sqlite3.Connection, file_id: str, user_id: str):
    """Context manager yielding a temp file with verified contents and correct extension."""
    result, plaintext = _verify_and_load(conn, file_id, user_id)
    if result.status == "tampered":
        raise FileTamperedError(result.reason or "file tampered")

    # Get original extension
    row = conn.execute("SELECT original_filename FROM evidence_files WHERE file_id = ?", (file_id,)).fetchone()
    suffix = Path(row["original_filename"]).suffix if row else ".bin"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(plaintext)
        tmp_path = Path(tmp.name)

    try:
        yield tmp_path
    finally:
        storage.safe_delete(tmp_path)