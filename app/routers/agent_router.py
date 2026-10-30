"""Agent router — exposes AI pipeline endpoints for case analysis."""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path as FPath

from ..auth import get_current_user, require_roles
from ..db import get_db
from ..vault.writer import VaultWriter
from ..vault.notes import parse_note
from ..agents.coordinator import (
    run_analysis,
    get_analysis_status,
    get_analysis_result,
)
from ..agents.network import run_network_analysis
from ..agents.report import run_report_generation

router = APIRouter(tags=["agents"])


# ---------------------------------------------------------------------------
# Analysis pipeline endpoints
# ---------------------------------------------------------------------------

@router.post("/cases/{case_id}/analyze", status_code=202)
def start_analysis(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(require_roles("police", "forensic", "prosecutor", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Start the full AI analysis pipeline for a case.

    Kicks off extraction → claims → contradiction → skeptic → report stages.
    Returns immediately with run_id; poll /status to track progress.
    """
    # Check case exists
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    try:
        run_id = run_analysis(case_id, user["user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "run_id": run_id,
        "case_id": case_id,
        "status": "pending",
        "message": "Analysis pipeline started. Poll /analysis/status for updates.",
    }


@router.get("/cases/{case_id}/analysis/status")
def analysis_status(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    run_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Get the status of the most recent (or specified) analysis run for a case.
    """
    status = get_analysis_status(case_id, run_id)
    if status is None:
        raise HTTPException(
            status_code=404,
            detail=f"No analysis runs found for case {case_id}",
        )
    return status


@router.get("/cases/{case_id}/analysis/result")
def analysis_result(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    run_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Get the final result of a completed analysis run.
    Returns 404 if no completed run exists, 202 if still running.
    """
    # Check current status
    status = get_analysis_status(case_id, run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No analysis runs found for case {case_id}")

    current_status = status.get("status", "")
    if current_status in ("pending", "extracting", "building_claims",
                          "checking_contradictions", "skeptic_review", "generating_report"):
        raise HTTPException(
            status_code=202,
            detail=f"Analysis still running (stage: {current_status}). Check /analysis/status.",
        )

    if current_status == "failed":
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed: {status.get('error_detail', 'unknown error')}",
        )

    result = get_analysis_result(case_id, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not available yet")

    return result


# ---------------------------------------------------------------------------
# Per-file extraction endpoint
# ---------------------------------------------------------------------------

@router.post("/cases/{case_id}/extract/{file_id}", status_code=200)
async def extract_single_file(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    file_id: str = FPath(..., pattern=r"^EV-\d{4,}$"),
    user: dict = Depends(require_roles("police", "forensic", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Run extraction on a single evidence file (without starting the full pipeline).
    Returns the extracted rows directly for inspection.
    """
    # Verify the file belongs to this case
    row = conn.execute(
        "SELECT file_id, file_type, status FROM evidence_files WHERE file_id = ? AND case_id = ?",
        (file_id, case_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Evidence file {file_id} not found in case {case_id}")
    if row["status"] != "verified":
        raise HTTPException(status_code=400, detail=f"File {file_id} is not verified (status: {row['status']})")

    from ..agents.extractor import run_extraction
    import uuid
    ephemeral_run_id = f"ephemeral-{uuid.uuid4()}"

    try:
        extractions = await run_extraction(case_id, file_id, user["user_id"], ephemeral_run_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")

    return {
        "file_id": file_id,
        "case_id": case_id,
        "extractions_count": len(extractions),
        "extractions": extractions,
    }


# ---------------------------------------------------------------------------
# Claims listing endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/claims")
def list_claims(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    List all claims extracted for a case from the vault.
    """
    # Verify case exists
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    writer = VaultWriter.get_writer()
    claims_dir = writer.root / case_id / "claims"

    claims = []
    if claims_dir.exists():
        for md_file in sorted(claims_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "claim":
                    claims.append({
                        "claim_id": fm.get("claim_id"),
                        "claim_type": fm.get("claim_type"),
                        "text": fm.get("text", "")[:300],
                        "when": fm.get("when"),
                        "source_file_id": fm.get("source_file_id"),
                        "source_ref": fm.get("source_ref"),
                        "confidence": fm.get("confidence"),
                        "entities": fm.get("entities", []),
                        "status": fm.get("status", "active"),
                    })
            except Exception:
                pass

    return {"case_id": case_id, "count": len(claims), "claims": claims}


# ---------------------------------------------------------------------------
# Flags listing endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/flags")
def list_flags(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    List all contradiction flags for a case.
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    writer = VaultWriter.get_writer()
    flags_dir = writer.root / case_id / "flags"

    flags = []
    if flags_dir.exists():
        for md_file in sorted(flags_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "flag":
                    flags.append({
                        "flag_id": fm.get("flag_id"),
                        "conflict_type": fm.get("conflict_type"),
                        "claim_a": fm.get("claim_a"),
                        "claim_b": fm.get("claim_b"),
                        "description": fm.get("description", "")[:300],
                        "entities": fm.get("entities", []),
                        "skeptic_verdict": fm.get("skeptic_verdict"),
                        "reviewer_decision": fm.get("reviewer_decision"),
                    })
            except Exception:
                pass

    survived = [f for f in flags if f.get("skeptic_verdict") == "survived"]
    explained = [f for f in flags if f.get("skeptic_verdict") == "explained_away"]
    pending = [f for f in flags if f.get("skeptic_verdict") is None]

    return {
        "case_id": case_id,
        "count": len(flags),
        "survived": len(survived),
        "explained_away": len(explained),
        "pending_review": len(pending),
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Entity graph endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/graph")
async def get_graph(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Get the entity relationship graph for a case.
    Returns nodes, edges, clusters, suspiciousness scores, and investigation leads.
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    try:
        graph = await run_network_analysis(case_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Graph analysis failed: {exc}")

    return graph


# ---------------------------------------------------------------------------
# Report generation endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/report")
async def get_report(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(require_roles("prosecutor", "judge", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Generate and return a court-ready forensic report for a case.

    The report includes:
    - Case summary and evidence inventory
    - Timeline of events
    - All extracted claims
    - Contradictions (survived vs. explained)
    - Entity network summary and investigation leads
    - Tamper-evident report hash

    Access restricted to prosecutor, judge, and admin roles.
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    try:
        report = await run_report_generation(case_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    return report
