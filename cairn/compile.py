"""
cairn/compile.py
PROTOCOL.md generator — the session compiled state.

Not a summary. Not a compression. A correlation.
Compare what was loaded against what appeared in output.
Produce a human-readable map the next session loads FIRST.

v2: adds conversation context + session delta.
The 60% that was missing before — user intent + reasoning turns +
what changed since last time.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vault import Vault


def compile_session(vault: "Vault", session_id: str, output_dir: Path) -> Path:
    nodes = vault.session_nodes(session_id)

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "PROTOCOL.md"

    if not nodes:
        out.write_text(
            f"# PROTOCOL.md\nsession: {session_id}\nstatus: empty — no nodes recorded\n",
            encoding="utf-8",
        )
        return out

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── classify nodes ────────────────────────────────────────────────────────
    tool_calls    = [r for r in nodes if r["kind"] == "tool_call" and r["tool"]]
    turns         = [r for r in nodes if r["kind"] == "conversation_turn"]
    stamps        = [r for r in nodes if r["kind"] == "context_stamp"]
    decisions     = [r for r in nodes if r["kind"] == "decision"]
    questions     = [r for r in nodes if r["kind"] in ("question", "open_item")]
    blockers      = [r for r in nodes if r["kind"] == "blocker"]
    resolved      = [r for r in nodes if r["kind"] == "resolved"]
    insights      = [r for r in nodes if r["kind"] in ("insight", "hypothesis")]
    warnings_n    = [r for r in nodes if r["kind"] == "warning"]

    all_parents    = {r["parent"] for r in nodes if r["parent"]}
    referenced_ids = {r["id"]     for r in nodes if r["id"] in all_parents}
    active_threads = [r for r in nodes if r["id"] in referenced_ids]

    struggle  = [r for r in nodes
                 if (r["latency_ms"] or 0) > 800
                 or (r["result_count"] is not None and r["result_count"] <= 1)]
    flagged   = [r for r in nodes if r["flagged"]]
    voided    = [r for r in nodes if r["status"] == "void"]

    # ── session delta: what changed vs previous compile ───────────────────────
    protocols_root = Path.home() / ".cairn" / "protocols"
    prev_summary   = _load_prev_summary(session_id, protocols_root)

    # ── multi-agent attribution ───────────────────────────────────────────────
    models_seen: dict[str, dict] = {}
    for r in nodes:
        m = r["model"] if r["model"] and r["model"] != "unknown" else None
        if m:
            if m not in models_seen:
                models_seen[m] = {
                    "nodes": 0, "struggles": 0, "flags": 0,
                    "role": r["agent_role"], "speaker": r["speaker"],
                }
            models_seen[m]["nodes"] += 1
            if (r["latency_ms"] or 0) > 800 or (r["result_count"] or 99) <= 1:
                models_seen[m]["struggles"] += 1
            if r["flagged"]:
                models_seen[m]["flags"] += 1

    n_total    = len(nodes)
    n_turns    = len(turns)
    n_stamps   = len(stamps)
    n_threads  = len(active_threads)
    n_struggle = len(struggle)
    n_flagged  = len(flagged)
    n_decisions= len(decisions)
    n_questions= len(questions)
    n_open_q   = len([q for q in questions if q["status"] == "active"])
    open_items = [r for r in nodes if r["kind"] == "open_item" and r["status"] == "active"]

    lines = [
        "# PROTOCOL.md",
        f"session:  {session_id}",
        f"compiled: {ts}",
        f"nodes:    {n_total} total | {n_turns} turns | {n_stamps} stamps | "
        f"{n_threads} threads | {n_struggle} struggles | {n_flagged} flagged",
        f"intent:   {n_decisions} decisions | {n_questions} questions ({n_open_q} open)",
        "",
        "---",
        "",
        "## How to use this file",
        "> Load at position 0 before any other context.",
        "> Context stamps first — they are memory_tier=0 (always hot).",
        "> Follow conversation thread to understand the WHY.",
        "> Navigate traversal path for the HOW.",
        "> Active threads mark the important chains.",
        "> Hard points warn where to be careful next session.",
        "",
    ]

    # ── SECTION 1: Context stamps (the WHY — why this session exists) ─────────
    lines += [
        "---",
        "",
        "## Context stamps",
        "_Why this session started. What carries forward. Written explicitly by the agent._",
        "_These are memory_tier=0 (hot) — always the first context loaded._",
        "",
    ]
    if stamps:
        for r in stamps:
            text = (r["output_preview"] or r["query"] or "")[:400].replace("\n", " | ")
            ts_node = r["timestamp"][:16] if r["timestamp"] else ""
            tag = " [PreCompact]" if "pre-compact" in (r["tags"] or "") else ""
            lines.append(f"- **{ts_node}**{tag} — {text}  `{r['id']}`")
    else:
        lines.append("_no context stamps this session — run `python -m cairn orient` at start_")

    # ── SECTION 2: Conversation (user intent + agent reasoning) ──────────────
    if turns:
        lines += [
            "",
            "---",
            "",
            "## Conversation",
            "_What the user said and what the agent concluded — the intent layer._",
            "_Both sides embedded. Query: 'what did the user say about X?' across sessions._",
            "",
        ]
        user_turns  = [t for t in turns if t["speaker"] == "user"]
        agent_turns = [t for t in turns if t["speaker"] != "user"]

        if user_turns:
            lines.append("### User intent")
            for r in user_turns[-10:]:  # last 10 user turns
                text = (r["query"] or "")[:200].replace("\n", " ")
                lines.append(f"- {text}  `{r['id']}`")

        if agent_turns:
            lines.append("")
            lines.append("### Agent conclusions")
            for r in agent_turns[-8:]:  # last 8 agent turns
                text = (r["query"] or "")[:200].replace("\n", " ")
                lines.append(f"- {text}  `{r['id']}`")

    # ── SECTION 3: Decisions made ─────────────────────────────────────────────
    if decisions:
        lines += [
            "",
            "---",
            "",
            "## Decisions made",
            "_Architectural, implementation, and strategic choices locked in this session._",
            "",
        ]
        for r in decisions:
            text = (r["query"] or "")[:200].replace("\n", " ")
            model_tag = f" [{r['model']}]" if r["model"] and r["model"] != "unknown" else ""
            lines.append(f"- {text}{model_tag}  `{r['id']}`")

    # ── SECTION 4: Open questions + blockers ──────────────────────────────────
    open_q = [q for q in questions if q["kind"] == "question" and q["status"] == "active"]
    open_b = [b for b in blockers  if b["status"] == "active"]

    if open_q or open_b or open_items:
        lines += [
            "",
            "---",
            "",
            "## Open items",
            "_Unresolved questions and blockers that carry to the next session._",
            "",
        ]
        if open_q:
            lines.append("### Open questions")
            for r in open_q:
                text = (r["query"] or "")[:160].replace("\n", " ")
                lines.append(f"- ? {text}  `{r['id']}`")
        if open_b:
            lines.append("")
            lines.append("### Blockers")
            for r in open_b:
                text = (r["query"] or "")[:160].replace("\n", " ")
                lines.append(f"- ⛔ {text}  `{r['id']}`")
        if open_items:
            lines.append("")
            lines.append("### Carry-forward tasks")
            for r in open_items:
                text = (r["query"] or "")[:160].replace("\n", " ")
                lines.append(f"- 📌 {text}  `{r['id']}`")

    if resolved:
        lines += ["", "### Resolved this session"]
        for r in resolved:
            text = (r["query"] or "")[:140].replace("\n", " ")
            lines.append(f"- ✓ {text}  `{r['id']}`")

    # ── SECTION 5: Traversal path ─────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Traversal path",
        "_The sequence of tool calls — the path, not just the destination._",
        "",
    ]

    if tool_calls:
        for r in tool_calls[:50]:
            q    = (r["query"] or "")[:90].replace("\n", " ")
            rc   = f" → {r['result_count']}" if r["result_count"] is not None else ""
            ms   = f" [{r['latency_ms']}ms]" if r["latency_ms"] else ""
            flg  = " 🚩" if r["flagged"] else ""
            mdl  = f" [{r['model']}]" if r["model"] and r["model"] != "unknown" else ""
            role = f" ({r['agent_role']})" if r["agent_role"] and r["agent_role"] != "worker" else ""
            lines.append(f"- `{r['tool']}`{mdl}{role}  {q}{rc}{ms}{flg}  `{r['id']}`")
    else:
        lines.append("_no tool calls recorded_")

    # ── SECTION 6: Active threads ─────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Active threads",
        "_Nodes referenced by other nodes — the important chains._",
        "",
    ]
    if active_threads:
        for r in active_threads:
            preview = (r["output_preview"] or r["query"] or r["episodic_text"] or "")[:120]
            preview = preview.replace("\n", " ")
            kind_tag = r["kind"]
            model_tag = f" [{r['model']}]" if r["model"] and r["model"] != "unknown" else ""
            lines.append(f"- **`{r['id']}`** `{kind_tag}`{model_tag} — {preview}")
    else:
        lines.append("_none — no nodes formed chains this session_")

    # ── SECTION 7: Hard points ────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Hard points",
        "_Where the agent struggled: slow responses or empty results._",
        "_Episodic markers — knowing WHERE was hard matters next session._",
        "",
    ]
    if struggle:
        for r in struggle:
            why = []
            if (r["latency_ms"] or 0) > 800:
                why.append(f"{r['latency_ms']}ms")
            rc = r["result_count"]
            if rc is not None and rc <= 1:
                why.append(f"{rc} result{'s' if rc != 1 else ''}")
            q = (r["query"] or "")[:80].replace("\n", " ")
            tool = r["tool"] or r["kind"]
            model_tag = f" [{r['model']}]" if r["model"] and r["model"] != "unknown" else ""
            lines.append(f"- `{r['id']}` `{tool}`{model_tag}  {q}  — {', '.join(why)}")
    else:
        lines.append("_none — smooth session_")

    # ── SECTION 8: Insights and warnings ─────────────────────────────────────
    if insights or warnings_n:
        lines += ["", "---", "", "## Insights & warnings", ""]
        for r in (insights + warnings_n):
            emoji = "💡" if r["kind"] in ("insight", "hypothesis") else "⚠️"
            text  = (r["query"] or "")[:180].replace("\n", " ")
            lines.append(f"- {emoji} `{r['kind']}` {text}  `{r['id']}`")

    # ── SECTION 9: Multi-agent collaboration ─────────────────────────────────
    if len(models_seen) > 1:
        lines += [
            "",
            "---",
            "",
            "## Multi-agent collaboration",
            "_Models that contributed this session, their roles and struggle rates._",
            "",
        ]
        for model, stats in sorted(models_seen.items(), key=lambda x: -x[1]["nodes"]):
            role  = f" [{stats['role']}]" if stats["role"] != "worker" else ""
            line  = f"- `{model}`{role}: {stats['nodes']} nodes"
            if stats["struggles"]:
                pct   = round(stats["struggles"] / stats["nodes"] * 100)
                line += f" | {stats['struggles']} struggles ({pct}%)"
            if stats["flags"]:
                line += f" | {stats['flags']} flagged"
            lines.append(line)

    # ── SECTION 10: Session delta ─────────────────────────────────────────────
    if prev_summary:
        prev_sess  = prev_summary.get("session_name", "previous session")
        prev_nodes = prev_summary.get("node_count", 0)
        delta      = n_total - prev_nodes
        delta_str  = f"+{delta}" if delta >= 0 else str(delta)
        lines += [
            "",
            "---",
            "",
            "## Session delta",
            f"_What the previous session decided — loaded for continuity._",
            "",
            f"> Previous session: `{prev_sess}`",
            f"> Compiled: {prev_summary['timestamp']}",
            f"> Nodes then: {prev_nodes} | Now: {n_total} ({delta_str})",
            "",
        ]
        if prev_summary.get("new_decisions"):
            lines.append("### Decisions from previous session")
            for d in prev_summary["new_decisions"]:
                lines.append(f"- {d}")
            lines.append("")
        if prev_summary.get("new_questions"):
            lines.append("### Open items from previous session")
            for q in prev_summary["new_questions"]:
                lines.append(f"- ? {q}")

    # ── SECTION 11: Flags and voids ───────────────────────────────────────────
    if flagged:
        lines += [
            "",
            "---",
            "",
            "## Flagged for review",
            "",
        ]
        for r in flagged:
            desc = (r["episodic_text"] or r["query"] or "")[:120].replace("\n", " ")
            lines.append(f"- `{r['id']}` — {desc}")

    if voided:
        lines += [
            "",
            "---",
            "",
            "## Voided this session",
            "_Invalidated nodes — archived, never deleted._",
            "",
        ]
        for r in voided:
            desc = (r["query"] or r["output_preview"] or "")[:80].replace("\n", " ")
            lines.append(f"- `{r['id']}` — {desc}")

    # ── footer ────────────────────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## Navigation instructions",
        "> **Orient**: read Context stamps first. That's the session's identity.",
        "> **Gather**: walk the traversal path. Load nodes by ID via `cairn chain`.",
        "> **Check**: Hard points warn where to approach differently.",
        "> **Work**: continue from Active threads.",
        "> **Update**: write a new context_stamp when direction changes.",
        "",
        f"_cairn compiled — {ts}_",
        f"_vault: {n_total} nodes | model-agnostic | local-first | structurally incapable of surveillance_",
    ]

    compiled_text = "\n".join(lines)
    out.write_text(compiled_text, encoding="utf-8")

    # ── close the golden-angle feedback loop ──────────────────────────────────
    # Correlate what was LOADED against what APPEARED in compiled output.
    # Hit → compiled_hits++ and FSRS stability grows (successful recall).
    # Miss from middle → underattended next session (front-loaded).
    # This is the self-correcting part — placement learns from attention.
    try:
        from cairn.schedule import update_compiled_hits, PositionRecord

        records = vault.load_position_records()

        # Loaded = this session's nodes + vault-wide hot tier (always injected)
        hot_rows = vault.conn.execute(
            "SELECT id FROM nodes WHERE memory_tier=0 AND status='active'"
        ).fetchall()
        loaded_ids = [r["id"] for r in nodes] + [r["id"] for r in hot_rows]

        # Each compile is a recall test: every loaded node gets a load tick,
        # middle third of the load order gets a middle tick. Position order
        # here approximates context order (session nodes are chronological).
        total = len(loaded_ids)
        mid_s, mid_e = total // 3, (2 * total) // 3
        for i, nid in enumerate(loaded_ids):
            rec = records.setdefault(nid, PositionRecord(nid))
            rec.total_loads += 1
            if mid_s <= i < mid_e:
                rec.middle_hits += 1

        hits = update_compiled_hits(records, compiled_text, loaded_ids)

        # FSRS: citation in compiled output = successful recall → stability grows.
        # Growth factor 1.6 per recall, capped at 365 days (a year-stable memory).
        cited_ids = []
        for nid, hit in hits.items():
            if hit:
                row = vault.get(nid)
                if row:
                    new_stability = min(365.0, (row["stability_days"] or 1.0) * 1.6)
                    vault.set_stability(nid, new_stability)
                    cited_ids.append(nid)

        # attention ledger: mark every shown-receipt for cited nodes — the
        # ledger row goes from "shown" to "shown AND used", which is the
        # signal the scheduler learns placement from
        vault.mark_cited(cited_ids)

        vault.save_position_records(records)
    except Exception:
        pass  # the loop is an optimization — compile must never fail because of it

    # ── close the loop on the PUSH (hook) channel ─────────────────────────────
    # The block above can only mark nodes in loaded_ids (this session + hot tier),
    # so hook-surfaced CROSS-session memories are structurally unmarkable — the
    # hook channel reads 0% cited forever, and every placement/attention signal
    # built on it runs on a dead sensor. Measure them honestly: a pushed memory
    # counts as "used" when at least two salient terms of its gist actually
    # surface in this session's work. Conservative topic-overlap proxy (not true
    # use — to be sharpened with embedding match in a later phase); scoped to
    # THIS session's hook receipts so it can never inflate across sessions.
    try:
        import re as _re
        _STOP = {"about", "there", "their", "would", "could", "should", "which",
                 "where", "thing", "things", "really", "because", "being", "these",
                 "those", "going", "session", "cairn", "nodes"}
        sess_rows = vault.conn.execute(
            "SELECT episodic_text, query FROM nodes WHERE session=?", (session_id,)
        ).fetchall()
        corpus = (compiled_text + " " + " ".join(
            (r["episodic_text"] or r["query"] or "") for r in sess_rows)).lower()
        shown = vault.conn.execute(
            "SELECT DISTINCT node_id FROM attention_ledger "
            "WHERE session=? AND channel='hook' AND cited=0", (session_id,)
        ).fetchall()
        used = []
        for sr in shown:
            node = vault.get(sr["node_id"])
            if not node:
                continue
            gist = (node["gist"] or node["query"] or "").lower()
            terms = {t for t in _re.findall(r"[a-z][a-z0-9]{4,}", gist) if t not in _STOP}
            if sum(1 for t in terms if t in corpus) >= 2:
                used.append(sr["node_id"])
        if used:
            now = datetime.now(timezone.utc).isoformat()
            with vault.conn:
                vault.conn.executemany(
                    "UPDATE attention_ledger SET cited=1, cited_at=? "
                    "WHERE node_id=? AND session=? AND channel='hook' AND cited=0",
                    [(now, nid, session_id) for nid in used])
            # close the FSRS loop for the hook channel too (2026-07-03): these
            # "used" marks previously fed the ledger but never stability, so a
            # hook-shown node stayed at stability 1.0 = max pressure forever —
            # half the hoarding machine. Gentler growth than a compiled-text
            # citation (×1.3 vs ×1.6): the term-overlap proxy is noisier.
            for nid in used:
                row = vault.get(nid)
                if row:
                    vault.set_stability(
                        nid, min(365.0, (row["stability_days"] or 1.0) * 1.3))
    except Exception:
        pass  # telemetry must never break compile

    # ── hot-tier lease (salience decay, nightly) ──────────────────────────────
    # Hot is a LEASE, not a title: 35 day-one nodes squatted tier 0 and the
    # 9-slot fovea for weeks. An old, heavily-aired hot node yields its slot —
    # demoted to warm, where due-pressure still rotates it honestly. Nothing
    # is deleted (append-only holds; tier is scheduling metadata, explicitly
    # allowed by the immutability trigger). The human's pin outranks decay:
    # flagged nodes are exempt.
    try:
        stale_hot = vault.conn.execute("""
            SELECT n.id FROM nodes n
            JOIN (SELECT node_id, COUNT(*) AS shows
                  FROM attention_ledger GROUP BY node_id) al
              ON al.node_id = n.id
            WHERE n.memory_tier = 0 AND n.status = 'active'
              AND COALESCE(n.flagged, 0) = 0
              AND al.shows > 150
              AND julianday('now') - julianday(n.timestamp) > 14
        """).fetchall()
        for r in stale_hot:
            vault.set_tier(r["id"], 1)
    except Exception:
        pass  # decay is hygiene — never break compile

    return out


def _load_prev_summary(current_session_id: str, protocols_root: Path) -> dict | None:
    """
    Reads the most recent OTHER session's PROTOCOL.md to compute a delta.
    Extracts decisions and questions so the delta section shows actual content,
    not just node counts.
    """
    if not protocols_root.exists():
        return None

    # All PROTOCOL.md files except the current session
    all_protos = [
        p for p in protocols_root.glob("*/PROTOCOL.md")
        if p.parent.name != current_session_id
    ]
    if not all_protos:
        return None

    # Pick the most-recent OTHER session by its RECORDED compile time (the
    # 'compiled:' line inside each PROTOCOL.md), NOT file mtime. Two concurrent
    # sessions re-touch their protocol files (and OneDrive sync bumps mtime),
    # which made mtime-max flip the DELTA onto a *parallel* session instead of
    # the real predecessor. The recorded compile time is stable and meaningful;
    # mtime falls back only as a tiebreak / when the header can't be read.
    def _compiled_ts(p: Path) -> str:
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("compiled:"):
                    return line.split("compiled:", 1)[1].strip()
        except Exception:
            pass
        return ""
    last = max(all_protos, key=lambda p: (_compiled_ts(p), p.stat().st_mtime))

    try:
        text = last.read_text(encoding="utf-8")
    except Exception:
        return None

    result: dict = {
        "timestamp":     "",
        "node_count":    0,
        "new_decisions": [],
        "new_questions": [],
        "session_name":  last.parent.name,
    }

    in_decisions = False
    in_questions = False

    for line in text.split("\n"):
        stripped = line.strip()

        if line.startswith("compiled:"):
            result["timestamp"] = line.split("compiled:")[1].strip()
        elif line.startswith("nodes:"):
            try:
                result["node_count"] = int(line.split("nodes:")[1].strip().split()[0])
            except Exception:
                pass
        elif stripped == "## Decisions made":
            in_decisions = True
            in_questions = False
        elif stripped == "## Open items":
            in_decisions  = False
            in_questions  = True
        elif stripped.startswith("##"):
            in_decisions  = False
            in_questions  = False
        elif in_decisions and line.startswith("- ") and "`" in line:
            # "- decision text [model]  `node_id`" — strip node_id and model tag
            parts = line[2:].rsplit("`", 2)
            d = parts[0].strip()
            # strip trailing [model] tag if present
            if d.endswith("]") and "[" in d:
                d = d[:d.rfind("[")].strip()
            if d and len(d) > 5:
                result["new_decisions"].append(d[:160])
        elif in_questions and line.startswith(("- ", "- 📌", "- ?")):
            text_part = line.lstrip("-📌? ").strip()
            if "`" in text_part:
                text_part = text_part.rsplit("`", 2)[0].strip()
            if text_part and len(text_part) > 5:
                result["new_questions"].append(text_part[:160])

    if result["timestamp"] or result["node_count"]:
        return result
    return None
