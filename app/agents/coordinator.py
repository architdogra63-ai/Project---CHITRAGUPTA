"""
Case Coordinator (Orchestrator) — drives the full analysis pipeline for a case.

Pipeline stages:
  1. extracting           → run Extraction Agent for each evidence file
  2. building_claims      → run Claims Builder on all extractions
  3. checking_contradictions → run Contradiction Investigator
  4. skeptic_review       → run Skeptic Agent on all flags
  5. generating_report    → run Report Agent
  6. completed / failed

State is persisted in the analysis_runs and extractions tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..db import get_connection, transaction
from ..timeutil import utc_now_iso
from .extractor import run_extraction
from .claims_builder import run_claims_builder
from .contradiction import run_contradiction_check
from .skeptic import run_skeptic_review
from .report import run_report_generation

logger = logging.getLogger(__name__)

# Pipeline statuses (must match schema CHECK constraint)
STATUS_PENDING = "pending"
STATUS_EXTRACTING = "extracting"
STATUS_BUILDING_CLAIMS = "building_claims"
STATUS_CHECKING = "checking_contradictions"
STATUS_SKEPTIC = "skeptic_review"
STATUS_REPORTING = "generating_report"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _create_run(conn, case_id: str, user_id: str) -> str:
    """Insert a new analysis_runs row and return the run_id."""
    run_id = str(uuid.uuid4())
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO analysis_runs
              (run_id, case_id, status, started_at, started_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, case_id, STATUS_PENDING, utc_now_iso(), user_id),
        )
    return run_id


def _update_status(conn, run_id: str, status: str, error: str | None = None) -> None:
    """Update the status (and optionally error) of a run."""
    with transaction(conn):
        if status == STATUS_COMPLETED or status == STATUS_FAILED:
            conn.execute(
                """
                UPDATE analysis_runs
                SET status = ?, completed_at = ?, error_detail = ?
                WHERE run_id = ?
                """,
                (status, utc_now_iso(), error, run_id),
            )
        else:
            conn.execute(
                "UPDATE analysis_runs SET status = ? WHERE run_id = ?",
                (status, run_id),
            )


def _save_extractions(conn, extractions: list[dict]) -> None:
    """Bulk-insert extraction rows."""
    with transaction(conn):
        for row in extractions:
            conn.execute(
                """
                INSERT INTO extractions
                  (run_id, file_id, source_ref, extracted_text,
                   entities_json, timestamps_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    row["file_id"],
                    row["source_ref"],
                    row["extracted_text"],
                    row.get("entities_json", "[]"),
                    row.get("timestamps_json", "[]"),
                    utc_now_iso(),
                ),
            )


def _save_result(conn, run_id: str, result: dict) -> None:
    """Persist the final result JSON in analysis_runs."""
    with transaction(conn):
        conn.execute(
            "UPDATE analysis_runs SET result_json = ? WHERE run_id = ?",
            (json.dumps(result, ensure_ascii=False), run_id),
        )


def _get_evidence_files(conn, case_id: str) -> list[dict]:
    """Return all verified evidence files for a case."""
    rows = conn.execute(
        """
        SELECT file_id, file_type, status
        FROM evidence_files
        WHERE case_id = ? AND status = 'verified'
        ORDER BY seq
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Pipeline runner (async)
# ---------------------------------------------------------------------------

async def _run_pipeline(case_id: str, run_id: str, user_id: str) -> None:
    """
    Execute all pipeline stages for a case.
    Updates DB status at each stage; catches and records failures.
    """
    conn = get_connection()
    try:
        # --- Stage 1: Extraction ---
        _update_status(conn, run_id, STATUS_EXTRACTING)
        logger.info("[run=%s] Stage: extracting", run_id)

        evidence_files = _get_evidence_files(conn, case_id)
        if not evidence_files:
            _update_status(conn, run_id, STATUS_FAILED, "No verified evidence files found")
            return

        all_extractions: list[dict] = []
        for ef in evidence_files:
            try:
                rows = await run_extraction(case_id, ef["file_id"], user_id, run_id)
                all_extractions.extend(rows)
            except Exception as exc:
                logger.warning(
                    "[run=%s] Extraction failed for %s: %s", run_id, ef["file_id"], exc
                )
                # Add an error placeholder so the claim builder can skip it gracefully
                all_extractions.append({
                    "run_id": run_id,
                    "file_id": ef["file_id"],
                    "source_ref": "error",
                    "extracted_text": f"[Extraction failed: {exc}]",
                    "entities_json": "[]",
                    "timestamps_json": "[]",
                })

        _save_extractions(conn, all_extractions)
        logger.info(
            "[run=%s] Extracted %d rows from %d files",
            run_id, len(all_extractions), len(evidence_files),
        )

        # --- Stage 2: Claims ---
        _update_status(conn, run_id, STATUS_BUILDING_CLAIMS)
        logger.info("[run=%s] Stage: building_claims", run_id)

        try:
            claim_ids = await run_claims_builder(case_id, run_id, user_id)
            logger.info("[run=%s] Built %d claims", run_id, len(claim_ids))
        except Exception as exc:
            logger.warning("[run=%s] Claims builder error: %s", run_id, exc)
            claim_ids = []

        # --- Stage 3: Contradiction check ---
        _update_status(conn, run_id, STATUS_CHECKING)
        logger.info("[run=%s] Stage: checking_contradictions", run_id)

        try:
            flag_ids = await run_contradiction_check(case_id, run_id)
            logger.info("[run=%s] Found %d contradictions", run_id, len(flag_ids))
        except Exception as exc:
            logger.warning("[run=%s] Contradiction check error: %s", run_id, exc)
            flag_ids = []

        # --- Stage 4: Skeptic review ---
        _update_status(conn, run_id, STATUS_SKEPTIC)
        logger.info("[run=%s] Stage: skeptic_review", run_id)

        try:
            skeptic_results = await run_skeptic_review(case_id)
            logger.info(
                "[run=%s] Reviewed %d flags; survived=%d",
                run_id,
                len(skeptic_results),
                sum(1 for v in skeptic_results.values() if v == "survived"),
            )
        except Exception as exc:
            logger.warning("[run=%s] Skeptic review error: %s", run_id, exc)
            skeptic_results = {}

        # --- Stage 5: Report ---
        _update_status(conn, run_id, STATUS_REPORTING)
        logger.info("[run=%s] Stage: generating_report", run_id)

        try:
            report = await run_report_generation(case_id)
        except Exception as exc:
            logger.warning("[run=%s] Report generation error: %s", run_id, exc)
            report = {"error": str(exc), "case_id": case_id}

        # --- Done ---
        _save_result(conn, run_id, report)
        _update_status(conn, run_id, STATUS_COMPLETED)
        logger.info("[run=%s] Pipeline completed for case %s", run_id, case_id)

    except Exception as exc:
        logger.error("[run=%s] Unhandled pipeline error: %s", run_id, exc, exc_info=True)
        try:
            _update_status(conn, run_id, STATUS_FAILED, str(exc)[:1000])
        except Exception:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_analysis(case_id: str, user_id: str) -> str:
    """
    Kick off the analysis pipeline for a case.

    Creates an analysis_runs row, launches the async pipeline in the background,
    and returns the run_id immediately.

    Args:
        case_id: The case to analyze.
        user_id: The user starting the analysis (for audit).

    Returns:
        run_id (UUID string).
    """
    conn = get_connection()
    try:
        # Check case exists
        row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if not row:
            raise ValueError(f"Case {case_id} not found")

        run_id = _create_run(conn, case_id, user_id)
    finally:
        conn.close()

    # Launch async pipeline as a background task
    # We use asyncio.ensure_future if we're in an async context,
    # otherwise create a new event loop thread.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_run_pipeline(case_id, run_id, user_id))
        else:
            import threading
            def _run():
                asyncio.run(_run_pipeline(case_id, run_id, user_id))
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
    except RuntimeError:
        import threading
        def _run():
            asyncio.run(_run_pipeline(case_id, run_id, user_id))
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    return run_id


def get_analysis_status(case_id: str, run_id: str | None = None) -> dict | None:
    """
    Return the status of an analysis run.

    If run_id is None, returns the most recent run for the case.
    Returns None if no runs exist.
    """
    conn = get_connection()
    try:
        if run_id:
            row = conn.execute(
                """
                SELECT run_id, case_id, status, started_at, completed_at,
                       started_by, error_detail
                FROM analysis_runs
                WHERE run_id = ? AND case_id = ?
                """,
                (run_id, case_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT run_id, case_id, status, started_at, completed_at,
                       started_by, error_detail
                FROM analysis_runs
                WHERE case_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        if not row:
            return None

        return {
            "run_id": row["run_id"],
            "case_id": row["case_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "started_by": row["started_by"],
            "error_detail": row["error_detail"],
        }
    finally:
        conn.close()


def get_analysis_result(case_id: str, run_id: str | None = None) -> dict | None:
    """
    Return the result JSON of a completed run.

    Returns None if the run doesn't exist or hasn't completed.
    """
    conn = get_connection()
    try:
        if run_id:
            row = conn.execute(
                "SELECT status, result_json FROM analysis_runs WHERE run_id = ? AND case_id = ?",
                (run_id, case_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT status, result_json FROM analysis_runs
                WHERE case_id = ? AND status = 'completed'
                ORDER BY started_at DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()

        if not row or not row["result_json"]:
            return None

        return json.loads(row["result_json"])
    finally:
        conn.close()
