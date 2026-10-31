"""Tests for frontend integration, bridge injection, and supporting backend endpoints."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.db import get_connection, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Test client with isolated sqlite db."""
    db_file = tmp_path / "test_chitragupta.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_frontend_serving_and_bridge_injection(client):
    """Verify HTML pages are served with the bridge script injected."""
    # Test index root
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert '<script src="/static/bridge.js"></script>' in res.text
    assert "CHITRAGUPTA - Identity Auth" in res.text

    # Test index.html
    res = client.get("/index.html")
    assert res.status_code == 200
    assert '<script src="/static/bridge.js"></script>' in res.text

    # Test dashboard.html
    res = client.get("/dashboard.html")
    assert res.status_code == 200
    assert '<script src="/static/bridge.js"></script>' in res.text
    assert "Autonomous Evidence Workspace" in res.text

    # Test static bridge script
    res = client.get("/static/bridge.js")
    assert res.status_code == 200
    assert "Chitragupta Frontend-Backend Integration Bridge" in res.text


def test_frontend_files_on_disk_remain_untouched():
    """Verify that original files in backend/frontend/ were not edited."""
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    index_file = frontend_dir / "index.html"
    dashboard_file = frontend_dir / "dashboard.html"

    assert index_file.exists()
    assert dashboard_file.exists()

    index_text = index_file.read_text(encoding="utf-8")
    dashboard_text = dashboard_file.read_text(encoding="utf-8")

    # Bridge script is injected dynamically by FastAPI, NOT written to disk
    assert '<script src="/static/bridge.js"></script>' not in index_text
    assert '<script src="/static/bridge.js"></script>' not in dashboard_text


def test_badge_login_endpoint(client):
    """Test biometric badge photo login."""
    # Register / login new investigator
    res = client.post(
        "/auth/badge-login",
        json={"username": "Agrim Jain", "id_photo": "data:image/png;base64,mockphoto"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["role"] in ("police", "admin")
    assert data["username"] == "Agrim Jain"

    token = data["access_token"]

    # Verify audit log recorded the badge login
    audit_res = client.get(
        "/admin/audit",
        headers={"Authorization": f"Bearer {token}"} if data["role"] == "admin" else None,
    )
    # If role is police, /admin/audit might require admin; check via case audit or DB
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'badge_login' ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert "Agrim Jain" in row["detail"]


def test_active_case_resolution(client):
    """Test active case resolution and auto-initialization."""
    # Login first
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Get active case
    res = client.get("/cases/active", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == "CASE-001"
    assert "FIR-2026/0417" in data["name"]


def test_agents_status_endpoint(client):
    """Test 9-agent status endpoint."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Initialize active case
    client.get("/cases/active", headers=headers)

    res = client.get("/cases/CASE-001/agents", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == "CASE-001"
    assert len(data["agents"]) == 9
    agent_names = [a[0] for a in data["agents"]]
    assert "Case Coordinator" in agent_names
    assert "Custody" in agent_names
    assert "Skeptic" in agent_names


def test_timeline_endpoint(client):
    """Test timeline event aggregation."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    res = client.get("/cases/CASE-001/timeline", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "events" in data
    assert len(data["events"]) > 0
    # Each event is [utc_time, raw_time, text, source, has_conflict]
    ev = data["events"][0]
    assert len(ev) == 5


def test_flags_and_review_flow(client):
    """Test contradiction flags listing and investigator review submission."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    # List flags
    res = client.get("/cases/CASE-001/flags", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "flags" in data

    # Submit review
    review_res = client.post(
        "/cases/CASE-001/flags/FLAG-001/review",
        json={"decision": "Confirmed", "comment": "Verified against ATM surveillance log."},
        headers=headers,
    )
    assert review_res.status_code == 200
    review_data = review_res.json()
    assert review_data["reviewer_decision"] == "Confirmed"

    # Verify audit log recorded review
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'flag_review' ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["result"] == "Confirmed"
    assert "Verified against ATM" in row["detail"]


def test_case_audit_endpoint(client):
    """Test case audit trail inspection."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    res = client.get("/cases/CASE-001/audit", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "logs" in data
    assert len(data["logs"]) > 0
    # Each log is [time, actor, details]
    log = data["logs"][0]
    assert len(log) == 3


def test_court_report_download(client):
    """Test downloading the court forensic report."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    res = client.get("/cases/CASE-001/report/download", headers=headers)
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert "attachment; filename=\"court_report_CASE-001.txt\"" in res.headers["content-disposition"]
    assert "CHITRAGUPTA AUTONOMOUS FORENSIC REPORT" in res.text


def test_remove_evidence_endpoint(client):
    """Test removing an evidence file from a case."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    # 1. Upload a test evidence file
    upload_res = client.post(
        "/cases/CASE-001/evidence",
        files={"upload": ("test_remove.csv", b"timestamp,event\n2026-10-31 06:00:00,test event\n", "text/csv")},
        headers=headers,
    )
    assert upload_res.status_code == 201
    file_id = upload_res.json()["file_id"]

    # Verify it exists in case evidence list
    list_res = client.get("/cases/CASE-001/evidence", headers=headers)
    assert any(f["file_id"] == file_id for f in list_res.json())

    # 2. Delete the evidence file
    del_res = client.delete(f"/cases/CASE-001/evidence/{file_id}", headers=headers)
    assert del_res.status_code == 200
    del_data = del_res.json()
    assert del_data["deleted"] is True
    assert del_data["file_id"] == file_id
    assert "test_remove.csv" in del_data["filename"]

    # 3. Verify it is no longer in case evidence list
    list_after = client.get("/cases/CASE-001/evidence", headers=headers)
    assert not any(f["file_id"] == file_id for f in list_after.json())

    # 4. Verify audit log recorded the removal
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'remove_evidence' AND file_id = ? ORDER BY audit_id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["result"] == "ok"
    assert "removed from case" in row["detail"]

    # 5. Verify 404 for non-existent evidence
    del_404 = client.delete("/cases/CASE-001/evidence/EV-9999", headers=headers)
    assert del_404.status_code == 404


def test_remove_unused_evidence_endpoint(client):
    """Test purging unused/inactive evidence (tampered, quarantined, failed)."""
    login_res = client.post(
        "/auth/badge-login",
        json={"username": "officer_17", "id_photo": "mock"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get("/cases/active", headers=headers)

    # Insert an unused/quarantined evidence record
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO evidence_files (seq, file_id, case_id, original_filename, file_type, size_bytes, sha256, status, uploaded_by, uploaded_at)
        VALUES (999, 'EV-0999', 'CASE-001', 'malicious_quarantined.exe', 'exe', 1024, 'dummy_sha256', 'quarantined', 'officer_17', '2026-10-31T06:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    # Call bulk purge
    purge_res = client.delete("/cases/CASE-001/evidence-unused", headers=headers)
    assert purge_res.status_code == 200
    purge_data = purge_res.json()
    assert purge_data["deleted_count"] >= 1
    assert any(f["file_id"] == "EV-0999" for f in purge_data["removed_files"])

    # Verify EV-0999 is gone
    conn = get_connection()
    check_row = conn.execute("SELECT * FROM evidence_files WHERE file_id = 'EV-0999'").fetchone()
    conn.close()
    assert check_row is None
