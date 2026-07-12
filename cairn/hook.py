"""
cairn/hook.py — Claude Code PostToolUse hook

Claude Code calls this after EVERY tool I use.
It reads the event from stdin as JSON, writes one micro-node to the vault.
I never see it happening. The path just accumulates.

Add to .claude/settings.json:
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python -X utf8 <path-to-cairn>/cairn/hook.py",
        "timeout": 10
      }]
    }]
  }
}
"""
import sys, json, os
from datetime import datetime, timezone
from pathlib import Path

# self-contained: add cairn package root to path without needing pip install
sys.path.insert(0, str(Path(__file__).parent.parent))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# bookkeeping / meta tools that aren't project reasoning — never capture them
# (task tracking, todo lists, plan-mode toggles, and cairn's own MCP tools
# watching themselves). Keeps the feed signal, not noise.
_SKIP_TOOLS = {
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskStop", "TaskOutput",
    "TodoWrite", "ExitPlanMode", "EnterPlanMode",
}


def extract_query(tool_name: str, tool_input: dict) -> str | None:
    """Pull the most meaningful field from each tool's input."""
    extractors = {
        "Grep":      lambda i: i.get("pattern") or i.get("query"),
        "Read":      lambda i: i.get("file_path"),
        "Glob":      lambda i: i.get("pattern"),
        "Edit":      lambda i: i.get("file_path"),
        "Write":     lambda i: i.get("file_path"),
        "Bash":      lambda i: (i.get("description") or i.get("command") or "")[:200],
        "WebSearch": lambda i: i.get("query"),
        "WebFetch":  lambda i: i.get("url"),
    }
    fn = extractors.get(tool_name)
    result = fn(tool_input) if fn else str(tool_input)[:200]
    return str(result)[:500] if result else None


def extract_result_count(tool_name: str, tool_result) -> int | None:
    """Estimate result richness from the output."""
    if tool_result is None:
        return None
    text = str(tool_result)
    if not text.strip():
        return 0
    lines = [l for l in text.strip().split("\n") if l.strip()]
    # Grep: each line is a match
    if tool_name == "Grep":
        return len(lines)
    # Read: line count
    if tool_name == "Read":
        return len(lines)
    # Bash: non-empty lines
    return len(lines) if len(lines) > 0 else None


def _model_from_transcript(tpath: str | None) -> str | None:
    """The REAL model for this turn — read from the tail of the transcript
    (each assistant message records message.model). Cheap: only the last chunk
    is read. Authoritative, unlike a stale CAIRN_MODEL env override."""
    if not tpath:
        return None
    try:
        p = Path(tpath)
        if not p.exists():
            return None
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "assistant":
                m = (e.get("message") or {}).get("model")
                if m:
                    return m
    except Exception:
        pass
    return None


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        event = json.loads(raw)
    except Exception:
        sys.exit(0)  # never block Claude Code on a parse failure

    # ── capture mute: per-chat env switch, or global pause marker ─────────────
    # Lets a user say "don't write THIS chat to my brain" even under global
    # capture (CAIRN_CAPTURE=0), or pause capture everywhere (cairn capture off).
    if os.environ.get("CAIRN_CAPTURE") == "0":
        sys.exit(0)
    if (Path.home() / ".cairn" / "CAPTURE_OFF").exists():
        sys.exit(0)

    session_id  = event.get("session_id", "unknown-session")
    tool_name   = event.get("tool_name", "unknown")

    # ── skip bookkeeping / meta tools — noise, not project reasoning ──────────
    if tool_name in _SKIP_TOOLS or "cairn" in tool_name.lower():
        sys.exit(0)

    tool_input  = event.get("tool_input", {})
    tool_result = event.get("tool_result", {})
    latency_ms  = event.get("duration_ms") or event.get("latency_ms")

    # 5a: the turn node carries the model (turn_hook reads the REAL model from
    # the transcript at Stop). A buffered tool record needs no per-call model —
    # one less thing to get wrong, and the explosion of model-tagged tool_call
    # nodes is exactly what this rework removes.

    # stringify result for preview
    if isinstance(tool_result, dict):
        result_text = (tool_result.get("output") or
                       tool_result.get("content") or
                       json.dumps(tool_result))[:500]
    else:
        result_text = str(tool_result)[:500]

    query        = extract_query(tool_name, tool_input if isinstance(tool_input, dict) else {})
    result_count = extract_result_count(tool_name, result_text)

    # ── secret redaction — THE safety gate. The vault is append-only: a
    # credential captured here lives forever (void hides, never deletes).
    # Scrub query + result BEFORE the node is built. Opt-out only via
    # CAIRN_NO_REDACT=1 for users who knowingly want raw capture.
    if os.environ.get("CAIRN_NO_REDACT") != "1":
        try:
            from cairn.redact import scrub
            result_text = scrub(result_text)
            query       = scrub(query)
        except Exception:
            # redaction must never crash capture; but if it can't run, fail
            # CLOSED on BOTH fields rather than leak — the query can carry a
            # secret too (a pasted token), so suppress it as well.
            result_text = "[capture suppressed — redactor unavailable]"
            query       = "[capture suppressed — redactor unavailable]"

    # ── 5a: buffer the call instead of minting a node ────────────────────────
    # APPEND a one-line record to ~/.cairn/pending_tools/<session>.jsonl. No
    # node → no explosion. turn_hook.py drains this at Stop and bakes the list
    # into the turn; the dashboard tails it for the live "tools as they fire"
    # stream. last_node.txt is intentionally NOT written here anymore — there's
    # no tool_call node to chain to, and conversation turns chain via capture.
    cairn_dir = Path.home() / ".cairn"
    cairn_dir.mkdir(parents=True, exist_ok=True)

    from cairn.pending import append as _buffer_append
    _buffer_append(session_id, {
        "tool":         tool_name,
        "query":        query,
        "preview":      result_text if result_text.strip() else None,
        "result_count": result_count,
        "latency_ms":   int(latency_ms) if latency_ms is not None else None,
        "ts":           event.get("timestamp") or _now_iso(),
    })

    # expose session ID so CLI (cairn note, cairn status, etc.) uses the same session
    # this is the single source of truth — CLI reads last_session.txt
    (cairn_dir / "last_session.txt").write_text(session_id)

    # ── active injection: surface relevant past context when triggers fire ──
    try:
        from cairn.inject import run_inject
        _lat = int(latency_ms) if latency_ms is not None else None
        run_inject(tool_name, query, result_count, _lat, session_id)
    except Exception:
        pass  # never block Claude Code on injection errors

    sys.exit(0)


if __name__ == "__main__":
    main()
