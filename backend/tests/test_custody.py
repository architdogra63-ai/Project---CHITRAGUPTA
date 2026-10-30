"""Custody/verification tests - file integrity, tamper detection, upload validation."""

import hashlib
import os
from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient
import sqlite3

from app import custody, crypto, storage
from app.config import get_settings


def _get_officer_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for officer_17."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'officer_17'").fetchone()
    assert row is not None, "officer_17 user not found"
    return row["user_id"]


def _get_admin_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for admin_1."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'admin_1'").fetchone()
    assert row is not None, "admin_1 user not found"
    return row["user_id"]


def _create_test_case(conn: sqlite3.Connection, case_id: str, created_by: str) -> None:
    """Helper to create a test case."""
    conn.execute(
        "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, datetime('now'))",
        (case_id, "Test Case", created_by),
    )
    conn.commit()


def _upload_file_direct(
    conn: sqlite3.Connection,
    case_id: str,
    content: bytes,
    filename: str,
    uploader_id: str,
) -> dict:
    """Upload a file directly via custody.seal_file (bypassing HTTP)."""
    # Ensure storage directories exist (tmp, files, quarantine)
    storage.ensure_dirs()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        sha256 = hashlib.sha256(content).hexdigest()
        result = custody.seal_file(
            conn=conn,
            tmp_path=tmp_path,
            sha256=sha256,
            size_bytes=len(content),
            original_filename=filename,
            case_id=case_id,
            user_id=uploader_id,
        )
        return result
    finally:
        storage.safe_delete(tmp_path)


def _upload_file_api(
    client: TestClient,
    headers: dict,
    case_id: str,
    content: bytes,
    filename: str = "test.csv",
    content_type: str = "text/csv",
) -> tuple[int, dict]:
    """Upload a file via the API and return (status_code, response_json)."""
    files = {"upload": (filename, content, content_type)}
    response = client.post(f"/cases/{case_id}/evidence", files=files, headers=headers)
    return response.status_code, response.json() if response.content else {}


class TestCustodyVerify:
    """Test file verification of untouched files."""

    def test_verify_untouched_file_status_verified(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 6: Verify an untouched file -> status: verified."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-VERIFY-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file
        content = b"Original evidence content"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Verify via API
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "verified"
        assert data["reason"] is None
        assert data["file_id"] == file_id
        assert data["expected_sha256"] == hashlib.sha256(content).hexdigest()
        assert data["actual_sha256"] == hashlib.sha256(content).hexdigest()
        assert data["chain_ok"] is True


class TestCustodyTamperDetection:
    """Test tamper detection for various attack vectors."""

    def test_flip_byte_in_enc_file_verify_tampered_decryption_failed(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 7: Flip a byte in .enc file, verify -> tampered, reason decryption_failed;
        DB status tampered; 1 alert row; 1 FILE_TAMPERED event; /download returns 423."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-TAMPER-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file
        content = b"Evidence to be tampered"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Get the stored .enc file path
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row is not None
        enc_path = Path(row["stored_path"])
        assert enc_path.exists()

        # Flip a byte in the encrypted file (corrupt the ciphertext)
        data = bytearray(enc_path.read_bytes())
        # Flip a byte in the ciphertext portion (after 12-byte nonce)
        if len(data) > 12:
            data[12] ^= 0xFF  # Flip all bits of first ciphertext byte
        enc_path.write_bytes(data)

        # Verify via API - should detect tampering
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "tampered"
        assert data["reason"] == "decryption_failed"
        assert data["file_id"] == file_id

        # Check DB status is tampered
        row = test_db.execute(
            "SELECT status, tamper_reason FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row["status"] == "tampered"
        assert row["tamper_reason"] == "decryption_failed"

        # Check 1 alert row exists
        alerts = test_db.execute(
            "SELECT * FROM alerts WHERE file_id = ?", (file_id,)
        ).fetchall()
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert "decryption_failed" in alerts[0]["message"]

        # Check 1 FILE_TAMPERED event
        events = test_db.execute(
            "SELECT * FROM events WHERE event_type = 'FILE_TAMPERED' AND json_extract(payload_json, '$.file_id') = ?",
            (file_id,),
        ).fetchall()
        assert len(events) == 1
        payload = events[0]["payload_json"]
        import json
        payload_data = json.loads(payload)
        assert payload_data["file_id"] == file_id
        assert payload_data["reason"] == "decryption_failed"

        # /download should return 423 (Locked)
        response = client.get(f"/evidence/{file_id}/download", headers=officer_headers)
        assert response.status_code == 423

    def test_decrypt_edit_reencrypt_same_key_verify_tampered_hash_mismatch(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 8: Decrypt, edit, re-encrypt with same key, verify -> tampered, reason hash_mismatch."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-TAMPER-002"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file
        content = b"Original evidence content"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Get the stored .enc file path
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row is not None
        enc_path = Path(row["stored_path"])
        settings = get_settings()

        # Decrypt the file
        plaintext = crypto.decrypt_file_to_bytes(enc_path, settings.master_key, file_id)
        assert plaintext == content

        # Edit the plaintext
        modified_content = b"Modified evidence content"
        assert modified_content != content

        # Re-encrypt with same key (overwrites the .enc file)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(modified_content)
            tmp_src_path = Path(tmp.name)
        try:
            crypto.encrypt_file(tmp_src_path, enc_path, settings.master_key, file_id)
        finally:
            storage.safe_delete(tmp_src_path)

        # Actually we need a proper temp file approach
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(modified_content)
            tmp_path = Path(tmp.name)
        try:
            crypto.encrypt_file(tmp_path, enc_path, settings.master_key, file_id)
        finally:
            storage.safe_delete(tmp_path)

        # Verify via API - should detect hash mismatch
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "tampered"
        assert data["reason"] == "hash_mismatch"
        assert data["actual_sha256"] == hashlib.sha256(modified_content).hexdigest()
        assert data["expected_sha256"] == hashlib.sha256(content).hexdigest()

    def test_delete_enc_file_verify_tampered_file_missing(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 9: Delete .enc file, verify -> tampered, reason file_missing."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-TAMPER-003"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file
        content = b"Evidence to be deleted"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Get the stored .enc file path
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row is not None
        enc_path = Path(row["stored_path"])
        assert enc_path.exists()

        # Delete the .enc file
        enc_path.unlink()
        assert not enc_path.exists()

        # Verify via API - should detect missing file
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "tampered"
        assert data["reason"] == "file_missing"
        assert data["actual_sha256"] is None

        # Check DB status is tampered
        row = test_db.execute(
            "SELECT status, tamper_reason FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row["status"] == "tampered"
        assert row["tamper_reason"] == "file_missing"


class TestCustodyStickyTampered:
    """Test sticky tampered behavior - once tampered, always tampered."""

    def test_get_verified_bytes_after_tamper_raises_filetamperederror(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 10: After test 7, call get_verified_bytes again -> raises FileTamperedError (sticky)."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-STICKY-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file
        content = b"Evidence for sticky test"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Get the stored .enc file path
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert row is not None
        enc_path = Path(row["stored_path"])

        # Corrupt the file
        data = bytearray(enc_path.read_bytes())
        if len(data) > 12:
            data[12] ^= 0xFF
        enc_path.write_bytes(data)

        # First verification - marks as tampered
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "tampered"
        assert data["reason"] == "decryption_failed"

        # Second call to get_verified_bytes (via API download) should raise 423
        response = client.get(f"/evidence/{file_id}/download", headers=officer_headers)
        assert response.status_code == 423

        # Direct call to get_verified_bytes should raise FileTamperedError
        with pytest.raises(custody.FileTamperedError):
            custody.get_verified_bytes(test_db, file_id, officer_id)

        # Verify again via API - still tampered (sticky)
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "tampered"
        assert data["reason"] == "decryption_failed"


class TestCustodyUploadValidation:
    """Test upload validation: file type, magic bytes, size limits."""

    def test_upload_evil_exe_returns_415(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 11: Upload evil.exe -> 415."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-UPLOAD-001"
        _create_test_case(test_db, case_id, officer_id)

        # Try to upload .exe file (not in ALLOWED extensions)
        content = b"MZ fake executable"
        status, resp = _upload_file_api(
            client, officer_headers, case_id, content, "evil.exe", "application/octet-stream"
        )
        assert status == 415
        assert "unsupported file type" in resp.get("detail", "").lower()

    def test_upload_fake_pdf_with_mz_content_returns_422_quarantined(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 12: Upload fake.pdf with MZ content -> 422, row status quarantined,
        no ledger entry, file in quarantine/."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-UPLOAD-002"
        _create_test_case(test_db, case_id, officer_id)

        # Upload .pdf with MZ (executable) content
        content = b"MZ fake pdf with executable content"
        status, resp = _upload_file_api(client, officer_headers, case_id, content, "fake.pdf")
        assert status == 422
        assert resp.get("detail", {}).get("status") == "quarantined"
        assert "executable content" in resp.get("detail", {}).get("reason", "")

        # Check DB row exists with status quarantined
        row = test_db.execute(
            "SELECT file_id, status, stored_path, ledger_entry_no FROM evidence_files WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "quarantined"
        assert row["ledger_entry_no"] is None  # No ledger entry for quarantined
        file_id = row["file_id"]

        # Check file is in quarantine/ directory
        quarantine_path = Path(row["stored_path"])
        assert quarantine_path.exists()
        assert "quarantine" in str(quarantine_path)
        assert quarantine_path.name == f"{file_id}.bin"

        # Verify no ledger entry was created
        ledger_entries = test_db.execute(
            "SELECT * FROM ledger_entries WHERE file_id = ?", (file_id,)
        ).fetchall()
        assert len(ledger_entries) == 0

    def test_upload_file_larger_than_max_upload_mb_returns_413(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 13: Upload file larger than MAX_UPLOAD_MB (1 MB) -> 413, nothing left in tmp/."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-UPLOAD-003"
        _create_test_case(test_db, case_id, officer_id)

        settings = get_settings()
        max_bytes = settings.max_upload_bytes  # 1 MB = 1048576 bytes

        # Create content slightly larger than 1 MB
        oversized_content = b"x" * (max_bytes + 100)

        status, resp = _upload_file_api(client, officer_headers, case_id, oversized_content, "large.csv")
        assert status == 413

        # Check tmp/ directory is empty (no leftover files)
        tmp_dir = settings.storage_dir / "tmp"
        tmp_files = list(tmp_dir.glob("*"))
        assert len(tmp_files) == 0, f"Expected empty tmp dir, found: {tmp_files}"

        # Verify no evidence file was created
        rows = test_db.execute(
            "SELECT * FROM evidence_files WHERE case_id = ?", (case_id,)
        ).fetchall()
        assert len(rows) == 0


class TestCustodyDirectFunctions:
    """Test custody module functions directly."""

    def test_verify_file_direct_verified(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Direct test of custody.verify_file returning verified."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DIRECT-001"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Direct verify test"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        verify_result = custody.verify_file(test_db, file_id, officer_id)
        assert verify_result.status == "verified"
        assert verify_result.reason is None
        assert verify_result.actual_sha256 == hashlib.sha256(content).hexdigest()

    def test_verify_file_direct_tampered_sticky(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Direct test of sticky tampered behavior in verify_file."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DIRECT-002"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Sticky test"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Corrupt the .enc file
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        enc_path = Path(row["stored_path"])
        data = bytearray(enc_path.read_bytes())
        if len(data) > 12:
            data[12] ^= 0xFF
        enc_path.write_bytes(data)

        # First verify - marks tampered
        verify_result = custody.verify_file(test_db, file_id, officer_id)
        assert verify_result.status == "tampered"
        assert verify_result.reason == "decryption_failed"

        # Second verify - still tampered (sticky)
        verify_result2 = custody.verify_file(test_db, file_id, officer_id)
        assert verify_result2.status == "tampered"
        assert verify_result2.reason == "decryption_failed"
        assert verify_result2.actual_sha256 is None

    def test_get_verified_bytes_direct_raises_on_tampered(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Direct test of get_verified_bytes raising FileTamperedError."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DIRECT-003"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Get bytes test"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Corrupt the .enc file
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        enc_path = Path(row["stored_path"])
        data = bytearray(enc_path.read_bytes())
        if len(data) > 12:
            data[12] ^= 0xFF
        enc_path.write_bytes(data)

        # First verify to mark as tampered
        custody.verify_file(test_db, file_id, officer_id)

        # get_verified_bytes should raise FileTamperedError
        with pytest.raises(custody.FileTamperedError, match="decryption_failed"):
            custody.get_verified_bytes(test_db, file_id, officer_id)

    def test_verified_tempfile_context_manager(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Test verified_tempfile context manager yields temp file with correct content."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DIRECT-004"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Tempfile test content"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        with custody.verified_tempfile(test_db, file_id, officer_id) as tmp_path:
            assert tmp_path.exists()
            assert tmp_path.read_bytes() == content
            assert tmp_path.suffix == ".csv"

        # Temp file should be cleaned up after context
        assert not tmp_path.exists()

    def test_verified_tempfile_raises_on_tampered(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Test verified_tempfile raises FileTamperedError for tampered file."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DIRECT-005"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Tempfile tampered test"
        result = _upload_file_direct(test_db, case_id, content, "evidence.csv", officer_id)
        file_id = result["file_id"]

        # Corrupt the .enc file
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        enc_path = Path(row["stored_path"])
        data = bytearray(enc_path.read_bytes())
        if len(data) > 12:
            data[12] ^= 0xFF
        enc_path.write_bytes(data)

        # Mark as tampered
        custody.verify_file(test_db, file_id, officer_id)

        # verified_tempfile should raise
        with pytest.raises(custody.FileTamperedError, match="decryption_failed"):
            with custody.verified_tempfile(test_db, file_id, officer_id):
                pass


class TestCryptoRoundtrip:
    """Test crypto encryption/decryption roundtrip."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """Test that encrypt_file + decrypt_file_to_bytes preserves data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "source.txt"
            dst = tmpdir_path / "encrypted.enc"
            key = b"0" * 32  # 32-byte key
            file_id = "EV-TEST-001"

            test_data = b"Hello, World! This is test data for encryption."
            src.write_bytes(test_data)

            crypto.encrypt_file(src, dst, key, file_id)
            assert dst.exists()

            decrypted = crypto.decrypt_file_to_bytes(dst, key, file_id)
            assert decrypted == test_data

    def test_decrypt_corrupted_file_raises_decryptionerror(self) -> None:
        """Test that decrypting corrupted file raises DecryptionError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "source.txt"
            dst = tmpdir_path / "encrypted.enc"
            key = b"0" * 32
            file_id = "EV-TEST-002"

            test_data = b"Test data"
            src.write_bytes(test_data)

            crypto.encrypt_file(src, dst, key, file_id)

            # Corrupt the file
            data = bytearray(dst.read_bytes())
            if len(data) > 12:
                data[12] ^= 0xFF
            dst.write_bytes(data)

            with pytest.raises(crypto.DecryptionError, match="Invalid authentication tag"):
                crypto.decrypt_file_to_bytes(dst, key, file_id)

    def test_decrypt_wrong_key_raises_decryptionerror(self) -> None:
        """Test that decrypting with wrong key raises DecryptionError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "source.txt"
            dst = tmpdir_path / "encrypted.enc"
            key1 = b"0" * 32
            key2 = b"1" * 32
            file_id = "EV-TEST-003"

            test_data = b"Test data"
            src.write_bytes(test_data)

            crypto.encrypt_file(src, dst, key1, file_id)

            with pytest.raises(crypto.DecryptionError, match="Invalid authentication tag"):
                crypto.decrypt_file_to_bytes(dst, key2, file_id)

    def test_decrypt_wrong_file_id_raises_decryptionerror(self) -> None:
        """Test that decrypting with wrong file_id (AAD) raises DecryptionError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "source.txt"
            dst = tmpdir_path / "encrypted.enc"
            key = b"0" * 32
            file_id = "EV-TEST-004"

            test_data = b"Test data"
            src.write_bytes(test_data)

            crypto.encrypt_file(src, dst, key, file_id)

            with pytest.raises(crypto.DecryptionError, match="Invalid authentication tag"):
                crypto.decrypt_file_to_bytes(dst, key, "EV-WRONG-001")


class TestStorageUtilities:
    """Test storage utility functions."""

    def test_stored_path_for_format(self, test_db: sqlite3.Connection) -> None:
        """Test stored_path_for returns correct format."""
        path = storage.stored_path_for("EV-0001")
        assert path.name == "EV-0001.enc"
        assert "files" in path.parts

    def test_quarantine_file_moves_to_quarantine_dir(self, test_db: sqlite3.Connection) -> None:
        """Test quarantine_file moves file to quarantine/ with .bin extension."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"quarantine test")
            tmp_path = Path(tmp.name)

        file_id = "EV-QUAR-001"
        quarantine_path = storage.quarantine_file(tmp_path, file_id)

        assert quarantine_path.exists()
        assert quarantine_path.name == f"{file_id}.bin"
        assert "quarantine" in str(quarantine_path)
        assert not tmp_path.exists()  # Original moved

        # Cleanup
        storage.safe_delete(quarantine_path)

    def test_safe_delete_ignores_missing(self) -> None:
        """Test safe_delete does not raise on missing file."""
        fake_path = Path("/nonexistent/path/file.txt")
        storage.safe_delete(fake_path)  # Should not raise

    def test_save_upload_to_tmp_respects_max_bytes(self, test_db: sqlite3.Connection) -> None:
        """Test save_upload_to_tmp raises UploadTooLargeError when exceeded."""
        from fastapi import UploadFile
        from io import BytesIO

        settings = get_settings()
        max_bytes = settings.max_upload_bytes

        # Create a file-like object with oversized content
        oversized = b"x" * (max_bytes + 100)
        upload_file = UploadFile(filename="test.csv", file=BytesIO(oversized))

        with pytest.raises(storage.UploadTooLargeError):
            storage.save_upload_to_tmp(upload_file, max_bytes)

        # tmp should be cleaned up
        tmp_dir = settings.storage_dir / "tmp"
        tmp_files = list(tmp_dir.glob("*"))
        assert len(tmp_files) == 0