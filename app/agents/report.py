"""
Report Agent — generates a structured court-ready report for a case.

Collects all evidence, claims, flags, and network data, then renders a
comprehensive report as a dict (JSON-serializable) and optionally a PDF.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..vault.writer import VaultWriter
from ..vault.notes import parse_note
from ..db import get_connection
from .network import run_network_analysis

logger = logging.getLogger(__name__)


def _load_all_vault_data(case_id: str) -> dict:
    """Load all notes for a case and categorize them."""
    writer = VaultWriter.get_writer()
    case_path = writer.root / case_id

    result = {
        "evidence": [],
        "claims": [],
        "flags_survived": [],
        "flags_explained": [],
        "flags_pending": [],
        "entities": {},
    }

    # Evidence
    evidence_dir = case_path / "evidence"
    if evidence_dir.exists():
        for md_file in sorted(evidence_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "evidence":
                    result["evidence"].append(fm)
            except Exception:
                pass

    # Claims
    claims_dir = case_path / "claims"
    if claims_dir.exists():
        for md_file in sorted(claims_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "claim":
                    result["claims"].append(fm)
            except Exception:
                pass

    # Sort claims by timestamp
    result["claims"].sort(key=lambda c: c.get("when") or "", reverse=False)

    # Flags — categorize by verdict
    flags_dir = case_path / "flags"
    if flags_dir.exists():
        for md_file in sorted(flags_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "flag":
                    verdict = fm.get("skeptic_verdict")
                    if verdict == "survived":
                        result["flags_survived"].append(fm)
                    elif verdict == "explained_away":
                        result["flags_explained"].append(fm)
                    else:
                        result["flags_pending"].append(fm)
            except Exception:
                pass

    # Entities
    entities_dir = case_path / "entities"
    if entities_dir.exists():
        for subdir in entities_dir.iterdir():
            if not subdir.is_dir():
                continue
            for md_file in sorted(subdir.glob("*.md")):
                if md_file.name.startswith("."):
                    continue
                try:
                    text = md_file.read_text(encoding="utf-8")
                    fm, _ = parse_note(text)
                    if fm.get("type") == "entity":
                        result["entities"][fm.get("key", md_file.stem)] = fm
                except Exception:
                    pass

    return result


def _get_case_meta(case_id: str) -> dict:
    """Load case metadata from the database."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT case_id, name, created_by, created_at FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row:
            return dict(row)
        return {"case_id": case_id, "name": "Unknown", "created_by": "unknown", "created_at": "unknown"}
    finally:
        conn.close()


def _render_report_dict(
    case_id: str,
    case_meta: dict,
    vault_data: dict,
    network_data: dict,
    generated_at: str,
) -> dict:
    """Assemble the complete report as a JSON-serializable dict."""
    evidence = vault_data["evidence"]
    claims = vault_data["claims"]
    flags_survived = vault_data["flags_survived"]
    flags_explained = vault_data["flags_explained"]
    flags_pending = vault_data["flags_pending"]
    entities = vault_data["entities"]

    report = {
        "report_type": "forensic_analysis_report",
        "case_id": case_id,
        "case_name": case_meta.get("name", case_id),
        "created_by": case_meta.get("created_by", "unknown"),
        "case_created_at": case_meta.get("created_at", ""),
        "generated_at": generated_at,

        # Section 1: Case Summary
        "case_summary": {
            "total_evidence_files": len(evidence),
            "verified_files": sum(1 for e in evidence if e.get("status") == "verified"),
            "tampered_files": sum(1 for e in evidence if e.get("status") == "tampered"),
            "quarantined_files": sum(1 for e in evidence if e.get("status") == "quarantined"),
            "total_claims_extracted": len(claims),
            "total_contradictions_found": len(flags_survived) + len(flags_explained) + len(flags_pending),
            "contradictions_survived": len(flags_survived),
            "contradictions_explained": len(flags_explained),
            "contradictions_pending_review": len(flags_pending),
            "total_entities_identified": len(entities),
        },

        # Section 2: Evidence Inventory
        "evidence_inventory": [
            {
                "file_id": e.get("id", ""),
                "filename": e.get("file", ""),
                "file_type": e.get("file_type", ""),
                "status": e.get("status", ""),
                "sha256": e.get("sha256", ""),
                "ledger_entry": e.get("ledger_entry"),
                "uploaded_by": e.get("uploaded_by", ""),
                "uploaded_at": e.get("uploaded_at", ""),
                "claims_count": len(e.get("claims", [])),
            }
            for e in evidence
        ],

        # Section 3: Timeline of Events (claims with timestamps)
        "timeline": [
            {
                "claim_id": c.get("claim_id", ""),
                "when": c.get("when", ""),
                "claim_type": c.get("claim_type", ""),
                "text": c.get("text", "")[:300],
                "source_file_id": c.get("source_file_id", ""),
                "source_ref": c.get("source_ref", ""),
                "confidence": c.get("confidence"),
                "status": c.get("status", "active"),
            }
            for c in claims
            if c.get("when")
        ],

        # Section 4: All Claims (including those without timestamps)
        "all_claims": [
            {
                "claim_id": c.get("claim_id", ""),
                "claim_type": c.get("claim_type", ""),
                "text": c.get("text", "")[:500],
                "when": c.get("when"),
                "source_file_id": c.get("source_file_id", ""),
                "source_ref": c.get("source_ref", ""),
                "confidence": c.get("confidence"),
                "entities": c.get("entities", []),
                "status": c.get("status", "active"),
            }
            for c in claims
        ],

        # Section 5: Contradictions Survived (high priority)
        "contradictions_survived": [
            {
                "flag_id": f.get("flag_id", ""),
                "conflict_type": f.get("conflict_type", ""),
                "claim_a": f.get("claim_a", ""),
                "claim_b": f.get("claim_b", ""),
                "description": f.get("description", ""),
                "entities": f.get("entities", []),
                "skeptic_verdict": f.get("skeptic_verdict", ""),
                "reviewer_decision": f.get("reviewer_decision"),
            }
            for f in flags_survived
        ],

        # Section 6: Contradictions Dismissed
        "contradictions_dismissed": [
            {
                "flag_id": f.get("flag_id", ""),
                "conflict_type": f.get("conflict_type", ""),
                "claim_a": f.get("claim_a", ""),
                "claim_b": f.get("claim_b", ""),
                "description": f.get("description", ""),
                "skeptic_verdict": f.get("skeptic_verdict", ""),
            }
            for f in flags_explained
        ],

        # Section 7: Pending Contradictions (not yet reviewed)
        "contradictions_pending": [
            {
                "flag_id": f.get("flag_id", ""),
                "conflict_type": f.get("conflict_type", ""),
                "description": f.get("description", ""),
            }
            for f in flags_pending
        ],

        # Section 8: Entity Graph Summary
        "entity_graph_summary": {
            "total_entities": network_data.get("summary", {}).get("total_entities", 0),
            "total_connections": network_data.get("summary", {}).get("total_edges", 0),
            "total_clusters": network_data.get("summary", {}).get("total_clusters", 0),
            "top_entity": network_data.get("summary", {}).get("top_entity"),
        },

        # Section 9: Investigation Leads
        "investigation_leads": network_data.get("investigation_leads", []),

        # Section 10: Top Entities by Suspiciousness
        "top_entities": network_data.get("nodes", [])[:10],
    }

    return report


async def run_report_generation(case_id: str) -> dict:
    """
    Generate a comprehensive forensic report for a case.

    1. Load all vault data (evidence, claims, flags, entities).
    2. Run network analysis.
    3. Assemble structured report dict.
    4. Compute tamper-evident hash of the report.
    5. Return the full report dict.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    case_meta = _get_case_meta(case_id)
    vault_data = _load_all_vault_data(case_id)
    network_data = await run_network_analysis(case_id)

    report = _render_report_dict(case_id, case_meta, vault_data, network_data, generated_at)

    # Compute tamper-evident hash of the report body
    report_json = json.dumps(report, sort_keys=True, ensure_ascii=False)
    report_hash = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
    report["report_hash_sha256"] = report_hash

    logger.info(
        "Report generated for case %s: %d claims, %d survived contradictions, hash=%s...",
        case_id,
        report["case_summary"]["total_claims_extracted"],
        report["case_summary"]["contradictions_survived"],
        report_hash[:16],
    )

    return report
