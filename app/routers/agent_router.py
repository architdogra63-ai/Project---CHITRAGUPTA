"""Agent router — exposes AI pipeline endpoints for case analysis."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
import urllib.error
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path as FPath
from fastapi.responses import PlainTextResponse

from ..auth import get_current_user, require_roles
from ..config import get_settings
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
# LLM / Model health endpoint
# ---------------------------------------------------------------------------

_llm_cache = {"online": None, "checked_at": 0.0, "models": []}


def check_llm_online(timeout: float = 4.0, pipeline_running: bool = False) -> tuple[bool, list[str]]:
    """
    Check whether the Ollama server at LLM_BASE_URL is reachable.

    Protects against marking the model offline during heavy inference:
    - If a pipeline run is active or if the model was already verified online,
      temporary timeouts mean Ollama is busy generating tokens, NOT offline.
    - Only true connection errors (refusal, unreachable host) mark it offline.
    """
    import time
    now = time.time()

    # If pipeline is actively running, LLM is actively in use
    if pipeline_running:
        _llm_cache["online"] = True
        return True, _llm_cache["models"] or ["qwen3:4b"]

    # Use cache if fresh (within 15 seconds)
    if _llm_cache["online"] is not None and (now - _llm_cache["checked_at"]) < 15.0:
        return _llm_cache["online"], _llm_cache["models"]

    cfg = get_settings()
    ollama_base = cfg.llm_base_url.rstrip("/")
    if ollama_base.endswith("/v1"):
        ollama_base = ollama_base[:-3]
    tags_url = f"{ollama_base}/api/tags"

    try:
        req = urllib.request.Request(tags_url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        raw_models = body.get("models", [])
        models_avail = [
            m["name"] if isinstance(m, dict) else str(m)
            for m in raw_models
        ]
        _llm_cache["online"] = True
        _llm_cache["checked_at"] = now
        _llm_cache["models"] = models_avail
        return True, models_avail
    except (TimeoutError, urllib.error.URLError) as exc:
        err_str = str(exc).lower()
        # If it was previously online and timed out, Ollama is just busy with model inference
        if _llm_cache["online"] is True and ("timed out" in err_str or "timeout" in err_str):
            _llm_cache["checked_at"] = now
            return True, _llm_cache["models"] or [cfg.llm_model or "qwen3:4b"]

        # If connection refused or host unreachable, it is genuinely offline
        _llm_cache["online"] = False
        _llm_cache["checked_at"] = now
        _llm_cache["models"] = []
        return False, []
    except Exception:
        if _llm_cache["online"] is True:
            return True, _llm_cache["models"]
        _llm_cache["online"] = False
        _llm_cache["checked_at"] = now
        _llm_cache["models"] = []
        return False, []


@router.get("/health/llm")
def llm_health() -> dict:
    """
    Check whether the configured Ollama / LLM server is reachable.

    Returns a JSON payload with:
      - online: bool — True if the server responded successfully
      - base_url: str — the configured LLM_BASE_URL (OpenAI-compat endpoint)
      - model: str — the configured LLM_MODEL
      - models_available: list[str] — models listed by Ollama /api/tags
      - error: str | None — error message if offline
    """
    cfg = get_settings()
    online, models = check_llm_online(timeout=3.0)
    return {
        "online": online,
        "base_url": cfg.llm_base_url,
        "model": cfg.llm_model or "qwen3:4b",
        "models_available": models,
        "error": None if online else "Connection to Ollama server timed out or failed",
    }


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
# Agent status endpoint
# ---------------------------------------------------------------------------

_AGENT_NAMES = [
    "Case Coordinator",
    "Custody",
    "Extractor",
    "Claims Builder",
    "Network Analyst",
    "Contradiction Detector",
    "Skeptic",
    "Report Agent",
    "Vault Sync",
]


STAGE_TO_ACTIVE_AGENT = {
    "pending": "Case Coordinator",
    "started": "Case Coordinator",
    "extracting": "Extractor",
    "building_claims": "Claims Builder",
    "checking_contradictions": "Contradiction Detector",
    "skeptic_review": "Skeptic",
    "generating_report": "Report Agent",
}

STAGE_SEQUENCE = [
    "pending",
    "extracting",
    "building_claims",
    "checking_contradictions",
    "skeptic_review",
    "generating_report",
]

AGENT_TO_STAGE = {
    "Case Coordinator": 0,
    "Extractor": 1,
    "Claims Builder": 2,
    "Contradiction Detector": 3,
    "Skeptic": 4,
    "Report Agent": 5,
}


@router.get("/cases/{case_id}/agents")
def get_agents(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Return the status of all 9 pipeline agents for a case.

    Each entry is [name, status, detail] where status is one of:
    'on' (active/ready/online), 'done' (completed), 'wait' (idle/pending), 'err' (error).
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    progress_detail = None
    try:
        run_row = conn.execute(
            "SELECT status, progress_detail FROM analysis_runs WHERE case_id = ? ORDER BY started_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if run_row:
            progress_detail = run_row["progress_detail"]
    except sqlite3.OperationalError:
        run_row = conn.execute(
            "SELECT status FROM analysis_runs WHERE case_id = ? ORDER BY started_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    run_status = run_row["status"] if run_row else "idle"
    cfg = get_settings()
    is_pipeline_active = (run_status in STAGE_TO_ACTIVE_AGENT) or (run_status in ("running", "started"))
    llm_online, _ = check_llm_online(pipeline_running=is_pipeline_active)

    pipeline_running = False
    active_agent = None

    if run_status == "completed":
        agents = [[name, "done", "Completed"] for name in _AGENT_NAMES]
    elif is_pipeline_active:
        pipeline_running = True
        active_agent = STAGE_TO_ACTIVE_AGENT.get(run_status, "Case Coordinator")
        cur_idx = STAGE_SEQUENCE.index(run_status) if run_status in STAGE_SEQUENCE else 0

        agents = []
        for name in _AGENT_NAMES:
            if name == active_agent:
                agents.append([name, "on", "Active"])
            elif name in AGENT_TO_STAGE:
                stg_idx = AGENT_TO_STAGE[name]
                if stg_idx < cur_idx:
                    agents.append([name, "done", "Finished"])
                else:
                    agents.append([name, "wait", "Pending"])
            else:
                agents.append([name, "on", "Active"])
    elif run_status == "failed":
        agents = [[name, "err" if name in ("Skeptic", "Extractor") else "wait", "Failed"] for name in _AGENT_NAMES]
    else:
        # Idle / standby: when Ollama/LLM is reachable, all models and agents are ONLINE ('on')
        status = "on" if llm_online else "wait"
        detail = "Online & Ready" if llm_online else "Standby"
        agents = [[name, status, detail] for name in _AGENT_NAMES]

    return {
        "case_id": case_id,
        "run_status": run_status,
        "pipeline_running": pipeline_running,
        "active_agent": active_agent,
        "progress_detail": progress_detail,
        "model": cfg.llm_model or "qwen3:4b",
        "model_online": llm_online,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# Timeline endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/timeline")
def get_timeline(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Return a timeline of events for a case.

    Events are pulled from:
    1. Latest completed analysis run (result_json['timeline'])
    2. Vault claims (items with a 'when' timestamp)
    3. Audit log milestones (uploads, analysis completions, etc.)

    Each event is a 5-tuple:
    [utc_time, raw_time, text, source, has_conflict]
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    events: list[list] = []
    seen_texts: set[str] = set()

    # Determine which claims have conflicts from the latest run or vault flags
    conflict_claim_ids: set[str] = set()
    latest_run = conn.execute(
        "SELECT result_json FROM analysis_runs WHERE case_id = ? ORDER BY started_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()

    if latest_run and latest_run["result_json"]:
        try:
            rdata = json.loads(latest_run["result_json"])
            for flag in rdata.get("contradictions_survived", []):
                if flag.get("claim_a"):
                    conflict_claim_ids.add(flag["claim_a"])
                if flag.get("claim_b"):
                    conflict_claim_ids.add(flag["claim_b"])
            for flag in rdata.get("contradictions_dismissed", []):
                if flag.get("claim_a"):
                    conflict_claim_ids.add(flag["claim_a"])
                if flag.get("claim_b"):
                    conflict_claim_ids.add(flag["claim_b"])

            for item in rdata.get("timeline", []):
                when = item.get("when")
                txt = (item.get("text") or "").strip()
                if when and txt and txt not in seen_texts:
                    seen_texts.add(txt)
                    cid = item.get("claim_id")
                    has_conflict = (cid in conflict_claim_ids) if cid else False
                    src = item.get("source_file_id") or "evidence"
                    events.append([when, when, txt[:200], src, has_conflict])
        except Exception:
            pass

    # Source 2: vault claims with timestamps
    writer = VaultWriter.get_writer()
    claims_dir = writer.root / case_id / "claims"
    if claims_dir.exists():
        for md_file in sorted(claims_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                when = fm.get("when")
                txt = (fm.get("text") or "").strip()
                cid = fm.get("claim_id")
                if when and txt and fm.get("type") == "claim" and txt not in seen_texts:
                    seen_texts.add(txt)
                    has_conflict = (cid in conflict_claim_ids) if cid else False
                    events.append([when, when, txt[:200], fm.get("source_file_id", "vault"), has_conflict])
            except Exception:
                pass

    # Source 3: audit log entries (only key milestones, skipping spam like verify checks if claims exist)
    audit_rows = conn.execute(
        """SELECT timestamp as created_at, action, detail, result FROM audit_log
           WHERE case_id = ? ORDER BY timestamp ASC LIMIT 100""",
        (case_id,),
    ).fetchall()

    has_evidence_claims = len(events) > 0
    for r in audit_rows:
        action = r["action"]
        if has_evidence_claims and action in ("verify", "access_check", "view_audit"):
            continue
        label = f"{action} ({r['result']})"
        detail = r["detail"] or ""
        txt = f"{label}: {detail}"[:200]
        if txt not in seen_texts:
            seen_texts.add(txt)
            events.append([r["created_at"], r["created_at"], txt, "audit", False])

    # If no events at all, synthesize a stub
    if not events:
        from ..timeutil import utc_now_iso
        now = utc_now_iso()
        events.append([now, now, f"Case {case_id} opened", "system", False])

    events.sort(key=lambda e: e[0] or "")

    return {"case_id": case_id, "events": events}


# ---------------------------------------------------------------------------
# Case audit trail endpoint
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/audit")
def get_case_audit(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Return the audit trail for a specific case.

    Each log entry is a 3-tuple: [time, actor, details].
    """
    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    # Pull audit entries related to this case
    audit_rows = conn.execute(
        """SELECT al.timestamp as created_at, COALESCE(u.username, al.user_id, 'system') as actor,
                  al.action || ' (' || al.result || ')' || COALESCE(': ' || al.detail, '') as details
           FROM audit_log al
           LEFT JOIN users u ON al.user_id = u.user_id
           WHERE al.case_id = ?
           ORDER BY al.timestamp ASC
           LIMIT 200""",
        (case_id,),
    ).fetchall()

    logs = [[r["created_at"], r["actor"], r["details"]] for r in audit_rows]

    # Always return at least one entry (case creation)
    if not logs:
        case_row = conn.execute(
            "SELECT c.created_at, COALESCE(u.username, c.created_by) as actor FROM cases c LEFT JOIN users u ON c.created_by = u.user_id WHERE c.case_id = ?",
            (case_id,),
        ).fetchone()
        if case_row:
            logs.append([case_row["created_at"], case_row["actor"], "case created (ok)"])

    return {"case_id": case_id, "count": len(logs), "logs": logs}


# ---------------------------------------------------------------------------
# Flag review endpoint
# ---------------------------------------------------------------------------

@router.post("/cases/{case_id}/flags/{flag_id}/review")
def review_flag(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    flag_id: str = FPath(...),
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Submit an investigator review decision for a contradiction flag.

    Body: {"decision": str, "comment": str}
    Updates the flag's vault note with the reviewer_decision field
    and writes an audit entry with action='flag_review'.
    """
    from ..audit import write_audit as _write_audit

    row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    decision = body.get("decision", "")
    comment = body.get("comment", "")

    # Try to find and update the flag in the vault
    writer = VaultWriter.get_writer()
    flags_dir = writer.root / case_id / "flags"
    flag_file = None
    flag_fm = {}

    if flags_dir.exists():
        for md_file in sorted(flags_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, body_text = parse_note(text)
                if fm.get("flag_id") == flag_id or md_file.stem == flag_id:
                    flag_file = md_file
                    flag_fm = fm
                    break
            except Exception:
                pass

    if flag_file and flag_file.exists():
        # Update the flag note with the reviewer decision
        flag_fm["reviewer_decision"] = decision
        flag_fm["reviewer_comment"] = comment
        # Re-write the note
        import yaml
        fm_str = yaml.dump(flag_fm, allow_unicode=True, sort_keys=False)
        new_text = f"---\n{fm_str}---\n"
        flag_file.write_text(new_text, encoding="utf-8")

    # Write audit entry
    _write_audit(
        conn=conn,
        user_id=user["user_id"],
        action="flag_review",
        result=decision,
        detail=f"flag {flag_id}: {comment[:200]}",
        case_id=case_id,
    )

    return {
        "flag_id": flag_id,
        "case_id": case_id,
        "reviewer_decision": decision,
        "comment": comment,
    }


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


# ---------------------------------------------------------------------------
# Court report download — returns a human-readable plain-text file
# ---------------------------------------------------------------------------

@router.get("/cases/{case_id}/report/download")
async def download_report(
    case_id: str = FPath(..., pattern=r"^CASE-\d{3,}$"),
    format: Optional[str] = None,
    user: dict = Depends(require_roles("police", "forensic", "prosecutor", "judge", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Download a court-ready forensic report as a plain-text file or JSON.

    Returns a text/plain attachment named ``court_report_{case_id}.txt``
    containing all sections of the forensic analysis in human-readable form,
    ending with the tamper-evident SHA-256 report hash.
    If ?format=json is passed, returns the full report dictionary.
    """
    row = conn.execute("SELECT case_id, name FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    try:
        report = await run_report_generation(case_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    if format == "json":
        return report

    # ── Render plain-text report ────────────────────────────────────────────
    lines: list[str] = []
    sep = "=" * 72

    lines += [
        sep,
        "  CHITRAGUPTA AUTONOMOUS FORENSIC REPORT",
        sep,
        f"  Case ID   : {report['case_id']}",
        f"  Case Name : {report['case_name']}",
        f"  Created By: {report['created_by']}",
        f"  Generated : {report['generated_at']}",
        sep,
        "",
    ]

    # Section 1 — Summary
    s = report["case_summary"]
    lines += [
        "SECTION 1 — CASE SUMMARY",
        "-" * 40,
        f"  Evidence files      : {s['total_evidence_files']}",
        f"    Verified          : {s['verified_files']}",
        f"    Tampered          : {s['tampered_files']}",
        f"    Quarantined       : {s['quarantined_files']}",
        f"  Claims extracted    : {s['total_claims_extracted']}",
        f"  Contradictions found: {s['total_contradictions_found']}",
        f"    Survived review   : {s['contradictions_survived']}",
        f"    Explained away    : {s['contradictions_explained']}",
        f"    Pending review    : {s['contradictions_pending_review']}",
        f"  Entities identified : {s['total_entities_identified']}",
        "",
    ]

    # Section 2 — Evidence Inventory
    lines += ["SECTION 2 — EVIDENCE INVENTORY", "-" * 40]
    for ev in report["evidence_inventory"]:
        lines += [
            f"  [{ev['file_id']}] {ev['filename']}",
            f"    Status    : {ev['status']}",
            f"    SHA-256   : {ev['sha256'][:16]}…" if ev.get("sha256") else "    SHA-256   : (none)",
            f"    Uploaded  : {ev['uploaded_at']}  by {ev['uploaded_by']}",
            f"    Claims    : {ev['claims_count']}",
            "",
        ]

    # Section 3 — Timeline
    lines += ["SECTION 3 — TIMELINE OF EVENTS", "-" * 40]
    if report["timeline"]:
        for ev in report["timeline"]:
            lines += [
                f"  [{ev['when']}] ({ev['claim_type']}) {ev['text']}",
                f"    Source: {ev['source_file_id']}  Confidence: {ev.get('confidence', 'n/a')}",
                "",
            ]
    else:
        lines += ["  (no timestamped events)", ""]

    # Section 4 — Contradictions Survived
    lines += ["SECTION 4 — CONTRADICTIONS SURVIVED", "-" * 40]
    if report["contradictions_survived"]:
        for f in report["contradictions_survived"]:
            lines += [
                f"  [{f['flag_id']}] {f['conflict_type']}",
                f"    Claim A : {str(f['claim_a'])[:120]}",
                f"    Claim B : {str(f['claim_b'])[:120]}",
                f"    Details : {f['description'][:200]}",
                f"    Verdict : {f['skeptic_verdict']}",
                "",
            ]
    else:
        lines += ["  (none)", ""]

    # Section 5 — Contradictions Dismissed
    lines += ["SECTION 5 — CONTRADICTIONS DISMISSED", "-" * 40]
    if report["contradictions_dismissed"]:
        for f in report["contradictions_dismissed"]:
            lines += [
                f"  [{f['flag_id']}] {f['conflict_type']} — {f['description'][:120]}",
                "",
            ]
    else:
        lines += ["  (none)", ""]

    # Section 6 — Entity Graph Summary
    eg = report["entity_graph_summary"]
    lines += [
        "SECTION 6 — ENTITY GRAPH SUMMARY",
        "-" * 40,
        f"  Entities      : {eg['total_entities']}",
        f"  Connections   : {eg['total_connections']}",
        f"  Clusters      : {eg['total_clusters']}",
        f"  Top Entity    : {eg.get('top_entity') or '(none)'}",
        "",
    ]

    # Section 7 — Investigation Leads
    lines += ["SECTION 7 — INVESTIGATION LEADS", "-" * 40]
    if report["investigation_leads"]:
        for lead in report["investigation_leads"]:
            lines += [f"  • {lead}", ""]
    else:
        lines += ["  (none)", ""]

    # Footer — tamper-evident hash
    lines += [
        sep,
        "  TAMPER-EVIDENT REPORT HASH (SHA-256)",
        f"  {report['report_hash_sha256']}",
        sep,
        "",
    ]

    text_body = "\n".join(lines)

    return PlainTextResponse(
        content=text_body,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="court_report_{case_id}.txt"',
        },
    )
