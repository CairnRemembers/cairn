"""
cairn/capture.py
Conversation and intent capture — the layer that was missing.

Tool calls tell you WHAT the agent did.
Conversation turns tell you WHY.
Context stamps tell you how we got here.

Three things this module captures that nothing else does:

1. conversation_turn — an actual exchange. User says something. Agent responds.
   Both sides stored. Both embedded. Both queryable.
   "what did the user say about the auth bug?" now has an answer.

2. context_stamp — the session's intent and state, written explicitly.
   Why did this session start? What changed since last time?
   What's unresolved? What's the mood? What's the pressure?
   Written at session start (load PROTOCOL.md, write a stamp).
   Written at key pivots (direction changed, write a stamp).

3. compress_summary — when Claude Code compresses context, it produces
   a summary of what's being collapsed. PreCompact fires at that moment.
   This module extracts that summary and writes it as a node before it's lost.
   The highest-leverage capture point in the entire system.

Usage by agent during sessions:
  python -m cairn note --kind=context_stamp "building Cairn v2, pivoted from flat structure to backend abstraction after hardware research"
  python -m cairn note --kind=conversation_turn --speaker=user "user wants the system to capture actual conversation, not just tool calls"

Usage by hooks (automatic):
  compact_hook.py calls capture.extract_compress_summary(event)
  which writes a compress_summary node before context collapses
"""
from __future__ import annotations
import os, json
from pathlib import Path
from typing import Optional

from cairn.vault import Vault, MicroNode


def get_session() -> str:
    """Unified session — same source as __main__.py."""
    sid = (os.environ.get("CAIRN_SESSION") or
           os.environ.get("CLAUDE_SESSION_ID"))
    if sid:
        return sid
    session_file = Path.home() / ".cairn" / "last_session.txt"
    if session_file.exists():
        saved = session_file.read_text().strip()
        if saved:
            return saved
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_parent() -> Optional[str]:
    state = Path.home() / ".cairn" / "last_node.txt"
    if state.exists():
        return state.read_text().strip() or None
    return None


def _project_keys() -> set[str]:
    """Declared project tags — the keys of ~/.cairn/projects.json. READ-ONLY.
    Empty set (never guess) when the file is missing or unreadable."""
    f = Path.home() / ".cairn" / "projects.json"
    if f.exists():
        try:
            return {str(k).lower() for k in
                    json.loads(f.read_text(encoding="utf-8")).keys()}
        except Exception:
            pass
    return set()


def resolve_project_tag(cwd: Optional[str] = None) -> Optional[str]:
    """Derive the project tag for a capture from where the work is happening —
    zero tokens, resolved at write time (tags freeze once written).

    Resolution order (first hit wins; NO match → None, never a guess):
      (a) CAIRN_PROJECT env var — the reliable channel, and the only one the
          MCP server can trust (its cwd is the client's, not the project's).
      (b) ~/.cairn/project_map.json {"substring-or-folder": "tag", ...} —
          an optional, READ-ONLY override map. Matched against the full cwd
          (lowercased) as a substring, then against each path component.
          Create nothing — only read if it already exists.
      (c) fallback: the cwd folder name and its parents (up to 2 levels up),
          lowercased, matched against the project keys in projects.json. A cwd
          ending in \\cairn → "cairn".

    Returns the tag string, or None when nothing matches confidently.
    """
    # (a) explicit env — wins everywhere, the MCP-safe channel
    env = (os.environ.get("CAIRN_PROJECT") or "").strip()
    if env:
        return env

    path = cwd if cwd is not None else os.getcwd()
    if not path:
        return None
    low = path.replace("\\", "/").lower().rstrip("/")
    parts = [p for p in low.split("/") if p]

    # (b) optional user map — READ-ONLY, only if present
    mf = Path.home() / ".cairn" / "project_map.json"
    if mf.exists():
        try:
            mapping = json.loads(mf.read_text(encoding="utf-8"))
            if isinstance(mapping, dict):
                # exact folder-component match first (most specific),
                # then substring-anywhere-in-cwd
                for key, tag in mapping.items():
                    if isinstance(key, str) and key.lower() in parts:
                        if isinstance(tag, str) and tag.strip():
                            return tag.strip()
                for key, tag in mapping.items():
                    if isinstance(key, str) and key.lower() and key.lower() in low:
                        if isinstance(tag, str) and tag.strip():
                            return tag.strip()
        except Exception:
            pass

    # (c) folder name (and up to 2 parents) vs declared project keys
    keys = _project_keys()
    if keys:
        for name in parts[-3:][::-1]:   # cwd folder first, then 2 parents up
            if name in keys:
                return name
    return None


def write_turn(
    text: str,
    speaker: str = "agent",         # "agent" or "user"
    session: Optional[str] = None,
    vault: Optional[Vault] = None,
    model: str = "unknown",
    truncate: int = 2000,
    usage: Optional[dict] = None,   # transcript token usage (set on agent turns)
    tool_calls: Optional[list] = None,  # 5a: tools fired this turn (agent turns)
) -> MicroNode:
    """
    Write a conversation turn to the vault.

    speaker="user"  → what the user said, their intent, their meaning
    speaker="agent" → what the agent responded, its reasoning, its conclusions

    The text is truncated to `truncate` chars for the query field
    (the full text lives in output_preview).
    Both sides are embedded into episodic vectors.
    "what did the user say about authentication?" is now queryable.
    """
    v       = vault or Vault()
    sess    = session or get_session()
    parent  = get_parent()

    # detect model from env if not provided
    if model == "unknown":
        model = (os.environ.get("CLAUDE_MODEL") or
                 os.environ.get("OPENAI_MODEL") or
                 os.environ.get("MODEL_NAME") or
                 os.environ.get("CAIRN_MODEL") or   # manual override — last resort
                 "unknown")

    summary = text[:truncate]

    # cwd-derived project tag, resolved at write (tags freeze once written).
    # Additive only: append the tag if we can identify the project, else the
    # tags stay exactly ["conversation", speaker] as before. Never guesses.
    tags = ["conversation", speaker]
    proj = resolve_project_tag()
    if proj and proj not in tags:
        tags.append(proj)

    node = v.write(MicroNode(
        session        = sess,
        kind           = "conversation_turn",
        query          = summary,
        output_preview = text,         # full text preserved here
        parent         = parent,
        speaker        = speaker,
        model          = model if speaker == "agent" else "human",
        agent_role     = "worker",
        tags           = tags,
        tokens_in          = (usage or {}).get("input_tokens"),
        tokens_out         = (usage or {}).get("output_tokens"),
        tokens_cache_read  = (usage or {}).get("cache_read_input_tokens"),
        tokens_cache_write = (usage or {}).get("cache_creation_input_tokens"),
        tool_calls         = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
    ))

    # update last_node for chaining
    state = Path.home() / ".cairn" / "last_node.txt"
    state.write_text(node.id)
    return node


def write_stamp(
    intent: str,
    carried_forward: str = "",
    open_questions: list[str] | None = None,
    session: Optional[str] = None,
    vault: Optional[Vault] = None,
    model: str = "unknown",
    memory_tier: int = 1,   # warm by default — only hot when explicitly promoted
) -> MicroNode:
    """
    Write a context stamp — the session's intent and state.

    This is the answer to: "how did we get here?"
    Load PROTOCOL.md. Read it. Then write a stamp that says:
      - why this session started
      - what carries forward from last time
      - what's still open
      - what changed

    Every session should start with one of these.
    Every major pivot should get one.

    Example:
      cairn note --kind=context_stamp "building Cairn v2. carried forward:
      abstraction layer plan, hardware research. open: conversation capture,
      dashboard. pivoting: from per-session thinking to cross-session retrieval."
    """
    v    = vault or Vault()
    sess = session or get_session()

    if model == "unknown":
        model = (os.environ.get("CAIRN_MODEL") or
                 os.environ.get("CLAUDE_MODEL") or "unknown")

    parts = [f"session intent: {intent}"]
    if carried_forward:
        parts.append(f"carried forward: {carried_forward}")
    if open_questions:
        parts.append(f"open: {'; '.join(open_questions)}")

    full_text = "\n".join(parts)

    node = v.write(MicroNode(
        session        = sess,
        kind           = "context_stamp",
        query          = intent[:500],
        output_preview = full_text,
        model          = model,
        memory_tier    = memory_tier,
        tags           = ["context", "intent", "session-start"],
    ))

    state = Path.home() / ".cairn" / "last_node.txt"
    state.write_text(node.id)
    return node


def extract_compress_summary(
    event: dict,
    session: Optional[str] = None,
    vault: Optional[Vault] = None,
    model: str = "unknown",
) -> Optional[MicroNode]:
    """
    Called by compact_hook.py when PreCompact fires.

    Claude Code's PreCompact event may include a summary field describing
    what's being compressed. This function extracts that summary and writes
    it as a node BEFORE the context window collapses.

    This is the highest-leverage capture point:
    - Fires at the moment context is about to be lost
    - The summary IS the condensed meaning of the session so far
    - Writing it preserves what the model had coherently understood
    - Without this, every compression loses the accumulated understanding

    The node gets memory_tier=0 (hot) — it should always be loaded first
    next session because it represents the last coherent state before collapse.
    """
    v    = vault or Vault()
    sess = session or event.get("session_id") or get_session()

    if model == "unknown":
        model = (os.environ.get("CAIRN_MODEL") or
                 os.environ.get("CLAUDE_MODEL") or "unknown")

    # Claude Code PreCompact event may contain summary in various fields
    summary = (event.get("summary") or
               event.get("context_summary") or
               event.get("compression_summary") or
               event.get("message") or "")

    # also check for any nested summary
    if not summary and isinstance(event.get("data"), dict):
        summary = event["data"].get("summary", "")

    if not summary:
        # no summary available — write a marker that compression happened
        # so the audit trail shows the event even without content
        summary = "context compressed — no summary extracted"

    node = v.write(MicroNode(
        session        = sess,
        kind           = "context_stamp",
        query          = f"[PreCompact] {summary[:400]}",
        output_preview = summary,
        model          = model,
        memory_tier    = 0,   # hot — load this first next session
        tags           = ["compress-event", "pre-compact", "checkpoint"],
    ))
    return node


def session_intent_from_protocol(
    protocol_path: Path,
    session: Optional[str] = None,
    vault: Optional[Vault] = None,
) -> Optional[MicroNode]:
    """
    At session START: read PROTOCOL.md, extract what carries forward,
    write ONE warm context_stamp so this session knows why it exists.

    Called by the agent at the beginning of a session:
      python -m cairn orient

    Deduplication guard: only writes ONE stamp per session. If a
    context_stamp already exists for this session, returns None and skips.

    Writes warm (tier=1) not hot — the model explicitly reads the orient
    output directly (via _print_orient_digest). The stamp is a vault record
    for search/chain purposes, not for automatic injection.
    """
    if not protocol_path.exists():
        return None

    v    = vault or Vault()
    sess = session or get_session()

    # ── deduplication guard: one stamp per session ────────────────────────────
    existing = v.conn.execute(
        "SELECT id FROM nodes WHERE session=? AND kind='context_stamp' AND status='active' LIMIT 1",
        (sess,)
    ).fetchone()
    if existing:
        return None   # already oriented this session — don't write duplicates

    text = protocol_path.read_text(encoding="utf-8")

    # ── extract previous session name from header ─────────────────────────────
    prev_sess = ""
    for line in text.split("\n"):
        if line.startswith("session:"):
            prev_sess = line.split(":", 1)[1].strip()
            break

    # ── extract real decisions (not raw Markdown node IDs) ────────────────────
    decisions = []
    in_decisions = False
    for line in text.split("\n"):
        if line.strip() == "## Decisions made":
            in_decisions = True
            continue
        if in_decisions:
            if line.startswith("## "):
                break
            # bullet points that look like actual decisions (not metadata lines)
            if line.startswith("- ") and not line.startswith("- `") and len(line) > 10:
                clean = line[2:].split("[")[0].strip()  # strip [model] suffix
                if clean and not clean.startswith("_"):
                    decisions.append(clean[:100])
            if len(decisions) >= 3:
                break

    # ── extract open item count ───────────────────────────────────────────────
    open_count = text.count("\n- ", text.find("## Open items"), text.find("## Hard points")) if "## Open items" in text and "## Hard points" in text else 0

    # ── build clean intent string ─────────────────────────────────────────────
    parts = [f"continuing from {prev_sess}" if prev_sess else "continuing from previous session"]
    if decisions:
        parts.append(f"last decisions: {' / '.join(d[:80] for d in decisions[:2])}")
    if open_count > 0:
        parts.append(f"{open_count} open items pending")

    intent = " | ".join(parts)

    return write_stamp(
        intent      = intent,
        vault       = v,
        session     = sess,
        memory_tier = 1,   # warm — orient prints content directly; stamp is for vault record only
    )
