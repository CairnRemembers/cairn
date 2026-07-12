"""
cairn/local_agent_reader.py — batch import of Claude Desktop "local agent mode"
chat (Cowork / Dispatch-from-phone) from the on-disk transcript store.

WHY THIS EXISTS. Claude Desktop's local-agent sessions run as a sandboxed nested
agent with their OWN isolated .claude dir, so the user's global
~/.claude/settings.json Stop hook (turn_hook.py) never fires for them — their
turns are hook-less and go uncaptured (verified 2026-07-07: Cowork reads the
vault via MCP but writes nothing back). But the sandbox writes a STANDARD Claude
Code transcript JSONL — the exact schema turn_hook already parses. This reader
is the complete-capture path those hook-less surfaces can't get from a hook: it
reads files the app already writes, needs zero cooperation, and unifies turns
under a local-agent-<session> key with turn:<uuid> dedup so a re-run — or a
future tail-watcher on the same files — never double-captures.

Direct sibling of codex_reader.py (same forward-only watermark / dedup /
dry-run-then-apply / --account contract). Parsing is single-sourced: it reuses
turn_hook's _text_from_content / _is_real_user_turn so the Claude-transcript
format lives in exactly one place.

Three capture paths for Claude surfaces, kept separate on purpose:
  cairn_note                    = salience  (deliberate notes)
  turn_hook (Stop hook)         = real-time (CLI/hooked surfaces only)
  import local-agent-sessions   = full chat (this module; hook-less surfaces)

Stdlib only (Cairn law). READ-ONLY on the transcript store — never writes under
the Claude app dirs.
"""
from __future__ import annotations

import glob as _glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cairn.vault import Vault, MicroNode
from cairn.importer import TRUNC_QUERY, TRUNC_PREVIEW, _iso, _is_noise
from cairn.turn_hook import _text_from_content, _is_real_user_turn, _salient

CAIRN_HOME = Path.home() / ".cairn"
STATE_FILE = CAIRN_HOME / "local_agent_import_state.json"
DIAG_LOG   = CAIRN_HOME / "import_local_agent_diag.log"

# MUST stay stable — dedup + a future tail-watcher key off the turn:<uuid> tag,
# and cross-lane alignment depends on these base tags not drifting.
_BASE_TAGS = ["claude-desktop", "local-agent", "conversation"]

# Cowork/Dispatch "brief mode" injects runtime nudges on the USER channel that no
# human typed (e.g. "You ended the turn without calling SendUserMessage…"). The
# agent's reply to one is a runtime ack, not conversation — drop the whole turn.
_HARNESS_INJECT_MARKERS = (
    "without calling sendusermessage",
    "you ended the turn without",
    "in brief mode you must use sendusermessage",
)


def _is_harness_inject(text) -> bool:
    t = (text or "").strip().lower()
    return any(m in t[:160] for m in _HARNESS_INJECT_MARKERS)


def _default_roots() -> list:
    """Claude Desktop local-agent-mode transcript stores, per platform — every
    candidate that EXISTS. MSIX-packaged installs (Microsoft Store) virtualize
    AppData\\Roaming\\Claude to a Packages\\Claude_*\\LocalCache path that a plain
    process must read directly (the Roaming alias isn't stat-able outside the
    package sandbox), so glob that first, then fall back to a non-packaged
    install. Overridable via --root / the root= arg."""
    home = Path.home()
    cands: list = []
    if sys.platform == "win32":
        cands += [Path(p) for p in _glob.glob(str(
            home / "AppData" / "Local" / "Packages" / "Claude_*" /
            "LocalCache" / "Roaming" / "Claude" / "local-agent-mode-sessions"))]
        cands.append(home / "AppData" / "Roaming" / "Claude" / "local-agent-mode-sessions")
    elif sys.platform == "darwin":
        cands.append(home / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions")
    else:
        cands.append(home / ".config" / "Claude" / "local-agent-mode-sessions")
    seen, out = set(), []
    for c in cands:
        s = str(c)
        if s not in seen and c.exists():
            seen.add(s)
            out.append(c)
    return out


# ── state + diagnostics (mirror codex_reader) ─────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def _diag(rec: dict, on: bool) -> None:
    """Metadata-only diagnostic line (NO transcript bodies) — mirrors the hook."""
    if not on:
        return
    try:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        with open(DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **rec},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def _session_for(sid: str) -> str:
    """local-agent-<sessionId>. Blank id → a single fallback bucket so a file
    still keys to exactly one session."""
    return f"local-agent-{sid}" if sid else "local-agent-unknown"


def _already_captured(vault, session: str, turn_id: str) -> bool:
    """Idempotency: a turn tagged turn:<uuid> already lives in this session.
    Mirrors codex_hook._already_captured so a re-run — or a future tail-watcher
    on the same transcript — never doubles. Only meaningful with a turn_id."""
    if not turn_id:
        return False
    try:
        row = vault.conn.execute(
            "SELECT 1 FROM nodes WHERE session=? AND tags LIKE ? LIMIT 1",
            (session, f'%"turn:{turn_id}"%')).fetchone()
        return row is not None
    except Exception:
        return False


def _last_node_id(vault, session: str):
    """Newest active node in this session — the chain parent, so a thread becomes
    a walkable path just like the hook / codex import produce."""
    try:
        row = vault.conn.execute(
            "SELECT id FROM nodes WHERE session=? AND status='active' "
            "ORDER BY timestamp DESC LIMIT 1", (session,)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


# ── per-file turn parser (Claude Code transcript → clean user/agent pairs) ────
def _iter_turns(path: Path):
    """Stream ONE transcript .jsonl → (turns, session_id, stats). Each turn:
        {turn_id, model, user_text, agent_text, user_ts, agent_ts}
    Pairs a real user message with the assistant reply(ies) that follow it, up to
    the next real user message (tool_use / tool_result records interleave a turn
    but never split it). Reuses turn_hook's helpers so the format is single-
    sourced. Never raises on a bad line."""
    turns: list = []
    stats = {"bad_lines": 0, "truncated": False}
    session_id = ""

    p_user = p_user_ts = p_user_uuid = None
    p_agent_parts: list = []
    p_agent_ts = p_agent_uuid = p_model = None

    def flush():
        nonlocal p_user, p_user_ts, p_user_uuid
        nonlocal p_agent_parts, p_agent_ts, p_agent_uuid, p_model
        u = (p_user or "").strip()
        a = "\n".join(t for t in p_agent_parts if t and t.strip()).strip()
        if u or a:
            # dedup id = the assistant turn's uuid (a real reply); fall back to the
            # user uuid for a trailing user-only turn. Stable across re-runs and a
            # future tail-watcher reading the same file.
            turns.append({
                "turn_id":    p_agent_uuid or p_user_uuid,
                "model":      p_model,
                "user_text":  u,
                "agent_text": a,
                "user_ts":    p_user_ts or p_agent_ts,
                "agent_ts":   p_agent_ts or p_user_ts,
            })
        p_user = p_user_ts = p_user_uuid = None
        p_agent_parts = []
        p_agent_ts = p_agent_uuid = p_model = None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    stats["bad_lines"] += 1
                    continue
                if not isinstance(rec, dict):
                    continue
                if not session_id:
                    sid = rec.get("sessionId")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                rtype = rec.get("type")
                if rtype == "user":
                    if not _is_real_user_turn(rec):
                        continue                      # tool_result echo, not typed
                    if p_user is not None or p_agent_parts:
                        flush()                       # a new prompt closes the turn
                    msg = rec.get("message") or {}
                    p_user = _text_from_content(msg.get("content"))
                    p_user_ts = rec.get("timestamp")
                    p_user_uuid = rec.get("uuid")
                elif rtype == "assistant":
                    msg = rec.get("message") or {}
                    txt = _text_from_content(msg.get("content"))
                    if txt and txt.strip():
                        p_agent_parts.append(txt)
                    m = msg.get("model")
                    if isinstance(m, str) and m.strip():
                        p_model = m.strip()
                    p_agent_ts = rec.get("timestamp")
                    p_agent_uuid = rec.get("uuid")
                # queue-operation / last-prompt / attachment / summary / system /
                # file-history-snapshot / … are not turns → skip
    except Exception:
        stats["truncated"] = True

    if p_user is not None or p_agent_parts:
        flush()
    return turns, session_id, stats


# ── public API ────────────────────────────────────────────────────────────────
def read_local_agent_sessions(root=None, vault: Optional[Vault] = None,
                              account: Optional[str] = None, tier: int = 2,
                              since: Optional[str] = None,
                              include_before: Optional[str] = None,
                              watermark: Optional[str] = None,
                              dry_run: bool = True, limit: Optional[int] = None,
                              progress: Optional[Callable] = None,
                              debug: bool = False) -> dict:
    """Scan the Claude Desktop local-agent transcript store and (dry-run) preview
    or (apply) import chat as local-agent-<session> conversation_turn nodes.
    Same forward-only / dedup / attribution contract as read_codex_sessions."""
    roots = [Path(root)] if root else _default_roots()
    v = vault or Vault()
    say = progress or (lambda *a, **k: None)

    report = {
        "root": (";".join(str(r) for r in roots) if roots else None), "account": account,
        "locked": bool(account), "files_scanned": 0, "threads_found": 0,
        "forward_turns": 0, "forward_new": 0, "forward_user": 0, "forward_agent": 0,
        "historical_turns": 0, "historical_new": 0, "already_captured": 0,
        "bad_lines": 0, "truncated_files": 0, "dropped": 0,
        "date_min": None, "date_max": None, "preview": None,
        "first_run": False, "provisional_watermark": False, "watermark": None,
        "applied": (not dry_run), "written_user": 0, "written_agent": 0,
        "written_nodes": 0, "sessions_written": 0, "backup": None,
    }
    if not roots:
        return report

    # transcript files live under …/local_ditto_<id>/.claude/projects/**/*.jsonl.
    # scope the glob to that shape so per-session audit.jsonl (and other stray
    # jsonl) never get parsed as conversation.
    files = []
    for r in roots:
        files += _glob.glob(
            str(r / "**" / ".claude" / "projects" / "**" / "*.jsonl"), recursive=True)
    files = sorted(f for f in files if Path(f).name != "audit.jsonl")
    report["files_scanned"] = len(files)
    if not files:
        return report

    # forward-only watermark: provided → state → (first-ever) now
    state = _load_state()
    first_run = "watermark" not in state
    wm = watermark or state.get("watermark")
    provisional = False
    if not wm:
        wm = datetime.now(timezone.utc).isoformat()
        provisional = True                            # everything on disk is history
    wm = _iso(wm)
    inc_floor   = _iso(include_before) if include_before else None
    since_floor = _iso(since) if since else None
    report.update(first_run=first_run, provisional_watermark=provisional, watermark=wm)

    def _span(lo, hi, ts):
        ts = _iso(ts)
        return (ts if lo is None or ts < lo else lo,
                ts if hi is None or ts > hi else hi)

    backup_path = None
    added_ids: list = []
    sessions_written: set = set()
    if not dry_run:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = CAIRN_HOME / f"import-local-agent-backup-{stamp}.json"

    processed = 0
    for fp in files:
        if limit is not None and processed >= limit:
            break
        path = Path(fp)
        turns, sid, stats = _iter_turns(path)
        report["bad_lines"] += stats["bad_lines"]
        if stats.get("truncated"):
            report["truncated_files"] += 1
        if not turns:
            continue
        processed += 1
        report["threads_found"] += 1
        session = _session_for(sid or path.stem)

        parent = None
        stamped = False
        turns.sort(key=lambda t: _iso(t.get("user_ts") or t.get("agent_ts")))

        for t in turns:
            tts = _iso(t.get("user_ts") or t.get("agent_ts"))
            report["date_min"], report["date_max"] = _span(
                report["date_min"], report["date_max"], tts)
            if since_floor and tts < since_floor:
                report["dropped"] += 1
                continue

            is_forward = tts >= wm
            if is_forward:
                report["forward_turns"] += 1
                do_import = True
            else:
                report["historical_turns"] += 1
                do_import = bool(inc_floor and tts >= inc_floor)

            turn_id = t.get("turn_id")
            if turn_id and _already_captured(v, session, turn_id):
                report["already_captured"] += 1
                continue
            if not do_import:
                continue

            # brief-mode runtime nudge (+ its ack) is not conversation → drop it
            # whole; otherwise salience-gate like turn_hook so local-agent capture
            # matches the real-time Claude experience (no pleasantries / bare acks).
            if _is_harness_inject(t["user_text"]):
                u = a = ""
            else:
                ur, ar = t["user_text"], t["agent_text"]
                u = ur if (_salient(ur, "user")  and not _is_noise(ur)) else ""
                a = ar if (_salient(ar, "agent") and not _is_noise(ar)) else ""
            if not u and not a:
                report["dropped"] += 1
                continue

            if is_forward:
                report["forward_new"] += 1
                report["forward_user"] += 1 if u else 0
                report["forward_agent"] += 1 if a else 0
            else:
                report["historical_new"] += 1

            if report["preview"] is None and (u or a):
                report["preview"] = {
                    "session": session, "turn_id": (turn_id or "")[:8],
                    "user":  (u[:60] + ("…" if len(u) > 60 else "")),
                    "agent": (a[:60] + ("…" if len(a) > 60 else "")),
                }
            if dry_run:
                continue

            # ── APPLY: stamp the account once (guarded), then write atomically ──
            if account and not stamped:
                try:
                    started = _iso(turns[0].get("user_ts") or turns[0].get("agent_ts"))
                    v.conn.execute(
                        "INSERT INTO sessions (id, started_at, account, account_locked) "
                        "VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                        "account = CASE WHEN account_locked = 0 THEN excluded.account "
                        "               ELSE account END, "
                        "account_locked = MAX(account_locked, excluded.account_locked)",
                        (session, started, account))
                    v.conn.commit()
                except Exception:
                    pass
                stamped = True

            model = t.get("model") or "claude"
            turn_tag = [f"turn:{turn_id}"] if turn_id else []
            if parent is None:
                parent = _last_node_id(v, session)
            try:
                tp, new_ids = parent, []
                if u:
                    n = v.write(MicroNode(
                        session=session, kind="conversation_turn",
                        query=u[:TRUNC_QUERY], output_preview=u[:TRUNC_PREVIEW],
                        episodic_full=u if len(u) > TRUNC_PREVIEW else None,
                        parent=tp, speaker="user", model="human",
                        agent_role="worker", memory_tier=tier,
                        tags=_BASE_TAGS + ["user"] + turn_tag,
                        timestamp=_iso(t.get("user_ts"))), commit=False)
                    tp = n.id
                    new_ids.append(n.id)
                if a:
                    n = v.write(MicroNode(
                        session=session, kind="conversation_turn",
                        query=a[:TRUNC_QUERY], output_preview=a[:TRUNC_PREVIEW],
                        episodic_full=a if len(a) > TRUNC_PREVIEW else None,
                        parent=tp, speaker="agent", model=model,
                        agent_role="worker", memory_tier=tier,
                        tags=_BASE_TAGS + ["agent"] + turn_tag,
                        timestamp=_iso(t.get("agent_ts"))), commit=False)
                    tp = n.id
                    new_ids.append(n.id)
                v.conn.commit()                       # atomic: whole turn or neither
            except Exception as e:
                try:
                    v.conn.rollback()
                except Exception:
                    pass
                _diag({"error": str(e)[:200], "turn": turn_id, "session": session,
                       "type": "turn_write_failed"}, debug)
                continue
            parent = tp
            added_ids.extend(new_ids)
            report["written_user"]  += 1 if u else 0
            report["written_agent"] += 1 if a else 0
            sessions_written.add(session)

    # ── finalize (apply only): persist watermark + a reversible backup ─────────
    report["written_nodes"] = report["written_user"] + report["written_agent"]
    report["sessions_written"] = len(sessions_written)
    if not dry_run:
        if first_run or "watermark" not in state:
            state["watermark"] = wm                   # forward-only from first apply
        state["last_apply"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        if backup_path is not None and added_ids:
            try:
                backup_path.write_text(json.dumps(
                    {"added_ids": added_ids, "account": account,
                     "sessions": sorted(sessions_written)},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                report["backup"] = str(backup_path)
            except Exception:
                pass
    return report
