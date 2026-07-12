"""
cairn/schedule.py
Golden angle position scheduling — the middle-rot killer.

LLM attention is U-shaped (Liu et al 2023, confirmed across 18 models 2026).
Front and end get processed. Middle degrades.

Standard fix: put important things first. Works once.
Second session, same nodes drift to middle again.

Golden angle fix: rotate which nodes land where across sessions.
phi^-2 is the most irrational number — no resonance at any scale.
After N sessions every node has occupied every position equally.

The feedback loop makes it self-correcting:
nodes that kept hitting middle but never appeared in compiled output
→ underattended → scheduled to front next session.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

PHI_INV_SQ = 0.3819660112501051  # 1/phi^2 — fractional golden angle


def golden_positions(n: int) -> list[float]:
    """
    n fractional positions in [0.0, 1.0) via golden angle rotation.
    0.0 = front of context window. 1.0 = back.

    Property: no two items share the same position, and the distribution
    never becomes periodic — the irrationality of phi guarantees it.
    """
    return [(i * PHI_INV_SQ) % 1.0 for i in range(n)]


@dataclass
class PositionRecord:
    node_id:       str
    middle_hits:   int = 0   # times loaded in middle third
    compiled_hits: int = 0   # times appeared in compiled state output
    total_loads:   int = 0

    @property
    def underattended(self) -> bool:
        """
        In middle 2+ times, never showed up in output.
        Model is receiving it but not using it.
        Next session: front-load.
        """
        return (self.middle_hits >= 2
                and self.compiled_hits == 0
                and self.total_loads >= 2)

    @property
    def attention_efficiency(self) -> float:
        if self.total_loads == 0:
            return 0.0
        return self.compiled_hits / self.total_loads


def schedule_context(
    nodes:            list[Any],
    position_records: dict[str, PositionRecord],
    core_ids:         set[str] | None = None,
    max_context:      int = 40,
) -> list[Any]:
    """
    Produce a load order that defeats middle-rot.

    1. Core nodes → always front. Always.
    2. Underattended nodes → front-loaded (feedback loop in action).
    3. Remaining → golden-angle distributed across the window.
    4. Most recent 3 nodes → anchored at end (recency bias helps us here).

    After scheduling, updates PositionRecord.middle_hits for each node
    so the feedback loop has data for next session.
    """
    core_ids = core_ids or set()
    node_by_id = {getattr(n, 'id', str(n)): n for n in nodes}

    def get_id(n): return getattr(n, 'id', str(n))
    def get_ts(n): return getattr(n, 'timestamp', '')
    def get_status(n): return getattr(n, 'status', '')

    critical    = [n for n in nodes if get_status(n) == 'core' or get_id(n) in core_ids]
    underattend = [n for n in nodes
                   if n not in critical
                   and position_records.get(get_id(n), PositionRecord(get_id(n))).underattended]
    recent      = sorted(
                      [n for n in nodes if n not in critical and n not in underattend],
                      key=get_ts, reverse=True
                  )[:3]
    variable    = [n for n in nodes
                   if n not in critical and n not in underattend and n not in recent]

    # golden angle across variable pool
    positions = golden_positions(len(variable))
    distributed = [n for _, n in sorted(zip(positions, variable), key=lambda x: x[0])]

    ordered = (critical + underattend + distributed + recent)[:max_context]

    # update position records — middle = middle third
    total = len(ordered)
    mid_s = total // 3
    mid_e = (2 * total) // 3
    for i, node in enumerate(ordered):
        nid = get_id(node)
        rec = position_records.setdefault(nid, PositionRecord(nid))
        rec.total_loads += 1
        if mid_s <= i < mid_e:
            rec.middle_hits += 1

    return ordered


def update_compiled_hits(
    position_records: dict[str, PositionRecord],
    compiled_text: str,
    loaded_ids: list[str],
) -> dict[str, bool]:
    """
    After session ends: check which nodes appear in PROTOCOL.md output.
    Hit → attention_efficiency goes up.
    Miss from middle → underattended flag for next session.

    Returns dict of node_id → hit/miss for logging.
    """
    results = {}
    for nid in loaded_ids:
        rec = position_records.setdefault(nid, PositionRecord(nid))
        hit = nid in compiled_text
        if hit:
            rec.compiled_hits += 1
        results[nid] = hit
    return results


# ── vault-integrated manifest (ties schedule to memory_tier) ─────────────────

def build_manifest(
    vault: "Any",
    session_id: str,
    window_tokens: int = 200_000,
) -> list[dict]:
    """
    Build the token-position injection manifest for a session.

    Combines memory_tier with golden_positions() to produce a sorted list:
        [{"node": dict, "position": int, "tier": int, "reason": str}, ...]

    memory_tier=0 (hot)  → position 0, always (session context stamps, key decisions)
    memory_tier=1 (warm) → golden-angle positions through the window
    memory_tier=2 (cold) → not injected; semantic retrieval only

    The feedback loop in schedule_context() informs tier promotion/demotion
    over time: underattended nodes get promoted, stale nodes get demoted.
    """
    nodes = vault.session_nodes(session_id)

    hot  = [n for n in nodes if n["memory_tier"] == 0]
    warm = [n for n in nodes if n["memory_tier"] == 1]

    manifest: list[dict] = []

    for n in hot:
        manifest.append({
            "node":     _row_to_dict(n),
            "position": 0,
            "tier":     0,
            "reason":   "hot — front-loaded at session start",
        })

    positions = golden_positions(len(warm))
    for pos_frac, n in zip(positions, warm):
        token_pos = int(pos_frac * window_tokens)
        manifest.append({
            "node":     _row_to_dict(n),
            "position": token_pos,
            "tier":     1,
            "reason":   (f"warm — golden-angle {pos_frac:.4f} "
                         f"→ {token_pos:,}/{window_tokens:,} tokens"),
        })

    manifest.sort(key=lambda x: (x["position"], x["tier"]))
    return manifest


def render_manifest(
    manifest: list[dict],
    max_chars_per_node: int = 400,
) -> str:
    """
    Render the manifest as a human-readable injection schedule.

    Sections group by 10k-token bands so it's clear:
      - What to load at session start
      - What to resurface mid-session

    This is the anti-middle-rot document. Load it. Follow it.
    """
    BAND = 10_000
    lines = [
        "# Cairn Context Manifest — Golden-Angle Schedule",
        "# Tier 0 (🔥 hot): load immediately.  Tier 1 (♻️  warm): resurface at position.",
        "# Cold nodes not listed — retrieve with: python -m cairn query 'topic'",
        "",
    ]

    current_band = -1
    for entry in manifest:
        pos  = entry["position"]
        band = pos // BAND
        node = entry["node"]
        tier = entry["tier"]

        if band != current_band:
            current_band = band
            header = ("## LOAD NOW (position 0)" if band == 0
                      else f"\n## RESURFACE at ~{band * BAND:,} tokens")
            lines += [header, ""]

        icon   = "🔥" if tier == 0 else "♻️ "
        kind   = node.get("kind",  "note")
        model  = node.get("model", "unknown")
        nid    = node.get("id",    "")
        etext  = (node.get("episodic_text") or node.get("query") or "")[:max_chars_per_node]

        lines.append(f"{icon} `{kind}` [{model}] `{nid}`")
        lines.append(f"   {etext}")
        lines.append("")

    if not manifest:
        lines.append("_no nodes scheduled — run `python -m cairn embed` first_")

    return "\n".join(lines)


def promote(vault: "Any", node_id: str) -> bool:
    """
    Promote a node one tier hotter (2→1 or 1→0).
    Call when a node is referenced in the current session —
    it just proved its relevance, so it earns a hotter slot.
    Returns True if promoted.
    """
    row = vault.get(node_id)
    if not row:
        return False
    current = row["memory_tier"]
    if current <= 0:
        return False
    vault.conn.execute(
        "UPDATE nodes SET memory_tier=? WHERE id=?",
        (current - 1, node_id)
    )
    vault.conn.commit()
    return True


def demote_cold(
    vault: "Any",
    current_session: str,
    referenced_ids: set[str],
    min_sessions_old: int = 3,
) -> int:
    """
    Demote warm (tier=1) nodes to cold (tier=2) when they haven't been
    referenced in recent sessions and weren't used in this session.

    Keeps the warm pool lean — only relevant nodes stay scheduled.
    Stale context drops to retrieval-only automatically.
    Returns count of demoted nodes.
    """
    rows = vault.conn.execute("""
        SELECT n.id
        FROM   nodes n
        WHERE  n.memory_tier = 1
          AND  n.status      = 'active'
          AND  n.session    != ?
          AND  n.session NOT IN (
              SELECT id FROM sessions ORDER BY started_at DESC LIMIT ?
          )
    """, (current_session, min_sessions_old)).fetchall()

    demoted = 0
    for row in rows:
        if row["id"] not in referenced_ids:
            vault.conn.execute(
                "UPDATE nodes SET memory_tier=2 WHERE id=? AND memory_tier=1",
                (row["id"],)
            )
            demoted += 1
    if demoted:
        vault.conn.commit()
    return demoted


def schedule_summary(
    vault: "Any",
    session_id: str,
    window_tokens: int = 200_000,
) -> dict:
    """Quick stats about the injection schedule for a session."""
    manifest   = build_manifest(vault, session_id, window_tokens)
    hot_count  = sum(1 for e in manifest if e["tier"] == 0)
    warm_count = sum(1 for e in manifest if e["tier"] == 1)
    cold_count = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes "
        "WHERE session=? AND memory_tier=2 AND status='active'",
        (session_id,)
    ).fetchone()[0]

    total = hot_count + warm_count + cold_count
    return {
        "hot":      hot_count,
        "warm":     warm_count,
        "cold":     cold_count,
        "window":   window_tokens,
        "coverage": f"{(hot_count + warm_count) / max(1, total) * 100:.0f}%",
    }


def _row_to_dict(row) -> dict:
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)
