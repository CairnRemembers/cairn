"""
The audit organ — Cairn's nightly immune system (Lane D).

Every weakness found in the vault's first month was caught by the OWNER
noticing: the register hiding his own planted notes, the attention channel
reciting one py_compile line 967 times, an import blob wearing a topic
label. This module is the succession plan for his attention — zero-token
structural checks that run at the end of every sleep and put what they
find on the Desk, so the system catches the next one before he does.

Pure stdlib + SQL against the vault. It never mutates memory content; its
only write is ONE warning node, and only when findings exist AND differ
from the previous audit's (append-only holds, no nightly exhaust).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def audit(vault) -> list[str]:
    """Run every structural check; return human-readable findings.
    Each check is independently guarded — one broken sensor never blinds
    the rest of the organ."""
    findings: list[str] = []
    c = vault.conn

    # 1. Stale open items — parked-never-dropped means SURFACED, not silent.
    try:
        rows = c.execute("""
            SELECT id, substr(COALESCE(gist, query), 1, 60) g, timestamp
            FROM nodes WHERE status='active' AND kind='open_item'
              AND session NOT LIKE 'import-%' AND timestamp < ?
            ORDER BY timestamp ASC
        """, (_iso_days_ago(21),)).fetchall()
        if rows:
            oldest = ", ".join(f"'{r['g']}' ({r['timestamp'][:10]})"
                               for r in rows[:3])
            findings.append(
                f"{len(rows)} open item(s) untouched >21 days — oldest: {oldest}")
    except Exception:
        findings.append("audit sensor error: stale-open-items check failed")

    # 2. Attribution honesty — Lane C regression watch: fresh nodes still
    #    wearing anonymous/mcp-generic model labels.
    try:
        n = c.execute("""
            SELECT COUNT(*) FROM nodes WHERE status='active'
              AND timestamp >= ? AND session NOT LIKE 'import-%'
              AND COALESCE(model, '') IN ('', 'unknown', 'mcp-client')
        """, (_iso_days_ago(7),)).fetchone()[0]
        if n > 25:
            findings.append(
                f"{n} nodes this week carry anonymous model labels "
                f"(''/unknown/mcp-client) — attribution drifting (Lane C)")
    except Exception:
        findings.append("audit sensor error: attribution check failed")

    # 3. Suspicious dates — future stamps or pre-history creep in via
    #    imports and clock bugs; both poison every time-ordered surface.
    try:
        n = c.execute("""
            SELECT COUNT(*) FROM nodes WHERE status != 'void'
              AND (timestamp > ? OR timestamp < '2020-01-01')
        """, (_iso_days_ago(-1),)).fetchone()[0]
        if n:
            findings.append(f"{n} node(s) with impossible timestamps "
                            f"(future or pre-2020)")
    except Exception:
        findings.append("audit sensor error: date check failed")

    # 4. Exposure hoarding — the salience-decay regression guard. If any
    #    single node takes >60 heartbeat airings in a week, the attention
    #    economy is hoarding again ('All 6 files pass py_compile', 967).
    try:
        rows = c.execute("""
            SELECT node_id, COUNT(*) n FROM attention_ledger
            WHERE shown_at >= ? GROUP BY node_id
            HAVING n > 60 ORDER BY n DESC LIMIT 3
        """, (_iso_days_ago(7),)).fetchall()
        if rows:
            worst = c.execute(
                "SELECT substr(COALESCE(gist, query),1,50) FROM nodes WHERE id=?",
                (rows[0]["node_id"],)).fetchone()
            findings.append(
                f"attention hoarding: {len(rows)} node(s) >60 airings this "
                f"week — worst {rows[0]['n']}x: '{worst[0] if worst else '?'}'")
    except Exception:
        findings.append("audit sensor error: exposure check failed")

    # 5. Retrieval blind spots — meaning-kind nodes the nightly embed keeps
    #    missing are invisible to fetch/search: silent memory loss.
    try:
        from cairn.book import MEANING_KINDS
        ph = ",".join(f"'{k}'" for k in MEANING_KINDS)
        n = c.execute(f"""
            SELECT COUNT(*) FROM nodes WHERE status='active'
              AND kind IN ({ph}) AND embedding IS NULL AND timestamp < ?
        """, (_iso_days_ago(2),)).fetchone()[0]
        if n:
            findings.append(f"{n} meaning node(s) older than 2 days still "
                            f"unembedded — invisible to retrieval")
    except Exception:
        findings.append("audit sensor error: embed-coverage check failed")

    # 6. Declared-project ghosts — a projects.json row whose tags match zero
    #    active nodes is a registry entry pointing at nothing.
    try:
        import json as _json
        from pathlib import Path
        pf = Path.home() / ".cairn" / "projects.json"
        if pf.exists():
            data = _json.loads(pf.read_text(encoding="utf-8"))
            for ptag, pv in (data.items() if isinstance(data, dict) else []):
                tags = [ptag] + (list(pv[2]) if len(pv) > 2
                                 and isinstance(pv[2], list) else [])
                hit = c.execute(
                    "SELECT 1 FROM nodes WHERE status='active' AND ("
                    + " OR ".join("tags LIKE ?" for _ in tags)
                    + ") LIMIT 1",
                    tuple(f'%"{t}"%' for t in tags)).fetchone()
                if not hit:
                    findings.append(
                        f"declared project '{ptag}' matches zero active "
                        f"nodes — ghost row in projects.json")
    except Exception:
        findings.append("audit sensor error: project-ghost check failed")

    # 7. Voided-but-scheduled — a voided node keeping a hot/warm tier is
    #    inconsistent state, not a live leak (every injection path filters
    #    status='active'); flag it so the inconsistency can't silently
    #    become a leak when a future query forgets the status clause.
    try:
        n = c.execute("""
            SELECT COUNT(*) FROM nodes
            WHERE status='void' AND memory_tier <= 1
        """).fetchone()[0]
        if n:
            findings.append(f"{n} voided node(s) still carry hot/warm tier — "
                            f"harmless today (injection filters status), but "
                            f"inconsistent; tier should retire with the node")
    except Exception:
        findings.append("audit sensor error: void-schedule check failed")

    return findings


def write_report(vault, findings: list[str]) -> str | None:
    """Append ONE warning node carrying the findings — only when there ARE
    findings and they differ from the previous audit's (no nightly exhaust
    restating the same problems; append-only untouched). Tagged
    'cairn-audit' — deliberately NOT a PROCESS_TAGS word: these findings
    are the system talking to the OWNER, and the register must show them.
    Returns the node id, or None when nothing was written."""
    if not findings:
        return None
    body = "CAIRN AUDIT — " + "; ".join(findings)
    try:
        prev = vault.conn.execute("""
            SELECT output_preview FROM nodes
            WHERE tags LIKE '%"cairn-audit"%' AND status='active'
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        if prev and (prev["output_preview"] or "").split(" (as of", 1)[0] == body:
            return None   # same findings as last night — say it once
    except Exception:
        pass
    from cairn.vault import MicroNode
    stamp = datetime.now(timezone.utc).isoformat()[:16].replace("T", " ")
    node = vault.write(MicroNode(
        session="cairn-audit",
        kind="warning",
        query=body[:500],
        output_preview=f"{body} (as of {stamp} UTC)",
        model="cairn-audit",
        tags=["cairn-audit"],
    ))
    return node.id
