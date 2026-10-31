"""API-level tests for authentication, audit logging, and AAD binding."""

import hashlib
from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient
import sqlite3

from app import storage, crypto
from app.config import get_settings
from app.custody import seal_file


def _get_officer_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for officer_17."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'officer_17'").fetchone()
    assert row is not None, "officer_17 user not found"
    return row["user_id"]


def _get_judge_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for judge_1."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'judge_1'").fetchone()
    assert row is not None, "judge_1 user not found"
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
    storage.ensure_dirs()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        sha256 = hashlib.sha256(content).hexdigest()
        result = seal_file(
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


class TestAuthAndPermissions:
    """Test 14: Authentication and authorization."""

    def test_judge_upload_returns_403(
        self, client: TestClient, judge_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Login as judge_1, upload -> 403 (judges cannot upload)."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-AUTH-001"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Evidence from judge"
        files = {"upload": ("evidence.csv", content, "text/csv")}
        response = client.post(f"/cases/{case_id}/evidence", files=files, headers=judge_headers)

        assert response.status_code == 403
        assert "Insufficient permissions" in response.json().get("detail", "")

    def test_upload_without_token_returns_401(
        self, client: TestClient, test_db: sqlite3.Connection
    ) -> None:
        """No token gives 401 on upload endpoint."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-AUTH-002"
        _create_test_case(test_db, case_id, officer_id)

        content = b"Evidence without token"
        files = {"upload": ("evidence.csv", content, "text/csv")}
        response = client.post(f"/cases/{case_id}/evidence", files=files)

        assert response.status_code == 401
        assert "Not authenticated" in response.json().get("detail", "")

    def test_verify_without_token_returns_401(
        self, client: TestClient, test_db: sqlite3.Connection
    ) -> None:
        """No token gives 401 on verify endpoint."""
        response = client.get("/evidence/EV-0001/verify")
        assert response.status_code == 401

    def test_download_without_token_returns_401(
        self, client: TestClient, test_db: sqlite3.Connection
    ) -> None:
        """No token gives 401 on download endpoint."""
        response = client.get("/evidence/EV-0001/download")
        assert response.status_code == 401


class TestAuditLogging:
    """Test 15: Audit rows exist for upload, verify, download."""

    def test_audit_rows_for_upload_verify_download(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """After upload, verify and download -> audit rows exist for upload, verify, download."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-AUDIT-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file via API
        content = b"Evidence for audit test"
        files = {"upload": ("evidence.csv", content, "text/csv")}
        upload_response = client.post(f"/cases/{case_id}/evidence", files=files, headers=officer_headers)
        assert upload_response.status_code == 201
        file_id = upload_response.json()["file_id"]

        # Verify the file via API
        verify_response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert verify_response.status_code == 200
        assert verify_response.json()["status"] == "verified"

        # Download the file via API
        download_response = client.get(f"/evidence/{file_id}/download", headers=officer_headers)
        assert download_response.status_code == 200
        assert download_response.content == content

        # Check audit_log for the three actions
        audit_rows = test_db.execute(
            """
            SELECT action, result FROM audit_log
            WHERE file_id = ? AND user_id = ?
            ORDER BY audit_id
            """,
            (file_id, officer_id),
        ).fetchall()

        # There may be an extra verify audit from download (which calls verify internally)
        # We expect: upload, verify (from /verify), verify (from /download), download
        # Filter to just the three expected actions, allowing duplicate verify
        expected_actions = ["upload", "verify", "download"]
        filtered_rows = [row for row in audit_rows if row["action"] in expected_actions]

        # Should have at least 3 (upload, verify, download) - verify may appear twice
        assert len(filtered_rows) >= 3
        assert filtered_rows[0]["action"] == "upload"
        assert filtered_rows[0]["result"] == "ok"
        assert filtered_rows[-1]["action"] == "download"
        assert filtered_rows[-1]["result"] == "ok"
        # At least one verify
        verify_rows = [row for row in filtered_rows if row["action"] == "verify"]
        assert len(verify_rows) >= 1
        for vr in verify_rows:
            assert vr["result"] == "ok"

    def test_audit_rows_for_quarantined_upload(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Upload fake.pdf with MZ content -> audit row for upload_quarantined."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-AUDIT-002"
        _create_test_case(test_db, case_id, officer_id)

        # Upload .pdf with MZ (executable) content
        content = b"MZ fake pdf with executable content"
        files = {"upload": ("fake.pdf", content, "application/pdf")}
        response = client.post(f"/cases/{case_id}/evidence", files=files, headers=officer_headers)
        assert response.status_code == 422

        # Get file_id from the database (quarantined files still get a file_id)
        row = test_db.execute(
            "SELECT file_id FROM evidence_files WHERE case_id = ? ORDER BY seq DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        assert row is not None
        file_id = row["file_id"]

        # Check audit_log for upload_quarantined
        audit_rows = test_db.execute(
            "SELECT action, result FROM audit_log WHERE file_id = ? AND user_id = ?",
            (file_id, officer_id),
        ).fetchall()

        assert len(audit_rows) == 1
        assert audit_rows[0]["action"] == "upload_quarantined"
        assert audit_rows[0]["result"] == "ok"


class TestAadBinding:
    """Test 17: Swap two stored .enc files between their IDs -> both verify as tampered (AAD binding)."""

    def test_swap_enc_files_both_tampered(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Swap two stored .enc files between their IDs -> both verify as tampered (AAD binding)."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-AAD-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload first file
        content1 = b"Evidence file A - original content"
        result1 = _upload_file_direct(test_db, case_id, content1, "file_a.csv", officer_id)
        file_id_a = result1["file_id"]

        # Upload second file
        content2 = b"Evidence file B - different content"
        result2 = _upload_file_direct(test_db, case_id, content2, "file_b.csv", officer_id)
        file_id_b = result2["file_id"]

        # Get stored paths for both files
        row_a = test_db.execute(
            "SELECT stored_path, sha256 FROM evidence_files WHERE file_id = ?", (file_id_a,)
        ).fetchone()
        row_b = test_db.execute(
            "SELECT stored_path, sha256 FROM evidence_files WHERE file_id = ?", (file_id_b,)
        ).fetchone()

        assert row_a is not None and row_b is not None
        path_a = Path(row_a["stored_path"])
        path_b = Path(row_b["stored_path"])
        sha256_a = row_a["sha256"]
        sha256_b = row_b["sha256"]

        # Verify both files are initially verified
        resp_a = client.get(f"/evidence/{file_id_a}/verify", headers=officer_headers)
        assert resp_a.status_code == 200
        assert resp_a.json()["status"] == "verified"

        resp_b = client.get(f"/evidence/{file_id_b}/verify", headers=officer_headers)
        assert resp_b.status_code == 200
        assert resp_b.json()["status"] == "verified"

        # Swap the .enc files: copy A's encrypted content to B's path and vice versa
        enc_a = path_a.read_bytes()
        enc_b = path_b.read_bytes()

        path_a.write_bytes(enc_b)
        path_b.write_bytes(enc_a)

        # Verify file A (now contains B's ciphertext) -> should be tampered due to AAD mismatch
        resp_a = client.get(f"/evidence/{file_id_a}/verify", headers=officer_headers)
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert data_a["status"] == "tampered"
        assert data_a["reason"] == "decryption_failed"
        assert data_a["file_id"] == file_id_a

        # Verify file B (now contains A's ciphertext) -> should be tampered due to AAD mismatch
        resp_b = client.get(f"/evidence/{file_id_b}/verify", headers=officer_headers)
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["status"] == "tampered"
        assert data_b["reason"] == "decryption_failed"
        assert data_b["file_id"] == file_id_b

        # Check DB status is tampered for both
        row_a = test_db.execute(
            "SELECT status, tamper_reason FROM evidence_files WHERE file_id = ?", (file_id_a,)
        ).fetchone()
        row_b = test_db.execute(
            "SELECT status, tamper_reason FROM evidence_files WHERE file_id = ?", (file_id_b,)
        ).fetchone()

        assert row_a["status"] == "tampered"
        assert row_a["tamper_reason"] == "decryption_failed"
        assert row_b["status"] == "tampered"
        assert row_b["tamper_reason"] == "decryption_failed"

        # Check alerts were created for both
        alerts_a = test_db.execute(
            "SELECT * FROM alerts WHERE file_id = ?", (file_id_a,)
        ).fetchall()
        alerts_b = test_db.execute(
            "SELECT * FROM alerts WHERE file_id = ?", (file_id_b,)
        ).fetchall()

        assert len(alerts_a) == 1
        assert alerts_a[0]["severity"] == "critical"
        assert "decryption_failed" in alerts_a[0]["message"]

        assert len(alerts_b) == 1
        assert alerts_b[0]["severity"] == "critical"
        assert "decryption_failed" in alerts_b[0]["message"]

        # Check FILE_TAMPERED events were created for both
        events_a = test_db.execute(
            "SELECT * FROM events WHERE event_type = 'FILE_TAMPERED' AND json_extract(payload_json, '$.file_id') = ?",
            (file_id_a,),
        ).fetchall()
        events_b = test_db.execute(
            "SELECT * FROM events WHERE event_type = 'FILE_TAMPERED' AND json_extract(payload_json, '$.file_id') = ?",
            (file_id_b,),
        ).fetchall()

        assert len(events_a) == 1
        assert len(events_b) == 1

        # Verify download returns 423 for both
        download_a = client.get(f"/evidence/{file_id_a}/download", headers=officer_headers)
        assert download_a.status_code == 423

        download_b = client.get(f"/evidence/{file_id_b}/download", headers=officer_headers)
        assert download_b.status_code == 423

    def test_aad_binding_direct_crypto_test(self, test_db: sqlite3.Connection) -> None:
        """Direct test: encrypt with file_id A, try to decrypt with file_id B -> DecryptionError (AAD binding)."""
        settings = get_settings()
        key = settings.master_key

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src_a = tmpdir_path / "source_a.txt"
            src_b = tmpdir_path / "source_b.txt"
            dst_a = tmpdir_path / "encrypted_a.enc"
            dst_b = tmpdir_path / "encrypted_b.enc"

            content_a = b"Content for file A"
            content_b = b"Content for file B"

            src_a.write_bytes(content_a)
            src_b.write_bytes(content_b)

            # Encrypt with file_id A
            crypto.encrypt_file(src_a, dst_a, key, "EV-TEST-A")
            # Encrypt with file_id B
            crypto.encrypt_file(src_b, dst_b, key, "EV-TEST-B")

            # Try to decrypt A's ciphertext with file_id B (wrong AAD) -> should fail
            with pytest.raises(crypto.DecryptionError, match="Invalid authentication tag"):
                crypto.decrypt_file_to_bytes(dst_a, key, "EV-TEST-B")

            # Try to decrypt B's ciphertext with file_id A (wrong AAD) -> should fail
            with pytest.raises(crypto.DecryptionError, match="Invalid authentication tag"):
                crypto.decrypt_file_to_bytes(dst_b, key, "EV-TEST-A")

            # Correct decryptions should work
            assert crypto.decrypt_file_to_bytes(dst_a, key, "EV-TEST-A") == content_a
            assert crypto.decrypt_file_to_bytes(dst_b, key, "EV-TEST-B") == content_b