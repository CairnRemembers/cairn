"""
cairn/codex_hook.py — real-time conversation capture from OpenAI Codex.

Codex (desktop app + CLI) fires a `notify` program on the agent-turn-complete
event, passing a JSON payload as the FINAL argument. This module IS that program
(one hop, chained): it captures the just-finished exchange into the vault, then
re-runs whatever notify command was there before, exactly as Codex would have.

Codex's counterpart to Claude Code's turn_hook.py (the Stop hook) — same job,
different plumbing. Claude streams a transcript we parse; Codex hands us the two
messages directly on the command line. We write the SAME nodes: a user
conversation_turn (model="human") and an agent conversation_turn (model = the
last turn_context in the thread's own rollout file — the model picker's truth —
falling back to config.toml), chained within a codex-<thread> session.

FAIL-SAFE IS THE WHOLE POINT. This program stands between Codex and OpenAI's own
notify plumbing (the user's codex-computer-use.exe). If capture throws, hangs on
a locked vault, or the schema drifts under us, Codex must not notice. So EVERY
capture step is wrapped: any exception → append the raw payload + traceback to
~/.cairn/codex_hook_debug.log → STILL run the chain → exit 0. A wall-clock guard
abandons a slow/locked vault rather than delay the chain. The hook is incapable
of breaking Codex — that invariant outranks capturing the turn.

Invoked as (install writes this into config.toml's notify line):
  python -X utf8 -m cairn codex-hook [--chain <program> [chain-args...] --] <payload-json>

The payload is ALWAYS the last argv (Codex appends it). --chain ... -- brackets
the ORIGINAL notify command so we can replay it byte-for-byte; each original arg
is its own argv element, so args containing spaces survive untouched.

Payload schema (per developers.openai.com/codex/config-reference), treated
DEFENSIVELY — fields may be missing or renamed across Codex builds:
  {"type":"agent-turn-complete","turn-id":..,"thread-id":..,
   "input-messages":[..],"last-assistant-message":..}

Stdlib only (Cairn law): json, sys, subprocess, tomllib, pathlib, datetime,
traceback.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# self-contained: add the cairn package root to sys.path so this runs as a hook
# without needing a pip install, mirroring turn_hook.py / compact_hook.py.
sys.path.insert(0, str(Path(__file__).parent.parent))

# ~/.codex — read-only from here (config.toml is only ever WRITTEN by the
# install/uninstall CLI, never by the hook). Model truth is PER-THREAD in the
# sessions rollout; config.toml's global `model` key does not follow the model
# picker (proven 2026-07-11: picker Luna→Terra→Sol left the key frozen at luna)
# and is only the fallback.
CODEX_HOME     = Path.home() / ".codex"
CODEX_CONF     = CODEX_HOME / "config.toml"
CODEX_SESSIONS = CODEX_HOME / "sessions"

# Backward-scan tuning for finding the last turn_context in a rollout. A
# turn_context lands at every turn START, so everything the final turn streamed
# (event_msg lines can be huge) sits between it and EOF — a fixed one-shot tail
# missed it on real threads (live probe, 2026-07-11). Scan backward in steps,
# extending until found, bounded by a hard cap that keeps the worst case well
# inside the capture budget.
ROLLOUT_SCAN_STEP_BYTES = 262_144      # 256 KB per backward read
ROLLOUT_SCAN_CAP_BYTES  = 33_554_432   # give up past 32 MB → config fallback

CAIRN_HOME  = Path.home() / ".cairn"
DEBUG_LOG   = CAIRN_HOME / "codex_hook_debug.log"

# Wall-clock budget for the WHOLE capture attempt. The vault under WAL rarely
# blocks, but a stuck lock or slow disk must not delay Codex's own notify — if
# capture can't finish inside this, we log-and-bail to the chain. Generous
# enough for a cold Vault() open (schema + migrations) on a healthy machine.
CAPTURE_BUDGET_SEC = 4.0

# Chain runs after capture. Short timeout, errors swallowed — the chained
# program is OpenAI's plumbing; we replay it and move on, we don't babysit it.
CHAIN_TIMEOUT_SEC  = 20.0

# query = short display/search field; output_preview = a larger display slice.
# And — like the importer — episodic_full carries the COMPLETE text when a turn
# overflows output_preview's cap, so the derived episodic_text keeps full fidelity
# and nothing past the cap is lost (owner bar: full capture of whatever is said).
# The display caps stay bounded so one runaway turn can't bloat those fields.
TRUNC_QUERY   = 2000
TRUNC_PREVIEW = 8000


# ── debug log (the adaptation path when the schema drifts) ────────────────────
def _debug(payload_raw: str, note: str) -> None:
    """Append a raw payload + traceback + note to the debug log. This is the
    single place a broken capture leaves a trace — and the file to read when a
    Codex build renames a field: the raw payload here shows the NEW shape, and
    the fix is a one-line field addition in _extract(). Best-effort; a failure
    to log must never propagate (we're already on the failure path)."""
    try:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {stamp} =====\n")
            f.write(f"note: {note}\n")
            f.write(f"payload: {payload_raw[:4000]}\n")
            tb = traceback.format_exc()
            if tb and tb.strip() != "NoneType: None":
                f.write(tb)
    except Exception:
        pass  # logging the failure must not itself fail loudly


# ── argv parsing (own flags + the Codex-appended payload) ─────────────────────
def _parse_argv(argv: list[str]) -> tuple[list[str], str]:
    """Split our argv into (chain_command, payload_json).

    Format:  [--chain <program> [chain-args...] --] <payload-json>

    The payload is ALWAYS the final element (Codex appends it). --chain begins
    the original notify command; -- closes it. Everything between is the chain,
    one argv element per token, so spaces inside a token are preserved. If the
    closing -- is missing (defensive), the chain runs to the second-to-last argv
    and the last is still the payload.

    Returns ([] , payload) when no chain was encoded.
    """
    if not argv:
        return [], ""

    # payload is the last argv no matter what.
    payload = argv[-1]
    head    = argv[:-1]

    chain: list[str] = []
    if head and head[0] == "--chain":
        rest = head[1:]
        if "--" in rest:
            stop  = rest.index("--")
            chain = rest[:stop]
        else:
            chain = rest[:]   # no closing sentinel — take the rest defensively
    return chain, payload


# ── payload extraction (DEFENSIVE — fields may be missing / renamed) ──────────
def _first_str(*candidates) -> str:
    """First candidate that is a non-empty string, else ''."""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c
    return ""


def _message_text(entry) -> str:
    """Coerce one input-message into text. Codex may send a plain string, or a
    structured {"type":"text","text":..} / {"content":..} block, or a list of
    such blocks. Defensive across all of them so a shape change degrades to
    'we got less text' rather than a crash."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        # common structured shapes: {"text":..} | {"content":..} | {"message":..}
        direct = _first_str(entry.get("text"), entry.get("content"),
                             entry.get("message"), entry.get("value"))
        if direct:
            return direct
        # nested content list: {"content":[{"type":"text","text":..}, ...]}
        content = entry.get("content")
        if isinstance(content, list):
            return "\n".join(_message_text(b) for b in content).strip()
        return ""
    if isinstance(entry, list):
        return "\n".join(_message_text(b) for b in entry).strip()
    return ""


def _extract(payload: dict) -> dict:
    """Pull the fields we write from a parsed payload. Every lookup tolerates the
    field being absent or renamed (hyphen vs underscore variants checked). Never
    raises — returns a dict with best-effort values and ''/None fallbacks."""
    thread_id = _first_str(payload.get("thread-id"), payload.get("thread_id"),
                           payload.get("threadId"), payload.get("conversation-id"),
                           payload.get("conversation_id"))
    turn_id   = _first_str(payload.get("turn-id"), payload.get("turn_id"),
                           payload.get("turnId"))

    # user text = LAST entry of input-messages (the prompt that drove this turn)
    inputs = (payload.get("input-messages") or payload.get("input_messages")
              or payload.get("inputMessages") or [])
    user_text = ""
    if isinstance(inputs, list) and inputs:
        user_text = _message_text(inputs[-1])
    elif isinstance(inputs, str):
        user_text = inputs

    agent_text = _message_text(
        _first_str(payload.get("last-assistant-message"),
                   payload.get("last_assistant_message"),
                   payload.get("lastAssistantMessage"))
        or payload.get("last-assistant-message")
        or payload.get("last_assistant_message"))

    return {
        "thread_id":  thread_id,
        "turn_id":    turn_id,
        "user_text":  (user_text or "").strip(),
        "agent_text": (agent_text or "").strip(),
    }


# ── codex model (read from config.toml — read-only, defensive) ────────────────
def _thread_model(thread_id: str) -> str:
    """The model that ACTUALLY ran this thread's turns: the last
    turn_context.payload.model in the thread's rollout file
    (~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl). The app stamps a
    turn_context per executed turn, tracking the model picker even mid-thread;
    resumed threads keep their original dated file, so the search must span all
    dates. Scans backward from EOF in steps until the marker appears (a long
    final turn pushes it arbitrarily far from EOF), capped. Returns "" when the
    thread/file/field can't be found — caller falls back to _codex_model()."""
    try:
        if not thread_id:
            return ""
        hits = list(CODEX_SESSIONS.rglob(f"rollout-*{thread_id}.jsonl"))
        if not hits:
            return ""
        hits.sort(key=lambda p: p.stat().st_mtime)   # newest file is the live one
        with open(hits[-1], "rb") as f:
            f.seek(0, 2)
            pos     = f.tell()
            buf     = b""
            scanned = 0
            seen    = False
            while pos > 0 and scanned < ROLLOUT_SCAN_CAP_BYTES:
                step     = min(ROLLOUT_SCAN_STEP_BYTES, pos)
                pos     -= step
                scanned += step
                f.seek(pos)
                chunk    = f.read(step)
                straddle = buf[:48]   # marker split across the chunk edge
                buf      = chunk + buf
                if not seen and b'"turn_context"' not in chunk + straddle:
                    continue          # cheap byte check — extend backward
                seen = True           # marker in buf: decode and scan every pass
                lines = buf.decode("utf-8", errors="replace").splitlines()
                if pos > 0 and lines:
                    lines = lines[1:]   # first line is torn at the chunk edge
                for line in reversed(lines):
                    if '"turn_context"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue   # torn or mid-write line — skip
                    if rec.get("type") != "turn_context":
                        continue
                    m = (rec.get("payload") or {}).get("model")
                    if isinstance(m, str) and m.strip():
                        return m.strip()
                # marker seen but no complete parseable hit yet → extend further
    except Exception:
        pass   # any surprise → fall back; capturing with a coarser label beats failing
    return ""


def _codex_model() -> str:
    """Read the agent model from ~/.codex/config.toml `model` key. Falls back to
    'codex' when the file/key is missing or unreadable — the hook never depends
    on config being present. tomllib is stdlib (3.11+); Cairn ships 3.14."""
    try:
        import tomllib
        with open(CODEX_CONF, "rb") as f:
            conf = tomllib.load(f)
        m = conf.get("model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    except Exception:
        pass
    return "codex"


# ── capture (every step guarded; wall-clock bounded by the caller) ────────────
def _session_for(thread_id: str) -> str:
    """codex-<thread-id>, or a dated fallback when Codex didn't send a thread id
    (so turns from an id-less build still group by day rather than colliding)."""
    if thread_id:
        return f"codex-{thread_id}"
    return "codex-unknown-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _already_captured(vault, session: str, turn_id: str) -> bool:
    """Idempotency: a turn tagged turn:<turn-id> already lives in this session.
    Codex can re-fire notify (retries, app relaunch) — this makes a double-fire a
    no-op. Only meaningful when turn_id is present; without one we can't dedup and
    accept the (rare) risk of a duplicate, which is preferable to dropping turns."""
    if not turn_id:
        return False
    try:
        row = vault.conn.execute(
            "SELECT 1 FROM nodes WHERE session=? AND tags LIKE ? LIMIT 1",
            (session, f'%"turn:{turn_id}"%')).fetchone()
        return row is not None
    except Exception:
        return False


def _last_node_id(vault, session: str) -> "str | None":
    """Newest active node id in this session — the parent for chaining, so a
    Codex thread becomes a walkable reasoning path just like an import does."""
    try:
        row = vault.conn.execute(
            "SELECT id FROM nodes WHERE session=? AND status='active' "
            "ORDER BY timestamp DESC LIMIT 1", (session,)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


# ── internal-helper filter + diagnostics ──────────────────────────────────────
# Codex fires `notify` for its OWN backstage LLM calls too — title generation,
# ambient-suggestion generation, safety/compliance checks — not just the user's
# chat. Those must NOT become memory nodes. Detect them by system-prompt
# signature (user side) and/or the structured-JSON reply (agent side).
# CONSERVATIVE: only skip a CLEAR helper, never a real turn.
_HELPER_USER_SIGNATURES = (
    "you are a helpful assistant. you will be presented with a user prompt",  # title-gen
    "you are an expert at upholding safety and compliance standards",         # safety/compliance
    "generate 0 to 3 hyperpersonalized suggestions",                          # ambient suggestions
)
_HELPER_AGENT_PREFIXES = ('{"title":', '{"exclude":', '{"suggestions":')


def _internal_helper_reason(user_text: str, agent_text: str) -> "str | None":
    """Short reason string if this exchange is a Codex INTERNAL helper call (not
    the user's conversation), else None."""
    u = (user_text or "").lstrip().lower()
    for sig in _HELPER_USER_SIGNATURES:
        if sig in u[:400]:
            return "helper:user-signature"
    if u.startswith("# overview") and "suggestion" in u[:500]:
        return "helper:ambient-overview"
    a = (agent_text or "").lstrip()
    for pre in _HELPER_AGENT_PREFIXES:
        if a.startswith(pre):
            return "helper:agent-json"
    return None


def _diag_on() -> bool:
    """Diagnostics fire when CAIRN_CODEX_DEBUG env is set OR a ~/.cairn/CODEX_DEBUG
    marker file exists (the marker is easier — Codex controls the hook's env)."""
    import os
    if os.environ.get("CAIRN_CODEX_DEBUG"):
        return True
    try:
        return (CAIRN_HOME / "CODEX_DEBUG").exists()
    except Exception:
        return False


def _diag(record: dict) -> None:
    """Append ONE metadata line to ~/.cairn/codex_hook_diag.log — event type,
    top-level keys, thread/turn id PRESENCE, text LENGTHS, decision + skip reason.
    NO transcript content is written (lengths only). Best-effort; never raises."""
    if not _diag_on():
        return
    try:
        CAIRN_HOME.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with open(CAIRN_HOME / "codex_hook_diag.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _capture(payload_raw: str, deadline: float) -> int:
    """Write the turn's nodes to the vault. Returns the count written (0 on skip
    or any handled failure). Bounded by `deadline` (perf_counter seconds): if the
    vault open or a write would push past it, we bail to the chain rather than
    delay Codex. Raises nothing the caller must handle — but the caller still
    wraps it, belt-and-suspenders, because fail-safe is the whole contract."""
    import time

    # ── parse ────────────────────────────────────────────────────────────────
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except Exception:
        _debug(payload_raw, "payload is not valid JSON — skipped capture")
        return 0
    if not isinstance(payload, dict):
        _debug(payload_raw, "payload JSON is not an object — skipped capture")
        return 0

    # only the turn-complete event carries an exchange; anything else → skip
    # (the caller still chains). Accept hyphen/underscore variants defensively.
    ev = _first_str(payload.get("type"), payload.get("event"))
    if ev and ev not in ("agent-turn-complete", "agent_turn_complete"):
        return 0

    fields = _extract(payload)
    if not fields["user_text"] and not fields["agent_text"]:
        return 0   # nothing to write — not an error, just an empty turn

    # Filter Codex's OWN backstage llm calls (title-gen / ambient suggestions /
    # safety checks): they fire notify too but are NOT the user's conversation.
    # Diagnostics (metadata only, no transcript) record EVERY event so a
    # controlled turn shows whether the main chat payload actually arrives.
    reason = _internal_helper_reason(fields["user_text"], fields["agent_text"])
    _diag({
        "type": ev or "(none)",
        "keys": sorted(payload.keys())[:24],
        "thread_id": bool(fields["thread_id"]),
        "turn_id": bool(fields["turn_id"]),
        "user_len": len(fields["user_text"]),
        "agent_len": len(fields["agent_text"]),
        "decision": "skip" if reason else "capture",
        "reason": reason or "main-turn",
    })
    if reason:
        return 0   # internal Codex helper — not the user's conversation

    if time.perf_counter() > deadline:
        _debug(payload_raw, "capture budget exhausted before vault open — skipped")
        return 0

    from cairn.vault import Vault, MicroNode
    vault   = Vault()   # WAL + busy_timeout make this safe under concurrent writers
    session = _session_for(fields["thread_id"])
    turn_id = fields["turn_id"]

    if _already_captured(vault, session, turn_id):
        return 0   # idempotent: this turn-id already recorded

    model     = _thread_model(fields["thread_id"]) or _codex_model()
    turn_tag  = [f"turn:{turn_id}"] if turn_id else []
    base_tags = ["codex", "conversation"]
    written   = 0
    parent    = _last_node_id(vault, session)

    # user turn first (so the agent turn chains onto it), mirroring capture.py:
    # user speaker → model="human"; agent speaker → the real model.
    if fields["user_text"]:
        if time.perf_counter() > deadline:
            _debug(payload_raw, "budget exhausted before user write — partial skip")
            return written
        node = vault.write(MicroNode(
            session        = session,
            kind           = "conversation_turn",
            query          = fields["user_text"][:TRUNC_QUERY],
            output_preview = fields["user_text"][:TRUNC_PREVIEW],
            episodic_full  = fields["user_text"] if len(fields["user_text"]) > TRUNC_PREVIEW else None,
            parent         = parent,
            speaker        = "user",
            model          = "human",
            agent_role     = "worker",
            tags           = base_tags + ["user"] + turn_tag,
        ))
        parent = node.id
        written += 1

    if fields["agent_text"]:
        if time.perf_counter() > deadline:
            _debug(payload_raw, "budget exhausted before agent write — partial skip")
            return written
        vault.write(MicroNode(
            session        = session,
            kind           = "conversation_turn",
            query          = fields["agent_text"][:TRUNC_QUERY],
            output_preview = fields["agent_text"][:TRUNC_PREVIEW],
            episodic_full  = fields["agent_text"] if len(fields["agent_text"]) > TRUNC_PREVIEW else None,
            parent         = parent,
            speaker        = "agent",
            model          = model,
            agent_role     = "worker",
            tags           = base_tags + ["agent"] + turn_tag,
        ))
        written += 1

    return written


# ── chain (replay the original notify command, exactly as Codex would) ────────
def _run_chain(chain: list[str], payload_raw: str) -> None:
    """Re-invoke the original notify command with its original args + the payload
    appended last — mirroring what Codex does when it calls a notify program.
    Short timeout, all errors swallowed: the chain is OpenAI's plumbing, we hand
    off and move on. A missing/failing chain must never surface to Codex."""
    if not chain:
        return
    try:
        subprocess.run(
            list(chain) + [payload_raw],
            timeout=CHAIN_TIMEOUT_SEC,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        # timeout, missing exe, permission — none of it is ours to fix here.
        _debug(payload_raw, f"chain invocation failed: {chain[:1]}")


# ── entry point ───────────────────────────────────────────────────────────────
def main(argv: "list[str] | None" = None) -> int:
    """Codex notify entry point. Parse our flags, attempt capture (fully guarded
    + wall-clock bounded), then ALWAYS run the chain and exit 0. Returns 0 always
    — the return value exists for tests; the process exit is unconditionally 0 so
    Codex is never handed a failure."""
    import time
    argv = list(sys.argv[1:] if argv is None else argv)
    chain, payload_raw = _parse_argv(argv)

    # ── capture, wrapped so ANYTHING short-circuits to the chain ──────────────
    try:
        deadline = time.perf_counter() + CAPTURE_BUDGET_SEC
        n = _capture(payload_raw, deadline)
        if n:
            # stdout is invisible to Codex (it doesn't read notify output), but a
            # human running the hook by hand for a smoke test wants the receipt.
            print(f"cairn: captured {n} codex conversation turn(s)")
    except Exception:
        # the catch-all the contract promises: log raw payload + traceback, then
        # fall through to the chain. Capture is best-effort; the chain is not.
        _debug(payload_raw, "unhandled exception in capture — chained anyway")

    # ── chain ALWAYS runs (success or fail above) ─────────────────────────────
    try:
        _run_chain(chain, payload_raw)
    except Exception:
        _debug(payload_raw, "unhandled exception invoking chain")

    return 0


if __name__ == "__main__":
    # exit 0 unconditionally — the hook must be incapable of failing Codex.
    main()
    sys.exit(0)
