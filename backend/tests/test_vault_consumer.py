"""Vault consumer tests (Phase 2 tests 24-29)."""

import json
import time
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import sqlite3

from app.vault.consumer import (
    get_offset,
    set_offset,
    process_pending_events,
    run_vault_sync,
    vault_status,
    start_poller,
    stop_poller,
)
from app.vault.writer import get_writer
from app.timeutil import utc_now_iso


def _sample_row() -> dict:
    """Helper returning sample evidence row."""
    return {
        "file_id": "EV-0001",
        "case_id": "CASE-001",
        "original_filename": "bank.csv",
        "file_type": "csv",
        "size_bytes": 120,
        "sha256": "a" * 64,
        "status": "verified",
        "ledger_entry_no": 1,
        "uploaded_by": "officer_17",
        "uploaded_at": "2026-10-30T10:00:00.000000Z",
        "tamper_reason": None,
    }


def _create_case(conn: sqlite3.Connection, case_id: str, created_by: str) -> None:
    """Create a test case in the database."""
    conn.execute(
        "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
        (case_id, "Test Case", created_by, utc_now_iso()),
    )
    conn.commit()


def _get_officer_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for officer_17."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'officer_17'").fetchone()
    assert row is not None, "officer_17 user not found"
    return row["user_id"]


class TestVaultConsumer:
    """Test vault consumer event processing."""

    def test_upload_csv_then_sync_creates_note(self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection) -> None:
        """Test 24: Upload a CSV via the API, call process_pending_events -> evidence note exists; offset equals last event id; second call returns 0."""
        # Create case first
        officer_id = _get_officer_user_id(test_db)
        _create_case(test_db, "CASE-001", officer_id)
        
        # Upload a file
        content = b"test,data,csv"
        files = {"upload": ("test.csv", content, "text/csv")}
        response = client.post("/cases/CASE-001/evidence", files=files, headers=officer_headers)
        assert response.status_code == 201
        
        # Process events
        conn = test_db
        writer = get_writer()
        
        processed = process_pending_events(conn, writer)
        assert processed >= 1
        
        # Check offset advanced
        offset = get_offset(conn, "vault")
        last_event = conn.execute("SELECT MAX(event_id) as max_id FROM events").fetchone()
        assert offset == last_event["max_id"]
        
        # Check note exists
        note_path = writer._path("CASE-001", "evidence", "EV-0001.md")
        assert note_path.exists()
        
        # Second call returns 0 (no new events)
        processed2 = process_pending_events(conn, writer)
        assert processed2 == 0

    def test_tamper_then_verify_then_sync_marks_tampered_and_stale(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 25: Upload, write a claim, flip a byte in storage/files/EV-0001.enc, call verify, sync -> note status tampered; claim is stale."""
        import hashlib
        from app import crypto, storage
        from app.config import get_settings
        from app.vault.writer import VaultWriter
        
        # Create case first
        officer_id = _get_officer_user_id(test_db)
        _create_case(test_db, "CASE-001", officer_id)
        
        writer = get_writer()
        
        # Upload a file
        content = b"Evidence to be tampered"
        files = {"upload": ("evidence.csv", content, "text/csv")}
        response = client.post("/cases/CASE-001/evidence", files=files, headers=officer_headers)
        assert response.status_code == 201
        file_id = response.json()["file_id"]
        
        # Write a claim
        from app.vault.writer import ClaimInput, EntityRef
        claim = ClaimInput(
            claim_id="CL-0001",
            source_file_id=file_id,
            source_ref="page 1",
            claim_type="statement",
            text="Test claim from evidence",
            entities=(EntityRef("person", "Suspect A"),),
        )
        process_pending_events(test_db, writer)
        writer.write_claim("CASE-001", claim)
        
        # Flip a byte in the .enc file
        row = test_db.execute(
            "SELECT stored_path FROM evidence_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        enc_path = Path(row["stored_path"])
        data = bytearray(enc_path.read_bytes())
        if len(data) > 12:
            data[12] ^= 0xFF
        enc_path.write_bytes(data)
        
        # Verify (marks tampered)
        response = client.get(f"/evidence/{file_id}/verify", headers=officer_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "tampered"
        
        # Sync - should mark claim stale
        processed = process_pending_events(test_db, writer)
        assert processed >= 1
        
        # Check note status is tampered
        note_path = writer._path("CASE-001", "evidence", f"{file_id}.md")
        content = note_path.read_text(encoding="utf-8")
        assert "tampered" in content
        assert "[!danger]" in content
        
        # Check claim is stale
        claim_path = writer._path("CASE-001", "claims", "CL-0001.md")
        claim_content = claim_path.read_text(encoding="utf-8")
        assert "stale" in claim_content
        assert "[!warning]" in claim_content

    def test_quarantined_upload_then_sync_creates_quarantined_note(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 26: Upload fake.pdf that starts with MZ (HTTP 422), sync -> note exists with tag quarantined."""
        # Create case first
        officer_id = _get_officer_user_id(test_db)
        _create_case(test_db, "CASE-001", officer_id)
        
        writer = get_writer()
        
        # Upload fake.pdf with MZ content (executable)
        content = b"MZ fake pdf with executable content"
        files = {"upload": ("fake.pdf", content, "application/pdf")}
        response = client.post("/cases/CASE-001/evidence", files=files, headers=officer_headers)
        assert response.status_code == 422
        assert response.json()["detail"]["status"] == "quarantined"
        
        # Get the file_id from DB
        row = test_db.execute(
            "SELECT file_id FROM evidence_files WHERE case_id = 'CASE-001' AND status = 'quarantined'"
        ).fetchone()
        assert row is not None
        file_id = row["file_id"]
        
        # Process events
        processed = process_pending_events(test_db, writer)
        assert processed >= 1
        
        # Check note exists with quarantined tag
        note_path = writer._path("CASE-001", "evidence", f"{file_id}.md")
        assert note_path.exists()
        
        content = note_path.read_text(encoding="utf-8")
        assert "quarantined" in content
        assert "[!warning]" in content
        assert "Quarantined" in content

    def test_unknown_event_type_and_missing_file_id_advance_offset(
        self, test_db: sqlite3.Connection
    ) -> None:
        """Test 27: Insert an event of unknown type, and one whose file_id doesn't exist, then sync -> no error; offset advances past both."""
        from app.timeutil import utc_now_iso
        
        writer = get_writer()
        
        # Insert unknown event type
        test_db.execute(
            "INSERT INTO events (created_at, event_type, payload_json) VALUES (?, ?, ?)",
            (utc_now_iso(), "UNKNOWN_TYPE", json.dumps({"file_id": "EV-9999"})),
        )
        # Insert event with missing file_id
        test_db.execute(
            "INSERT INTO events (created_at, event_type, payload_json) VALUES (?, ?, ?)",
            (utc_now_iso(), "FILE_SEALED", json.dumps({"file_id": "EV-NONEXISTENT", "case_id": "CASE-001"})),
        )
        test_db.commit()
        
        # Process - should not error, should advance offset
        processed = process_pending_events(test_db, writer)
        assert processed == 2
        
        # Offset should be at the last event
        offset = get_offset(test_db, "vault")
        last_event = test_db.execute("SELECT MAX(event_id) as max_id FROM events").fetchone()
        assert offset == last_event["max_id"]

    def test_admin_sync_endpoint_and_status_endpoint(
        self, client: TestClient, admin_headers: dict, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 28: POST /vault/sync as admin, as police; GET /vault/status as admin -> 200 with processed; 403; body has the keys in B8."""
        # Create case first
        officer_id = _get_officer_user_id(test_db)
        _create_case(test_db, "CASE-001", officer_id)
        
        writer = get_writer()
        
        # Upload a file first
        content = b"test,data,csv"
        files = {"upload": ("test.csv", content, "text/csv")}
        client.post("/cases/CASE-001/evidence", files=files, headers=officer_headers)
        
        # POST /vault/sync as admin
        response = client.post("/vault/sync", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert "processed" in data
        assert isinstance(data["processed"], int)
        assert data["processed"] >= 1
        
        # POST /vault/sync as police -> 403
        response = client.post("/vault/sync", headers=officer_headers)
        assert response.status_code == 403
        
        # GET /vault/status as admin
        response = client.get("/vault/status", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        # Check B8 keys
        assert "vault_dir" in data
        assert "offset" in data
        assert "pending_events" in data
        assert "cases" in data
        assert "CASE-001" in data["cases"]
        case_data = data["cases"]["CASE-001"]
        assert "evidence" in case_data
        assert "entities" in case_data
        assert "claims" in case_data
        assert "flags" in case_data

    def test_poller_creates_note_automatically(
        self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection
    ) -> None:
        """Test 29: Start the poller with VAULT_POLL_SECONDS=0.1, upload, wait up to 3 s -> note appears; stop_poller() returns."""
        # Create case first
        officer_id = _get_officer_user_id(test_db)
        _create_case(test_db, "CASE-001", officer_id)
        
        import os
        
        # Set poll seconds to 0.1 for fast test
        os.environ["VAULT_POLL_SECONDS"] = "0.1"
        
        writer = get_writer()
        
        # Start poller
        start_poller()
        try:
            # Upload a file
            content = b"polling test"
            files = {"upload": ("poll.csv", content, "text/csv")}
            response = client.post("/cases/CASE-001/evidence", files=files, headers=officer_headers)
            assert response.status_code == 201
            file_id = response.json()["file_id"]
            
            # Wait up to 3 seconds for poller to process
            note_path = writer._path("CASE-001", "evidence", f"{file_id}.md")
            for _ in range(30):
                if note_path.exists():
                    break
                time.sleep(0.1)
            
            assert note_path.exists(), "Poller did not create note within 3 seconds"
            
        finally:
            stop_poller()
            # Restore
            os.environ.pop("VAULT_POLL_SECONDS", None)