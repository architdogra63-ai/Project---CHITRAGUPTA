"""
Network Agent — graph analysis of entities extracted from vault data.

Builds a graph from claim co-occurrences, computes centrality metrics,
identifies clusters, and ranks entities by suspiciousness.
Returns JSON compatible with standard graph visualization formats.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from ..vault.writer import VaultWriter
from ..vault.notes import parse_note

logger = logging.getLogger(__name__)


def _load_vault_data(case_id: str) -> tuple[list[dict], list[dict]]:
    """Load all claims and entity notes from the vault for a case."""
    writer = VaultWriter.get_writer()
    case_path = writer.root / case_id

    claims: list[dict] = []
    entities: list[dict] = []

    # Load claims
    claims_dir = case_path / "claims"
    if claims_dir.exists():
        for md_file in sorted(claims_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_note(text)
                if fm.get("type") == "claim":
                    claims.append(fm)
            except Exception:
                pass

    # Load entities
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
                        entities.append(fm)
                except Exception:
                    pass

    return claims, entities


def _build_graph(claims: list[dict], entities: list[dict]) -> tuple[dict, list]:
    """
    Build a co-occurrence graph from vault data.

    Nodes = entities (people, accounts, phones, places, devices)
    Edges = co-occurrence in a claim (both entities appear in same claim)

    Returns:
        nodes: dict of {node_id: {id, label, type, claim_count, evidence_count}}
        edges: list of {source, target, weight, claims}
    """
    # Build entity metadata index
    entity_meta: dict[str, dict] = {}
    for ent in entities:
        key = ent.get("key", "")
        if key:
            entity_meta[key] = {
                "id": key,
                "label": ent.get("name", key),
                "type": ent.get("entity_type", "unknown"),
                "claim_count": len(ent.get("claims", [])),
                "evidence_count": len(ent.get("evidence", [])),
            }

    # Build co-occurrence edges from claims
    edge_weights: dict[tuple[str, str], int] = defaultdict(int)
    edge_claims: dict[tuple[str, str], list[str]] = defaultdict(list)

    for claim in claims:
        claim_entities = claim.get("entities", [])
        claim_id = claim.get("claim_id", "")

        # Ensure all entities appear as nodes
        for ent_key in claim_entities:
            if ent_key not in entity_meta:
                entity_meta[ent_key] = {
                    "id": ent_key,
                    "label": ent_key,
                    "type": "unknown",
                    "claim_count": 1,
                    "evidence_count": 0,
                }
            else:
                entity_meta[ent_key]["claim_count"] = entity_meta[ent_key].get("claim_count", 0) + 1

        # Add edges for all pairs
        for i, e1 in enumerate(claim_entities):
            for e2 in claim_entities[i + 1:]:
                if e1 == e2:
                    continue
                key = (min(e1, e2), max(e1, e2))
                edge_weights[key] += 1
                if claim_id:
                    edge_claims[key].append(claim_id)

    nodes = list(entity_meta.values())
    edges = [
        {
            "source": k[0],
            "target": k[1],
            "weight": w,
            "claims": edge_claims[k],
        }
        for k, w in edge_weights.items()
    ]

    return entity_meta, edges


def _compute_degree_centrality(entity_meta: dict, edges: list) -> dict[str, int]:
    """Compute degree (number of direct connections) for each node."""
    degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
    return dict(degree)


def _compute_suspiciousness(
    entity_meta: dict,
    degree: dict[str, int],
    edges: list,
) -> dict[str, float]:
    """
    Rank entities by suspiciousness score (0.0–1.0).

    Factors:
    - Degree centrality (normalized): 40%
    - Claim count (normalized): 40%
    - Evidence count (normalized): 20%
    """
    if not entity_meta:
        return {}

    max_degree = max(degree.values(), default=1) or 1
    max_claims = max(e.get("claim_count", 0) for e in entity_meta.values()) or 1
    max_evidence = max(e.get("evidence_count", 0) for e in entity_meta.values()) or 1

    scores: dict[str, float] = {}
    for node_id, meta in entity_meta.items():
        d_score = degree.get(node_id, 0) / max_degree
        c_score = meta.get("claim_count", 0) / max_claims
        e_score = meta.get("evidence_count", 0) / max_evidence
        scores[node_id] = round(0.4 * d_score + 0.4 * c_score + 0.2 * e_score, 3)

    return scores


def _find_clusters(entity_meta: dict, edges: list) -> dict[str, int]:
    """
    Simple union-find clustering.
    Returns {node_id: cluster_id}.
    """
    parent: dict[str, str] = {k: k for k in entity_meta}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for edge in edges:
        s, t = edge["source"], edge["target"]
        if s in parent and t in parent:
            union(s, t)

    # Assign numeric cluster IDs
    root_to_cluster: dict[str, int] = {}
    cluster_counter = 0
    result: dict[str, int] = {}
    for node_id in entity_meta:
        root = find(node_id)
        if root not in root_to_cluster:
            root_to_cluster[root] = cluster_counter
            cluster_counter += 1
        result[node_id] = root_to_cluster[root]

    return result


def _generate_leads(
    entity_meta: dict,
    suspiciousness: dict[str, float],
    degree: dict[str, int],
    top_n: int = 5,
) -> list[dict]:
    """Generate investigation lead suggestions for the top-N most suspicious entities."""
    ranked = sorted(suspiciousness.items(), key=lambda x: x[1], reverse=True)[:top_n]
    leads = []
    for node_id, score in ranked:
        meta = entity_meta.get(node_id, {})
        leads.append({
            "entity_id": node_id,
            "entity_label": meta.get("label", node_id),
            "entity_type": meta.get("type", "unknown"),
            "suspiciousness_score": score,
            "degree": degree.get(node_id, 0),
            "claim_count": meta.get("claim_count", 0),
            "suggestion": _lead_suggestion(meta, score, degree.get(node_id, 0)),
        })
    return leads


def _lead_suggestion(meta: dict, score: float, degree: int) -> str:
    """Generate a human-readable lead suggestion based on entity type and score."""
    etype = meta.get("type", "unknown")
    label = meta.get("label", "this entity")

    if etype == "person":
        return (
            f"Investigate {label} further — appears in {meta.get('claim_count', 0)} claims "
            f"with {degree} connected entities (score: {score:.2f}). "
            "Consider interviewing or cross-referencing financial records."
        )
    elif etype == "account":
        return (
            f"Trace account {label} — linked to {meta.get('claim_count', 0)} claims. "
            "Request bank statements and transaction history."
        )
    elif etype == "phone":
        return (
            f"Pull CDR for {label} — appears in {meta.get('claim_count', 0)} claims. "
            "Check call logs, tower dumps, and contact network."
        )
    elif etype == "place":
        return (
            f"Review {label} — location mentioned in {meta.get('claim_count', 0)} claims. "
            "Obtain CCTV footage and witness statements from this location."
        )
    elif etype == "device":
        return (
            f"Forensically examine device {label} — linked to {meta.get('claim_count', 0)} claims. "
            "Extract call logs, messages, and app data."
        )
    else:
        return f"Investigate {label} (score: {score:.2f}) — linked to {meta.get('claim_count', 0)} claims."


async def run_network_analysis(case_id: str) -> dict:
    """
    Build the entity network graph for a case.

    Returns a dict with:
      - nodes: list of node objects
      - edges: list of edge objects
      - clusters: dict of {node_id: cluster_id}
      - suspiciousness: dict of {node_id: score}
      - investigation_leads: list of lead suggestions
      - summary: overall summary stats
    """
    claims, entities = _load_vault_data(case_id)

    entity_meta, edges = _build_graph(claims, entities)
    degree = _compute_degree_centrality(entity_meta, edges)
    suspiciousness = _compute_suspiciousness(entity_meta, degree, edges)
    clusters = _find_clusters(entity_meta, edges)
    leads = _generate_leads(entity_meta, suspiciousness, degree)

    # Build final node list with all computed properties
    nodes = []
    for node_id, meta in entity_meta.items():
        nodes.append({
            **meta,
            "degree": degree.get(node_id, 0),
            "suspiciousness": suspiciousness.get(node_id, 0.0),
            "cluster": clusters.get(node_id, 0),
        })

    # Sort nodes by suspiciousness descending
    nodes.sort(key=lambda n: n["suspiciousness"], reverse=True)

    num_clusters = len(set(clusters.values())) if clusters else 0

    return {
        "case_id": case_id,
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "suspiciousness": suspiciousness,
        "investigation_leads": leads,
        "summary": {
            "total_entities": len(nodes),
            "total_edges": len(edges),
            "total_clusters": num_clusters,
            "total_claims": len(claims),
            "top_entity": nodes[0]["label"] if nodes else None,
        },
    }
