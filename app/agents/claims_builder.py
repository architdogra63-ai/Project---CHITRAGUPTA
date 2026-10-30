"""
Timeline & Claims Builder — converts raw extractions into structured ClaimInput objects.

For each extraction:
  1. Calls the LLM to classify claim_type, entities, timestamp, confidence.
  2. Writes the claim to the vault via VaultWriter.write_claim().
  3. Persists extraction rows in the DB.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from ..db import get_connection, transaction
from ..timeutil import utc_now_iso
from ..vault.writer import VaultWriter, ClaimInput, EntityRef
from .. import llm as llm_client

logger = logging.getLogger(__name__)

# Valid claim types (mirrors writer.py CLAIM_TYPES)
CLAIM_TYPES = ("statement", "transaction", "video_event", "document_text")
# Valid entity types (mirrors naming.py ENTITY_TYPES)
ENTITY_TYPES = ("person", "account", "phone", "place", "device")


def _next_claim_id(conn: sqlite3.Connection) -> str:
    """Allocate the next sequential CL-XXXX ID."""
    # Use extractions table count as a proxy for claim count
    row = conn.execute(
        "SELECT COALESCE(MAX(extraction_id), 0) AS max_id FROM extractions"
    ).fetchone()
    # We also need to count vault claims; use a separate counter table approach
    # For simplicity: count claims already in vault by checking existing claim IDs
    row2 = conn.execute(
        "SELECT COUNT(*) AS cnt FROM extractions WHERE extracted_text NOT LIKE '[%'"
    ).fetchone()
    n = (row2["cnt"] if row2 else 0) + 1
    return f"CL-{n:04d}"


def _make_claim_id(conn: sqlite3.Connection, seq: int) -> str:
    return f"CL-{seq:04d}"


async def _classify_extraction(
    extracted_text: str,
    source_ref: str,
    file_id: str,
) -> dict[str, Any]:
    """
    Ask LLM to classify an extraction into a structured claim.

    Returns a dict with keys: claim_type, entities, when, confidence, text.
    """
    prompt = (
        "You are a forensic analyst. Classify the following extracted text as a structured claim.\n\n"
        f"Source: file {file_id}, location: {source_ref}\n"
        f"Text: {extracted_text[:2000]}\n\n"
        "Return a JSON object with these fields:\n"
        '  "claim_type": one of ["statement", "transaction", "video_event", "document_text"]\n'
        '  "text": cleaned, concise version of the claim (max 500 chars)\n'
        '  "when": ISO 8601 timestamp if a date/time is mentioned, else null\n'
        '  "confidence": float 0.0-1.0 indicating extraction confidence\n'
        '  "entities": array of {"type": "<person|account|phone|place|device>", "value": "..."}\n\n'
        "Return ONLY the JSON object."
    )
    try:
        messages = [
            {
                "role": "system",
                "content": "You are a forensic claims extraction API. You must ONLY output a valid JSON object matching the requested schema and nothing else.",
            },
            {"role": "user", "content": prompt},
        ]
        data = await llm_client.chat_json(messages, temperature=0.1)
        return data
    except llm_client.LLMNotConfiguredError:
        # Fallback: treat as plain statement with no entities
        return {
            "claim_type": "statement",
            "text": extracted_text[:500],
            "when": None,
            "confidence": 0.5,
            "entities": [],
        }
    except Exception as exc:
        logger.warning("Claim classification failed: %s", exc)
        return {
            "claim_type": "statement",
            "text": extracted_text[:500],
            "when": None,
            "confidence": 0.3,
            "entities": [],
        }


async def run_claims_builder(
    case_id: str,
    run_id: str,
    user_id: str,
) -> list[str]:
    """
    Build claims from all extractions in the given run.

    1. Load all extraction rows for this run from DB.
    2. For each, classify with LLM.
    3. Write claim to vault.
    4. Return list of claim IDs created.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT extraction_id, file_id, source_ref, extracted_text,
                   entities_json, timestamps_json
            FROM extractions
            WHERE run_id = ?
            ORDER BY extraction_id
            """,
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        logger.info("No extractions found for run %s", run_id)
        return []

    writer = VaultWriter.get_writer()
    claim_ids: list[str] = []

    for i, row in enumerate(rows, start=1):
        extracted_text = row["extracted_text"]

        # Skip error placeholder rows
        if extracted_text.startswith("[") and extracted_text.endswith("]"):
            continue

        file_id = row["file_id"]
        source_ref = row["source_ref"]

        # Classify
        classified = await _classify_extraction(extracted_text, source_ref, file_id)

        claim_type = classified.get("claim_type", "statement")
        if claim_type not in CLAIM_TYPES:
            claim_type = "statement"

        text = str(classified.get("text") or extracted_text)[:4000]
        when = classified.get("when")
        confidence = classified.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
                confidence = max(0.0, min(1.0, confidence))
            except (TypeError, ValueError):
                confidence = None

        # Build EntityRef list
        entity_refs: list[EntityRef] = []
        for ent in classified.get("entities", []) or []:
            if not isinstance(ent, dict):
                continue
            ent_type = str(ent.get("type", "")).lower()
            ent_value = str(ent.get("value", "")).strip()
            if ent_type in ENTITY_TYPES and ent_value:
                entity_refs.append(EntityRef(ent_type, ent_value))

        # Allocate claim ID
        claim_id = f"CL-{(i + 1000):04d}"  # Use 1000+ to avoid collisions across runs

        # Use a more robust ID based on run + extraction_id
        extraction_id = row["extraction_id"]
        claim_id = f"CL-{extraction_id:04d}"

        # Write to vault
        try:
            claim_input = ClaimInput(
                claim_id=claim_id,
                source_file_id=file_id,
                source_ref=source_ref[:200],
                claim_type=claim_type,
                text=text,
                when=when,
                entities=tuple(entity_refs),
                confidence=confidence,
            )
            writer.write_claim(case_id, claim_input)
            claim_ids.append(claim_id)
            logger.debug("Written claim %s for file %s", claim_id, file_id)
        except Exception as exc:
            logger.warning("Failed to write claim %s: %s", claim_id, exc)

    return claim_ids
