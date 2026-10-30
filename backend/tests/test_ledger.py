"""Ledger tests - tamper-evidence, append-only, and concurrency."""

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ledger


def _get_officer_user_id(conn: sqlite3.Connection) -> str:
    """Get the user_id for officer_17."""
    row = conn.execute("SELECT user_id FROM users WHERE username = 'officer_17'").fetchone()
    assert row is not None, "officer_17 user not found"
    return row["user_id"]


def _create_test_case(conn: sqlite3.Connection, case_id: str, created_by: str) -> None:
    """Helper to create a test case."""
    conn.execute(
        "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, datetime('now'))",
        (case_id, "Test Case", created_by),
    )
    conn.commit()


def _upload_file(client: TestClient, headers: dict, case_id: str, content: bytes, filename: str = "test.csv") -> dict:
    """Helper to upload a file and return the response."""
    # Use .csv extension which is allowed by scanner
    if not filename.endswith(".csv"):
        filename = filename.rsplit(".", 1)[0] + ".csv"
    files = {"upload": (filename, content, "text/csv")}
    response = client.post(f"/cases/{case_id}/evidence", files=files, headers=headers)
    return response


class TestLedgerVerify:
    """Test ledger chain verification."""

    def test_upload_3_files_verify_ok(self, client: TestClient, officer_headers: dict, test_db: sqlite3.Connection) -> None:
        """Test 2: Upload 3 files, call /ledger/verify -> ok: true, entries_checked: 3."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-VERIFY-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload 3 files
        for i in range(3):
            content = f"Evidence file {i+1}".encode()
            response = _upload_file(client, officer_headers, case_id, content, f"file{i+1}.txt")
            assert response.status_code == 201, f"Upload {i+1} failed: {response.text}"

        # Verify ledger chain
        response = client.get("/ledger/verify", headers=officer_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["entries_checked"] == 3
        assert data["broken_at"] is None
        assert data["reason"] is None


class TestLedgerAppendOnly:
    """Test ledger append-only enforcement via triggers."""

    def test_raw_sql_update_raises_databaseerror(self, test_db: sqlite3.Connection, officer_headers: dict, client: TestClient) -> None:
        """Test 3: Raw SQL UPDATE on ledger_entries -> raises sqlite3.DatabaseError."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-UPDATE-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file to create a ledger entry
        response = _upload_file(client, officer_headers, case_id, b"test content", "test.txt")
        assert response.status_code == 201

        # Try to UPDATE ledger_entries directly - should fail due to trigger
        with pytest.raises(sqlite3.DatabaseError, match="ledger is append-only"):
            test_db.execute("UPDATE ledger_entries SET file_id = 'EV-9999' WHERE entry_no = 1")
            test_db.commit()

    def test_raw_sql_delete_raises_databaseerror(self, test_db: sqlite3.Connection, officer_headers: dict, client: TestClient) -> None:
        """Test 4: Raw SQL DELETE on ledger_entries -> raises sqlite3.DatabaseError."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-DELETE-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload a file to create a ledger entry
        response = _upload_file(client, officer_headers, case_id, b"test content", "test.txt")
        assert response.status_code == 201

        # Try to DELETE from ledger_entries directly - should fail due to trigger
        with pytest.raises(sqlite3.DatabaseError, match="ledger is append-only"):
            test_db.execute("DELETE FROM ledger_entries WHERE entry_no = 1")
            test_db.commit()


class TestLedgerTamperDetection:
    """Test ledger tamper detection when triggers are bypassed."""

    def test_drop_trigger_edit_row_verify_fails(
        self, test_db: sqlite3.Connection, officer_headers: dict, client: TestClient
    ) -> None:
        """Test 5: Drop trigger, edit one ledger row, run verify_chain -> ok: false, broken_at is that entry."""
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-TAMPER-001"
        _create_test_case(test_db, case_id, officer_id)

        # Upload 3 files to create ledger entries 1, 2, 3
        for i in range(3):
            response = _upload_file(client, officer_headers, case_id, f"content {i}".encode(), f"file{i}.txt")
            assert response.status_code == 201

        # Verify chain is OK initially
        chain = ledger.verify_chain(test_db)
        assert chain.ok is True
        assert chain.entries_checked == 3

        # Drop the UPDATE trigger to bypass append-only protection
        test_db.execute("DROP TRIGGER IF EXISTS ledger_no_update")
        test_db.commit()

        # Tamper with entry 2: change file_sha256
        tampered_hash = "a" * 64
        test_db.execute(
            "UPDATE ledger_entries SET file_sha256 = ? WHERE entry_no = 2",
            (tampered_hash,),
        )
        test_db.commit()

        # Run verify_chain - should detect tampering at entry 2
        chain = ledger.verify_chain(test_db)
        assert chain.ok is False
        assert chain.broken_at == 2
        assert chain.reason == "entry_hash_mismatch"
        # entries_checked should be 1 (only entry 1 was verified before failure)
        assert chain.entries_checked == 1


class TestLedgerConcurrency:
    """Test concurrent ledger appends."""

    def test_20_threads_concurrent_append(
        self, test_db: sqlite3.Connection, officer_headers: dict
    ) -> None:
        """Test 16: 20 threads call ledger.append_entry at once -> entry numbers exactly 1-20 no duplicates, verify_chain ok."""
        # Get officer_id and case_id from the main thread's connection
        officer_id = _get_officer_user_id(test_db)
        case_id = "TEST-CONCURRENCY-001"
        _create_test_case(test_db, case_id, officer_id)

        # Get DB path for creating new connections in threads
        from app.config import get_settings
        db_path = get_settings().db_path

        errors: list[Exception] = []
        results: list[dict] = []
        lock = threading.Lock()

        def append_entry(thread_id: int) -> None:
            # Each thread gets its own connection
            conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                file_id = f"CONCURRENT-{thread_id:03d}"
                file_sha256 = hashlib.sha256(f"content-{thread_id}".encode()).hexdigest()
                entry = ledger.append_entry(
                    conn=conn,
                    file_id=file_id,
                    case_id=case_id,
                    file_sha256=file_sha256,
                    uploader_id=officer_id,
                )
                with lock:
                    results.append(entry)
            except Exception as e:
                with lock:
                    errors.append(e)
            finally:
                conn.close()

        # Launch 20 threads
        threads = [threading.Thread(target=append_entry, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Check no errors occurred
        assert len(errors) == 0, f"Errors in threads: {errors}"

        # Check we got 20 entries
        assert len(results) == 20

        # Check entry numbers are exactly 1-20 with no duplicates
        entry_nos = [r["entry_no"] for r in results]
        assert sorted(entry_nos) == list(range(1, 21))
        assert len(set(entry_nos)) == 20  # no duplicates

        # Verify chain integrity using the main connection
        chain = ledger.verify_chain(test_db)
        assert chain.ok is True
        assert chain.entries_checked == 20