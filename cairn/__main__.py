"""
cairn/__main__.py
CLI for the cairn vault.

Usage (from any directory):
  python -m cairn note "this decision matters because..."
  python -m cairn note --kind=decision "chose SQLite for local-first"
  python -m cairn note --kind=hypothesis "bug is in the token expiry path"
  python -m cairn note --kind=warning "this approach will break at scale"
  python -m cairn flag <node_id>
  python -m cairn void <node_id>
  python -m cairn embed              <- batch embed all pending nodes
  python -m cairn status             <- current session stats
  python -m cairn query "auth bug"   <- semantic search (needs embeddings)
  python -m cairn chain <node_id>    <- show reasoning chain
  python -m cairn compile            <- generate PROTOCOL.md now

The agent (me) calls this via the Bash tool during a Claude Code session.
No MCP needed. Just bash.
"""
import sys, os, json
from pathlib import Path

# Windows consoles default to cp1252, which can't print ✓/—/emoji in node text.
# Force UTF-8 on the CLI's own streams so orient/query never die mid-digest.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault, MicroNode

VALID_KINDS = {
    "note":              "general observation",
    "decision":          "architectural or implementation choice made",
    "hypothesis":        "theory being tested",
    "warning":           "potential problem spotted",
    "insight":           "pattern or connection noticed",
    "question":          "open question needing answer",
    "open_item":         "pending task or carry-forward item (survives compaction)",
    "procedure":         "session-independent how-to — never recency-decays (basal ganglia)",
    "idea":              "spark for later — lands in the Garden's Ideas bank, resurfaces over time",
    "blocker":           "something blocking progress",
    "resolved":          "something that was resolved",
    "conversation_turn": "a turn in the conversation — user or agent",
    "context_stamp":     "why this session started, what carries forward",
}


def get_session() -> str:
    """
    Unified session ID — single source of truth.

    Priority:
    1. CAIRN_SESSION — explicit override, set by any agent framework
    2. CLAUDE_SESSION_ID — Claude Code injects this into hook events
    3. last_session.txt — written by hook.py when it sees a session_id
    4. date-based fallback — worst case, at least groups by day
    """
    sid = (os.environ.get("CAIRN_SESSION") or
           os.environ.get("CLAUDE_SESSION_ID"))
    if sid:
        return sid
    # hook.py writes the session ID here when it processes events
    session_file = Path.home() / ".cairn" / "last_session.txt"
    if session_file.exists():
        saved = session_file.read_text().strip()
        if saved:
            return saved
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_parent() -> str | None:
    """Chain to last captured node if available."""
    state = Path.home() / ".cairn" / "last_node.txt"
    if state.exists():
        return state.read_text().strip() or None
    return None


def cmd_note(args: list[str]) -> None:
    """Write an agent-authored note to the vault."""
    kind    = "note"
    speaker = "agent"
    supersedes = None
    content = []

    i = 0
    while i < len(args):
        if args[i].startswith("--kind="):
            kind = args[i].split("=", 1)[1]
            if kind not in VALID_KINDS:
                print(f"unknown kind '{kind}'. valid: {', '.join(VALID_KINDS)}")
                sys.exit(1)
        elif args[i].startswith("--speaker="):
            speaker = args[i].split("=", 1)[1]
        elif args[i].startswith("--supersedes="):
            supersedes = args[i].split("=", 1)[1].strip()
        else:
            content.append(args[i])
        i += 1

    text = " ".join(content).strip()
    if not text:
        print("usage: python -m cairn note [--kind=decision] [--speaker=user|agent] 'text'")
        sys.exit(1)

    # route conversation_turn and context_stamp through capture module
    if kind in ("conversation_turn", "context_stamp"):
        from cairn.capture import write_turn, write_stamp
        if kind == "conversation_turn":
            node = write_turn(text, speaker=speaker)
        else:
            node = write_stamp(intent=text)
        print(f"cairn: wrote {kind} [{node.id}] (speaker={speaker})")
        print(f"       '{text[:80]}{'...' if len(text) > 80 else ''}'")
        return

    vault   = Vault()
    session = get_session()
    parent  = get_parent()

    model = (os.environ.get("CAIRN_MODEL") or
             os.environ.get("CLAUDE_MODEL") or "unknown")

    tags = ["agent-authored", kind]
    if supersedes:
        # durable meaning-edge stored ON THE NODE — the edges table is derived
        # (rebuilt by `cairn edges`), so a row there wouldn't survive.
        tags.append("supersedes:" + supersedes)

    node = vault.write(MicroNode(
        session        = session,
        kind           = kind,
        query          = text[:500],
        output_preview = text,
        parent         = parent,
        model          = model,
        tags           = tags,
    ))

    state = Path.home() / ".cairn" / "last_node.txt"
    state.write_text(node.id)

    print(f"cairn: wrote {kind} [{node.id}]")
    if parent:
        print(f"       chained to: {parent}")
    # retire the superseded node via the SANCTIONED append-only path — void() is
    # the only allowed status mutation (an immutability trigger blocks any other).
    # Voided nodes drop from fetch/inject (both filter status='active'); the
    # supersedes:<id> tag on the new node preserves the lineage either way.
    if supersedes:
        row = vault.conn.execute(
            "SELECT status FROM nodes WHERE id=?", (supersedes,)).fetchone()
        if row is None:
            print(f"       note: [{supersedes}] not found — link recorded, nothing retired")
        elif row["status"] == "void":
            print(f"       note: [{supersedes}] already retired — link recorded")
        else:
            vault.void(supersedes)
            print(f"       supersedes [{supersedes}] — retired (void), stops resurfacing")
    print(f"       '{text[:80]}{'...' if len(text) > 80 else ''}'")


def cmd_flag(args: list[str]) -> None:
    if not args:
        print("usage: python -m cairn flag <node_id>")
        sys.exit(1)
    vault = Vault()
    vault.flag(args[0])
    print(f"cairn: flagged {args[0]}")


def cmd_void(args: list[str]) -> None:
    if not args:
        print("usage: python -m cairn void <node_id>")
        sys.exit(1)
    vault = Vault()
    vault.void(args[0])
    print(f"cairn: voided {args[0]}")


def cmd_promote(args: list[str]) -> None:
    """
    Promote a node's memory tier — hotter = more likely to be injected.

    Usage:
      python -m cairn promote <node_id>          # one step hotter (2→1 or 1→0)
      python -m cairn promote <node_id> --hot    # force to tier 0 (always injected)
      python -m cairn promote <node_id> --warm   # force to tier 1 (golden-angle)

    Tier meanings:
      0 = hot  — injected at position 0 every session, always in context
      1 = warm — scheduled by golden-angle across the context window
      2 = cold — retrieval-only, never injected automatically
    """
    if not args:
        print("usage: python -m cairn promote <node_id> [--hot|--warm]")
        sys.exit(1)

    node_id = args[0]
    vault   = Vault()
    row     = vault.get(node_id)
    if not row:
        print(f"cairn: node {node_id} not found")
        sys.exit(1)

    current = row["memory_tier"]
    TIER_NAMES = {0: "hot", 1: "warm", 2: "cold"}

    if "--hot" in args:
        target = 0
    elif "--warm" in args:
        target = 1
    else:
        target = max(0, current - 1)   # one step hotter

    if target >= current:
        print(f"cairn: {node_id} is already {TIER_NAMES[current]} (tier {current}) — already at or hotter than target")
        return

    ok = vault.set_tier(node_id, target)
    if ok:
        text = (row["query"] or row["output_preview"] or "")[:60]
        print(f"cairn: promoted {node_id}  {TIER_NAMES[current]} → {TIER_NAMES[target]}")
        print(f"       '{text}'")
    else:
        print(f"cairn: could not promote {node_id}")


def cmd_demote(args: list[str]) -> None:
    """
    Demote a node's memory tier — colder = less likely to be injected.

    Usage:
      python -m cairn demote <node_id>           # one step colder (0→1 or 1→2)
      python -m cairn demote <node_id> --cold    # force to tier 2 (retrieval-only)
      python -m cairn demote <node_id> --warm    # force to tier 1 (golden-angle)

    Use when a node was promoted but is no longer relevant to current work,
    or to clear space from the hot tier (tier 0) when it's getting crowded.
    """
    if not args:
        print("usage: python -m cairn demote <node_id> [--cold|--warm]")
        sys.exit(1)

    node_id = args[0]
    vault   = Vault()
    row     = vault.get(node_id)
    if not row:
        print(f"cairn: node {node_id} not found")
        sys.exit(1)

    current = row["memory_tier"]
    TIER_NAMES = {0: "hot", 1: "warm", 2: "cold"}

    if "--cold" in args:
        target = 2
    elif "--warm" in args:
        target = 1
    else:
        target = min(2, current + 1)   # one step colder

    if target <= current:
        print(f"cairn: {node_id} is already {TIER_NAMES[current]} (tier {current}) — already at or colder than target")
        return

    ok = vault.set_tier(node_id, target)
    if ok:
        text = (row["query"] or row["output_preview"] or "")[:60]
        print(f"cairn: demoted {node_id}  {TIER_NAMES[current]} → {TIER_NAMES[target]}")
        print(f"       '{text}'")
    else:
        print(f"cairn: could not demote {node_id}")


def cmd_sessions(args: list[str]) -> None:
    """
    List all sessions with node counts, intent signals, and compile status.

    Usage:
      python -m cairn sessions           # all sessions
      python -m cairn sessions --top=5   # most recent N sessions
    """
    vault    = Vault()
    sessions = vault.all_sessions()

    top = None
    for arg in args:
        if arg.startswith("--top="):
            try:
                top = int(arg.split("=")[1])
            except ValueError:
                pass

    if top:
        sessions = sessions[:top]

    protocols_root = Path.home() / ".cairn" / "protocols"
    current        = get_session()

    print(f"cairn: {len(sessions)} session(s) in vault\n")
    for s in sessions:
        sid      = s["id"]
        marker   = " ← current" if sid == current else ""
        started  = (s["started_at"] or "")[:10]
        n_total  = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE session=? AND status!='void'", (sid,)
        ).fetchone()[0]
        n_intent = vault.session_decision_count(sid)
        compiled = "✓" if (protocols_root / sid / "PROTOCOL.md").exists() else "·"

        # tier breakdown
        hot  = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE session=? AND memory_tier=0 AND status!='void'", (sid,)
        ).fetchone()[0]
        warm = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE session=? AND memory_tier=1 AND status!='void'", (sid,)
        ).fetchone()[0]

        # short session name
        name = sid.rsplit("-20", 1)[0][:35] if "-20" in sid else sid[:35]

        print(f"  [{compiled}] {started}  {name}{marker}")
        print(f"       {n_total} nodes | {n_intent} intent | hot={hot} warm={warm}")


def cmd_embed(args: list[str]) -> None:
    """Batch embed all nodes that don't have embeddings yet."""
    print("cairn: loading sentence-transformers (first run takes ~10s)...")
    vault = Vault()
    try:
        n = vault.embed_pending()
    except ImportError:
        print("cairn: the embedder isn't installed — semantic search stays off until you add it.")
        print('      fix: pip install "cairn-remembers[embeddings]"   ·   from source: pip install -e ".[embeddings]"')
        return
    except Exception as e:
        print(f"cairn: embed couldn't run — {e}")
        print("      (the first embed downloads the model from HuggingFace; check your connection and retry)")
        return
    if n == 0:
        print("cairn: all nodes already embedded")
    else:
        print(f"cairn: embedded {n} nodes")


def cmd_status(args: list[str]) -> None:
    vault   = Vault()
    stats   = vault.stats()
    session = get_session()

    nodes     = vault.session_nodes(session)
    struggles = vault.struggle_points(session)

    # tier distribution across entire vault
    tier_counts = vault.conn.execute("""
        SELECT memory_tier, COUNT(*) as n FROM nodes
        WHERE status != 'void'
        GROUP BY memory_tier ORDER BY memory_tier
    """).fetchall()
    tier_map = {r["memory_tier"]: r["n"] for r in tier_counts}

    # embedding coverage
    total_active = stats["total"] - stats["voided"]
    embedded = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL AND status!='void'"
    ).fetchone()[0]
    embed_pct = int(embedded / total_active * 100) if total_active else 0

    # intent nodes this session (decisions, open_items, etc.)
    intent = [r for r in nodes if r["kind"] in VALID_KINDS and r["kind"] != "note"]

    print(f"cairn status")
    print(f"  session      : {session}")
    print(f"  this session : {len(nodes)} nodes | {len(struggles)} hard points | {len(intent)} intent notes")
    print(f"  vault total  : {stats['total']} nodes across {stats['sessions']} sessions")
    print(f"  active/voided: {total_active} / {stats['voided']}")
    print(f"  flagged      : {stats['flagged']}")
    print(f"  tiers        : hot={tier_map.get(0,0)}  warm={tier_map.get(1,0)}  cold={tier_map.get(2,0)}")
    print(f"  embeddings   : {embedded}/{total_active} ({embed_pct}%) — run 'cairn embed' to backfill")

    if intent:
        print(f"\n  intent notes this session ({len(intent)}):")
        for r in intent:
            tier_label = {0: "hot", 1: "warm", 2: "cold"}.get(r["memory_tier"], "?")
            text = (r["query"] or "")[:72]
            print(f"    [{r['kind']:14}] [{tier_label}]  {text}")


def cmd_query(args: list[str]) -> None:
    """Semantic search — requires embeddings."""
    if not args:
        print("usage: python -m cairn query 'what you're looking for'")
        sys.exit(1)
    question = " ".join(args)
    vault = Vault()

    # check if any embeddings exist
    has_emb = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL"
    ).fetchone()[0]

    if has_emb == 0:
        print("cairn: no embeddings yet — run 'python -m cairn embed' first")
        print("       falling back to keyword search...")
        # keyword fallback
        rows = vault.conn.execute("""
            SELECT * FROM nodes
            WHERE status != 'void'
              AND (query LIKE ? OR output_preview LIKE ? OR episodic_text LIKE ?)
            ORDER BY timestamp DESC LIMIT 10
        """, (f"%{question}%",) * 3).fetchall()
        if not rows:
            print("       no results")
            return
        for r in rows:
            text = (r["query"] or "")[:70]
            print(f"  [{r['id']}] {r['tool'] or r['kind']:12} {text}")
        return

    vault.load_embedder()
    results = vault.query_episodic(question, k=8)
    print(f"cairn: top results for '{question}'")
    print(f"       ranked by: 0.50*cosine + 0.30*recency + 0.20*importance\n")
    for r in results:
        comp    = r.get("score", 0.0)
        cos     = r.get("score_cosine", 0.0)
        rec     = r.get("score_recency", 0.0)
        imp     = r.get("score_import", 0.5)
        imp_raw = int(r.get("importance") or 5)
        text    = (r["query"] or r["output_preview"] or "")[:70]
        kind    = (r["tool"] or r["kind"] or "?")
        sess    = (r["session"] or "").rsplit("-20", 1)[0][-18:]
        tier    = {0: "hot", 1: "warm", 2: "cold"}.get(r.get("memory_tier"), "?")
        print(f"  [{r['id']}] {kind:16}  {text}")
        print(f"             score={comp:.3f}  "
              f"cos={cos:.3f}  rec={rec:.3f}  imp={imp:.2f} (raw={imp_raw})  "
              f"[{tier}] [{sess}]")
        if r.get("episodic_text"):
            print(f"             {r['episodic_text'][:90]}")
        print()


def cmd_chain(args: list[str]) -> None:
    if not args:
        print("usage: python -m cairn chain <node_id>")
        sys.exit(1)
    vault = Vault()
    chain = vault.chain(args[0])
    if not chain:
        print(f"cairn: node {args[0]} not found")
        return
    print(f"cairn: reasoning chain ({len(chain)} hops)\n")
    for i, r in enumerate(chain):
        indent = "  " * i
        arrow  = "└─ " if i > 0 else "   "
        text   = (r["query"] or "")[:60]
        ms     = f" [{r['latency_ms']}ms]" if r["latency_ms"] else ""
        rc     = f" → {r['result_count']}" if r["result_count"] is not None else ""
        print(f"  {indent}{arrow}[{r['id']}] {r['tool'] or r['kind']}{ms}{rc}  '{text}'")


def cmd_read(args: list[str]) -> None:
    """
    Read node(s) IN FULL by id or unambiguous prefix — the terminal's verbatim
    surface. The scan commands (query/fetch/logs) show gists and capped
    previews; this prints the complete stored text: query, output_preview, and
    episodic_text (which carries the lossless tail of long turns).

    Usage:
      python -m cairn read <id> [<id> ...] [--max-chars=N]
        --max-chars=N   optional per-field print cap (default: full text)
    """
    ids = [a for a in args if not a.startswith("--")]
    cap = None
    for a in args:
        if a.startswith("--max-chars="):
            try:
                cap = max(200, int(a.split("=", 1)[1]))
            except ValueError:
                print("cairn: --max-chars needs a number"); sys.exit(1)
    if not ids:
        print("usage: python -m cairn read <id> [<id> ...] [--max-chars=N]")
        sys.exit(1)
    vault = Vault()
    for want in ids[:8]:
        rows = vault.conn.execute(
            "SELECT id, kind, status, session, speaker, model, timestamp, tags, "
            "       query, output_preview, episodic_text "
            "FROM nodes WHERE id LIKE ? || '%' LIMIT 3", (want,)).fetchall()
        if not rows:
            print(f"── [{want}] not found."); continue
        if len(rows) > 1:
            print(f"── [{want}] ambiguous prefix — matches: "
                  + ", ".join(r["id"] for r in rows)); continue
        r = rows[0]
        print(f"── [{r['id']}] {r['kind']} · {r['session']} · "
              f"{r['speaker'] or '?'}/{r['model'] or '?'} · {r['timestamp']}")
        if r["status"] != "active":
            print(f"   ⚠ status={r['status']} — retired from ranked surfaces; "
                  f"historical record.")
        if r["tags"]:
            print(f"   tags: {r['tags']}")
        def _p(label, text):
            if not text:
                return
            t = text if cap is None or len(text) <= cap else \
                text[:cap] + f" [... capped at {cap} — drop --max-chars for all]"
            print(f"   {label}: {t}")
        _p("text", r["query"])
        if r["output_preview"] and r["output_preview"] != r["query"]:
            _p("preview", r["output_preview"])
        if r["episodic_text"] and r["episodic_text"] not in (r["query"], r["output_preview"]):
            _p("episodic", r["episodic_text"])
        print()


def cmd_session(args: list[str]) -> None:
    """
    Set or show the current session name.

    You usually never run this. With hooks on (cairn connect / --global) the
    harness owns session identity — a new chat IS a new session, re-stamped on
    every tool call — so orient just reads the carryover and you "start a new
    session" simply by starting a new chat.

    Where it IS needed — pull-mode harnesses (Claude Desktop agent mode, any runtime
    where PostToolUse hooks don't fire): without a hook writing last_session.txt,
    notes inherit a STALE session name from a previous run. Run this at session
    start to attribute the session correctly.

    Usage:
      python -m cairn session                       # show current
      python -m cairn session my-feature-2026-06-10 # set
      python -m cairn session --new                 # auto-name from date
    """
    from datetime import datetime, timezone
    session_file = Path.home() / ".cairn" / "last_session.txt"

    if not args:
        current = get_session()
        print(f"cairn: current session — {current}")
        src = ("env" if (os.environ.get("CAIRN_SESSION") or os.environ.get("CLAUDE_SESSION_ID"))
               else "last_session.txt" if session_file.exists() else "date fallback")
        print(f"       source: {src}")
        return

    if args[0] == "--new":
        name = datetime.now(timezone.utc).strftime("session-%Y-%m-%d-%H%M")
    else:
        name = args[0]

    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(name)
    # Clear stale parent chain — a new session must not chain to the old one
    last_node = Path.home() / ".cairn" / "last_node.txt"
    if last_node.exists():
        last_node.unlink()
    print(f"cairn: session set — {name}")
    print(f"       parent chain cleared; notes now attribute here")


def cmd_orient(args: list[str]) -> None:
    """
    Orient at session start — read PROTOCOL.md, write a context_stamp,
    AND print the key inherited context directly so this session reads it.

    Run this at the start of every session:
      python -m cairn orient

    Output goes to stdout so the model SEES the past decisions, open items,
    and hard points — not just a node ID. This is what makes cross-session
    memory actually work in a stateless environment.
    """
    from cairn.capture import session_intent_from_protocol

    session  = get_session()
    out_dir  = Path.home() / ".cairn" / "protocols" / session
    protocol = out_dir / "PROTOCOL.md"

    # page one first — computed LIVE and scoped to THIS galaxy, so the counts are
    # current (not the nightly-stale file) and a Codex/GPT session sees its own
    # activity. Falls back to the cached PAGE_ONE.md if the live render ever
    # throws — orient must never break.
    try:
        from cairn.book import page_one as _page_one
        from cairn.vault import Vault as _OVault, _live_account
        print(_page_one(_OVault(), account=_live_account(session)).strip())
        print()
    except Exception:
        page_one_file = Path.home() / ".cairn" / "PAGE_ONE.md"
        if page_one_file.exists():
            try:
                print(page_one_file.read_text(encoding="utf-8", errors="replace").strip())
                print()
            except Exception:
                pass

    # multi-account ambiguity warning — printed OUTSIDE the page_one try/except so
    # a page_one failure never suppresses it (and a warning failure never eats the
    # page). '' on an unambiguous/single-account session, so most runs print nothing.
    try:
        from cairn.vault import orient_account_warning as _oaw
        _w = _oaw(session).lstrip("\n")
        if _w:
            print(_w)
            print()
    except Exception:
        pass

    if not protocol.exists():
        # find the best PROTOCOL.md — prefer sessions with intent nodes over empty ones,
        # then fall back to most recently compiled. This prevents a zero-node test session
        # from becoming the "previous context" just because it compiled most recently.
        protocols_root = Path.home() / ".cairn" / "protocols"
        all_protos = list(protocols_root.glob("*/PROTOCOL.md")) if protocols_root.exists() else []
        if all_protos:
            def _proto_score(p: Path):
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    # count decision/open_item bullet points under key sections
                    has_intent = (
                        "## Decisions made" in text and
                        "\n- " in text.split("## Decisions made", 1)[-1].split("\n##")[0]
                    ) or (
                        "### Decisions from previous session" in text and
                        "\n- " in text.split("### Decisions from previous session", 1)[-1].split("\n##")[0]
                    )
                    return (has_intent, p.stat().st_mtime)
                except Exception:
                    return (False, 0.0)
            protocol = max(all_protos, key=_proto_score)

    if not protocol.exists():
        print("cairn: no PROTOCOL.md found — starting fresh")
        from cairn.capture import write_stamp
        write_stamp("new session — no prior context")
        return

    # ── write context_stamp node (vault record) ───────────────────────────────
    vault = Vault()
    node  = session_intent_from_protocol(protocol, session, vault)

    # ── print inherited context directly for the model to read ────────────────
    _print_orient_digest(protocol)

    # ── research queue: the human queued these from the Ideas tab ─────────────
    # This is the standing human→AI handoff. Do the research, write findings
    # as nodes with parent=<idea id>, then resolve the request.
    try:
        queue = vault.conn.execute("""
            SELECT id, parent, query FROM nodes
            WHERE kind='open_item' AND status='active'
              AND tags LIKE '%"research-queue"%'
            ORDER BY timestamp ASC LIMIT 10
        """).fetchall()
        if queue:
            print(f"\n  RESEARCH QUEUE — {len(queue)} request(s) from the Ideas tab:")
            for q in queue:
                print(f"    [{q['id']}] {(q['query'] or '')[:90]}")
                print(f"        -> write findings with parent={q['parent']}, "
                      f"then: note --kind=resolved + void {q['id']}")
    except Exception:
        pass


def _print_orient_digest(protocol: Path) -> None:
    """
    Parse PROTOCOL.md and print only what carries forward to this session:
    decisions, open items, hard points, session delta.

    Skips traversal path and active threads (too verbose).
    Output is intentionally compact — the model reads this at session start.
    """
    try:
        text = protocol.read_text(encoding="utf-8")
    except Exception:
        print("cairn: could not read PROTOCOL.md")
        return

    # extract header metadata
    session_line  = next((l for l in text.split("\n") if l.startswith("session:")),  "")
    compiled_line = next((l for l in text.split("\n") if l.startswith("compiled:")), "")
    nodes_line    = next((l for l in text.split("\n") if l.startswith("nodes:")),    "")

    prev_session = session_line.split(":", 1)[1].strip() if ":" in session_line else "unknown"

    print()
    print("=" * 68)
    print("CAIRN — inherited context from previous session")
    print(f"  from:     {prev_session}")
    print(f"  {compiled_line.strip()}")
    print(f"  {nodes_line.strip()}")
    print("=" * 68)

    # sections to extract and their display labels
    SECTIONS = [
        ("## Decisions made",        "DECISIONS",  20),
        ("## Open items",            "OPEN ITEMS", 15),
        ("## Hard points",           "HARD POINTS",10),
        ("## Session delta",         "DELTA",       8),
        ("## Insights & warnings",   "WARNINGS",    8),
    ]

    lines = text.split("\n")
    for section_header, label, max_items in SECTIONS:
        # find section start
        try:
            start_idx = next(i for i, l in enumerate(lines) if l.strip() == section_header)
        except StopIteration:
            continue

        # collect until next ## section (### subheaders are included, not a break)
        section_lines = []
        for l in lines[start_idx + 1:]:
            # only break on top-level sections (## followed by space)
            if l.startswith("## "):
                break

            if l.startswith("### "):
                # subheader — include as a divider line
                section_lines.append("  " + l[4:])
                continue

            if l.startswith("- ") or l.startswith("> "):
                clean = l

                # strip leading `node_id` pattern (hard points format):
                # "- `abc123456789` `Tool`  text — reason"
                # remove first backtick group if it looks like a node ID (hex, ≤12 chars)
                if clean.startswith("- `"):
                    parts = clean[3:].split("`", 1)
                    if parts and len(parts[0]) <= 12 and parts[0].isalnum():
                        clean = "- " + parts[1].lstrip(" `").split("`", 1)[-1].lstrip()
                        clean = "- " + clean[2:].lstrip()  # normalize

                # also strip trailing `node_id` from decisions:
                # "- text [model]  `node_id`"
                if "`" in clean:
                    parts = clean.rsplit("`", 2)
                    if len(parts) >= 3 and 6 <= len(parts[-2]) <= 12 and parts[-2].isalnum():
                        clean = parts[0].rstrip()

                clean = clean.strip()
                if clean and clean not in ("- _", "- "):
                    section_lines.append(clean[:160])

        if not section_lines:
            continue

        print()
        print(f"  [{label}]")
        for item in section_lines[:max_items]:
            print(f"  {item}")

    print()
    print("=" * 68)
    print("End of inherited context. Begin this session's work above this line.")
    print("=" * 68)
    print()


def cmd_cross_query(args: list[str]) -> None:
    """
    Search across ALL sessions — the vault remembers everything.
    'where did gpt-4o struggle with auth?' searches every session you've ever had.
    """
    if not args:
        print("usage: python -m cairn xquery 'what you're looking for'")
        sys.exit(1)
    question = " ".join(args)
    vault = Vault()

    has_emb = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL"
    ).fetchone()[0]

    if has_emb == 0:
        print("cairn: no embeddings yet — run 'python -m cairn embed' first")
        sys.exit(1)

    vault.load_embedder()
    results = vault.related_across_sessions(question, k=10)
    print(f"cairn: cross-session search for '{question}':\n")
    for r in results:
        score   = r.get("score", 0)
        text    = (r["query"] or r["output_preview"] or "")[:70]
        kind    = r["tool"] or r["kind"]
        session = r["session"][:30]
        model   = r["model"] or "unknown"
        print(f"  [{r['id']}] {kind:14} score={score:.3f}  [{model}]")
        print(f"             session: {session}")
        print(f"             {text}")
        print()


def cmd_compile(args: list[str]) -> None:
    from cairn.compile import compile_session
    vault   = Vault()
    session = get_session()
    out_dir = Path.home() / ".cairn" / "protocols" / session
    path    = compile_session(vault, session, out_dir)
    print(f"cairn: compiled PROTOCOL.md")
    print(f"       {path}")
    print(f"       {path.stat().st_size} bytes")


def cmd_schedule(args: list[str]) -> None:
    """
    Build and display the golden-angle context manifest for the current session.

    Shows what gets loaded at position 0 (hot tier) and what gets reinjected
    at golden-angle intervals through the context window (warm tier).

    Usage:
      python -m cairn schedule               # current session, 200k window
      python -m cairn schedule --window=100000
      python -m cairn schedule --render      # print full manifest text
    """
    from cairn.schedule import build_manifest, render_manifest, schedule_summary

    window  = 200_000
    render  = False
    session = get_session()

    for arg in args:
        if arg.startswith("--window="):
            window = int(arg.split("=")[1])
        elif arg == "--render":
            render = True
        elif arg.startswith("--session="):
            session = arg.split("=")[1]

    vault = Vault()
    stats = schedule_summary(vault, session, window)

    print(f"cairn: context manifest — session '{session}'")
    print(f"       window : {window:,} tokens")
    print(f"       hot    : {stats['hot']}  (load at position 0)")
    print(f"       warm   : {stats['warm']}  (golden-angle scheduled)")
    print(f"       cold   : {stats['cold']}  (retrieval-only)")
    print(f"       coverage: {stats['coverage']} of active nodes in injection schedule")

    if render:
        print()
        manifest = build_manifest(vault, session, window)
        print(render_manifest(manifest))


def cmd_consolidate(args: list[str]) -> None:
    """
    The REM sleep pass — episodic → semantic consolidation.

    Clusters embedded meaning-nodes (cosine >= 0.75) spanning 2+ sessions,
    synthesizes one insight/procedure node per cluster (zero-token, medoid-
    based), boosts member FSRS stability, demotes absorbed warm members
    to cold. Append-only: episodes are never modified, lineage in tags.

    Usage:
      python -m cairn consolidate
      python -m cairn consolidate --dry-run
      python -m cairn consolidate --threshold=0.80 --min-cluster=4
    """
    from cairn.consolidate import consolidate, COSINE_THRESHOLD, MIN_CLUSTER

    threshold   = COSINE_THRESHOLD
    min_cluster = MIN_CLUSTER
    dry_run     = False
    for arg in args:
        if arg.startswith("--threshold="):
            threshold = float(arg.split("=")[1])
        elif arg.startswith("--min-cluster="):
            min_cluster = int(arg.split("=")[1])
        elif arg == "--dry-run":
            dry_run = True

    print(f"cairn: consolidating (threshold={threshold}, min_cluster={min_cluster}"
          f"{', DRY RUN' if dry_run else ''})...")
    report = consolidate(threshold=threshold, min_cluster=min_cluster, dry_run=dry_run)

    print(f"cairn: {report['candidates']} meaning-nodes examined")
    print(f"       {report['clusters']} cross-session clusters found")
    if report["details"]:
        for d in report["details"]:
            mark = "would create" if dry_run else "created"
            new  = f" -> [{d.get('new_node', '')}]" if d.get("new_node") else ""
            print(f"       {mark} {d['kind']}: x{d['members']} members, "
                  f"{d['sessions']} sessions{new}")
            print(f"         '{d['summary'][:70]}'")
    if not dry_run:
        print(f"       insights: {report['insights']}  procedures: {report['procedures']}")
        print(f"       members boosted: {report['members_boosted']}  "
              f"demoted to cold: {report['members_demoted']}")


def cmd_mcp(args: list[str]) -> None:
    """
    Run Cairn as an MCP server (stdio). Lets any MCP client — Claude Desktop,
    Claude Code, Cursor, local model frontends — call vault tools natively:
    cairn_fetch, cairn_search, cairn_wander, cairn_note, cairn_orient,
    cairn_recent, cairn_read, cairn_logs.

    Register in claude_desktop_config.json:
      "mcpServers": { "cairn": {
        "command": "<python.exe>", "args": ["-X","utf8","-m","cairn","mcp"] } }
    """
    from cairn.mcp_server import serve
    serve()


def cmd_fetch(args: list[str]) -> None:
    """
    Token-saving retrieval: one query -> a compact context pack of only what
    matters. The CLI face of what cairn_fetch gives an AI.

    Usage:
      python -m cairn fetch "how does the inject gate work"
      python -m cairn fetch "golf scraper" --budget=2500
    """
    from cairn.retrieve import fetch_pack, render_pack
    budget = 1500
    terms = []
    for a in args:
        if a.startswith("--budget="):
            budget = int(a.split("=")[1])
        else:
            terms.append(a)
    if not terms:
        print("usage: cairn fetch 'your question' [--budget=1500]")
        return
    pack = fetch_pack(" ".join(terms), budget_tokens=budget, channel="cli_fetch")
    print(render_pack(pack))


def cmd_wander(args: list[str]) -> None:
    """
    The creative complement to fetch: walk the edge graph outward from the
    query's best hits, preferring weak/medium ties that cross topic
    boundaries. Surfaces adjacent ideas — the unseen connections.

    Usage:
      python -m cairn wander "what I'm working on"
      python -m cairn wander "the auth refactor" --hops=2 --k=15
    """
    from cairn.retrieve import drift_pack, render_drift
    hops, k, terms = 3, 10, []
    for a in args:
        if a.startswith("--hops="):
            hops = max(1, min(5, int(a.split("=")[1])))
        elif a.startswith("--k="):
            k = max(1, min(40, int(a.split("=")[1])))
        else:
            terms.append(a)
    if not terms:
        print("usage: cairn wander 'your topic' [--hops=3] [--k=10]")
        return
    print(render_drift(drift_pack(" ".join(terms), hops=hops, k=k)))


def cmd_ingest(args: list[str]) -> None:
    """
    Ingest project files into the vault as searchable chunks. Code/docs become
    queryable memory — fetch them instead of re-reading. Re-run anytime;
    unchanged files skip, changed files refresh.

    Usage:
      python -m cairn ingest C:\\path\\to\\project
      python -m cairn ingest server.py --project=myapp
      python -m cairn ingest . --dry-run
    """
    from pathlib import Path as _P
    from cairn.retrieve import ingest_path
    target = None
    project = None
    dry = False
    for a in args:
        if a.startswith("--project="):
            project = a.split("=")[1]
        elif a == "--dry-run":
            dry = True
        elif not a.startswith("--"):
            target = a
    if not target:
        print("usage: cairn ingest <path> [--project=name] [--dry-run]")
        return
    p = _P(target)
    if not p.exists():
        print(f"cairn: path not found — {target}")
        return
    print(f"cairn: ingesting {p}{' (DRY RUN)' if dry else ''}...")
    r = ingest_path(p, project=project, dry_run=dry)
    print(f"  project: {r['project']}")
    print(f"  files:   {r['files']} ingested, {r['skipped']} skipped, {r['updated']} updated")
    print(f"  chunks:  {r['chunks']}{' (would write)' if dry else ' written'}")
    if not dry and r["chunks"]:
        print(f"  next: python -m cairn embed   (vectorize {r['chunks']} chunks)")
        print(f"        then: python -m cairn fetch 'your question'")


def cmd_import(args: list[str]) -> None:
    """
    Bring your AI history home — import Claude/ChatGPT/Gemini data exports.

    Get the exports:
      Claude:  claude.ai -> Settings -> Privacy -> Export data
      ChatGPT: Settings -> Data Controls -> Export data (conversations.json)
      Gemini:  takeout.google.com -> Gemini Apps (JSON)

    Usage:
      python -m cairn import conversations.json --source=chatgpt
      python -m cairn import conversations.json --source=claude --since=2025-01-01
      python -m cairn import MyActivity.json --source=gemini --dry-run
      options: --tier=2 (default: cold/retrieval-only) --limit=N --dry-run

    After import: python -m cairn embed   (vectorize — may take a while)
                  python -m cairn sleep   (consolidate the history)
    """
    # `cairn import codex-sessions ...` → the plain-chat session-store reader
    # (distinct from the file-export importer below; same `import` verb for UX).
    if args and args[0] == "codex-sessions":
        return cmd_import_codex_sessions(args[1:])
    if args and args[0] == "local-agent-sessions":
        return cmd_import_local_agent_sessions(args[1:])

    from cairn.importer import import_export, EXTRACTORS

    path = source = account = None
    tier, limit, since, dry = 2, None, None, False
    for arg in args:
        if arg.startswith("--source="):
            source = arg.split("=")[1].lower()
        elif arg.startswith("--tier="):
            tier = int(arg.split("=")[1])
        elif arg.startswith("--limit="):
            limit = int(arg.split("=")[1])
        elif arg.startswith("--since="):
            since = arg.split("=")[1]
        elif arg == "--dry-run":
            dry = True
        elif arg.startswith("--account="):
            account = arg.split("=", 1)[1].strip()
        elif not arg.startswith("--"):
            path = arg

    if not path or source not in EXTRACTORS:
        print("usage: cairn import <export.json> --source=chatgpt|claude|gemini")
        print("       [--account=name] [--tier=2] [--limit=N] [--since=YYYY-MM-DD] [--dry-run]")
        sys.exit(1)
    if not Path(path).exists():
        print(f"cairn: file not found — {path}")
        sys.exit(1)

    label = f" as account '{account}'" if account else ""
    print(f"cairn: importing {source} export{label}{' (DRY RUN)' if dry else ''}...")
    r = import_export(Path(path), source, tier=tier, limit=limit,
                      since=since, dry_run=dry, account=account, progress=print)
    print(f"  conversations: {r['conversations']} imported, {r['skipped']} skipped (already in vault / filtered)")
    if r.get("resumed"):
        print(f"  resumed:       {r['resumed']} continued conversations — new tail turns imported")
    if r.get("shrunk"):
        print(f"  shrunk:        {r['shrunk']} skipped (export had fewer turns than the vault — nothing touched)")
    print(f"  turns:         {r['turns']} nodes{' (would be written)' if dry else ''}")
    if r.get("dropped"):
        print(f"  dropped:       {r['dropped']} export-cruft turns filtered at import")
    if r["sessions"][:3]:
        for s in r["sessions"][:3]:
            print(f"    {s}")
        if len(r["sessions"]) > 3:
            print(f"    ... +{len(r['sessions']) - 3} more")
    if not dry and r["turns"]:
        print(f"  next: python -m cairn embed   ({r['turns']} nodes to vectorize)")
        print(f"        python -m cairn sleep   (consolidate the history)")


def cmd_import_local_agent_sessions(args: list[str]) -> None:
    """
    Import Claude Desktop "local agent mode" chat (Cowork / Dispatch-from-phone)
    from the on-disk transcript store into the vault.

    Cowork/Dispatch runs a sandboxed nested agent with its OWN isolated .claude
    dir, so the global Stop hook never fires for it — its turns are hook-less.
    But it writes a standard Claude-Code transcript JSONL, which this reads
    (READ-ONLY) and files as local-agent-<session> conversation_turn nodes,
    deduped by turn:<uuid> so a re-run — or a future watcher — never doubles.
    Brief-mode runtime nudges (+ their acks) and bare pleasantries are filtered.

    DRY-RUN by default. --apply writes (a reversible manifest is saved first).
    FORWARD-ONLY by default: a watermark set on first --apply splits history from
    new-going-forward; backfill history with --include-before.

    Usage:
      python -m cairn import local-agent-sessions                  # dry-run preview
      python -m cairn import local-agent-sessions --apply          # write forward turns
      python -m cairn import local-agent-sessions --include-before=2026-07-01 --apply
      options: --root=PATH --account=name --since=YYYY-MM-DD --tier=2 --limit=N --debug
    """
    from cairn.local_agent_reader import read_local_agent_sessions

    root = account = include_before = since = None
    tier, limit = 2, None
    apply = "--apply" in args
    debug = "--debug" in args
    for arg in args:
        if arg.startswith("--root="):
            root = arg.split("=", 1)[1].strip()
        elif arg.startswith("--account="):
            account = arg.split("=", 1)[1].strip()
        elif arg.startswith("--include-before="):
            include_before = arg.split("=", 1)[1].strip()
        elif arg.startswith("--since="):
            since = arg.split("=", 1)[1].strip()
        elif arg.startswith("--tier="):
            try:    tier = int(arg.split("=", 1)[1])
            except Exception: pass
        elif arg.startswith("--limit="):
            try:    limit = int(arg.split("=", 1)[1])
            except Exception: pass

    r = read_local_agent_sessions(root=root, account=account, tier=tier, since=since,
                                  include_before=include_before, dry_run=not apply,
                                  limit=limit, debug=debug)

    head = "APPLY" if apply else "DRY-RUN (read-only)"
    acct = f"{r['account']} (LOCKED)" if r["account"] else "Claude / by-model (auto)"
    print(f"local-agent (Cowork/Desktop) import — {head}")
    print(f"  store:   {r['root']}")
    print(f"  account: {acct}")
    print(f"  scanned: {r['files_scanned']} transcript file(s)")
    if not r["files_scanned"]:
        print("  (no transcripts found — is Claude Desktop / Cowork installed, or is --root correct?)")
        return
    span = f"{(r['date_min'] or '?')[:16]} .. {(r['date_max'] or '?')[:16]}"
    print(f"  threads: {r['threads_found']} session(s)   span: {span}")
    print(f"\n  NEW GOING FORWARD (imports on --apply): {r['forward_new']} turns "
          f"({r['forward_user']} user / {r['forward_agent']} agent)")
    print(f"  HISTORY ON DISK (NOT imported unless --include-before): "
          f"{r['historical_turns']} turns")
    if include_before:
        print(f"    → --include-before={include_before}: {r['historical_new']} historical "
              f"turns WILL import on --apply")
    print(f"  already captured (turn:<id> present): {r['already_captured']} — skipped")
    if r["bad_lines"] or r["dropped"]:
        print(f"  skipped: {r['bad_lines']} unparseable lines · {r['dropped']} "
              f"empty/filtered (brief-mode nudges, pleasantries)")
    if r.get("truncated_files"):
        print(f"  ⚠ {r['truncated_files']} file(s) hit a read error mid-file — re-run to "
              f"finish (dedup makes it safe).")
    if r["preview"]:
        p = r["preview"]
        print(f"  preview turn {p['turn_id']}:  user \"{p['user']}\"  ->  agent \"{p['agent']}\"")
    if r["first_run"] and r["provisional_watermark"]:
        print("\n  NOTE: first import on this machine — the forward watermark is set to NOW on"
              "\n        --apply; existing history stays on disk untouched unless you pass"
              "\n        --include-before=YYYY-MM-DD.")
    if not apply:
        print("\n  DRY-RUN — nothing changed. Re-run with --apply to write the NEW-going-forward"
              "\n  set (a reversible manifest is saved to ~/.cairn/ first). Backfill history with"
              "\n  --include-before=YYYY-MM-DD.")
        return
    print(f"\n  APPLIED: {r['written_nodes']} nodes written "
          f"({r['written_user']} user / {r['written_agent']} agent) "
          f"across {r['sessions_written']} session(s).")
    if r["backup"]:
        print(f"  manifest: {Path(r['backup']).name}  (added node ids — void to reverse; append-only)")
    if r["written_nodes"]:
        print(f"  next: python -m cairn embed   ({r['written_nodes']} nodes to vectorize)")


def cmd_import_codex_sessions(args: list[str]) -> None:
    """
    Import PLAIN Codex/GPT chat from the on-disk session store into the vault.

    The notify hook captures only agentic / notify-fired turns — Codex never fires
    notify for plain conversational chat. That chat IS on disk, in
    ~/.codex/sessions/**/rollout-*.jsonl. This reads it (READ-ONLY) and files it as
    codex-<thread> conversation_turn nodes, deduped against the hook so the two
    never double-capture. This is the third capture path, distinct on purpose:
      cairn_note = salience · notify = agentic events · this = full plain chat.

    DRY-RUN by default — prints scope/counts, changes nothing. --apply writes (a
    reversible manifest is saved to ~/.cairn/ first). FORWARD-ONLY by default: a
    watermark set on first --apply separates history-on-disk from new-going-forward;
    historical backfill is an explicit --include-before opt-in.

    Usage:
      python -m cairn import codex-sessions                  # dry-run preview
      python -m cairn import codex-sessions --apply          # write forward turns
      python -m cairn import codex-sessions --account=name   # stamp + lock account
      python -m cairn import codex-sessions --include-before=2026-07-01 --apply
      options: --root=PATH --since=YYYY-MM-DD --tier=2 --limit=N --debug
    """
    from cairn.codex_reader import read_codex_sessions

    root = account = include_before = since = None
    tier, limit = 2, None
    apply = "--apply" in args
    debug = "--debug" in args
    for arg in args:
        if arg.startswith("--root="):
            root = arg.split("=", 1)[1].strip()
        elif arg.startswith("--account="):
            account = arg.split("=", 1)[1].strip()
        elif arg.startswith("--include-before="):
            include_before = arg.split("=", 1)[1].strip()
        elif arg.startswith("--since="):
            since = arg.split("=", 1)[1].strip()
        elif arg.startswith("--tier="):
            try:    tier = int(arg.split("=", 1)[1])
            except Exception: pass
        elif arg.startswith("--limit="):
            try:    limit = int(arg.split("=", 1)[1])
            except Exception: pass

    r = read_codex_sessions(root=root, account=account, tier=tier, since=since,
                            include_before=include_before, dry_run=not apply,
                            limit=limit, debug=debug)

    head = "APPLY" if apply else "DRY-RUN (read-only)"
    acct = f"{r['account']} (LOCKED)" if r["account"] else "codex identity (auto)"
    print(f"codex session-store import — {head}")
    print(f"  store:   {r['root']}")
    print(f"  account: {acct}")
    print(f"  scanned: {r['files_scanned']} rollout files across {r['date_dirs']} date dirs")
    if not r["files_scanned"]:
        print("  (no rollout files found — is Codex installed / is --root correct?)")
        return
    span = f"{(r['date_min'] or '?')[:16]} .. {(r['date_max'] or '?')[:16]}"
    print(f"  threads: {r['threads_found']} found "
          f"({r['threads_new']} with new turns, {r['threads_fully_captured']} already captured)")
    print(f"  span:    {span}")

    print(f"\n  NEW GOING FORWARD (imports on --apply): {r['forward_new']} turns "
          f"({r['forward_user']} user / {r['forward_agent']} agent) "
          f"across {r['forward_threads']} threads")
    hist_span = (f"{(r['hist_min'] or '?')[:16]} .. {(r['hist_max'] or '?')[:16]}"
                 if r["historical_turns"] else "—")
    print(f"  HISTORY ON DISK (backfill — NOT imported unless --include-before): "
          f"{r['historical_turns']} turns, {hist_span}")
    if include_before:
        print(f"    → --include-before={include_before}: {r['historical_new']} historical "
              f"turns WILL import on --apply")
    print(f"  already captured (turn:<id> present, e.g. by the notify hook): "
          f"{r['already_captured']} turns — skipped")
    if r["compacted"] or r["bad_lines"] or r["dropped"]:
        print(f"  skipped: {r['compacted']} compacted (correction history) · "
              f"{r['bad_lines']} unparseable lines · {r['dropped']} empty/out-of-scope")
    if r.get("truncated_files"):
        print(f"  ⚠ {r['truncated_files']} file(s) hit a read error mid-file — partial this "
              f"run; re-run to pick up the rest (dedup makes it safe).")

    if r["samples"]:
        print("\n  sample threads: " + ", ".join(r["samples"]))
    if r["preview"]:
        p = r["preview"]
        print(f"  preview turn {p['turn_id']}:  user \"{p['user']}\"  ->  agent \"{p['agent']}\"")

    if r["first_run"] and r["provisional_watermark"]:
        print("\n  NOTE: first import on this machine — the forward watermark is set to NOW on"
              "\n        --apply, so existing history stays on disk untouched unless you pass"
              "\n        --include-before=YYYY-MM-DD.")

    if not apply:
        print("\n  DRY-RUN — nothing changed. Re-run with --apply to write the NEW-going-forward"
              "\n  set (a reversible manifest is saved to ~/.cairn/ first). Backfill history with"
              "\n  --include-before=YYYY-MM-DD.")
        print("  KNOWN LIMIT: one account only — the store has no per-record account id, so a"
              "\n  second OpenAI login on this machine can't be told apart (all → one account).")
        return

    print(f"\n  APPLIED: {r['written_nodes']} nodes written "
          f"({r['written_user']} user / {r['written_agent']} agent).")
    if r["backup"]:
        print(f"  manifest: {Path(r['backup']).name}  (added node ids — void to reverse; append-only)")
    if r["written_nodes"]:
        print(f"  next: python -m cairn embed   ({r['written_nodes']} nodes to vectorize)")





def cmd_edges(args: list[str]) -> None:
    """
    Rebuild the typed edge graph + topic communities.

    Usage:
      python -m cairn edges            # full rebuild (chain/dendrite/semantic kNN)
      python -m cairn edges --k=8      # keep more neighbors per node

    Edge types: chain (parent lineage), dendrite (consolidation), semantic
    (embedding kNN tiered strong/medium/weak). Communities via label
    propagation land in nodes.community as 'c<n>|<label>'.
    Derived data — rerun any time; cairn sleep runs it nightly.
    """
    k = 6
    for a in args:
        if a.startswith("--k="):
            try:
                k = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                pass
    t0 = __import__("time").perf_counter()
    from cairn.edges import build_all
    rep = build_all(Vault(), k=k)
    ms = int((__import__("time").perf_counter() - t0) * 1000)
    print(f"cairn: edge graph rebuilt ({ms}ms)")
    print(f"  semantic:    {rep['semantic']} edges over {rep['embedded']} embedded nodes "
          f"(strong {rep['strong']} / medium {rep['medium']} / weak {rep['weak']})")
    print(f"  chain:       {rep['chain']}")
    print(f"  dendrite:    {rep['dendrite']}")
    print(f"  communities: {rep['communities']} topics, {rep['labeled_nodes']} nodes named "
          f"(largest {rep['largest']})")
    for i, name in enumerate(rep.get("names", [])):
        print(f"    c{i}: {name}")


def cmd_book(args: list[str]) -> None:
    """
    Regenerate the Book — BOOK.md (human contents, <=150 lines) and
    PAGE_ONE.md (model orientation head) in ~/.cairn. Pointer-only.
    Runs nightly inside cairn sleep; run manually anytime.

    Usage:  python -m cairn book [--show]
    """
    from cairn.book import write_book, page_one
    v = Vault()
    rep = write_book(v)
    print(f"cairn: book written — {rep['book']} ({rep['book_lines']} lines)")
    print(f"       page one     — {rep['page_one']}")
    if "--show" in args:
        print()
        print(page_one(v))


# ── capture scope helpers (off / per-project / global) ───────────────────────
def _global_settings_path() -> Path:
    """Claude Code's user-level settings — hooks here fire in EVERY project."""
    return Path.home() / ".claude" / "settings.json"


def _is_global_connected() -> bool:
    """True if FULL global capture is wired (the PostToolUse hook.py), not just a
    global `cairn orient` line. Used to warn against per-project double-write."""
    import json as _json
    f = _global_settings_path()
    if not f.exists():
        return False
    try:
        hooks = _json.loads(f.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return False
    dump = _json.dumps(hooks).lower()
    return "hook.py" in dump and "cairn" in dump


def _connect_prompt() -> str:
    """Ask a human where capture should live. Returns 'project'|'global'|'once'|
    'skip'. Enter defaults to per-project — the historical `cairn connect`."""
    print("Cairn — turn on ambient memory. Where should it capture?\n")
    print("  [P] This project (default)   every chat in THIS folder")
    print("  [G] Everywhere               every Claude Code chat on this machine")
    print("  [O] Just one chat            (how to do that honestly — nothing gets wired)")
    print("  [S] Not now                  set nothing up\n")
    print("  Enter = P. Change scope anytime: `cairn connect` / `cairn disconnect`.")
    print("  Keep one chat OUT later? set CAIRN_CAPTURE=0 in that shell.\n")
    try:
        ans = input("Choice [P/g/o/s]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "skip"
    if ans in ("g", "global", "everywhere"):
        return "global"
    if ans in ("o", "once", "one"):
        return "once"
    if ans in ("s", "skip", "n", "no"):
        return "skip"
    return "project"


def cmd_connect(args: list[str]) -> None:
    """
    Opt in to ambient capture + orient. THREE scopes, OFF by default — the
    user's call. Privacy is the point: nothing is captured unless you ask.
    Run with no scope at a terminal and it ASKS (project / global / skip);
    non-interactive (CI, hooks, pipes) or with any flag it stays scripted.

      python -m cairn connect              # this project only (per-project)
      python -m cairn connect <path>       # another project
      python -m cairn connect --global     # EVERY Claude Code chat on this machine
      python -m cairn connect --no-rules   # skip the AGENTS.md/etc. shims

    Global and per-project are mutually exclusive (both on = double-write):
    connecting a project while global is on is refused (--force overrides).
    Merge-safe — non-cairn settings/hooks are preserved.

    Each connected scope wires: SessionStart=orient · PostToolUse=tool capture ·
    Stop=conversation capture · PreCompact=checkpoint · SessionEnd=compile+embed.
    """
    import json as _json
    is_global = "--global" in args
    force     = "--force" in args

    # Interactive scope pick — ONLY for a human at a TTY who named no scope.
    # Non-TTY (CI, hooks, pipes) and any explicit flag/path keep the historical
    # behavior (per-project default), so nothing scripted ever changes.
    _explicit = (is_global
                 or any(not a.startswith("--") for a in args)
                 or "--yes" in args or "-y" in args
                 or "--project" in args or "--here" in args)
    if not _explicit and sys.stdin.isatty():
        _pick = _connect_prompt()
        if _pick == "skip":
            print("cairn: nothing set up. Run `cairn connect` whenever you like.")
            return
        if _pick == "once":
            print("cairn: Claude Code applies hooks per SETTINGS FILE, not per")
            print("       conversation — so there's no true one-chat hook. Two honest")
            print("       ways to keep memory to a single chat:")
            print("         1. Don't connect — just use Cairn's tools this session")
            print("            (MCP: python -m cairn mcp  ·  or `cairn note` / `cairn fetch`).")
            print("         2. `cairn connect` now, then `cairn disconnect` when done.")
            print("       Nothing was changed.")
            return
        if _pick == "global":
            is_global = True
        # "project" falls through to the per-project default below

    # conflict guard — per-project while global is already on would double-write
    if not is_global and _is_global_connected() and not force:
        print("cairn: GLOBAL capture is already on — connecting this project too would")
        print("       DOUBLE-WRITE every tool call. Skipped. Pick one:")
        print("       • leave global on (this project is already covered), or")
        print("       • `cairn disconnect --global` then `cairn connect` for per-project, or")
        print("       • `cairn connect --force` to override (not recommended).")
        return

    if is_global:
        sfile = _global_settings_path()
        sfile.parent.mkdir(parents=True, exist_ok=True)
        scope = "GLOBAL — every Claude Code chat on this machine"
        root = None
    else:
        root = Path(next((a for a in args if not a.startswith("--")), ".")).resolve()
        if not root.is_dir():
            print(f"cairn: not a directory — {root}")
            return
        (root / ".claude").mkdir(exist_ok=True)
        sfile = root / ".claude" / "settings.json"
        scope = f"'{root.name}' (per-project)"

    settings = {}
    if sfile.exists():
        try:
            settings = _json.loads(sfile.read_text(encoding="utf-8"))
        except Exception:
            print(f"cairn: {sfile} exists but is not valid JSON — fix it first, "
                  "not overwriting")
            return

    py    = sys.executable
    croot = Path(__file__).parent
    def H(script: str, t: int) -> dict:
        return {"type": "command",
                "command": f'"{py}" -X utf8 "{croot / script}"', "timeout": t}
    def CLI(sub: str, t: int) -> dict:
        return {"type": "command",
                "command": f'"{py}" -X utf8 -m cairn {sub}', "timeout": t}

    hooks = settings.setdefault("hooks", {})
    def merge(event: str, entries: list) -> None:
        # Clean at the INNER-hook level so a group that mixes a cairn hook with
        # someone else's (e.g. SessionStart = [orient, show_inbox]) keeps the
        # non-cairn one. Drop emptied groups, then append our fresh group.
        cleaned = []
        for g in hooks.get(event, []):
            inner = [hk for hk in g.get("hooks", []) if "cairn" not in _json.dumps(hk).lower()]
            if inner:
                g = dict(g); g["hooks"] = inner
                cleaned.append(g)
        hooks[event] = cleaned + entries

    merge("SessionStart",     [{"hooks": [CLI("orient", 30)]}])
    merge("UserPromptSubmit", [{"hooks": [H("prompt_hook.py", 10)]}])  # capture prompt at SEND time (right order)
    merge("PostToolUse",      [{"matcher": "*", "hooks": [H("hook.py", 10)]}])
    merge("Stop",             [{"hooks": [H("turn_hook.py", 15)]}])
    merge("PreCompact",       [{"hooks": [H("compact_hook.py", 30)]}])
    merge("SessionEnd",       [{"hooks": [H("stop_hook.py", 120)]}])
    sfile.write_text(_json.dumps(settings, indent=2), encoding="utf-8")

    print(f"cairn: connected {scope}")
    print("       captures prompts + tool calls + conversation · orients at start · compiles+embeds at end")
    print(f"       ({sfile})")

    # ── per-tool rules shims (per-project only — they're files in the dir) ────
    # Claude is covered by the hooks above. Other agents (Codex, Gemini, Cursor,
    # Windsurf) read their own rules file — drop a SELF-CONTAINED Cairn block.
    if root is not None and "--no-rules" not in args:
        # v3 marker: bumping the version makes re-connect REFRESH a stale block
        # in place (markers preserved) instead of skipping it forever.
        block = (
            "<!-- cairn:start v3 -->\n"
            "## Cairn memory (local-first)\n"
            "This project uses Cairn. Before working:\n"
            "1. `python -m cairn doctor` — act ONLY on its ✗/⚠ items; never recreate ✓ items.\n"
            "   (Its MCP line reads Claude Desktop's config only — never \"fix\" a Codex wire\n"
            "   because of it; check `codex mcp list` / `cairn codex-hook status` instead.)\n"
            "   If it says the vault is EMPTY and you're a remote/cloud agent, do NOT import\n"
            "   history or assume context — ask the user (the real vault is on their machine).\n"
            "2. `python -m cairn orient` — load inherited context before your first action.\n"
            "   New chat = new session automatically; orient only reads the carryover. No\n"
            "   hooks in your runtime? run `python -m cairn session --new` once before you note.\n"
            "3. `python -m cairn fetch \"question\"` instead of re-reading files/history.\n"
            "4. Fetch/search return GISTS — an index, not the text. Before relying on a\n"
            "   summary, `python -m cairn read <id>` prints the node IN FULL.\n"
            "Capture (Claude does this automatically via hooks; other agents by hand),\n"
            "by SALIENCE not quota — some exchanges = 0 nodes, some = several:\n"
            "  `python -m cairn note --kind=decision|warning|open_item|insight|resolved \"...\"`\n"
            "  `python -m cairn note --speaker=user \"what the user wants\"`\n"
            "  Write the complete salient fact without truncating it — but don't paste\n"
            "  transcripts (turns are captured separately); large artifact → save the\n"
            "  file, note its path and purpose.\n"
            "Laws: local-first (nothing leaves the machine) · append-only (void, never delete).\n"
            "<!-- cairn:end -->\n"
        )
        targets = ["AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules"]
        wrote = []
        for t in targets:
            f = root / t
            try:
                # BYTE-preserving splice: these files belong to the USER, so
                # everything outside the marked cairn block must survive
                # byte-identical — newline style (LF vs CRLF) and any BOM
                # included. Text-mode write_text would rewrite the whole file
                # in the platform's newline style; read/write bytes instead,
                # and render the block in the file's own newline convention.
                raw = f.read_bytes() if f.exists() else b""
                bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
                try:
                    existing = raw[len(bom):].decode("utf-8")
                except UnicodeDecodeError:
                    # Not UTF-8 (UTF-16, legacy codepage, …). errors="replace"
                    # here would silently mangle a USER-owned file on rewrite
                    # (measured: a 132-byte UTF-16 file ballooned to 1,520 bytes
                    # of replacement chars). Fail CLOSED: leave the file
                    # byte-for-byte untouched and say so.
                    print(f"       rules shim SKIPPED: {t} is not UTF-8 — "
                          f"left untouched (add the cairn block by hand if wanted)")
                    continue
                nl = "\r\n" if "\r\n" in existing else "\n"
                blk = block if nl == "\n" else block.replace("\n", "\r\n")
                if "cairn:start v3" in existing:
                    continue  # current block already present
                start = existing.find("<!-- cairn:start")
                end_m = existing.find("<!-- cairn:end -->")
                if start != -1 and end_m > start:
                    # stale versioned block — refresh it in place
                    end = end_m + len("<!-- cairn:end -->")
                    if existing[end:end + len(nl)] == nl:
                        end += len(nl)
                    f.write_bytes(bom + (existing[:start] + blk
                                         + existing[end:]).encode("utf-8"))
                    wrote.append(f"{t} (refreshed)")
                    continue
                if "Cairn memory" in existing:
                    continue  # hand-rolled block without markers — leave it alone
                sep = (nl * 2) if existing.strip() else ""
                f.write_bytes(bom + (existing + sep + blk).encode("utf-8"))
                wrote.append(t)
            except Exception:
                pass
        if wrote:
            print(f"       rules shims: {', '.join(wrote)} (Codex/Gemini/Cursor/Windsurf → run doctor)")

    print("       mute one chat: set CAIRN_CAPTURE=0 (PowerShell: $env:CAIRN_CAPTURE=\"0\")   ·   pause all: cairn capture off")
    print(f"       disconnect: cairn disconnect{' --global' if is_global else ''}")


def cmd_disconnect(args: list[str]) -> None:
    """
    Remove cairn hooks — the clean reverse of `connect`.
      python -m cairn disconnect            # this project
      python -m cairn disconnect <path>     # another project
      python -m cairn disconnect --global   # machine-wide
    Rules shims (AGENTS.md etc.) are left in place — delete their
    <!-- cairn:start -->..<!-- cairn:end --> block by hand if you want them gone.
    """
    import json as _json
    is_global = "--global" in args
    if is_global:
        sfile = _global_settings_path()
    else:
        root = Path(next((a for a in args if not a.startswith("--")), ".")).resolve()
        sfile = root / ".claude" / "settings.json"
    if not sfile.exists():
        print(f"cairn: nothing to disconnect — {sfile} not found")
        return
    try:
        settings = _json.loads(sfile.read_text(encoding="utf-8"))
    except Exception:
        print(f"cairn: {sfile} is not valid JSON — leaving it alone")
        return
    hooks = settings.get("hooks", {})
    removed = 0
    for event in list(hooks.keys()):
        cleaned = []
        for g in hooks[event]:
            inner = g.get("hooks", [])
            kept_inner = [hk for hk in inner if "cairn" not in _json.dumps(hk).lower()]
            removed += len(inner) - len(kept_inner)
            if kept_inner:
                g = dict(g); g["hooks"] = kept_inner
                cleaned.append(g)
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    sfile.write_text(_json.dumps(settings, indent=2), encoding="utf-8")
    scope = "global" if is_global else f"'{sfile.parent.parent.name}'"
    print(f"cairn: disconnected {scope} — removed {removed} cairn hook(s)")
    print(f"       ({sfile})")


def cmd_capture(args: list[str]) -> None:
    """
    Pause/resume capture without disconnecting.
      python -m cairn capture            # status
      python -m cairn capture off        # pause capture everywhere (marker file)
      python -m cairn capture on         # resume
    Per-CHAT mute (leave global on, skip just this session): set CAIRN_CAPTURE=0
    in that shell's environment.
    """
    marker = Path.home() / ".cairn" / "CAPTURE_OFF"
    marker.parent.mkdir(parents=True, exist_ok=True)
    sub = (args[0] if args else "status").lower()
    if sub == "off":
        marker.write_text("paused")
        print("cairn: capture PAUSED everywhere — `cairn capture on` to resume")
    elif sub == "on":
        if marker.exists():
            marker.unlink()
        print("cairn: capture resumed")
    else:
        muted = marker.exists() or os.environ.get("CAIRN_CAPTURE") == "0"
        print(f"cairn: capture is {'MUTED' if muted else 'ON'}")
        print("       cairn capture off|on   ·   per-chat: set CAIRN_CAPTURE=0")


def cmd_doc(args: list[str]) -> None:
    """
    The card catalog — register an important document so any model (and you)
    can find it instantly: what it is, what it's FOR, when it was made.

    Usage:
      python -m cairn doc add <path> "what this document is for" [--project=tag]
      python -m cairn doc list [project]

    Cards are first-class artifact nodes: embedded (semantic search finds
    "the file about X"), on the atlas, served over MCP, importance 8.
    Re-registering a path replaces its card (old card voided, lineage kept).
    """
    if not args or args[0] not in ("add", "list"):
        print("usage: cairn doc add <path> 'purpose' [--project=tag]")
        print("       cairn doc list [project]")
        return
    v = Vault()

    if args[0] == "list":
        proj = args[1] if len(args) > 1 else None
        rows = v.conn.execute(
            "SELECT * FROM nodes WHERE status='active' AND kind='artifact' "
            "ORDER BY timestamp DESC").fetchall()
        shown = 0
        for r in rows:
            tags = r["tags"] or "[]"
            if proj and f'"{proj}"' not in tags:
                continue
            import json as _json
            t = _json.loads(tags)
            path = next((x[5:] for x in t if x.startswith("file:")), "?")
            made = next((x[5:] for x in t if x.startswith("made:")), "?")
            print(f"  {(r['query'] or '')[5:]}")
            print(f"      made {made} · {path}")
            shown += 1
        print(f"cairn: {shown} document card(s)" + (f" in '{proj}'" if proj else ""))
        return

    path_arg = next((a for a in args[1:] if not a.startswith("--")), None)
    purpose  = next((a for a in args[2:] if not a.startswith("--")), "")
    project  = next((a.split("=", 1)[1] for a in args
                     if a.startswith("--project=")), None)
    if not path_arg or not purpose:
        print("usage: cairn doc add <path> 'purpose' [--project=tag]")
        return
    p = Path(path_arg).resolve()
    if not p.exists():
        print(f"cairn: file not found — {p}")
        return

    from datetime import datetime as _dt
    made = _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
    tags = ["doc", f"file:{p}", f"made:{made}"] + ([project] if project else [])

    # one live card per path — re-registering replaces (void keeps history)
    old = v.conn.execute(
        "SELECT id FROM nodes WHERE status='active' AND kind='artifact' "
        "AND tags LIKE ?", (f'%"file:{p}"%',)).fetchall()
    for o in old:
        v.void(o["id"])

    node = v.write(MicroNode(
        session     = get_session(),
        kind        = "artifact",
        query       = f"DOC: {p.name} — {purpose}",
        output_preview = f"{p}  (made {made}, {p.stat().st_size:,} bytes)",
        model       = "human",
        speaker     = "user",
        agent_role  = "curator",
        memory_tier = 1,
        tags        = tags,
    ))
    print(f"cairn: cataloged [{node.id}] {p.name}")
    print(f"       purpose: {purpose}")
    print(f"       made {made}" + (f" · project {project}" if project else "") +
          (f" · replaced {len(old)} old card(s)" if old else ""))
    print("       (embeds on next 'cairn embed' / sleep — then findable by meaning)")


def cmd_lessons(args: list[str]) -> None:
    """
    Mine struggle->resolution pairs into procedure lessons — the
    fail->investigate->distill loop, deterministic and governed: lessons are
    NEW nodes layered on top (append-only), never rewrites, every lesson tagged
    with the struggle and resolution it came from (full provenance).

    Usage:
      python -m cairn lessons            # dry run — show candidates
      python -m cairn lessons --write    # mint procedure nodes (idempotent)
    """
    import re as _re
    write = "--write" in args
    v = Vault()

    def kw(t):
        return set(_re.findall(r"[a-z0-9_\-\.]{4,}", (t or "").lower()))

    strugs = v.conn.execute("""
        SELECT id, session, query, timestamp FROM nodes
        WHERE status='active' AND kind='tool_call'
          AND (result_count = 0 OR latency_ms > 2000)""").fetchall()
    res = v.conn.execute("""
        SELECT id, session, query, gist, timestamp FROM nodes
        WHERE status='active' AND kind IN ('resolved', 'decision')""").fetchall()
    by_sess: dict = {}
    for r in res:
        by_sess.setdefault(r["session"], []).append(r)

    pairs = []
    for s in strugs:
        ks = kw(s["query"])
        if not ks:
            continue
        best, bs = None, 0.0
        for r in by_sess.get(s["session"], []):
            if (r["timestamp"] or "") <= (s["timestamp"] or ""):
                continue   # resolution must come AFTER the struggle
            overlap = len(ks & kw((r["gist"] or "") + " " + (r["query"] or "")))
            score = overlap / max(3, len(ks))
            if score > bs:
                bs, best = score, r
        if best is not None and bs >= 0.34:
            pairs.append((s, best, bs))

    keep: dict = {}   # one lesson per resolution — the strongest struggle wins
    for s, r, sc in pairs:
        if r["id"] not in keep or keep[r["id"]][2] < sc:
            keep[r["id"]] = (s, r, sc)
    pairs = sorted(keep.values(), key=lambda p: -p[2])

    print(f"cairn: {len(pairs)} struggle->resolution lessons found")
    for s, r, sc in pairs[:12]:
        print(f"  [{sc:.2f}] struggled: {(s['query'] or '')[:54]}")
        print(f"        resolved:  {(r['gist'] or r['query'] or '')[:72]}")

    if write and pairs:
        n = 0
        for s, r, sc in pairs:
            if v.conn.execute(
                    "SELECT 1 FROM nodes WHERE status='active' AND tags LIKE ?",
                    (f'%"lesson:{r["id"]}"%',)).fetchone():
                continue   # already minted — idempotent
            v.write(MicroNode(
                session     = r["session"],
                kind        = "procedure",
                query       = (f"LESSON: when '{(s['query'] or '')[:80]}' struggles, "
                               f"the path that worked: "
                               f"{(r['gist'] or r['query'] or '')[:160]}"),
                model       = "cairn-miner",
                agent_role  = "consolidator",
                memory_tier = 1,
                tags        = ["lesson", f"lesson:{r['id']}", f"from:{s['id']}"],
            ))
            n += 1
        print(f"cairn: wrote {n} procedure lessons (tag 'lesson') — "
              f"they now rotate through injection like any memory")
    elif pairs:
        print("  (dry run — add --write to mint procedure nodes)")


def cmd_sleep(args: list[str]) -> None:
    """
    The sleep cycle — nightly maintenance. Zero tokens, no model required.

    Runs the full biological night, eight stages: embed everything pending (so
    dreams have material), consolidate (REM — episodic -> semantic
    synthesis), demote stale warm memories to cold (synaptic pruning), build
    the typed edge graph + topic communities, regenerate the book
    (BOOK.md + PAGE_ONE.md), recompile PROTOCOL.md (so the morning starts
    oriented), run the structural audit, and compile the FINISH-LINES registry.

    Model-agnostic by construction: this is pure Python on the machine's
    clock, not any vendor's scheduler. Register with the OS:

      Windows:  schtasks /create /tn "Cairn Sleep" /sc daily /st 02:00 /tr
                "<python.exe> -X utf8 -m cairn sleep"
      Linux/Mac cron:  0 2 * * * python3 -m cairn sleep

    Usage:
      python -m cairn sleep
      python -m cairn sleep --dry-run    # show what would consolidate
    """
    dry = "--dry-run" in args
    t0 = __import__("time").perf_counter()
    vault = Vault()
    print(f"cairn: sleep cycle starting{' (DRY RUN)' if dry else ''}...")

    # 1. embed pending — dreams need material
    try:
        n = vault.embed_pending()
        print(f"  embed:       {n} nodes vectorized")
    except ImportError:
        print("  embed:       skipped (sentence-transformers not installed)")
    except Exception as e:
        print(f"  embed:       error — {e}")

    # 2. REM — consolidate episodes into insights/procedures
    try:
        from cairn.consolidate import consolidate
        r = consolidate(vault, dry_run=dry)
        print(f"  consolidate: {r['clusters']} clusters -> "
              f"{r['insights']} insights, {r['procedures']} procedures "
              f"({r['members_demoted']} members archived to cold)")
    except Exception as e:
        print(f"  consolidate: error — {e}")

    # 3. synaptic pruning — stale warm memories drop to cold
    try:
        from cairn.schedule import demote_cold
        sessions = vault.all_sessions()
        current  = sessions[0]["id"] if sessions else ""
        nodes    = vault.session_nodes(current) if current else []
        refs     = {r["parent"] for r in nodes if r["parent"]}
        demoted  = demote_cold(vault, current, refs, min_sessions_old=3) if current else 0
        print(f"  prune:       {demoted} stale warm nodes -> cold")
    except Exception as e:
        print(f"  prune:       error — {e}")

    # 3.5 connective tissue — typed edge graph + topic communities
    try:
        from cairn.edges import build_all
        rep = build_all(vault)
        print(f"  edges:       {rep['semantic']} semantic "
              f"({rep['strong']}/{rep['medium']}/{rep['weak']} s/m/w) + "
              f"{rep['chain']} chain + {rep['dendrite']} dendrite -> "
              f"{rep['communities']} topics ({rep['labeled_nodes']} nodes named)")
    except Exception as e:
        print(f"  edges:       error — {e}")

    # 3.6 the book — regenerate BOOK.md + PAGE_ONE.md from tonight's state
    try:
        from cairn.book import write_book
        rep = write_book(vault)
        print(f"  book:        BOOK.md ({rep['book_lines']} lines) + PAGE_ONE.md refreshed")
    except Exception as e:
        print(f"  book:        error — {e}")

    # 4. morning preparation — recompile latest PROTOCOL.md (ticks the
    #    golden-angle feedback loop + FSRS stability as a side effect)
    try:
        from cairn.compile import compile_session
        if current:
            out = compile_session(vault, current,
                                  Path.home() / ".cairn" / "protocols" / current)
            print(f"  compile:     {out.name} refreshed for session '{current[:40]}'")
    except Exception as e:
        print(f"  compile:     error — {e}")

    # 5. the audit organ — zero-token immune checks; findings land on the
    #    Desk as ONE warning node (only when they exist and changed).
    try:
        from cairn.audit import audit, write_report
        findings = audit(vault)
        nid = write_report(vault, findings) if not dry else None
        if findings:
            print(f"  audit:       {len(findings)} finding(s)"
                  + (f" -> desk warning {nid[:12]}" if nid else " (unchanged/dry — not rewritten)"))
        else:
            print("  audit:       clean — no structural findings")
    except Exception as e:
        print(f"  audit:       error — {e}")

    # 6. finish lines — compile the registry ledger to FINISH-LINES.md
    #    (nodes canonical, file derived — no project ever silently dropped)
    try:
        from cairn.registry import compile_finish_lines
        n = compile_finish_lines(vault)
        print(f"  registry:    FINISH-LINES.md compiled ({n} rows)")
    except Exception as e:
        print(f"  registry:    error — {e}")

    ms = int((__import__("time").perf_counter() - t0) * 1000)
    print(f"cairn: sleep cycle complete ({ms}ms) — the garden grew overnight")


def cmd_audit(args: list[str]) -> None:
    """
    The audit organ, on demand — the same zero-token structural checks the
    nightly sleep runs: stale open items, attribution drift, impossible
    dates, attention hoarding, unembedded meaning nodes, ghost projects,
    voided-but-scheduled. Read-only unless --write.

    Usage:
      python -m cairn audit            # print findings, write nothing
      python -m cairn audit --write    # also file the Desk warning node
    """
    from cairn.audit import audit, write_report
    vault = Vault()
    findings = audit(vault)
    if not findings:
        print("cairn audit: clean — no structural findings")
        return
    print(f"cairn audit: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    if "--write" in args:
        nid = write_report(vault, findings)
        print(f"  -> desk warning {nid}" if nid
              else "  -> unchanged since last report; not rewritten")


def cmd_dashboard(args: list[str]) -> None:
    """
    Launch the real-time Cairn dashboard.
    http://localhost:7331

    Three panels:
      LEFT:   live node feed (streams as nodes arrive)
      CENTER: D3.js force graph (Obsidian-style, drag, zoom)
      RIGHT:  node detail (click any node → episodic text, chain, meta)

    Bottom bar: session stats | token budget | vault total | embed coverage

    Usage:
      python -m cairn dashboard
      python -m cairn dashboard --port=8080
      python -m cairn dashboard --session=2026-06-08
      python -m cairn dashboard --no-browser
    """
    port       = 7331
    session    = None
    open_browser = True
    for arg in args:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg.startswith("--session="):
            session = arg.split("=")[1]
        elif arg == "--no-browser":
            open_browser = False

    from cairn.dashboard import run_dashboard
    run_dashboard(port=port, session_id=session, open_browser=open_browser)


def cmd_doctor(args: list[str]) -> None:
    """State oracle: report what's installed, present, and wired — so a fresh
    install (or any model handed this project) acts on the gaps instead of
    guessing or re-creating what already exists. Each line is ✓ ok / ⚠ needs
    attention / ✗ broken / ○ optional, with the fix command."""
    OK, WARN, BAD, OPT = "✓", "⚠", "✗", "○"
    rows, fixes = [], []
    def line(mark, label, detail, fix=None):
        rows.append((mark, label, detail))
        if fix and mark in (WARN, BAD):
            fixes.append(fix)

    # package
    try:
        from importlib.metadata import version
        line(OK, "package", f"cairn-remembers {version('cairn-remembers')}")
    except Exception:
        line(WARN, "package", "metadata not found (running from source?)")

    # core dep: numpy
    try:
        import numpy  # noqa
        line(OK, "numpy", "present")
    except Exception:
        line(BAD, "numpy", "MISSING — core retrieval/edges will crash",
             'pip install -e "."')

    # dashboard deps
    try:
        import fastapi, uvicorn  # noqa
        line(OK, "dashboard", "fastapi + uvicorn present")
    except Exception:
        line(OPT, "dashboard", "fastapi/uvicorn not installed (dashboard won't run)",
             'pip install -e ".[dashboard]"')

    # embedder + model cache
    try:
        import sentence_transformers  # noqa
        cached = False
        try:
            hub = Path.home() / ".cache" / "huggingface" / "hub"
            cached = any("all-MiniLM-L6-v2" in p.name for p in hub.glob("*")) if hub.exists() else False
        except Exception:
            pass
        line(OK, "embedder", "sentence-transformers" + (" (model cached)" if cached else " — model downloads on first embed"))
    except Exception:
        line(OPT, "embedder", "not installed (search/edges need it)",
             'pip install -e ".[embeddings]"')

    # vault — and the foreign/cloud-agent tell
    vault_total = None
    try:
        v = Vault()
        st = v.stats()
        vault_total = (st.get("total", 0) or 0) - (st.get("voided", 0) or 0)
        edge_n = 0
        try:
            edge_n = v.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        except Exception:
            pass
        emb_n = 0
        try:
            emb_n = v.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL AND status!='void'").fetchone()[0]
        except Exception:
            pass
        if vault_total and vault_total > 0:
            line(OK, "vault", f"~/.cairn/cairn.db — {vault_total:,} active nodes")
            pct = int(emb_n / vault_total * 100) if vault_total else 0
            line(OK if pct >= 90 else WARN, "embeddings",
                 f"{emb_n:,}/{vault_total:,} ({pct}%)",
                 None if pct >= 90 else "python -m cairn embed")
            line(OK if edge_n > 0 else WARN, "edges",
                 f"{edge_n:,} edges" if edge_n else "no edge graph yet",
                 None if edge_n else "python -m cairn edges")
        else:
            line(OPT, "vault", "empty — nothing here yet (expected on a fresh install)")
    except Exception as e:
        line(BAD, "vault", f"could not open (~/.cairn/cairn.db): {e}")

    # MCP registration (best-effort, platform-specific)
    try:
        if sys.platform == "win32":
            cfg = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
        elif sys.platform == "darwin":
            cfg = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        else:
            cfg = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        if cfg.exists() and "cairn" in cfg.read_text(encoding="utf-8", errors="ignore"):
            line(OK, "MCP", "registered in Claude Desktop config")
        else:
            line(OPT, "MCP", "not found in a known client config (optional)",
                 'add an MCP server: {"command":"python","args":["-m","cairn","mcp"]}')
    except Exception:
        line(OPT, "MCP", "could not check client config")

    # capture scope (cairn connect) — off / per-project / global, mutually exclusive
    try:
        glob_on = _is_global_connected()
        psf = Path.cwd() / ".claude" / "settings.json"
        proj_on = psf.exists() and "hook.py" in psf.read_text(encoding="utf-8", errors="ignore")
        if glob_on and proj_on:
            line(WARN, "capture", "BOTH global + per-project active — double-write risk",
                 "keep one: `cairn disconnect --global`  OR  `cairn disconnect`")
        elif glob_on:
            line(OK, "capture", "global — every chat on this machine feeds the vault")
        elif proj_on:
            line(OK, "capture", "this project is connected (per-project)")
        else:
            line(OPT, "capture", "off — not auto-capturing (optional)",
                 "per-project: cairn connect   ·   everywhere: cairn connect --global")
        if os.environ.get("CAIRN_CAPTURE") == "0" or (Path.home() / ".cairn" / "CAPTURE_OFF").exists():
            line(WARN, "capture mute", "capture is MUTED right now", "cairn capture on")
    except Exception:
        line(OPT, "capture", "could not check capture settings")

    # codex capture surface — the honest disclosure: notify ≠ full plain chat.
    # Only shown when a Codex store exists (Codex is in use on this machine), so
    # non-Codex users see nothing. This is the not-a-me-fix: doctor tells the
    # truth about the gap instead of any doc claiming "every turn is captured".
    try:
        import glob as _cxglob
        cx_root = Path.home() / ".codex" / "sessions"
        n_roll = (len(_cxglob.glob(str(cx_root / "**" / "rollout-*.jsonl"), recursive=True))
                  if cx_root.exists() else 0)
        if n_roll:
            st_wm = None
            try:
                st_wm = json.loads((Path.home() / ".cairn" / "codex_import_state.json")
                                   .read_text(encoding="utf-8")).get("watermark")
            except Exception:
                pass
            if st_wm:
                line(OK, "codex capture",
                     f"notify = agentic turns; plain chat via import codex-sessions "
                     f"(forward capture ON; {n_roll} threads on disk)")
            else:
                line(OPT, "codex capture",
                     f"notify hook = agentic/notify-fired turns ONLY; plain Codex chat "
                     f"({n_roll} threads) is NOT yet imported",
                     "cairn import codex-sessions   (dry-run preview; then --apply)")
    except Exception:
        pass

    # local-agent (Cowork / Claude Desktop) capture surface — hook-less, so it's
    # imported from the transcripts the app writes, not a live hook.
    try:
        from cairn.local_agent_reader import _default_roots, STATE_FILE
        import glob as _laglob
        n_la = 0
        for _r in _default_roots():
            n_la += len(_laglob.glob(
                str(_r / "**" / ".claude" / "projects" / "**" / "*.jsonl"), recursive=True))
        if n_la:
            wm = None
            try:
                wm = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("watermark")
            except Exception:
                pass
            if wm:
                line(OK, "local-agent capture",
                     f"Cowork/Desktop — {n_la} transcript(s); forward import ON")
            else:
                line(OPT, "local-agent capture",
                     f"Cowork/Desktop chat ({n_la} transcript(s)) NOT yet imported",
                     "cairn import local-agent-sessions   (dry-run; then --apply)")
    except Exception:
        pass

    # render
    print("cairn doctor — what's installed, present, and wired\n")
    w = max(len(lbl) for _, lbl, _ in rows)
    for mark, lbl, detail in rows:
        print(f"  {mark} {lbl.ljust(w)}  {detail}")
    if fixes:
        print("\n  to do (act ONLY on these — never recreate the ✓ items):")
        for f in fixes:
            print(f"    • {f}")
    else:
        print("\n  all set.")


def cmd_import_session(args: list[str]) -> None:
    """Port a Cairn-less session into the vault from a JSONL backfill produced by
    `cairn backfill --prompt`. Each line is one node with explicit when/kind/speaker/
    text/tags/parent/importance. Lossless: original timestamps, tags, and the
    reasoning chain (parent refs → real ids) are written as REAL fields, not just
    embedded in text. Run `embed` + `edges` afterward for vectors + graph.

    Usage:
      python -m cairn import-session <file.jsonl> [--session=NAME]
             [--date=YYYY-MM-DD] [--model=NAME]
    """
    from datetime import datetime, timezone, timedelta
    path = next((a for a in args if not a.startswith("--")), None)
    if not path or not Path(path).exists():
        print("usage: cairn import-session <file.jsonl> [--session=NAME] [--date=YYYY-MM-DD] [--model=NAME]")
        return
    opt = lambda k, d: next((a.split("=", 1)[1] for a in args if a.startswith(f"--{k}=")), d)
    date_str = opt("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    session  = opt("session", f"backfill-{date_str}")
    model    = opt("model", "backfill")
    try:
        # noon UTC, not midnight: midnight-UTC reads as the PREVIOUS day in
        # negative-UTC (US) timezones, so a "today" backfill gets filtered out
        # of the dashboard's local-"today" view. Noon stays the same calendar
        # day in every timezone.
        base = datetime.fromisoformat(date_str).replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
    except Exception:
        base = datetime.now(timezone.utc)

    vault = Vault()
    refmap, n_ok, n_bad = {}, 0, 0
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            n_bad += 1; continue
        text = (rec.get("text") or rec.get("gist") or "").strip()
        if not text:
            n_bad += 1; continue
        kind = rec.get("kind") or "conversation_turn"
        if kind not in VALID_KINDS:
            kind = "conversation_turn"
        # real ISO timestamp if given, else synthesize from base date + order
        when = str(rec.get("when") or "").strip()
        if "T" in when and when[:4].isdigit():
            ts = when                                   # full ISO datetime — keep as-is
        elif len(when) == 10 and when[:4].isdigit() and when[4] == "-":
            ts = when + "T12:00:00+00:00"               # bare date — noon UTC (timezone-safe)
        else:
            ts = (base + timedelta(seconds=i)).isoformat()  # 'turn N'/unknown — noon-anchored + order
        parent_id = refmap.get(rec.get("parent")) if rec.get("parent") else None
        tags = rec.get("tags") if isinstance(rec.get("tags"), list) else []
        if "backfill" not in tags:
            tags = list(tags) + ["backfill"]
        try:
            imp = int(rec["importance"]) if rec.get("importance") is not None else None
        except Exception:
            imp = None
        speaker = rec.get("speaker") if rec.get("speaker") in ("user", "agent") else "agent"
        node = vault.write(MicroNode(
            session=session, kind=kind, query=text[:2000], output_preview=text,
            speaker=speaker, model=(model if speaker == "agent" else "human"),
            agent_role="curator", parent=parent_id, timestamp=ts,
            tags=tags, importance=imp, memory_tier=1,
        ))
        if rec.get("ref"):
            refmap[rec["ref"]] = node.id
        n_ok += 1
    print(f"cairn: imported {n_ok} node(s) into session '{session}'"
          + (f" — {n_bad} line(s) skipped" if n_bad else ""))
    print("       next:  python -m cairn embed   (vectors)")
    print("              python -m cairn edges   (chain + semantic graph)")


def cmd_backfill(args: list[str]) -> None:
    """Distill captured conversations (native OR imported) into connected claim
    nodes — agent-driven, idempotent, cost-estimated up front. EXTRACTION is done
    by the connected agent (no bundled LLM); this wires the deterministic parts:
      cairn backfill <native|claude|gpt|all> [--reset] [--source-file=PATH] [--estimate]
      cairn backfill finalize     # embed new claims + rebuild edges + entity audit
      cairn backfill --prompt     # print the paste-in prompt to reconstruct a chat Cairn never saw
    """
    from cairn import backfill as bf
    if "--prompt" in args:
        print(bf.RECONSTRUCT_PROMPT)
        return
    v = Vault()
    if args and args[0] == "finalize":
        s = bf.finalize(v)
        print(f"finalized: embedded {s.get('embedded',0)} · semantic {s.get('semantic',0)} "
              f"· entity {s.get('entity',0)}")
        aud = s.get("entity_audit") or []
        if aud:
            print("entity bridges (#convos · entity) — watch for a generic spanning many:")
            for e, c in aud:
                print(f"   {c:4}  {e}")
        return
    flags = {a for a in args if a.startswith("--")}
    src_file = next((a.split("=", 1)[1] for a in args if a.startswith("--source-file=")), None)
    reset = "--reset" in flags
    source = next((a for a in args if not a.startswith("--")), "all")
    p = bf.estimate(v, source, source_path=src_file, reset=reset)
    print(bf.warning(p))
    if "--estimate" in flags:
        return
    if not p["pending"]:
        print("nothing to distill — all caught up."); return
    if reset:
        print(f"\n--reset: prior claims for {p['pending']} session(s) get voided + replaced "
              f"(append-only) as each is re-distilled.")
    print("\nDistillation is agent-driven (model-agnostic): the connected agent reads each")
    print("conversation, writes claims via cairn.backfill.ingest(), then `cairn backfill")
    print("finalize` embeds + rebuilds edges. Run inside your agent session.")
    print("\n" + bf.EXTRACTION_SPEC)


def _codex_conf_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _toml_encode_str(s: str) -> str:
    """Encode one string as a TOML basic (double-quoted) string. Backslashes and
    quotes are escaped — Windows paths are full of backslashes, so this matters."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_notify_array(text: str):
    """Parse the `notify = [ ... ]` array from config.toml via tomllib (robust to
    single/double quotes + spacing) → list[str], or None if there's no notify key.
    We parse with tomllib for CORRECTNESS but rewrite by hand for MINIMAL-DIFF."""
    import tomllib
    try:
        conf = tomllib.loads(text)
    except Exception:
        return None
    n = conf.get("notify")
    if isinstance(n, list) and all(isinstance(x, str) for x in n):
        return n
    return None


def _build_hook_notify(py: str, chain: list[str]) -> list[str]:
    """The notify array that points at our hook, encoding the ORIGINAL notify
    command as chain args:  [py, -X, utf8, -m, cairn, codex-hook,
    --chain, <orig...>, --]  — Codex appends the payload as the final arg."""
    head = [py, "-X", "utf8", "-m", "cairn", "codex-hook"]
    if chain:
        return head + ["--chain"] + list(chain) + ["--"]
    return head


def _notify_line_re():
    """Match a `notify = [...]` assignment (non-greedy). Kept for the presence
    check and the cairn-shaped uninstall-remove path (no `]`-inside-string there).
    The surgical REWRITE uses _find_notify_span instead, because a non-greedy
    `.*?]` stops at a `]` INSIDE a string value (e.g. a buried --previous-notify)."""
    import re
    return re.compile(r"(?m)^[ \t]*notify[ \t]*=[ \t]*\[.*?\]", re.DOTALL)


def _find_notify_span(text: str):
    """Char span (start, end) of a top-level `notify = [ ... ]` assignment, with
    the closing `]` located by bracket-matching that SKIPS any `]` inside quoted
    string values — e.g. a buried `--previous-notify` value like "[\"..\",\"--\"]".
    Returns None if there's no notify array or the brackets don't balance.

    This replaces a fragile non-greedy regex (`\\[.*?\\]`) that stopped at the
    FIRST `]` — which, on a Codex-rewritten notify, is the one inside the buried
    JSON string — corrupting the surgical rewrite into invalid TOML."""
    import re
    m = re.search(r"(?m)^[ \t]*notify[ \t]*=[ \t]*\[", text)
    if not m:
        return None
    i, n, depth = m.end(), len(text), 1     # m.end() is just past the opening [
    while i < n and depth > 0:
        ch = text[i]
        if ch == '"' or ch == "'":
            quote = ch                       # skip the whole string literal
            i += 1
            while i < n:
                if quote == '"' and text[i] == "\\":
                    i += 2                   # escaped char in a TOML basic string
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    if depth != 0:
        return None                          # unbalanced — refuse to rewrite
    return (m.start(), i)                     # end is just past the closing ]


def _rewrite_notify_line(text: str, new_array: list[str]):
    """Return (new_text, replaced_bool). Surgically replaces ONLY the notify
    assignment; if none exists, INSERTS one as a top-level key. Every other byte
    is preserved.

    Insertion matters: a top-level key must appear BEFORE the first [table]
    header, else TOML reads it as belonging to that table. So we splice notify in
    just above the first table header (or at end-of-file if there is none)."""
    encoded = "notify = [" + ", ".join(_toml_encode_str(x) for x in new_array) + "]"
    span = _find_notify_span(text)          # bracket-matched: skips ] inside strings
    if span:
        s, e = span
        return text[:s] + encoded + text[e:], True
    # no notify key — insert as a top-level key, above the first [table] header
    import re
    lines = text.splitlines(keepends=True)
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        if re.match(r"[ \t]*\[", ln):   # first table / array-of-tables header
            insert_at = i
            break
    new_line = encoded + "\n"
    lines.insert(insert_at, new_line)
    return "".join(lines), False


def _codex_is_installed(notify: "list[str] | None") -> bool:
    """True only if cairn is the PRIMARY notify target — i.e. Codex actually
    invokes our hook first. Only the HEAD (elements before the first --chain /
    --previous-notify boundary) is inspected: a cairn command BURIED inside a
    --previous-notify JSON arg (Codex's computer-use wrapper convention, which
    does NOT execute it) must NOT count as installed, or `install` becomes a
    silent no-op on a Codex-rewritten config."""
    if not notify:
        return False
    head = []
    for x in notify:
        if x in ("--chain", "--previous-notify"):
            break
        head.append(str(x).lower())
    return any("cairn" in h for h in head) and any("codex-hook" in h for h in head)


def _underlying_notify(notify: "list[str] | None") -> list:
    """Recover the real downstream notify command to preserve as the chain when
    (re)installing, dropping a `--previous-notify <arg>` tail. Codex's computer-
    use wrapper adds `--previous-notify <json>` to hold the PRIOR notify — for us
    a STALE cairn install we're replacing — so keeping it would re-nest a dead
    cairn inside the chain. Everything from --previous-notify onward is dropped;
    the clean underlying command (e.g. [computer-use.exe, 'turn-ended']) remains."""
    if not notify:
        return []
    if "--previous-notify" in notify:
        return list(notify[:notify.index("--previous-notify")])
    return list(notify)


def cmd_codex_hook(args: list[str]) -> None:
    """
    Wire Cairn into OpenAI Codex's notify hook — live conversation capture from
    the Codex desktop app / CLI. OPTIONAL and OFF by default.

    Usage:
      python -m cairn codex-hook install     # wrap notify → capture + chain
      python -m cairn codex-hook uninstall   # restore the original notify
      python -m cairn codex-hook status      # installed? current notify? log tail?

    install WRAPS any existing notify (OpenAI's own plumbing keeps working — it's
    encoded as chain args and replayed after capture), backs up config.toml once,
    and rewrites ONLY the notify line. Idempotent. Touches nothing but
    ~/.codex/config.toml. Capture is fail-safe: the hook can never break Codex.
    """
    from datetime import datetime, timezone

    # Dual role, one command name: `codex-hook install|uninstall|status` is the
    # human-facing CLI; ANY other argv is Codex itself invoking the hook (chain
    # flags + a JSON payload as the final arg). Route the hook path to
    # codex_hook.main so the notify line can literally say `... codex-hook
    # --chain <orig> -- <payload>`. Bare `codex-hook` (no args) → status.
    if args and args[0] not in ("install", "uninstall", "status"):
        from cairn.codex_hook import main as _hook_main
        _hook_main(args)
        return

    sub  = (args[0] if args else "status").lower()
    conf = _codex_conf_path()
    py   = sys.executable

    # ── status ────────────────────────────────────────────────────────────────
    if sub == "status":
        if not conf.exists():
            print(f"cairn: no Codex config at {conf} — Codex not installed here?")
            return
        try:
            text = conf.read_text(encoding="utf-8")
        except Exception as e:
            print(f"cairn: could not read {conf} — {e}")
            return
        notify = _read_notify_array(text)
        installed = _codex_is_installed(notify)
        print(f"cairn: codex-hook {'INSTALLED' if installed else 'not installed'}")
        print(f"       config : {conf}")
        if notify is None:
            print("       notify : (no notify key)")
        else:
            print(f"       notify : {notify}")
        log = Path.home() / ".cairn" / "codex_hook_debug.log"
        if log.exists() and log.stat().st_size:
            print(f"\n       debug log ({log}) — last lines:")
            try:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
                for ln in tail:
                    print(f"         {ln}")
            except Exception:
                pass
        else:
            print("       debug log: none (no capture failures recorded)")
        return

    # install / uninstall both need the file to exist
    if not conf.exists():
        print(f"cairn: no Codex config at {conf} — nothing to {sub}.")
        print("       (Codex writes this file; install/configure Codex first.)")
        return
    try:
        text = conf.read_text(encoding="utf-8")
    except Exception as e:
        print(f"cairn: could not read {conf} — {e}")
        return

    notify = _read_notify_array(text)

    # ── backup once — only if no cairn-hook backup already exists ─────────────
    def _backup_if_needed() -> None:
        existing = list(conf.parent.glob("config.toml.bak-cairn-hook-*"))
        if existing:
            return
        ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dest = conf.parent / f"config.toml.bak-cairn-hook-{ts}"
        # if two installs run the same day, keep the first (don't clobber)
        if not dest.exists():
            dest.write_text(text, encoding="utf-8")
            print(f"       backup : {dest}")

    # Guard: a notify line is textually present but tomllib couldn't parse it
    # (notify is None yet the line exists). Wrapping now would DROP OpenAI's
    # plumbing — refuse rather than clobber. The config is the user's to fix.
    if sub == "install" and notify is None and _notify_line_re().search(text):
        print("cairn: config.toml has a notify line I can't parse as valid TOML —")
        print("       refusing to touch it (would risk dropping the original notify).")
        print(f"       fix {conf} by hand, then re-run install.")
        return

    # ── install ───────────────────────────────────────────────────────────────
    if sub == "install":
        if _codex_is_installed(notify):
            print("cairn: codex-hook already installed — notify routes through it.")
            print(f"       notify : {notify}")
            return
        _backup_if_needed()
        # the ORIGINAL downstream notify becomes the chain — with any Codex
        # `--previous-notify <stale-cairn>` tail stripped so we don't re-nest a
        # dead cairn (a Codex auto-update can rewrite our install into that shape).
        chain = _underlying_notify(notify)
        new_array = _build_hook_notify(py, chain)
        new_text, replaced = _rewrite_notify_line(text, new_array)
        try:
            conf.write_text(new_text, encoding="utf-8")
        except Exception as e:
            print(f"cairn: could not write {conf} — {e}")
            return
        print("cairn: codex-hook INSTALLED")
        if chain:
            print(f"       wrapped original notify (replayed after capture): {chain}")
        else:
            print("       no prior notify — installed without a chain")
        print(f"       notify : {new_array}")
        print("       live-test: run a turn in Codex, then: cairn codex-hook status")
        return

    # ── uninstall ─────────────────────────────────────────────────────────────
    if sub == "uninstall":
        if not _codex_is_installed(notify):
            print("cairn: codex-hook not installed — nothing to remove.")
            print(f"       notify : {notify}")
            return
        # recover the original command from the encoded chain args
        original = []
        if "--chain" in notify:
            rest = notify[notify.index("--chain") + 1:]
            original = rest[:rest.index("--")] if "--" in rest else rest
        if original:
            new_text, _ = _rewrite_notify_line(text, original)
            print(f"       restored original notify: {original}")
        else:
            # we wrapped a config that had NO notify — remove the line entirely
            new_text = _notify_line_re().sub("", text)
            # tidy any doubled blank line the removal left behind
            new_text = new_text.replace("\n\n\n", "\n\n")
            print("       removed notify (there was no original to restore)")
        try:
            conf.write_text(new_text, encoding="utf-8")
        except Exception as e:
            print(f"cairn: could not write {conf} — {e}")
            return
        print("cairn: codex-hook UNINSTALLED")
        return


def cmd_backup(args: list[str]) -> None:
    """Snapshot the vault to a timestamped, integrity-checked .bak file. The
    vault is irreplaceable — run before migrations or bulk imports."""
    v = Vault()
    dest = args[0] if args else None
    path = v.backup(dest)
    print(f"cairn: backup written → {path}")


def _consent_path() -> Path:
    return Path.home() / ".cairn" / "consent.json"


def _consent_get() -> dict:
    try:
        return json.loads(_consent_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_consent(harness: str, account: str, answer: str) -> None:
    """One-time-ness of the consent walk, keyed by HARNESS x ACCOUNT so two
    accounts of one harness never share (or bleed) a decision. Once answered
    (yes OR no) for a harness+account, no agent raises it again (owner: 'i dont
    want you to keep hounding the people'); re-deciding is the human's move
    (`cairn setup`). A blank account falls back to a bare-harness key (back-compat)."""
    from datetime import datetime, timezone
    d = _consent_get()
    key = f"{harness}:{account}" if account else harness
    d[key] = {"answer": answer, "account": account or "",
              "at": datetime.now(timezone.utc).isoformat()[:16]}
    p = _consent_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _consent_lookup(consent: dict, harness: str, account: str):
    """Prior consent for a harness+account: the per-account key wins; a legacy
    bare-harness key is honored as the answer so upgrading never re-hounds."""
    return consent.get(f"{harness}:{account}") or consent.get(harness)


def _claude_present() -> bool:
    return (Path.home() / ".claude").exists()


def _claude_connected_global() -> bool:
    f = Path.home() / ".claude" / "settings.json"
    try:
        return f.exists() and "cairn" in f.read_text(encoding="utf-8").lower()
    except Exception:
        return False


def _codex_present() -> bool:
    return (Path.home() / ".codex" / "config.toml").exists()


def _codex_hook_installed() -> bool:
    f = Path.home() / ".codex" / "config.toml"
    try:
        return f.exists() and "codex-hook" in f.read_text(encoding="utf-8")
    except Exception:
        return False


def cmd_setup(args: list[str]) -> None:
    """
    First-run setup — the consent walk. Detects which AI harnesses live on
    this machine and ASKS, one at a time, whether to turn on ambient capture
    for each. Plain words, recommended-but-optional, default is NO, and
    nothing installs without a typed yes. Re-run anytime; everything it
    offers is reversible with one command.

    Usage:
      python -m cairn setup
    """
    import sys as _sys
    print("cairn setup — who writes to your vault, and only with your yes\n")

    rows = []
    if _claude_present():
        rows.append(("Claude Code", _claude_connected_global(),
                     "connect --global", "disconnect --global"))
    if _codex_present():
        rows.append(("OpenAI Codex", _codex_hook_installed(),
                     "codex-hook install", "codex-hook uninstall"))
    if not rows:
        print("  no supported AI harnesses detected (~/.claude or ~/.codex).")
        print("  install one, then re-run: python -m cairn setup")
        return

    for name, on, _cmd_on, cmd_off in rows:
        state = "ON  — every session already writes to the vault" if on \
                else "off — nothing is captured unless you `cairn note` it"
        print(f"  {name:14} ambient capture: {state}")
    print()

    if not _sys.stdin.isatty():
        print("(non-interactive shell — to change anything, run the commands")
        print(" yourself: python -m cairn connect --global  |  python -m cairn codex-hook install)")
        return

    changed = False
    consent = _consent_get()
    from cairn.accounts import resolve_slug_for_setup, galaxy_label
    for name, on, cmd_on, cmd_off in rows:
        slug = resolve_slug_for_setup(name)
        if slug == "default":
            # grab-first found no readable account (no env, no handle, no login id):
            # ASK once. The answer becomes the machine handle, so it sticks for live
            # capture and no later run re-asks. Enter = leave it 'default' for now.
            try:
                named = input(f"  {name}: what should this account be called?"
                              f" (Enter to skip) ").strip()
            except (EOFError, KeyboardInterrupt):
                named = ""
            if named:
                from cairn.accounts import set_handle
                if set_handle(named):
                    slug = resolve_slug_for_setup(name)
        disp = name if slug == "default" else f"{name} · {galaxy_label(slug)}"
        prior = _consent_lookup(consent, name, slug)
        if on:
            record_consent(name, slug, "yes")   # connected = answered; keep the record honest
            continue
        if prior:
            # actual hook state is truth: a stale 'yes' while capture is off gets reconciled
            if prior.get("answer") == "yes":
                record_consent(name, slug, "no")
                print(f"  {disp}: was 'yes' but capture isn't wired here — recorded off."
                      f" (Turn back on: python -m cairn {cmd_on})")
            else:
                print(f"  {disp}: already answered 'no' on {prior['at']}"
                      f" — not asking again. (Change your mind: python -m cairn {cmd_on})")
            continue
        print(f"── {disp} ──────────────────────────────────────────────")
        print(f"  Turning this ON means: every {name} session automatically")
        print(f"  writes its conversation into your vault as it happens —")
        print(f"  orient at the start, capture while you work, compile at the")
        print(f"  end. That is the memory doing its job, and it is RECOMMENDED.")
        print(f"  Leaving it off is fine too: sessions can still read the vault")
        print(f"  and write single notes on command — just nothing automatic.")
        print(f"  Reversible anytime:  python -m cairn {cmd_off}")
        ans = input(f"  Turn on ambient capture for {disp}? [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            print()
            COMMANDS[cmd_on.split()[0]](cmd_on.split()[1:])
            record_consent(name, slug, "yes")
            changed = True
        else:
            record_consent(name, slug, "no")
            print(f"  Kept OFF for {disp} — command-only capture. Recorded;")
            print(f"  nobody will ask again. (Reopen anytime: python -m cairn setup)\n")

    if changed:
        print("\nsetup done — re-run `python -m cairn setup` anytime to review.")
    else:
        print("nothing changed — your vault, your call.")


def cmd_hello(args: list[str]) -> None:
    """Set/show/reset the Garden Hub welcome greeting (config, not the vault —
    a mutable UI setting in ~/.cairn/settings.json, never a memory node).
    Usage:  cairn hello "Good evening"          set it
            cairn hello                          show the current greeting
            cairn hello --reset                  back to the default line
    Same value the click-to-edit headline writes."""
    import json as _j
    from pathlib import Path as _P
    sf = _P.home() / ".cairn" / "settings.json"
    data = {}
    if sf.exists():
        try:
            data = _j.loads(sf.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    reset = any(a in ("--reset", "--clear", "--default") for a in args)
    text = " ".join(a for a in args if not a.startswith("--")).strip()[:200]
    if not reset and not text:
        cur = str(data.get("greeting") or "").strip()
        print(f'cairn: Hub greeting is "{cur}"' if cur
              else 'cairn: Hub greeting is the default (unset) — set one with: cairn hello "your text"')
        return
    if text:
        data["greeting"] = text
    else:
        data.pop("greeting", None)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(_j.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f'cairn: Hub greeting set to "{text}"' if text
          else "cairn: Hub greeting reset to the default line")


def cmd_account(args: list[str]) -> None:
    """List the accounts (galaxies) in your vault, and rename their DISPLAY
    labels. A rename touches ONLY the display name — the stable key/id stays
    put, so two same-named accounts never merge and nodes keep their origin.
    There is no delete (append-only): an account you stop using just goes quiet.
    Usage:  cairn account                              list galaxies + node counts
            cairn account rename <key> <new display>   change a display name
            cairn account doctor                       read-only: stored label vs Desktop proof
            cairn account fix-session [<id>] [<slug>]  repair ONE session (LOCKED, backed up)"""
    import json as _j
    from pathlib import Path as _P
    from cairn.accounts import galaxy_label, maker_of

    reg_path = _P.home() / ".cairn" / "accounts.json"

    def _load_reg() -> dict:
        try:
            r = _j.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}
            return r if isinstance(r, dict) else {}
        except Exception:
            return {}

    sub = args[0] if args else "list"

    if sub == "rename":
        if len(args) < 3:
            print("usage: cairn account rename <key> <new display name>")
            return
        key = args[1].strip().lower()
        new_label = " ".join(args[2:]).strip()
        if not new_label:
            print("give a non-empty display name.")
            return
        reg = _load_reg()
        entry = reg.get(key)
        if entry is None or not isinstance(entry, dict):
            # allow labeling a galaxy that lives in the vault but was never
            # registered (a handle account, a legacy backfill galaxy)
            known = {(r["account"] or "").lower() for r in Vault().conn.execute(
                "SELECT DISTINCT account FROM sessions WHERE account IS NOT NULL")}
            if key not in known:
                print(f"no account '{key}'. run `cairn account` to see the list.")
                return
            entry = {}
        old = entry.get("label") or galaxy_label(key)
        entry["label"] = new_label
        reg[key] = entry
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(_j.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"renamed display: {old!r} -> {new_label!r}   (key '{key}' unchanged)")
        print("only the display name changed — nodes keep their origin, galaxies never merge.")
        return

    if sub == "doctor":
        # READ-ONLY: compare each live uuid session's stored account against the
        # Desktop cliSessionId proof, and report mismatches. Repairs are never
        # automatic — they go through `fix-session` with explicit approval.
        import re as _re
        from cairn.accounts import (desktop_account, claude_identity,
                                    codex_identity, _slug_for_account_uuid)
        v = Vault()
        print("account doctor — read-only attribution check\n")
        ci = claude_identity() or {}
        if ci.get("id"):
            cli_slug = _slug_for_account_uuid(ci["id"]) or "(unregistered id)"
            em = ci.get("email") or ""
            print("  CLI OAuth (~/.claude.json) currently: " + cli_slug
                  + (f"  [{em[:2]}***]" if "@" in em else ""))
        else:
            print("  CLI OAuth (~/.claude.json): none readable")
        # Codex identity — READ-ONLY lookup only. Must NOT call slug_register (it
        # WRITES accounts.json for an unregistered id, breaking doctor's promise);
        # _slug_for_account_uuid reads and returns None for an unknown id.
        co = codex_identity() or {}
        if co.get("id"):
            cx_slug = _slug_for_account_uuid(co["id"]) or "(unregistered id)"
            cem = co.get("email") or ""
            print("  Codex auth (~/.codex/auth.json) currently: " + cx_slug
                  + (f"  [{cem[:2]}***]" if "@" in cem else ""))
        else:
            print("  Codex auth (~/.codex/auth.json): none readable")
        uuid_re = _re.compile(r"^[0-9a-fA-F-]{36}$")
        rows = v.conn.execute(
            "SELECT id, account, account_locked FROM sessions WHERE account IS NOT NULL").fetchall()
        covered = agree = mismatch = uncovered = locked = 0
        mismatches = []
        nonuuid: dict = {}   # account -> count, for the non-Desktop-provable families
        for r in rows:
            sid = r["id"] or ""
            if not uuid_re.match(sid):
                acct = r["account"] or "(none)"
                nonuuid[acct] = nonuuid.get(acct, 0) + 1
                continue
            if r["account_locked"]:
                locked += 1
            d = desktop_account(sid)
            if not d:
                uncovered += 1
                continue
            covered += 1
            if (d["slug"] or "").lower() == (r["account"] or "").lower():
                agree += 1
            else:
                mismatch += 1
                mismatches.append((sid, r["account"], d["slug"], r["account_locked"]))
        print(f"\n  live uuid sessions: {covered + uncovered}   "
              f"(Desktop-covered {covered}, uncovered {uncovered})")
        print(f"  covered agree: {agree}   MISMATCH: {mismatch}   locked rows: {locked}")
        if mismatches:
            print("\n  MISMATCHES (stored label != Desktop proof) — repair candidates:")
            for sid, cur, proof, lk in mismatches:
                lockstr = "  [LOCKED — needs explicit approval]" if lk else ""
                print(f"    {sid[:8]}  {cur!r} -> proof {proof!r}{lockstr}")
                print(f"        cairn account fix-session {sid} {proof}")
        else:
            print("\n  no Desktop-provable mismatches. (Uncovered sessions are not "
                  "disproven — just unprovable from the Desktop store.)")
        # Codex / MCP / import sessions carry no Desktop proof — report them
        # honestly instead of silently skipping (the old behavior hid them).
        if nonuuid:
            tot = sum(nonuuid.values())
            print(f"\n  non-Desktop-provable sessions (Codex / MCP / import): {tot}"
                  f" — not verifiable from the Desktop store, grouped by stored label:")
            for acct, c in sorted(nonuuid.items(), key=lambda kv: -kv[1]):
                print(f"    {acct!r}: {c}")
            print("    (declare with CAIRN_ACCOUNT, or repair one with "
                  "cairn account fix-session <session_id> <slug>)")
        print("\n  read-only — nothing changed.")
        return

    if sub in ("fix-session", "fix_session"):
        # Repair ONE session's account (current via CLAUDE_SESSION_ID, or an
        # explicitly named session — NEVER 'newest'). A human-approved fix is
        # LOCKED. Old value is backed up first; node content is never touched.
        import os as _os, re as _re
        from datetime import datetime, timezone
        from cairn.accounts import desktop_account
        uuid_re = _re.compile(r"^[0-9a-fA-F-]{36}$")
        rest = args[1:]
        if len(rest) > 2:
            print("too many arguments.")
            print("  cairn account fix-session <session_id> [<slug>]   (repair a named session)")
            print("  cairn account fix-session <slug>                  (label the CURRENT session)")
            return
        v = Vault()

        def _sess_exists(x: str) -> bool:
            return v.conn.execute(
                "SELECT id FROM sessions WHERE id=?", (x,)).fetchone() is not None

        def _looks_like_session(x: str) -> bool:
            # session-id SHAPES cairn actually stamps: a bare harness uuid, or a
            # prefixed family id. Used only to fail closed on an unknown id that
            # was clearly MEANT as a session, not misread it as a current slug.
            return bool(uuid_re.match(x)) or x.startswith(
                ("codex-", "mcp-", "import-", "session-"))

        # Referee = session EXISTENCE, never a regex. This preserves every form:
        #   <existing-id>          -> target that session (recompute from proof)
        #   <existing-id> <slug>   -> force slug on that session
        #   <plain-name>           -> label the CURRENT session
        #   <unknown-id-shape>     -> FAIL CLOSED (the codex-id corruption guard)
        sid, forced = None, None
        if len(rest) == 2:
            # arg1 is ALWAYS a session id — fail closed if absent (never relabel the
            # CURRENT session; that non-existent-arg1 path was the corruption).
            if not _sess_exists(rest[0]):
                print(f"no session {rest[0][:8]}… in the vault — refusing (the "
                      f"two-arg form targets an existing session id, never the "
                      f"current one). Nothing changed.")
                return
            sid, forced = rest[0], rest[1]
        elif len(rest) == 1:
            if _sess_exists(rest[0]):
                sid = rest[0]                     # target THAT session (recompute from proof)
            elif _looks_like_session(rest[0]):
                print(f"no session {rest[0][:8]}… in the vault — refusing (that "
                      f"looks like a session id but none exists; to label the "
                      f"CURRENT session pass a plain name, or use the two-arg "
                      f"form). Nothing changed.")
                return
            else:
                forced = rest[0]                  # a plain slug for the CURRENT session
        if not sid:
            sid = _os.environ.get("CLAUDE_SESSION_ID")
        if not sid:
            print("no session id — run inside the session (CLAUDE_SESSION_ID) or pass one:")
            print("  cairn account fix-session <session_id> [<slug>]")
            return
        row = v.conn.execute(
            "SELECT account, account_locked FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            print(f"no session {sid[:8]}… in the vault.")
            return
        old_acct, old_lock = row["account"], row["account_locked"]
        if forced:
            new_acct = forced.strip()[:24]
        else:
            d = desktop_account(sid)
            if not d:
                print(f"session {sid[:8]}…: no Desktop proof and no slug given. "
                      f"Force it:  cairn account fix-session {sid} <slug>")
                return
            new_acct = d["slug"]
        if new_acct == old_acct and old_lock:
            print(f"session {sid[:8]}…: already {old_acct!r} and locked — nothing to do.")
            return
        bak = reg_path.parent / "restamp-backup-fix-session.jsonl"
        try:
            bak.parent.mkdir(parents=True, exist_ok=True)
            with open(bak, "a", encoding="utf-8") as f:
                f.write(_j.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                  "session": sid, "old_account": old_acct,
                                  "old_locked": old_lock, "new_account": new_acct}) + "\n")
        except Exception:
            pass
        v.conn.execute(
            "UPDATE sessions SET account=?, account_locked=1 WHERE id=?", (new_acct, sid))
        v.conn.commit()
        print(f"session {sid[:8]}…: {old_acct!r} -> {new_acct!r}  (LOCKED; node content untouched)")
        return

    if sub == "backfill":
        # Selective, PROOF-ONLY history repair. DRY-RUN by default (read-only);
        # --apply mutates AFTER a fresh full backup of the sessions table. Locks
        # only proven/explicit rows (Desktop proof, canary-pinned receipts,
        # explicit --account imports); NEVER touches uncovered/ambiguous rows.
        # Canary set is PINNED session ids (node 0b725ac8c082), not text search.
        import re as _re
        from datetime import datetime, timezone
        from cairn.accounts import desktop_account
        apply = "--apply" in args
        v = Vault()
        uuid_re = _re.compile(r"^[0-9a-fA-F-]{36}$")
        CANARY = {"a4b8f2c6", "afbe32ba", "57bab5b9", "1601542b", "ea35fbac", "28153388"}
        rows = v.conn.execute(
            "SELECT id, account, account_locked FROM sessions WHERE account IS NOT NULL").fetchall()
        repair, agree, canary, imports, leave = [], [], [], [], []
        for r in rows:
            sid, acct, lk = (r["id"] or ""), r["account"], r["account_locked"]
            if uuid_re.match(sid):
                d = desktop_account(sid)
                if d and (d["slug"] or "").lower() != (acct or "").lower():
                    repair.append((sid, acct, d["slug"]))
                elif d:
                    if not lk:
                        agree.append((sid, acct))
                elif sid[:8] in CANARY:
                    if not lk:
                        canary.append((sid, acct))
                else:
                    leave.append(sid)                     # uncovered uuid -> untouched
            elif sid.startswith("import-") and not lk:
                imports.append(sid)                       # explicit --account import
        print(f"selective backfill — {'APPLY' if apply else 'DRY-RUN (read-only)'}\n")
        print(f"1. REPAIR to Desktop proof + lock  ({len(repair)}):")
        for sid, cur, proof in repair:
            print(f"     {sid}  {cur!r} -> {proof!r}")
        print(f"\n2. LOCK — Desktop proof agrees  ({len(agree)}):")
        print("     " + (", ".join(s[:8] for s, _ in agree) if agree else "(none)"))
        print(f"\n3. LOCK — canary-pinned (owner-verified receipts)  ({len(canary)}):")
        for sid, cur in canary:
            print(f"     {sid[:8]}  {cur!r}")
        print(f"   LOCK — explicit --account imports  ({len(imports)} import-* rows)")
        print(f"\n4. LEAVE UNLOCKED / untouched — uncovered uuid sessions  ({len(leave)}):")
        print("     " + (", ".join(s[:8] for s in leave) if leave else "(none)"))
        if not apply:
            print("\n  DRY-RUN — nothing changed. Re-run `cairn account backfill --apply`")
            print("  to repair + lock the proven set (a full sessions backup is written first).")
            return
        allrows = v.conn.execute("SELECT id, account, account_locked FROM sessions").fetchall()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = reg_path.parent / f"backfill-backup-{stamp}.json"
        bak.write_text(_j.dumps([{"id": x["id"], "account": x["account"],
                                  "account_locked": x["account_locked"]} for x in allrows],
                                ensure_ascii=False), encoding="utf-8")
        n = 0
        for sid, cur, proof in repair:
            v.conn.execute("UPDATE sessions SET account=?, account_locked=1 WHERE id=?", (proof, sid)); n += 1
        for grp in (agree, canary):
            for sid, _ in grp:
                v.conn.execute("UPDATE sessions SET account_locked=1 WHERE id=?", (sid,)); n += 1
        for sid in imports:
            v.conn.execute("UPDATE sessions SET account_locked=1 WHERE id=?", (sid,)); n += 1
        v.conn.commit()
        print(f"\n  APPLIED: {len(repair)} repaired, {n} rows locked. backup: {bak.name}")
        print("  node content untouched. uncovered/ambiguous rows left unlocked & correctable.")
        return

    # default: list
    reg = _load_reg()
    rows = Vault().conn.execute(
        "SELECT s.account AS account, COUNT(n.id) AS nodes, "
        "       GROUP_CONCAT(DISTINCT s.harness) AS harnesses "
        "FROM sessions s LEFT JOIN nodes n ON n.session = s.id "
        "WHERE s.account IS NOT NULL "
        "GROUP BY s.account ORDER BY nodes DESC").fetchall()
    print("accounts (galaxies) — display name is editable; the key never changes\n")
    if not rows and not reg:
        print("  (none yet — capture some turns, or run `cairn setup`)")
        return
    seen = set()
    for r in rows:
        key = r["account"] or ""
        seen.add(key.lower())
        harn = r["harnesses"] or "?"
        print(f"  {maker_of(harn)} · {galaxy_label(key)}   "
              f"(key {key}, {r['nodes']} nodes, harness {harn})")
    for slug, e in reg.items():                    # registered but no data yet
        if slug.lower() in seen or not isinstance(e, dict):
            continue
        maker = e.get("maker") or maker_of(slug)
        print(f"  {maker} · {e.get('label') or galaxy_label(slug)}   (key {slug}, 0 nodes)")
    print("\nrename a display name:  cairn account rename <key> <new name>")


COMMANDS = {
    "account":   cmd_account,
    "setup":     cmd_setup,
    "hello":     cmd_hello,
    "note":      cmd_note,
    "backfill":  cmd_backfill,
    "doctor":    cmd_doctor,
    "backup":    cmd_backup,
    "import-session": cmd_import_session,
    "flag":      cmd_flag,
    "void":      cmd_void,
    "promote":   cmd_promote,
    "demote":    cmd_demote,
    "embed":     cmd_embed,
    "status":    cmd_status,
    "sessions":  cmd_sessions,
    "query":     cmd_query,
    "xquery":    cmd_cross_query,
    "read":      cmd_read,
    "chain":     cmd_chain,
    "compile":     cmd_compile,
    "orient":      cmd_orient,
    "session":     cmd_session,
    "schedule":    cmd_schedule,
    "book":        cmd_book,
    "consolidate": cmd_consolidate,
    "connect":     cmd_connect,
    "disconnect":  cmd_disconnect,
    "capture":     cmd_capture,
    "doc":         cmd_doc,
    "lessons":     cmd_lessons,
    "edges":       cmd_edges,
    "sleep":       cmd_sleep,
    "audit":       cmd_audit,
    "import":      cmd_import,
    "ingest":      cmd_ingest,
    "fetch":       cmd_fetch,
    "wander":      cmd_wander,
    "drift":       cmd_wander,   # legacy alias (renamed to wander) — hidden, prevents breakage
    "mcp":         cmd_mcp,
    "dashboard":   cmd_dashboard,
    "codex-hook":  cmd_codex_hook,
}


def main():
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print("cairn — local-first episodic agent memory\n")
        print("commands:")
        print("  setup              first-run consent walk: detect your AI harnesses, ASK before any capture turns on")
        print("  orient             read PROTOCOL.md + print inherited context — run at session start")
        print("  hello ['text']     set/show/reset the Garden Hub welcome greeting (--reset for default)")
        print("  account [rename <key> <name>]  list your accounts (galaxies) + node counts; rename a display name")
        print("  session [name]     set/show session name — REQUIRED at start in pull-mode harnesses")
        print("  note [--kind=decision|hypothesis|warning|insight|question|open_item|")
        print("        blocker|context_stamp|conversation_turn|procedure|idea|resolved]")
        print("       [--speaker=user|agent] 'text'")
        print("  flag    <node_id>")
        print("  void    <node_id>")
        print("  promote <node_id> [--hot|--warm]   move to hotter tier")
        print("  demote  <node_id> [--cold|--warm]  move to colder tier")
        print("  embed              batch embed all pending nodes")
        print("  status             session overview + tier distribution + embedding coverage")
        print("  doctor             state oracle: installed? vault present? edges/embeds fresh? MCP/hooks wired?")
        print("  import-session <f.jsonl>  port a Cairn-less session in from a `cairn backfill --prompt` JSONL (lossless: real ts/tags/chain)")
        print("  import <export>    bring your AI history home — Claude/ChatGPT/Gemini data exports")
        print("  import codex-sessions  import plain Codex/GPT chat from ~/.codex/sessions (dry-run first; forward-only)")
        print("  import local-agent-sessions  import Cowork/Claude-Desktop chat (dry-run first; forward-only)")
        print("  sessions [--top=N] list all sessions with intent counts + compile status")
        print("  query  'text'      semantic search in current session")
        print("  xquery 'text'      cross-session search across entire vault history")
        print("  fetch  'text'      compact context pack for a query — token-saving retrieval")
        print("  wander 'text'      walk the edge graph outward — creative/lateral complement to fetch")
        print("  read   <node_id>   print node(s) IN FULL — complete stored text (gists live in query/fetch/logs)")
        print("  chain  <node_id>   show the reasoning chain (parent path, 60-char hops) — full text: `read`")
        print("  edges              rebuild the typed edge graph + topic communities")
        print("  compile            generate PROTOCOL.md now")
        print("  schedule           golden-angle context manifest (--render for full output)")
        print("  book               regenerate the Book (BOOK.md contents + volumes)")
        print("  consolidate        REM pass: cluster episodes -> synthesize insights/procedures")
        print("  sleep              full nightly cycle: embed -> consolidate -> prune -> edges -> book -> compile -> audit -> registry")
        print("  audit [--write]    the immune organ: zero-token structural checks (also runs in sleep)")
        print("  backfill <native|claude|gpt|all|finalize> [--reset] [--source-file=PATH] [--estimate]")
        print("                     distill captured conversations into connected claims (cost-estimated, idempotent)")
        print("                     finalize: embed new claims + rebuild edges + entity audit")
        print("                     --prompt: print the paste-in prompt to reconstruct a chat Cairn never saw")
        print("  connect [--global] opt in to ambient capture — per-project, or --global for every chat")
        print("  disconnect [--global]  remove cairn hooks (clean reverse of connect)")
        print("  capture [on|off]   pause/resume capture · per-chat mute: set CAIRN_CAPTURE=0")
        print("  doc                card catalog: register an important document for retrieval")
        print("  codex-hook install|uninstall|status  live capture from OpenAI Codex (wraps its notify hook)")
        print("  dashboard          real-time force graph + live feed (localhost:7331)")
        sys.exit(0)

    COMMANDS[args[0]](args[1:])


if __name__ == "__main__":
    main()
