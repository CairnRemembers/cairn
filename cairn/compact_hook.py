"""
cairn/compact_hook.py — PreCompact hook

Fires RIGHT BEFORE Claude Code compresses the context window.
This is the safety net — if the session dies after compression,
we already have a compiled PROTOCOL.md from before it happened.

Two jobs:
  1. Compile PROTOCOL.md immediately (fast, synchronous, ~100ms)
  2. Trigger embedding in background (slow, non-blocking)

The compile must finish before we exit — Claude Code waits on this hook.
The embed runs detached so it doesn't delay the compression.

Claude Code settings.json:
  "PreCompact": [{
    "hooks": [{
      "type": "command",
      "command": "python -X utf8 <path-to-cairn>/cairn/compact_hook.py",
      "timeout": 15
    }]
  }]
"""
import sys, os, json, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    # read event from stdin (same format as PostToolUse)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}

    session_id = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")

    from cairn.vault import Vault, MicroNode
    from cairn.compile import compile_session
    from cairn.capture import extract_compress_summary

    vault   = Vault()
    out_dir = Path.home() / ".cairn" / "protocols" / session_id

    model = (os.environ.get("CAIRN_MODEL") or
             os.environ.get("CLAUDE_MODEL") or "unknown")

    # ── 1. extract conversation summary BEFORE anything else ─────────────────
    # This is the most important thing PreCompact does.
    # The event may contain a summary of what's being compressed.
    # Write it as a hot node (memory_tier=0) before context collapses.
    try:
        stamp = extract_compress_summary(event, session_id, vault, model)
        if stamp:
            print(f"cairn: context stamp written [{stamp.id}]")
    except Exception as e:
        print(f"cairn: stamp failed — {e}", file=sys.stderr)

    # ── 2. compile synchronously — MUST finish before we return ──────────────
    try:
        nodes = vault.session_nodes(session_id)
        path  = compile_session(vault, session_id, out_dir)

        # timestamped checkpoint — PROTOCOL-HHMM.md + latest PROTOCOL.md
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%H%M")
        stamped = out_dir / f"PROTOCOL-{ts}.md"
        import shutil
        shutil.copy2(path, stamped)

        vault.write(MicroNode(
            session        = session_id,
            kind           = "interrupt",
            query          = "PreCompact fired — context window filling",
            output_preview = f"compiled state saved to {path}",
            model          = model,
            memory_tier    = 0,
            tags           = ["compact-event", "checkpoint"],
        ))

        # ── carry open items forward as a hot node ────────────────────────────
        # Any question/blocker/open_item still active gets stamped at tier=0
        # so it's front-loaded at the start of the next context window.
        # Without this, open items written early in a long session evaporate
        # into the middle and get forgotten by compaction.
        try:
            open_rows = vault.conn.execute(
                "SELECT query FROM nodes "
                "WHERE session=? AND kind IN ('question','blocker','open_item') "
                "AND status='active' ORDER BY timestamp ASC",
                (session_id,)
            ).fetchall()
            if open_rows:
                items = " | ".join(
                    (r["query"] or "")[:80] for r in open_rows
                )
                vault.write(MicroNode(
                    session        = session_id,
                    kind           = "context_stamp",
                    query          = f"OPEN ITEMS BEFORE COMPACT: {items}",
                    output_preview = (f"{len(open_rows)} unresolved item(s) — "
                                      f"carry to next window"),
                    model          = model,
                    memory_tier    = 0,
                    tags           = ["open-items", "carry-forward", "pre-compact"],
                ))
                print(f"cairn: {len(open_rows)} open item(s) stamped as hot context")
        except Exception as e:
            print(f"cairn: open-items stamp failed — {e}", file=sys.stderr)

        print(f"cairn: checkpoint compiled ({len(nodes)} nodes → {path.stat().st_size}b)")
        print(f"       timestamped: {stamped.name}")
        print(f"       load PROTOCOL.md at next session start: {path}")

    except Exception as e:
        print(f"cairn: compile failed — {e}", file=sys.stderr)

    # ── 2. trigger embed in background — fire and forget ─────────────────────
    try:
        # subprocess.Popen detaches from this process
        # Claude Code won't wait for it — it runs after compression continues
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "cairn", "embed"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if sys.platform == "win32" else 0,
            # DETACHED_PROCESS on Windows so it survives parent exit
        )
    except Exception:
        pass  # embed failure never blocks anything

    sys.exit(0)


if __name__ == "__main__":
    main()
