"""
cairn/codex_reader.py — batch import of PLAIN Codex/GPT chat from the on-disk
session store (~/.codex/sessions/**/rollout-*.jsonl) into the vault.

WHY THIS EXISTS. Codex's notify hook (codex_hook.py) only fires on agentic /
computer-use turns and backstage helper calls — it NEVER fires for plain
conversational chat. That chat isn't lost, though: Codex writes every turn to a
newline-delimited JSON "rollout" file on disk. This reader is the complete-capture
path the notify hook structurally cannot be — it reads files Codex already writes,
needs zero cooperation from Codex, and unifies with the hook's nodes under the
SAME codex-<thread_id> session + turn:<turn_id> dedup so the two never
double-capture.

Three distinct capture paths, kept separate on purpose:
  cairn_note              = salience   (deliberate, owner/agent-chosen notes)
  notify hook             = agentic    (real-time, notify-fired events, partial)
  import codex-sessions   = full chat  (this module; batch, dry-run first)

DESIGN (verified against the live store):
  - Recursive glob across ALL date dirs. A rollout file's path date is its
    CREATION date; threads are appended for their whole life and span days, so a
    "scan today only" reader would MISS still-active threads.
  - Session key = codex-<thread_uuid> (the uuid in the filename == the first
    session_meta.payload.id). The exact key the hook uses.
  - Parse the CLEAN event stream: turn_context carries turn_id + model;
    event_msg{user_message,agent_message} carry the literal prompt/reply;
    task_complete / turn_aborted / the next turn_context closes a turn. N
    agent_message events collapse into ONE agent node, so the write is identical
    to the hook's single-turn write (dedup alignment). response_item records are
    IGNORED — they duplicate the clean pair and are polluted with system /
    permission-instruction wrappers.
  - Idempotent + cross-lane safe: skip any turn whose turn:<turn_id> tag already
    exists in the session (reuses codex_hook._already_captured), so re-running
    and the notify hook never produce doubles. (Turns with no turn_id can't be
    deduped across runs — same accepted rare-duplicate risk the hook documents.)
  - FORWARD-ONLY by default (owner boundary): a watermark, set on first --apply
    and stored in ~/.cairn/codex_import_state.json, splits history-on-disk from
    new-going-forward. Only forward turns import by default; historical backfill
    is an explicit, bounded --include-before opt-in.
  - Attribution: the store has NO per-record account id (only ~/.codex/auth.json
    has one, machine-wide). The whole store is one account. Pass --account to
    stamp+lock it; else the codex-<id> session resolves to the codex identity. A
    second OpenAI login on the machine would be indistinguishable — documented
    known limit.

Stdlib only (Cairn law): json, re, glob, datetime, pathlib, typing.
READ-ONLY on the store — this module never writes anything under ~/.codex.
"""
from __future__ import annotations

import glob as _glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cairn.vault import Vault, MicroNode
from cairn.importer import TRUNC_QUERY, TRUNC_PREVIEW, _iso, _is_noise
from cairn.codex_hook import _already_captured, _session_for, _codex_model

CAIRN_HOME   = Path.home() / ".cairn"
STATE_FILE   = CAIRN_HOME / "codex_import_state.json"
DIAG_LOG     = CAIRN_HOME / "import_codex_diag.log"
DEFAULT_ROOT = Path.home() / ".codex" / "sessions"

# MUST match codex_hook.base_tags exactly, or cross-lane dedup breaks.
_BASE_TAGS = ["codex", "conversation"]

_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


# ── small state + diagnostics helpers ─────────────────────────────────────────
def _thread_uuid_from_name(path: Path) -> Optional[str]:
    """The thread UUID embedded in rollout-<created-ts>-<uuid>.jsonl."""
    m = _UUID_RE.search(path.name)
    return m.group(1) if m else None


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


def _safe_turn_id(tid):
    """Trust only a hex/dash turn id (Codex UUIDs). Anything else → None, so an
    odd id (quotes, spaces) can never corrupt the turn:<id> JSON tag or break the
    LIKE-based dedup — such a turn is treated as id-less instead."""
    if isinstance(tid, str) and re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F\-]{7,63}", tid):
        return tid
    return None


# ── per-file turn parser (the clean event-stream state machine) ───────────────
def _iter_turns(path: Path):
    """Stream ONE rollout file → (turns, stats). Each turn:
        {turn_id, model, user_text, agent_text, user_ts, agent_ts}
    Uses only the clean event stream; never raises on a bad line."""
    turns: list = []
    stats = {"compacted": 0, "bad_lines": 0, "session_meta": 0,
             "truncated": False, "no_ts": 0}

    cur_turn = None
    cur_model = None
    pending_user = None
    pending_user_ts = None
    pending_agent: list = []
    pending_agent_ts = None

    def flush():
        nonlocal pending_user, pending_user_ts, pending_agent, pending_agent_ts
        u = (pending_user or "").strip()
        a = "\n".join(t for t in pending_agent if t and t.strip()).strip()
        ts0 = pending_user_ts or pending_agent_ts
        if (u or a) and ts0 is not None:
            # both node timestamps fall back to the turn's one real ts, so the
            # forward/historical watermark split never sees a None (which _iso
            # would silently stamp as "now" and mis-file as forward).
            turns.append({
                "turn_id":    _safe_turn_id(cur_turn),
                "model":      cur_model,
                "user_text":  u,
                "agent_text": a,
                "user_ts":    pending_user_ts or pending_agent_ts,
                "agent_ts":   pending_agent_ts or pending_user_ts,
            })
        elif u or a:
            stats["no_ts"] += 1                       # real text but no timestamp → drop
        pending_user = None
        pending_user_ts = None
        pending_agent = []
        pending_agent_ts = None

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
                rtype = rec.get("type")
                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                ts = rec.get("timestamp")

                if rtype == "session_meta":
                    stats["session_meta"] += 1
                    continue
                if rtype == "compacted":
                    # prior-version history for correction — do NOT re-import as turns
                    stats["compacted"] += 1
                    continue
                if rtype == "turn_context":
                    if pending_user is not None or pending_agent:
                        flush()                      # close the previous turn
                    cur_turn = payload.get("turn_id")
                    m = payload.get("model")
                    if isinstance(m, str) and m.strip():
                        cur_model = m.strip()
                    continue
                if rtype == "event_msg":
                    ptype = payload.get("type")
                    if ptype == "user_message":
                        if pending_user is not None or pending_agent:
                            flush()                  # a new prompt starts a new turn
                        msg = payload.get("message")
                        if isinstance(msg, str):
                            pending_user = msg
                            pending_user_ts = ts
                    elif ptype == "agent_message":
                        msg = payload.get("message")
                        if isinstance(msg, str) and msg:
                            pending_agent.append(msg)
                            pending_agent_ts = ts
                    elif ptype in ("task_complete", "turn_aborted"):
                        flush()
                    continue
                # response_item / token_count / reasoning / function_call / … → ignored
    except Exception:
        stats["truncated"] = True                     # I/O error mid-file — partial read

    if pending_user is not None or pending_agent:
        flush()                                       # trailing turn at EOF
    return turns, stats


def _canonical_session(path: Path) -> str:
    """codex-<thread_uuid>. Prefer the session_meta whose id matches the filename
    uuid; fall back to the filename uuid (defensive) so a file always keys to one
    session even on an unexpected/forked meta."""
    fid = _thread_uuid_from_name(path)
    if fid:
        return _session_for(fid)
    # last-resort: first session_meta id in the file
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    pid = (rec.get("payload") or {}).get("id")
                    if isinstance(pid, str) and pid:
                        return _session_for(pid)
                break
    except Exception:
        pass
    return _session_for(path.stem)


# ── public API ────────────────────────────────────────────────────────────────
def read_codex_sessions(root=None, vault: Optional[Vault] = None,
                        account: Optional[str] = None, tier: int = 2,
                        since: Optional[str] = None,
                        include_before: Optional[str] = None,
                        watermark: Optional[str] = None,
                        dry_run: bool = True, limit: Optional[int] = None,
                        progress: Optional[Callable] = None,
                        debug: bool = False) -> dict:
    """Scan the Codex session store and (dry-run) preview or (apply) import plain
    chat as codex-<thread> conversation_turn nodes. Returns a report dict. See the
    module docstring for the forward-only / dedup / attribution contract."""
    root = Path(root) if root else DEFAULT_ROOT
    v = vault or Vault()
    say = progress or (lambda *a, **k: None)

    files = sorted(_glob.glob(str(root / "**" / "rollout-*.jsonl"), recursive=True))
    date_dirs = set()
    for fp in files:
        parts = Path(fp).parts
        if len(parts) >= 4:
            date_dirs.add("/".join(parts[-4:-1]))     # …/YYYY/MM/DD

    # forward-only watermark: provided → state → (first-ever) now
    state = _load_state()
    first_run = "watermark" not in state
    wm = watermark or state.get("watermark")
    provisional = False
    if not wm:
        wm = datetime.now(timezone.utc).isoformat()
        provisional = True                            # everything on disk is "history"
    wm = _iso(wm)

    inc_floor   = _iso(include_before) if include_before else None
    since_floor = _iso(since) if since else None

    report = {
        "root": str(root), "account": account, "locked": bool(account),
        "first_run": first_run, "provisional_watermark": provisional, "watermark": wm,
        "files_scanned": len(files), "date_dirs": len(date_dirs),
        "threads_found": 0, "threads_new": 0, "threads_fully_captured": 0,
        "forward_turns": 0, "forward_new": 0, "forward_user": 0, "forward_agent": 0,
        "forward_threads": 0,
        "historical_turns": 0, "historical_new": 0, "historical_threads": 0,
        "already_captured": 0, "compacted": 0, "bad_lines": 0,
        "date_min": None, "date_max": None,
        "hist_min": None, "hist_max": None,
        "samples": [], "preview": None,
        "applied": (not dry_run), "written_user": 0, "written_agent": 0,
        "written_nodes": 0, "sessions_written": 0, "backup": None,
        "dropped": 0, "truncated_files": 0,
    }

    if not files:
        return report

    # apply: reversible pre-snapshot of the sessions rows we may stamp
    backup_path = None
    added_ids: list = []
    if not dry_run:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = CAIRN_HOME / f"import-codex-backup-{stamp}.json"

    def _span(lo, hi, ts):
        ts = _iso(ts)
        return (ts if lo is None or ts < lo else lo,
                ts if hi is None or ts > hi else hi)

    sessions_snapshot: list = []
    processed = 0
    for fp in files:
        if limit is not None and processed >= limit:
            break
        path = Path(fp)
        session = _canonical_session(path)
        turns, stats = _iter_turns(path)
        report["compacted"] += stats["compacted"]
        report["bad_lines"] += stats["bad_lines"]
        report["dropped"] += stats.get("no_ts", 0)
        if stats.get("truncated"):
            report["truncated_files"] += 1
            _diag({"path": path.name, "event": "file_truncated_io_error",
                   "turns_before_error": len(turns)}, debug)
        if not turns:
            continue
        processed += 1
        report["threads_found"] += 1

        # snapshot this session row before any account stamping (apply only)
        if not dry_run:
            try:
                row = v.conn.execute(
                    "SELECT id, account, account_locked FROM sessions WHERE id=?",
                    (session,)).fetchone()
                sessions_snapshot.append(
                    {"id": session,
                     "account": (row["account"] if row else None),
                     "account_locked": (row["account_locked"] if row else None),
                     "existed": bool(row)})
            except Exception:
                pass

        thread_forward_new = 0
        thread_hist = 0
        thread_all_seen = True
        parent = None
        stamped = False

        # turns arrive in file/turn order; sort by timestamp for stable chaining
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
                thread_hist += 1
                report["hist_min"], report["hist_max"] = _span(
                    report["hist_min"], report["hist_max"], tts)
                do_import = bool(inc_floor and tts >= inc_floor)

            turn_id = t.get("turn_id")
            already = _already_captured(v, session, turn_id) if turn_id else False
            if already:
                report["already_captured"] += 1
                continue
            thread_all_seen = False
            if not do_import:
                continue

            # this turn WOULD import (dry-run) / DOES import (apply)
            u = t["user_text"] if not _is_noise(t["user_text"]) else ""
            a = t["agent_text"] if not _is_noise(t["agent_text"]) else ""
            if not u and not a:
                report["dropped"] += 1
                continue

            if is_forward:
                report["forward_new"] += 1
                thread_forward_new += 1
                if u:
                    report["forward_user"] += 1
                if a:
                    report["forward_agent"] += 1
            else:
                report["historical_new"] += 1

            if report["preview"] is None and (u or a):
                report["preview"] = {
                    "session": session,
                    "turn_id": (turn_id or "")[:8],
                    "user":  (u[:60] + ("…" if len(u) > 60 else "")),
                    "agent": (a[:60] + ("…" if len(a) > 60 else "")),
                }

            if dry_run:
                continue

            # ── APPLY: write the turn ATOMICALLY ──────────────────────────────
            if account and not stamped:
                # earliest turn in the file = session start; GUARDED ON CONFLICT so
                # an already-LOCKED account (a prior --account run, or the hook's
                # codex identity) is never silently overwritten — mirrors vault.py.
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

            model = t.get("model") or _codex_model()
            turn_tag = [f"turn:{turn_id}"] if turn_id else []
            if parent is None:
                parent = _last_node_id(v, session)

            # Both nodes with commit=False, then ONE commit — either the whole turn
            # lands or neither node does. A half-written turn would leave its
            # turn:<id> tag behind and make dedup skip the rest of it forever.
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
                v.conn.commit()                       # atomic: both nodes, or neither
            except Exception as e:
                try:
                    v.conn.rollback()
                except Exception:
                    pass
                _diag({"error": str(e)[:200], "turn": turn_id, "session": session,
                       "type": "turn_write_failed"}, debug)
                continue                              # skip; nothing counted or added
            parent = tp
            added_ids.extend(new_ids)
            if u:
                report["written_user"] += 1
            if a:
                report["written_agent"] += 1

        if thread_forward_new:
            report["forward_threads"] += 1
        if thread_hist:
            report["historical_threads"] += 1
        if thread_all_seen and turns:
            report["threads_fully_captured"] += 1
        if not thread_all_seen:
            report["threads_new"] += 1
        if len(report["samples"]) < 5:
            report["samples"].append(session)

    report["written_nodes"] = report["written_user"] + report["written_agent"]

    _diag({"root": str(root), "files": len(files), "threads": report["threads_found"],
           "forward_new": report["forward_new"], "historical_new": report["historical_new"],
           "already": report["already_captured"], "dry_run": dry_run}, debug)

    # ── APPLY: commit, persist watermark, write the reversible manifest ────────
    if not dry_run:
        try:
            v.conn.commit()
        except Exception:
            pass
        if first_run:
            state["watermark"] = wm                   # cut over: forward-only from here
        state["last_apply"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        # only a run that actually WROTE nodes needs a reversible manifest — this
        # keeps a recurring forward sweep (most runs capture nothing new) from
        # littering ~/.cairn with empty backup files.
        report["sessions_written"] = report["forward_threads"] if added_ids else 0
        if added_ids:
            try:
                backup_path.write_text(json.dumps({
                    "when": datetime.now(timezone.utc).isoformat(),
                    "root": str(root),
                    "account": account,
                    "watermark_before": (None if first_run else state.get("watermark")),
                    "watermark_after": state.get("watermark"),
                    "sessions_before": sessions_snapshot,
                    "added_node_ids": added_ids,
                    "count": len(added_ids),
                    "note": "append-only import. To reverse: void the added_node_ids "
                            "(cairn void <id>) — nodes are never deleted. If --account "
                            "changed a session's stamp, sessions_before holds each "
                            "session's account/account_locked prior to this run.",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                report["backup"] = str(backup_path)
            except Exception:
                pass

    return report


def _last_node_id(vault, session: str) -> Optional[str]:
    """Newest active node in the session — the chain anchor for appended turns
    (mirrors codex_hook._last_node_id so imported turns extend the same thread)."""
    try:
        row = vault.conn.execute(
            "SELECT id FROM nodes WHERE session=? AND status='active' "
            "ORDER BY timestamp DESC LIMIT 1", (session,)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
