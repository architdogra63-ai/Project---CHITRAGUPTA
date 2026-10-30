"""
Skeptic Agent — LLM-powered review of contradiction flags.

For each flag, reads both source claims and asks the LLM to give a verdict:
  - "survived": the contradiction is real and cannot be explained away.
  - "explained_away": the conflict has a reasonable explanation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..vault.writer import VaultWriter
from ..vault.notes import parse_note
from .. import llm as llm_client

logger = logging.getLogger(__name__)


def _load_flags(case_id: str) -> list[dict]:
    """Load all flag frontmatters that haven't been reviewed yet."""
    writer = VaultWriter.get_writer()
    flags_dir = writer.root / case_id / "flags"

    if not flags_dir.exists():
        return []

    flags = []
    for md_file in sorted(flags_dir.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
            fm, _ = parse_note(text)
            if fm.get("type") == "flag" and fm.get("skeptic_verdict") is None:
                flags.append(fm)
        except Exception as exc:
            logger.warning("Could not read flag %s: %s", md_file.name, exc)

    return flags


def _load_claim_text(case_id: str, claim_id: str) -> str:
    """Load the text field from a claim note."""
    writer = VaultWriter.get_writer()
    claim_path = writer.root / case_id / "claims" / f"{claim_id}.md"

    if not claim_path.exists():
        return f"[Claim {claim_id} not found]"

    try:
        text = claim_path.read_text(encoding="utf-8")
        fm, body = parse_note(text)
        return fm.get("text", body[:500])
    except Exception as exc:
        return f"[Could not read claim {claim_id}: {exc}]"


async def _review_flag(case_id: str, flag: dict) -> str:
    """
    Ask the LLM to review a single flag and return the verdict.

    Returns: "survived" | "explained_away"
    """
    flag_id = flag["flag_id"]
    conflict_type = flag.get("conflict_type", "unknown")
    description = flag.get("description", "")
    claim_a_id = flag.get("claim_a", "")
    claim_b_id = flag.get("claim_b", "")

    claim_a_text = _load_claim_text(case_id, claim_a_id)
    claim_b_text = _load_claim_text(case_id, claim_b_id)

    prompt = (
        "You are a skeptical forensic reviewer. Your job is to determine if a detected "
        "contradiction between two claims is a genuine irreconcilable conflict, or if "
        "there is a reasonable explanation that resolves it.\n\n"
        f"Conflict type: {conflict_type}\n"
        f"Conflict description: {description}\n\n"
        f"Claim A ({claim_a_id}): {claim_a_text[:1000]}\n\n"
        f"Claim B ({claim_b_id}): {claim_b_text[:1000]}\n\n"
        "Possible explanations to consider: timezone differences, rounding errors, "
        "different reference time periods, transcription errors, different definitions "
        "of the same term, hearsay vs. direct observation, etc.\n\n"
        "Give your verdict as a JSON object:\n"
        '{"verdict": "survived" | "explained_away", "reasoning": "brief explanation"}\n\n'
        "Return ONLY the JSON object."
    )

    messages = [
        {
            "role": "system",
            "content": "You are a forensic evaluation API. You must ONLY output a JSON object with keys 'verdict' ('survived' or 'explained_away') and 'reasoning'. Do not output any preamble or explanation outside the JSON.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        data = await llm_client.chat_json(messages, temperature=0.1)
        verdict = str(data.get("verdict", "survived")).lower().strip()
        if verdict not in ("survived", "explained_away"):
            verdict = "survived"
        reasoning = str(data.get("reasoning", ""))
        logger.info(
            "Flag %s verdict: %s — %s", flag_id, verdict, reasoning[:100]
        )
        return verdict
    except llm_client.LLMNotConfiguredError:
        logger.info("LLM not configured; defaulting flag %s to 'survived'", flag_id)
        return "survived"
    except Exception as exc:
        logger.warning("Skeptic review failed for flag %s: %s", flag_id, exc)
        # Check if the raw error contains hints from plain-text output
        err_msg = str(exc).lower()
        if "not a genuine" in err_msg or "explained" in err_msg or "reasonable explanation" in err_msg:
            return "explained_away"
        return "survived"  # Default to survived on error (conservative)


async def run_skeptic_review(case_id: str) -> dict[str, str]:
    """
    Review all pending flags in the case with the Skeptic Agent.

    1. Load all unreviewed flags.
    2. For each: ask LLM for verdict.
    3. Update flag via VaultWriter.set_flag_review().
    4. Return dict of {flag_id: verdict}.
    """
    flags = _load_flags(case_id)
    if not flags:
        logger.info("No pending flags to review for case %s", case_id)
        return {}

    writer = VaultWriter.get_writer()
    results: dict[str, str] = {}

    for flag in flags:
        flag_id = flag["flag_id"]
        verdict = await _review_flag(case_id, flag)

        try:
            writer.set_flag_review(case_id, flag_id, skeptic_verdict=verdict)
            results[flag_id] = verdict
        except Exception as exc:
            logger.warning("Failed to update flag %s verdict: %s", flag_id, exc)
            results[flag_id] = "error"

    return results
