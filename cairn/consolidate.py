"""
cairn/consolidate.py — the REM sleep pass.

Complementary Learning Systems theory (McClelland, McNaughton & O'Reilly 1995):
the hippocampus captures episodes fast and verbatim; sleep replay consolidates
them into neocortical semantic knowledge — fewer, denser, longer-lived traces.

Cairn's mapping:
  hippocampus  = the vault's episodic nodes (append-only, high fidelity)
  sleep replay = this module, run between sessions
  neocortex    = synthesized insight/procedure nodes

The pass, in order:
  1. CLUSTER     — embedded meaning-nodes with cosine >= threshold,
                   spanning >= 2 sessions (single-session clusters are
                   just repetition, not generalization)
  2. SYNTHESIZE  — one insight node per cluster: the medoid's text as
                   representative + member gists as evidence. Zero-token,
                   deterministic, local. No LLM call.
  3. CRYSTALLIZE — clusters dominated by warning/resolved/blocker kinds
                   describe a repeated METHOD → kind=procedure instead.
                   Procedures are session-independent (basal ganglia):
                   recency decay never applies to "how to run the tests".
  4. DECAY       — consolidated members get FSRS stability boost (the
                   memory was strengthened by integration), then warm
                   members demote to cold: the insight now carries the
                   meaning at warm tier; the episodes remain retrievable.

Append-only invariant preserved: members are never modified or deleted.
The new node's tags record the member ids — full lineage, auditable.

Usage:
  python -m cairn consolidate            # run the pass
  python -m cairn consolidate --dry-run  # show clusters without writing
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from typing import Optional

from cairn.vault import Vault, MicroNode

# Kinds that carry MEANING — eligible for consolidation.
# tool_call/interrupt/conversation_turn are process noise at this layer.
MEANING_KINDS = (
    "decision", "warning", "resolved", "open_item",
    "insight", "hypothesis", "blocker", "question", "idea",
)

# Kinds that describe method/failure-recovery — clusters dominated by these
# crystallize into procedures rather than insights.
METHOD_KINDS = {"warning", "resolved", "blocker"}

# Calibrated against all-MiniLM-L6-v2's actual similarity distribution on the
# live vault (2026-06-10): near-duplicates land 0.81-0.88, genuine conceptual
# siblings 0.63-0.74, unrelated < 0.55. 0.65 catches concept clusters without
# merging unrelated topics. If you swap embedding models, recalibrate.
COSINE_THRESHOLD = 0.65   # cluster edge threshold
MIN_CLUSTER      = 3      # minimum members per cluster
MIN_SESSIONS     = 2      # members must span >= 2 sessions (generalization test)
STABILITY_BOOST  = 1.3    # FSRS growth for consolidated members


def _cosine(a_blob: bytes, b_blob: bytes, dim: int) -> float:
    a = struct.unpack(f"{dim}f", a_blob)
    b = struct.unpack(f"{dim}f", b_blob)
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(y * y for y in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _already_absorbed(vault: Vault) -> set[str]:
    """
    Node ids already absorbed into a prior consolidation.
    Lineage lives in the consolidated node's tags as 'member:<id>' entries —
    tags are immutable, so this is the durable record.
    """
    absorbed: set[str] = set()
    rows = vault.conn.execute(
        "SELECT tags FROM nodes WHERE kind IN ('insight','procedure') "
        "AND status='active' AND tags LIKE '%consolidated%'"
    ).fetchall()
    for r in rows:
        try:
            for tag in json.loads(r["tags"] or "[]"):
                if isinstance(tag, str) and tag.startswith("member:"):
                    absorbed.add(tag.split(":", 1)[1])
        except Exception:
            continue
    return absorbed


def _clusters(rows: list, dim: int, threshold: float) -> list[list]:
    """
    Union-find clustering on the cosine graph. O(n^2) pairwise — fine for
    the meaning-node population (hundreds, not tens of thousands).
    """
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if _cosine(rows[i]["embedding"], rows[j]["embedding"], dim) >= threshold:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rows[i])
    return [g for g in groups.values() if len(g) >= MIN_CLUSTER]


def _medoid(cluster: list, dim: int):
    """The member with the highest mean similarity to all others — the
    most representative episode, used as the cluster's voice."""
    best, best_score = cluster[0], -1.0
    for a in cluster:
        score = sum(
            _cosine(a["embedding"], b["embedding"], dim)
            for b in cluster if b["id"] != a["id"]
        ) / max(1, len(cluster) - 1)
        if score > best_score:
            best, best_score = a, score
    return best


def consolidate(
    vault: Optional[Vault] = None,
    threshold: float = COSINE_THRESHOLD,
    min_cluster: int = MIN_CLUSTER,
    dry_run: bool = False,
) -> dict:
    """
    Run the full REM pass. Returns a report dict.
    Embeds pending nodes first if an embedder is available — clustering
    needs vectors.
    """
    v = vault or Vault()

    # Ensure embeddings exist (skip silently if sentence-transformers absent)
    try:
        v.embed_pending()
    except Exception:
        pass

    absorbed = _already_absorbed(v)

    placeholders = ",".join("?" for _ in MEANING_KINDS)
    rows = [
        r for r in v.conn.execute(
            f"""SELECT * FROM nodes
                WHERE status='active' AND embedding IS NOT NULL
                  AND kind IN ({placeholders})""",
            MEANING_KINDS,
        ).fetchall()
        if r["id"] not in absorbed
    ]

    report = {
        "candidates": len(rows), "clusters": 0,
        "insights": 0, "procedures": 0,
        "members_boosted": 0, "members_demoted": 0,
        "details": [],
    }
    if len(rows) < min_cluster:
        return report

    dim = len(rows[0]["embedding"]) // 4  # float32 blob → dim

    for cluster in _clusters(rows, dim, threshold):
        sessions = {r["session"] for r in cluster}
        if len(sessions) < MIN_SESSIONS:
            continue  # repetition within one session, not generalization

        report["clusters"] += 1
        med = _medoid(cluster, dim)

        method_share = sum(1 for r in cluster if r["kind"] in METHOD_KINDS) / len(cluster)
        kind = "procedure" if method_share >= 0.5 else "insight"

        member_ids   = [r["id"] for r in cluster]
        member_gists = []
        for r in cluster:
            g = ""
            try:
                g = r["gist"] or ""
            except (KeyError, IndexError):
                pass
            member_gists.append(f"- [{r['id'][:8]}] {g or (r['query'] or '')[:80]}")

        query = (f"[consolidated x{len(cluster)} across {len(sessions)} sessions] "
                 f"{(med['query'] or '')[:300]}")
        preview = "\n".join(member_gists)[:1500]

        detail = {
            "kind": kind, "members": len(cluster),
            "sessions": len(sessions), "medoid": med["id"],
            "summary": (med["query"] or "")[:100],
        }
        report["details"].append(detail)

        if dry_run:
            continue

        new_node = v.write(MicroNode(
            session        = f"consolidation-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            kind           = kind,
            query          = query,
            output_preview = preview,
            parent         = med["id"],            # chain to the representative episode
            memory_tier    = 1,                    # warm — eligible for injection
            model          = "cairn-consolidate",  # attributed to the pass itself
            agent_role     = "curator",
            tags           = ["consolidated"] + [f"member:{mid}" for mid in member_ids],
        ))
        report["insights" if kind == "insight" else "procedures"] += 1

        # DECAY phase: members strengthened (FSRS boost), warm members → cold.
        # The insight now carries the meaning at warm; episodes stay retrievable.
        for r in cluster:
            new_stab = min(365.0, (r["stability_days"] or 1.0) * STABILITY_BOOST)
            if v.set_stability(r["id"], new_stab,
                               last_injected=r["last_injected"]):
                report["members_boosted"] += 1
            if r["memory_tier"] == 1:
                if v.set_tier(r["id"], 2):
                    report["members_demoted"] += 1

        detail["new_node"] = new_node.id

    # Embed the new synthesis nodes so they're searchable immediately
    if not dry_run and (report["insights"] or report["procedures"]):
        try:
            v.embed_pending()
        except Exception:
            pass

    return report
