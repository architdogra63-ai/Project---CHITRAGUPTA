"""
Contradiction Investigator — rule-based conflict detection between claims.

Loads all claims from the vault for a case and applies conflict rules to find
pairs of claims that contradict each other. Writes FlagInput objects to the vault.
"""

from __future__ import annotations

import itertools
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from ..vault.writer import VaultWriter, FlagInput, EntityRef
from ..vault.notes import parse_note

logger = logging.getLogger(__name__)

# Conflict types (mirrors writer.py CONFLICT_TYPES)
CONFLICT_TYPES = ("time", "place", "people", "amount", "count")


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp; return None if invalid.
    Always returns a timezone-aware UTC datetime to allow subtraction.
    """
    if not ts:
        return None
    try:
        # Handle Z suffix
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _parse_amount(text: str) -> float | None:
    """Extract a numeric amount from text (handles commas, currency symbols)."""
    # Remove currency symbols and commas
    cleaned = re.sub(r"[₹$€£,]", "", text)
    match = re.search(r"\b(\d+(?:\.\d+)?)\b", cleaned)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _load_claims(case_id: str) -> list[dict]:
    """Load all active claim frontmatters from the vault."""
    writer = VaultWriter.get_writer()
    case_path = writer.root / case_id
    claims_dir = case_path / "claims"

    if not claims_dir.exists():
        return []

    claims = []
    for md_file in sorted(claims_dir.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
            fm, _ = parse_note(text)
            if fm.get("type") == "claim" and fm.get("status") != "stale":
                claims.append(fm)
        except Exception as exc:
            logger.warning("Could not read claim %s: %s", md_file.name, exc)

    return claims


def _detect_conflicts(claims: list[dict], max_travel_minutes: int) -> list[dict]:
    """
    Apply all conflict rules to claim pairs.

    Returns a list of conflict dicts:
      {claim_a, claim_b, conflict_type, description, entities}
    """
    conflicts: list[dict] = []
    seen_pairs: set[frozenset] = set()

    for ca, cb in itertools.combinations(claims, 2):
        pair_key = frozenset([ca["claim_id"], cb["claim_id"]])
        if pair_key in seen_pairs:
            continue

        # --- Time conflict: same entity, overlapping timestamps, different places ---
        found = _check_time_conflict(ca, cb, max_travel_minutes)
        if found:
            conflicts.append({**found, "claim_a": ca["claim_id"], "claim_b": cb["claim_id"]})
            seen_pairs.add(pair_key)
            continue

        # --- Place conflict: same entity at two different places at same time ---
        found = _check_place_conflict(ca, cb)
        if found:
            conflicts.append({**found, "claim_a": ca["claim_id"], "claim_b": cb["claim_id"]})
            seen_pairs.add(pair_key)
            continue

        # --- Amount conflict: contradictory transaction amounts ---
        found = _check_amount_conflict(ca, cb)
        if found:
            conflicts.append({**found, "claim_a": ca["claim_id"], "claim_b": cb["claim_id"]})
            seen_pairs.add(pair_key)
            continue

        # --- Count conflict: contradictory counts ---
        found = _check_count_conflict(ca, cb)
        if found:
            conflicts.append({**found, "claim_a": ca["claim_id"], "claim_b": cb["claim_id"]})
            seen_pairs.add(pair_key)
            continue

        # --- People conflict: contradictory presence statements ---
        found = _check_people_conflict(ca, cb)
        if found:
            conflicts.append({**found, "claim_a": ca["claim_id"], "claim_b": cb["claim_id"]})
            seen_pairs.add(pair_key)

    return conflicts


def _shared_entities(ca: dict, cb: dict) -> list[str]:
    """Return entity names shared between two claims."""
    ea = set(ca.get("entities", []))
    eb = set(cb.get("entities", []))
    return list(ea & eb)


def _check_time_conflict(ca: dict, cb: dict, max_travel_minutes: int) -> dict | None:
    """
    Check: same entity, both have timestamps, both mention a place,
    and the timestamps are close enough that travel between the two places
    would be impossible.
    """
    shared = _shared_entities(ca, cb)
    if not shared:
        return None

    ta = _parse_iso(ca.get("when"))
    tb = _parse_iso(cb.get("when"))
    if ta is None or tb is None:
        return None

    # Only flag if timestamps are within max_travel_minutes of each other
    diff_minutes = abs((ta - tb).total_seconds()) / 60
    if diff_minutes > max_travel_minutes:
        return None

    # Both claims must mention different places
    text_a = ca.get("text", "").lower()
    text_b = cb.get("text", "").lower()

    # Simple heuristic: look for "at <place>" or "in <place>" in text
    place_pattern = re.compile(r"\b(?:at|in|near|from)\s+([A-Z][a-zA-Z\s]{2,30})")
    places_a = set(m.group(1).strip().lower() for m in place_pattern.finditer(ca.get("text", "")))
    places_b = set(m.group(1).strip().lower() for m in place_pattern.finditer(cb.get("text", "")))

    # Only flag if places differ
    if places_a and places_b and not (places_a & places_b):
        return {
            "conflict_type": "time",
            "description": (
                f"Entity {shared[0]} appears in two locations {diff_minutes:.0f} minutes apart "
                f"({ta.isoformat()} vs {tb.isoformat()}). "
                f"Claim A places them at: {', '.join(places_a)}. "
                f"Claim B places them at: {', '.join(places_b)}."
            ),
            "entities": shared,
        }
    return None


def _check_place_conflict(ca: dict, cb: dict) -> dict | None:
    """Check: same entity, same timestamp, different place claims."""
    shared = _shared_entities(ca, cb)
    if not shared:
        return None

    ta = _parse_iso(ca.get("when"))
    tb = _parse_iso(cb.get("when"))
    if ta is None or tb is None:
        return None

    # Same time (within 5 minutes)
    diff_minutes = abs((ta - tb).total_seconds()) / 60
    if diff_minutes > 5:
        return None

    # Look for different place mentions
    place_pattern = re.compile(r"\b(?:at|in|near)\s+([A-Z][a-zA-Z\s]{2,30})")
    places_a = set(m.group(1).strip().lower() for m in place_pattern.finditer(ca.get("text", "")))
    places_b = set(m.group(1).strip().lower() for m in place_pattern.finditer(cb.get("text", "")))

    if places_a and places_b and not (places_a & places_b):
        return {
            "conflict_type": "place",
            "description": (
                f"Entity {shared[0]} is claimed to be at two different places at the same time "
                f"(~{ta.isoformat()}). "
                f"Claim A: {', '.join(places_a)}. Claim B: {', '.join(places_b)}."
            ),
            "entities": shared,
        }
    return None


def _check_amount_conflict(ca: dict, cb: dict) -> dict | None:
    """Check: both claims are transactions referencing the same entity with different amounts."""
    if ca.get("claim_type") != "transaction" or cb.get("claim_type") != "transaction":
        return None

    shared = _shared_entities(ca, cb)
    if not shared:
        return None

    amt_a = _parse_amount(ca.get("text", ""))
    amt_b = _parse_amount(cb.get("text", ""))

    if amt_a is None or amt_b is None:
        return None

    # Flag if amounts differ by more than 1% (not just rounding)
    if amt_a == 0 or amt_b == 0:
        return None
    diff_pct = abs(amt_a - amt_b) / max(amt_a, amt_b) * 100
    if diff_pct < 1.0:
        return None

    return {
        "conflict_type": "amount",
        "description": (
            f"Conflicting transaction amounts for entity {shared[0]}: "
            f"Claim A states {amt_a:,.2f}, Claim B states {amt_b:,.2f} "
            f"(difference: {diff_pct:.1f}%)."
        ),
        "entities": shared,
    }


def _check_count_conflict(ca: dict, cb: dict) -> dict | None:
    """Check: same entity, conflicting numeric counts about the same set."""
    shared = _shared_entities(ca, cb)
    if not shared:
        return None

    # Look for count phrases: "X people", "X items", "X units", etc.
    count_pattern = re.compile(r"\b(\d+)\s+(?:people|persons|individuals|items|units|members|witnesses)\b", re.IGNORECASE)
    counts_a = [int(m.group(1)) for m in count_pattern.finditer(ca.get("text", ""))]
    counts_b = [int(m.group(1)) for m in count_pattern.finditer(cb.get("text", ""))]

    if not counts_a or not counts_b:
        return None

    # Only flag if both have a single count and they differ
    if len(counts_a) == 1 and len(counts_b) == 1 and counts_a[0] != counts_b[0]:
        return {
            "conflict_type": "count",
            "description": (
                f"Conflicting counts related to entity {shared[0]}: "
                f"Claim A states {counts_a[0]}, Claim B states {counts_b[0]}."
            ),
            "entities": shared,
        }
    return None


def _check_people_conflict(ca: dict, cb: dict) -> dict | None:
    """Check: contradictory statements about presence (was/was not present)."""
    text_a = ca.get("text", "").lower()
    text_b = cb.get("text", "").lower()

    # Detect negation keywords
    negation = re.compile(r"\b(?:not|wasn't|weren't|never|absent|denied|no one|nobody)\b")
    presence = re.compile(r"\b(?:was|were|present|attended|seen|witnessed|at the scene)\b")

    a_has_presence = bool(presence.search(text_a))
    b_has_presence = bool(presence.search(text_b))
    a_has_negation = bool(negation.search(text_a))
    b_has_negation = bool(negation.search(text_b))

    shared = _shared_entities(ca, cb)
    if not shared:
        return None

    # One affirms presence, other denies
    if (a_has_presence and not a_has_negation) and (b_has_presence and b_has_negation):
        return {
            "conflict_type": "people",
            "description": (
                f"Contradictory presence statement for entity {shared[0]}: "
                f"Claim A affirms presence, Claim B denies it."
            ),
            "entities": shared,
        }
    if (b_has_presence and not b_has_negation) and (a_has_presence and a_has_negation):
        return {
            "conflict_type": "people",
            "description": (
                f"Contradictory presence statement for entity {shared[0]}: "
                f"Claim B affirms presence, Claim A denies it."
            ),
            "entities": shared,
        }
    return None


async def run_contradiction_check(case_id: str, run_id: str) -> list[str]:
    """
    Run contradiction detection for all claims in the given case.

    1. Load all active claims from vault.
    2. Apply conflict rules.
    3. Write flags to vault.
    4. Return list of flag IDs created.
    """
    settings = get_settings()
    max_travel = settings.investigation_max_travel_minutes

    claims = _load_claims(case_id)
    if len(claims) < 2:
        logger.info("Not enough claims for contradiction check in case %s", case_id)
        return []

    conflicts = _detect_conflicts(claims, max_travel)
    if not conflicts:
        logger.info("No conflicts found in case %s", case_id)
        return []

    writer = VaultWriter.get_writer()
    flag_ids: list[str] = []

    for i, conflict in enumerate(conflicts, start=1):
        # Generate FLAG ID using run_id prefix to avoid collisions
        # Use a hash of the pair for uniqueness
        pair_hash = abs(hash(frozenset([conflict["claim_a"], conflict["claim_b"]]))) % 9000 + 1000
        flag_id = f"FLAG-{pair_hash:04d}"

        entity_refs = tuple(
            EntityRef("person", e) for e in conflict.get("entities", [])
            # Note: entities here are entity note names (e.g. PERSON-rahul)
            # We pass them as person type; the vault writer will normalize
        )

        try:
            flag_input = FlagInput(
                flag_id=flag_id,
                conflict_type=conflict["conflict_type"],
                claim_a=conflict["claim_a"],
                claim_b=conflict["claim_b"],
                description=conflict["description"],
                entities=entity_refs,
                skeptic_verdict=None,
                reviewer_decision=None,
            )
            writer.write_flag(case_id, flag_input)
            flag_ids.append(flag_id)
            logger.info("Written flag %s (%s) for case %s", flag_id, conflict["conflict_type"], case_id)
        except Exception as exc:
            logger.warning("Failed to write flag %s: %s", flag_id, exc)

    return flag_ids
