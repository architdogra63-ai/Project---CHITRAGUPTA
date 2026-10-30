"""Unit and integration tests for Phase 2 agents and agent_router."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.vault.writer import VaultWriter, ClaimInput, FlagInput, EntityRef
from app.agents.extractor import extract_file
from app.agents.contradiction import _detect_conflicts, run_contradiction_check
from app.agents.network import run_network_analysis
from app.agents.report import run_report_generation
from app.agents.coordinator import get_analysis_status


# ---------------------------------------------------------------------------
# Extractor Tests
# ---------------------------------------------------------------------------

def test_csv_extraction():
    async def _test():
        csv_bytes = b"date,location,amount\n2026-10-01,Delhi,5000\n2026-10-02,Mumbai,12000\n"
        res = await extract_file("EV-0001", "csv", csv_bytes)
        assert len(res) == 2
        assert "Delhi" in res[0].text
        assert "Mumbai" in res[1].text
        assert res[0].source_ref == "row 2"
        assert res[1].source_ref == "row 3"
    asyncio.run(_test())


def test_fallback_text_extraction():
    async def _test():
        content = b"Plain statement from a witness."
        res = await extract_file("EV-0002", "txt", content)
        assert len(res) == 1
        assert "Plain statement" in res[0].text
    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Contradiction Detection Rules Tests
# ---------------------------------------------------------------------------

def test_detect_amount_conflict():
    claims = [
        {
            "claim_id": "CL-0001",
            "claim_type": "transaction",
            "text": "Transferred 50000 INR to account ACC-1234",
            "entities": ["ACC-1234"],
        },
        {
            "claim_id": "CL-0002",
            "claim_type": "transaction",
            "text": "Transferred 90000 INR to account ACC-1234",
            "entities": ["ACC-1234"],
        },
    ]
    conflicts = _detect_conflicts(claims, max_travel_minutes=15)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "amount"
    assert conflicts[0]["claim_a"] == "CL-0001"
    assert conflicts[0]["claim_b"] == "CL-0002"


def test_detect_count_conflict():
    claims = [
        {
            "claim_id": "CL-0003",
            "claim_type": "statement",
            "text": "Rahul met 2 people at the dinner",
            "entities": ["PERSON-rahul"],
        },
        {
            "claim_id": "CL-0004",
            "claim_type": "statement",
            "text": "Rahul met 5 people at the dinner",
            "entities": ["PERSON-rahul"],
        },
    ]
    conflicts = _detect_conflicts(claims, max_travel_minutes=15)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "count"


def test_detect_people_presence_conflict():
    claims = [
        {
            "claim_id": "CL-0005",
            "claim_type": "statement",
            "text": "Rahul was present at the scene",
            "entities": ["PERSON-rahul"],
        },
        {
            "claim_id": "CL-0006",
            "claim_type": "statement",
            "text": "Rahul was not present and denied attending",
            "entities": ["PERSON-rahul"],
        },
    ]
    conflicts = _detect_conflicts(claims, max_travel_minutes=15)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "people"


def test_detect_time_location_conflict():
    claims = [
        {
            "claim_id": "CL-0007",
            "claim_type": "statement",
            "when": "2026-10-30T10:00:00Z",
            "text": "Rahul was seen at Delhi airport",
            "entities": ["PERSON-rahul"],
        },
        {
            "claim_id": "CL-0008",
            "claim_type": "statement",
            "when": "2026-10-30T10:10:00Z",
            "text": "Rahul was seen in Mumbai harbor",
            "entities": ["PERSON-rahul"],
        },
    ]
    conflicts = _detect_conflicts(claims, max_travel_minutes=15)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "time"


# ---------------------------------------------------------------------------
# Vault Network Analysis & Report Generation
# ---------------------------------------------------------------------------

def test_network_and_report_generation(test_env, test_db):
    async def _test():
        writer = VaultWriter.get_writer()
        case_id = "CASE-001"
        writer.ensure_case(case_id)

        # Sync an evidence note
        writer.sync_evidence_note({
            "case_id": case_id,
            "file_id": "EV-0001",
            "original_filename": "records.csv",
            "file_type": "csv",
            "size_bytes": 128,
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "ledger_entry_no": 1,
            "status": "verified",
            "uploaded_by": "officer_17",
            "uploaded_at": "2026-10-30T10:00:00Z",
        })

        # Add 2 claims linking entities
        writer.write_claim(
            case_id,
            ClaimInput(
                claim_id="CL-0001",
                source_file_id="EV-0001",
                source_ref="row 2",
                claim_type="transaction",
                text="Rahul transferred money to Amit",
                when="2026-10-30T10:00:00Z",
                entities=(
                    EntityRef("person", "Rahul"),
                    EntityRef("person", "Amit"),
                ),
                confidence=0.9,
            ),
        )

        # Network analysis test
        net = await run_network_analysis(case_id)
        assert net["case_id"] == case_id
        assert len(net["nodes"]) >= 2
        assert len(net["edges"]) >= 1

        # Insert case in DB for report metadata
        row = test_db.execute("SELECT user_id FROM users WHERE username = 'officer_17'").fetchone()
        user_id = row[0]
        test_db.execute(
            "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (case_id, "Test Case", user_id, "2026-10-30T09:00:00Z"),
        )

        # Report test
        report = await run_report_generation(case_id)
        assert report["case_id"] == case_id
        assert "report_hash_sha256" in report
        assert report["case_summary"]["total_claims_extracted"] == 1
        assert report["case_summary"]["verified_files"] == 1

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# API Endpoints Tests
# ---------------------------------------------------------------------------

def test_agent_router_endpoints(client: TestClient, test_env, test_db, officer_headers, judge_headers):
    # 1. Create a case
    res = client.post(
        "/cases",
        json={"name": "Financial Probe"},
        headers=officer_headers,
    )
    assert res.status_code == 201
    case_id = res.json()["case_id"]

    # 2. Check claims endpoint (empty initially)
    res = client.get(f"/cases/{case_id}/claims", headers=officer_headers)
    assert res.status_code == 200
    assert res.json()["count"] == 0

    # 3. Check flags endpoint (empty initially)
    res = client.get(f"/cases/{case_id}/flags", headers=officer_headers)
    assert res.status_code == 200
    assert res.json()["count"] == 0

    # 4. Check graph endpoint
    res = client.get(f"/cases/{case_id}/graph", headers=officer_headers)
    assert res.status_code == 200
    assert "nodes" in res.json()
    assert "edges" in res.json()

    # 5. Check report endpoint (RBAC: judge allowed, officer denied)
    res_denied = client.get(f"/cases/{case_id}/report", headers=officer_headers)
    assert res_denied.status_code == 403

    res_allowed = client.get(f"/cases/{case_id}/report", headers=judge_headers)
    assert res_allowed.status_code == 200
    report_data = res_allowed.json()
    assert report_data["case_id"] == case_id
    assert "report_hash_sha256" in report_data

    # 6. Kick off analysis pipeline
    res_start = client.post(f"/cases/{case_id}/analyze", headers=officer_headers)
    assert res_start.status_code == 202
    run_id = res_start.json()["run_id"]
    assert run_id is not None

    # 7. Check status endpoint
    res_status = client.get(f"/cases/{case_id}/analysis/status?run_id={run_id}", headers=officer_headers)
    assert res_status.status_code == 200
    assert res_status.json()["run_id"] == run_id
