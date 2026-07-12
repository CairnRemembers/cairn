"""
cairn/inject.py — Active memory injection engine.

Called from hook.py after every tool call.
Checks triggers and emits CAIRN surface blocks via the PostToolUse JSON
envelope: {"hookSpecificOutput": {"additionalContext": ...}}. That is the
ONLY stdout path Claude Code appends to the model's context — plain print()
at exit 0 goes to the user's transcript view and the model never sees it
(verified the hard way: an external audit found months of heartbeats that
no model ever read). Every emission is receipted in the attention ledger.

Four triggers:
  1. Counter heartbeat  — every 25 calls, surface top hot decisions from past sessions
  2. Struggle injection — rc=0 or latency>2s, surface relevant past nodes for this query
  3. File recurrence    — file Read/Edited in a previous session, surface related decisions
  4. Drift detection    — Jaccard keyword similarity < DRIFT_THRESHOLD, topic shifted,
                          surface past context for the new topic. No ML, <1ms, zero deps.
                          Designed to upgrade to embedding cosine once async path exists.

Design principles:
  - All queries are SQL-based (no embedding model loaded) → sub-millisecond latency
  - Drift uses Jaccard keyword overlap — O(n) strings, no torch, no model cold-start
  - Never raises exceptions (never blocks Claude Code)
  - State is a small JSON file (~2KB) — no DB writes during injection
  - Each file is only surfaced once per session (state-tracked)
  - Counter resets per-session via stop_hook clearing inject_state.json
"""
from __future__ import annotations
import json, sqlite3, sys
from pathlib import Path

STATE_FILE       = Path.home() / ".cairn" / "inject_state.json"
DB_PATH          = Path.home() / ".cairn" / "cairn.db"
COUNTER_INTERVAL = 25    # inject heartbeat every N tool calls
STRUGGLE_LATENCY = 2000  # ms threshold for struggle detection
MAX_NODES        = 3     # verbatim nodes per injection block (the fovea)
GIST_FOVEA       = 6     # gist lines per heartbeat (the parafovea, ~12 tokens each)
BOX_WIDTH        = 72    # visual width of surface box
DRIFT_INTERVAL   = 5     # check drift every N calls
DRIFT_THRESHOLD  = 0.15  # Jaccard similarity below this → topic shift

# Token-fill awareness — positional bias phase transition (arXiv 2508.07479, 2025).
# At >50% context fill, the U-shaped "lost in the middle" curve collapses and
# recency dominates entirely. Warm-tier nodes injected beyond this point land
# in the invisible middle. We suppress warm injections after WARM_INJECT_LIMIT
# blocks per session to stay comfortably under the phase transition.
# Hot tier is never suppressed — it rides position 0 in the system prompt.
#
# Model-aware context window sizes (tokens):
_MODEL_CONTEXT = {
    "claude-fable-5":   1_000_000,   # Fable 5 — Mythos-class, released 2026-06-09
    "claude-mythos-5":  1_000_000,
    "claude-opus-4":      200_000,
    "claude-sonnet-4":    200_000,
    "claude-haiku-4":     200_000,
    "gpt-4o":             128_000,
    "gpt-4-turbo":        128_000,
    "gemini-1.5-pro":   1_000_000,
}
_DEFAULT_CONTEXT = 200_000

def _context_window() -> int:
    """Read CAIRN_MODEL env var and return the model's context window size."""
    import os
    m = (os.environ.get("CAIRN_MODEL") or
         os.environ.get("CLAUDE_MODEL") or
         os.environ.get("MODEL_NAME") or "").lower()
    for k, v in _MODEL_CONTEXT.items():
        if k in m:
            return v
    return _DEFAULT_CONTEXT

def _warm_inject_limit() -> int:
    """
    Scale WARM_INJECT_LIMIT with context window.
    Target: ~15k tokens of warm context = 7.5% of 200k.
    For 1M context: same 7.5% target = ~200 blocks.
    Formula: ceil(40 * (context_window / 200_000))
    """
    import math
    return math.ceil(40 * (_context_window() / _DEFAULT_CONTEXT))

WARM_INJECT_LIMIT = 40   # legacy constant — use _warm_inject_limit() at runtime


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"call_counter": 0, "last_inject_at": 0, "file_injected": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        # trim file_injected if it grows large — keep only current session entries
        fi = state.get("file_injected", {})
        if len(fi) > 300:
            current = state.get("_current_session", "")
            state["file_injected"] = {
                k: v for k, v in fi.items()
                if k.startswith(current + ":")
            }
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_node(r) -> str:
    """Format one node as a box line."""
    kind = r["kind"] or "note"
    label = {
        "decision":      "DECISION",
        "warning":       "WARNING ",
        "open_item":     "OPEN    ",
        "resolved":      "RESOLVED",
        "context_stamp": "CONTEXT ",
        "hypothesis":    "THEORY  ",
        "blocker":       "BLOCKER ",
        "insight":       "INSIGHT ",
    }.get(kind, kind.upper()[:8].ljust(8))

    text = (r["query"] or r["output_preview"] or "")[:90].replace("\n", " ")
    sess = (r["session"] or "")
    # strip date prefix from session name for readability
    # e.g. "cairn-upgrade-session-2026-06-09" → "cairn-upgrade"
    sess_short = sess.rsplit("-20", 1)[0][:22] if "-20" in sess else sess[:22]

    line = f"│ {label}  {text}"
    # right-align session hint
    suffix = f"  [{sess_short}]"
    max_line = BOX_WIDTH - 1
    if len(line) + len(suffix) <= max_line:
        line = line + suffix
    else:
        line = line[:max_line - len(suffix)] + suffix
    return line.ljust(BOX_WIDTH - 1) + "│"


def _fmt_gist(r) -> str:
    """
    Format one node as a single gist line — the parafovea.
    ~12 tokens per line vs ~40 for a verbatim row. Fuzzy-trace theory:
    the gist is what survives; verbatim is on demand via `cairn chain <id>`.
    """
    kind  = (r["kind"] or "note")[:8]
    gist  = ""
    try:
        gist = r["gist"] or ""
    except (KeyError, IndexError):
        pass
    if not gist:
        gist = (r["query"] or r["output_preview"] or "")[:80]
    gist = gist.replace("\n", " ")
    line = f"│ ·{kind}· {gist}"
    max_line = BOX_WIDTH - 1
    if len(line) > max_line:
        line = line[:max_line]
    return line.ljust(BOX_WIDTH - 1) + "│"


def _box(title: str, rows: list, gist_rows: list | None = None) -> list[str]:
    """
    Wrap node rows in a visual box. Foveal structure:
      rows      — verbatim detail (the fovea, full attention)
      gist_rows — one-line gists (the parafovea, 10x cheaper per fact)
    """
    top    = f"┌─ CAIRN · {title} " + "─" * max(0, BOX_WIDTH - 12 - len(title)) + "┐"
    bottom = "└" + "─" * (BOX_WIDTH - 2) + "┘"
    lines  = [top]
    for r in rows:
        lines.append(_fmt_node(r))
    if gist_rows:
        sep = "│ " + "·" * (BOX_WIDTH - 4) + " │"
        lines.append(sep)
        for r in gist_rows:
            lines.append(_fmt_gist(r))
    lines.append(bottom)
    return lines


def _stamp_injected(conn: sqlite3.Connection, rows: list,
                    trigger: str = "", session: str = "") -> None:
    """
    Record last_injected on every surfaced node (scheduling metadata —
    allowed by the immutability trigger) AND write attention-ledger receipts.
    Write-through at the moment of showing: the ledger is what lets the
    scheduler learn which positions and triggers actually reach the model.
    """
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for i, r in enumerate(rows):
            conn.execute(
                "UPDATE nodes SET last_injected=? WHERE id=?", (now, r["id"])
            )
            conn.execute(
                "INSERT INTO attention_ledger "
                "(node_id, session, channel, position, trigger, shown_at) "
                "VALUES (?, ?, 'hook', ?, ?, ?)",
                (r["id"], session, i, trigger, now)
            )
        conn.commit()
    except Exception:
        pass  # stamping is telemetry — never block injection


# ── Drift helpers ────────────────────────────────────────────────────────────

def _keyword_set(text: str) -> set[str]:
    """
    Extract meaningful keywords from a query string.
    Split on path separators, strip punctuation, drop short/noise words.
    Used by Jaccard drift detection — no ML, no model load, ~0.1ms.
    """
    if not text:
        return set()
    words: set[str] = set()
    for raw in text.replace("/", " ").replace("\\", " ").replace(".", " ").split():
        w = raw.lower().strip("_-:(),;'\"")
        if len(w) > 3 and not w.startswith("-") and not w.isdigit():
            words.add(w)
    return words


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 1.0   # no data → no drift
    union = a | b
    return len(a & b) / len(union)


# ── Trigger 1: Counter heartbeat ─────────────────────────────────────────────

def _warm_gate_ok(state: dict) -> bool:
    """
    Return True if warm-tier injection is still within budget for this session.

    Based on: "Positional Biases Shift as Inputs Approach Context Window Limits"
    (arXiv 2508.07479, 2025). Beyond ~50% context fill the bias pattern changes —
    warm injected content lands in the invisible middle. Hot tier is exempt:
    it rides position 0 (system prompt) which always gets full attention.
    """
    return state.get("warm_blocks_sent", 0) < _warm_inject_limit()


def check_counter(
    state:           dict,
    conn:            sqlite3.Connection,
    current_session: str,
) -> list[str] | None:
    """
    Every COUNTER_INTERVAL calls: surface top hot/warm decisions from past sessions.
    Keeps past context in the model's working memory without manual queries.

    Ordering: memory_tier ASC (hot first), then importance DESC (warnings before
    context stamps), then timestamp DESC (recent first within same importance).
    Importance column added in vault upgrade — existing nodes backfilled from kind.
    """
    n    = state.get("call_counter", 0)
    last = state.get("last_inject_at", 0)
    if n == 0 or (n - last) < COUNTER_INTERVAL:
        return None

    # Hot tier always injects. Warm tier only if within budget.
    tier_clause = "AND memory_tier = 0" if not _warm_gate_ok(state) else "AND memory_tier <= 1"

    # Foveal injection: MAX_NODES verbatim (the fovea) + GIST_FOVEA gists
    # (the parafovea). Same token budget shows ~3x more memory surface,
    # and each gist-sized fact is too small to rot in the middle.
    #
    # ROTATION (spaced-repetition airtime): rank by due-pressure =
    # overdue * importance, where overdue = days_since_last_injected /
    # stability_days. Never-injected nodes are maximally overdue (first
    # airing). After surfacing, _stamp_injected resets their clock — they
    # sit out while neglected memories cycle back in. High-stability nodes
    # (cited often at compile) accrue pressure slowly: well-learned
    # memories need less review. The heartbeat fovea is never static —
    # this is "shift the middle" as a scheduling law, not a heuristic.
    #
    # SALIENCE DECAY (the hoarding fix, 2026-07-03): the old hard
    # `ORDER BY memory_tier ASC` made the ~35-node hot tier an aristocracy —
    # with a 9-slot fovea the same day-one club cycled forever ('All 6 files
    # pass py_compile': 967 shows) while every warm node waited and 78% of
    # the vault never aired once. Two changes, selection-layer only:
    #   • exposure damping — pressure divides by (1 + lifetime_shows/25),
    #     so a 967-show node yields to a never-shown one (÷ ~40);
    #   • tier becomes a WEIGHT (hot ×2), not a wall — hot still leads,
    #     but a starved warm memory can outrank a worn-out hot one.
    all_rows = conn.execute(f"""
        SELECT n.*,
          ( (julianday('now') - julianday(COALESCE(n.last_injected, '2020-01-01')))
            / MAX(COALESCE(n.stability_days, 1.0), 0.1) )
          * COALESCE(n.importance, 5)
          / (1.0 + COALESCE(al.shows, 0) / 25.0)
          * (CASE n.memory_tier WHEN 0 THEN 2.0 ELSE 1.0 END) AS due_pressure
        FROM nodes n
        LEFT JOIN (SELECT node_id, COUNT(*) AS shows
                   FROM attention_ledger GROUP BY node_id) al
               ON al.node_id = n.id
        WHERE  n.status = 'active'
          AND  n.session != ?
          AND  n.kind   IN ('decision', 'warning', 'open_item', 'resolved',
                          'context_stamp', 'insight', 'procedure', 'idea')
          {tier_clause}
        ORDER  BY due_pressure DESC
        LIMIT  ?
    """, (current_session, MAX_NODES + GIST_FOVEA)).fetchall()

    if not all_rows:
        return None

    rows      = all_rows[:MAX_NODES]
    gist_rows = all_rows[MAX_NODES:]

    state["last_inject_at"] = n
    # Track warm blocks sent for token-fill gate.
    # Gists count at 1/3 weight — they're ~12 tokens vs ~40 for verbatim.
    warm_count  = sum(1 for r in rows if r["memory_tier"] == 1)
    warm_count += sum(1 for r in gist_rows if r["memory_tier"] == 1) / 3.0
    state["warm_blocks_sent"] = state.get("warm_blocks_sent", 0) + warm_count

    _stamp_injected(conn, all_rows, trigger="heartbeat", session=current_session)
    return _box(f"heartbeat #{n}", rows, gist_rows)


# ── Trigger 2: Struggle injection ────────────────────────────────────────────

def check_struggle(
    result_count:    int | None,
    latency_ms:      int | None,
    query:           str | None,
    tool_name:       str,
    conn:            sqlite3.Connection,
    current_session: str,
) -> list[str] | None:
    """
    On struggle (empty results or slow): surface past nodes related to this query.
    "You searched for X before and found it in Y" — before the model gives up.
    """
    is_slow  = latency_ms is not None and latency_ms > STRUGGLE_LATENCY
    is_empty = result_count is not None and result_count == 0

    # Bash returning 0 lines is often success — don't surface on empty Bash
    if tool_name == "Bash" and is_empty and not is_slow:
        return None

    if not is_slow and not is_empty:
        return None

    reasons = []
    if is_slow:  reasons.append(f"{latency_ms}ms")
    if is_empty: reasons.append("no results")
    reason_str = ", ".join(reasons)

    # Keyword search: pull meaningful words from the query
    candidates = []
    kw: set = set()
    if query:
        words = [w.lower() for w in query.replace("/", " ").replace("\\", " ").split()
                 if len(w) > 3 and not w.startswith("-")]
        kw = set(words)
        keyword = words[0] if words else query[:30]
        # candidate POOL (wider than the cap) for the relevance floor below
        candidates = conn.execute("""
            SELECT * FROM nodes
            WHERE  status  = 'active'
              AND  session != ?
              AND  kind    IN ('decision', 'warning', 'resolved', 'open_item')
              AND  (query LIKE ? OR output_preview LIKE ? OR episodic_text LIKE ?)
            ORDER  BY memory_tier ASC, importance DESC, timestamp DESC
            LIMIT  ?
        """, (current_session,
              f"%{keyword}%", f"%{keyword}%", f"%{keyword}%",
              MAX_NODES * 8)).fetchall()

    # RELEVANCE FLOOR + DEDUP + SILENCE DEFAULT (same discipline as check_drift).
    # The old path fell through to "recent hot decisions" at ~0% relevance and
    # re-showed the same cards on every struggle — the loudest noise source
    # (735 hook pushes / 2h). Now: NO fall-through; a card must share >= 2 of the
    # struggling query's words (a one-word/regex struggle surfaces nothing); and
    # nothing already pushed this session re-fires. Silence is the safe default.
    if not candidates:
        return None
    already = {r["node_id"] for r in conn.execute(
        "SELECT DISTINCT node_id FROM attention_ledger "
        "WHERE session=? AND channel='hook'", (current_session,)).fetchall()}

    def _on_topic(r):
        txt = ((r["query"] or "") + " " + (r["output_preview"] or "")
               + " " + (r["episodic_text"] or "")).lower()
        return sum(1 for w in kw if w in txt) >= 2

    candidates = [r for r in candidates if r["id"] not in already and _on_topic(r)][:MAX_NODES]
    if not candidates:
        return None

    _stamp_injected(conn, candidates, trigger="struggle", session=current_session)
    return _box(f"struggle [{reason_str}]", candidates)


# ── Trigger 3: File recurrence ────────────────────────────────────────────────

def check_file_recurrence(
    tool_name:       str,
    query:           str | None,
    state:           dict,
    conn:            sqlite3.Connection,
    current_session: str,
) -> list[str] | None:
    """
    When a file is Read/Edited that appeared in previous sessions: surface decisions.
    "Last time you touched auth/middleware.py you decided X" — before you repeat the work.
    Each file surfaces at most once per session.
    """
    if tool_name not in ("Read", "Edit", "Write") or not query:
        return None

    # Normalize to filename only for matching
    try:
        fname = Path(query).name
    except Exception:
        fname = query.split("/")[-1].split("\\")[-1]

    if not fname or len(fname) < 3:
        return None

    # State key includes session so each session gets a fresh reminder
    state_key = f"{current_session}:{fname}"
    if state_key in state.get("file_injected", {}):
        return None

    rows = conn.execute("""
        SELECT * FROM nodes
        WHERE  status  = 'active'
          AND  session != ?
          AND  kind    IN ('decision', 'warning', 'resolved')
          AND  (query LIKE ? OR output_preview LIKE ? OR episodic_text LIKE ?)
        ORDER  BY memory_tier ASC, importance DESC, timestamp DESC
        LIMIT  ?
    """, (current_session,
          f"%{fname}%", f"%{fname}%", f"%{fname}%",
          MAX_NODES)).fetchall()

    if not rows:
        return None

    # Mark this (session, file) pair as injected
    if "file_injected" not in state:
        state["file_injected"] = {}
    state["file_injected"][state_key] = True

    _stamp_injected(conn, rows, trigger="file_recurrence", session=current_session)
    return _box(f"seen before: {fname}", rows)


# ── Trigger 4: Topic drift ───────────────────────────────────────────────────

def check_drift(
    query:           str | None,
    state:           dict,
    conn:            sqlite3.Connection,
    current_session: str,
) -> list[str] | None:
    """
    Detect topic drift using Jaccard keyword overlap across a rolling query window.

    ALWAYS appends query to rolling history — so every call contributes signal.
    Every DRIFT_INTERVAL calls, compares current keyword set against recent
    history window. If Jaccard similarity < DRIFT_THRESHOLD → topic shifted.

    At that moment: surface past context for the NEW topic before the model
    starts working without that history.

    Why Jaccard instead of embeddings:
      - Zero deps, no model cold-start, <1ms per check
      - Sufficient for "working on Golf routes → now working on Cairn internals"
      - Upgrade path: swap _jaccard() for cosine similarity once async embedding
        cache exists. State already stores query history for that migration.
    """
    if not query:
        return None

    # ── always update rolling history (regardless of drift check interval) ──
    history: list[str] = state.get("query_history", [])
    history.append(query[:200])
    if len(history) > 20:
        history = history[-20:]
    state["query_history"] = history

    # ── only run the comparison every DRIFT_INTERVAL calls ──────────────────
    n = state.get("call_counter", 0)
    if n % DRIFT_INTERVAL != 0:
        return None

    # Need enough history to compare against
    if len(history) <= DRIFT_INTERVAL:
        return None

    current_kw = _keyword_set(query)
    if not current_kw:
        return None

    # Build keyword set from prior history window (all entries except current)
    prior = history[:-1]   # everything before this call
    hist_kw: set[str] = set()
    for q in prior[-(DRIFT_INTERVAL * 2):]:   # look back up to 2x interval
        hist_kw |= _keyword_set(q)

    similarity = _jaccard(current_kw, hist_kw)
    if similarity >= DRIFT_THRESHOLD:
        return None

    # Topic shift confirmed — find most distinctive new keyword to search
    new_kw = current_kw - hist_kw
    keyword = max(new_kw, key=len) if new_kw else next(iter(current_kw))

    # Pull a CANDIDATE pool (wider than the final cap) so the relevance floor +
    # dedup below can pick the genuinely-relevant few, not just the first match.
    rows = conn.execute("""
        SELECT * FROM nodes
        WHERE  status  = 'active'
          AND  session != ?
          AND  kind    IN ('decision', 'warning', 'resolved', 'context_stamp')
          AND  (query LIKE ? OR output_preview LIKE ? OR episodic_text LIKE ?)
        ORDER  BY memory_tier ASC, importance DESC, timestamp DESC
        LIMIT  ?
    """, (current_session,
          f"%{keyword}%", f"%{keyword}%", f"%{keyword}%",
          MAX_NODES * 8)).fetchall()

    # RELEVANCE FLOOR + DEDUP + SILENCE DEFAULT. The trigger used to surface
    # one-keyword LIKE matches and — worse — fall through to "recent hot
    # decisions" at ~0% relevance, re-showing the same cards on every drift (the
    # live failure: agents got SQLite/golf cards on unrelated topics). Now:
    #   (1) NO fall-through — if the new topic matches nothing, surface NOTHING;
    #   (2) a card must actually be about the new topic (contain >= 2 of the
    #       current keywords, not just the single LIKE term);
    #   (3) no card already pushed THIS session may re-fire (dedup via ledger).
    # Silence is the safe default: a wrong card costs accuracy and trust; an
    # empty box costs neither.
    already = {r["node_id"] for r in conn.execute(
        "SELECT DISTINCT node_id FROM attention_ledger "
        "WHERE session=? AND channel='hook'", (current_session,)).fetchall()}

    def _on_topic(r):
        txt = ((r["query"] or "") + " " + (r["output_preview"] or "")
               + " " + (r["episodic_text"] or "")).lower()
        return sum(1 for kw in current_kw if kw.lower() in txt) >= 2

    rows = [r for r in rows if r["id"] not in already and _on_topic(r)][:MAX_NODES]

    if not rows:
        return None

    pct = int(similarity * 100)
    _stamp_injected(conn, rows, trigger="topic_drift", session=current_session)
    return _box(f"topic shift [{pct}%] -> {keyword[:22]}", rows)


# ── Main entry ────────────────────────────────────────────────────────────────

def run_inject(
    tool_name:       str,
    query:           str | None,
    result_count:    int | None,
    latency_ms:      int | None,
    current_session: str,
) -> None:
    """
    Called from hook.py after every tool call.
    Checks all triggers. Prints surface blocks to stdout if any fire.
    Wrapped in broad try/except — must never block Claude Code.
    """
    try:
        state = _load_state()
        # Session-change guard: reset warm_blocks_sent when session ID changes.
        # Handles crash/restart cases where stop_hook didn't fire — prevents
        # the new session from inheriting the previous session's token-fill count.
        if (state.get("_current_session") and
                state["_current_session"] != current_session):
            state["warm_blocks_sent"] = 0
        state["call_counter"]    = state.get("call_counter", 0) + 1
        state["_current_session"] = current_session

        conn   = _get_conn()
        blocks: list[str] = []

        # Trigger 1: counter heartbeat
        b = check_counter(state, conn, current_session)
        if b:
            blocks.extend(b)

        # Trigger 2: struggle injection
        b = check_struggle(result_count, latency_ms, query, tool_name, conn, current_session)
        if b:
            blocks.extend(b)

        # Trigger 3: file recurrence (Read/Edit path)
        file_path = query if tool_name in ("Read", "Edit", "Write") else None
        b = check_file_recurrence(tool_name, file_path, state, conn, current_session)
        if b:
            blocks.extend(b)

        # Trigger 4: topic drift (Jaccard keyword overlap across rolling window)
        b = check_drift(query, state, conn, current_session)
        if b:
            blocks.extend(b)

        conn.close()
        _save_state(state)

        if blocks:
            # THE CHANNEL. Plain stdout from a PostToolUse hook at exit 0 goes
            # to the user's transcript view, NOT the model's context. The only
            # supported way to put hook content in front of the model is the
            # hookSpecificOutput.additionalContext JSON envelope (or exit 2 +
            # stderr, which reads as an error). Raw print() here = decorative.
            text = "\n".join(blocks)
            envelope = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName":     "PostToolUse",
                    "additionalContext": text,
                }
            }, ensure_ascii=True)
            try:
                print(envelope)
            except Exception:
                sys.stdout.buffer.write((envelope + "\n").encode("utf-8", errors="replace"))

    except Exception:
        pass  # never block Claude Code on injection errors
