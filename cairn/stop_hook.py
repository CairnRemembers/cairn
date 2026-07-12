"""
cairn/stop_hook.py — Stop hook

Fires when Claude Code session ends cleanly.
Does the full job: compile PROTOCOL.md + embed all pending nodes.

Unlike PreCompact this is not time-sensitive — the session is already
ending — so we can run embed synchronously and get full search coverage
before the next session starts.

Claude Code settings.json:
  "Stop": [{
    "hooks": [{
      "type": "command",
      "command": "python -X utf8 <path-to-cairn>/cairn/stop_hook.py",
      "timeout": 120
    }]
  }]
"""
import sys, os, json, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}

    session_id = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")

    from cairn.vault import Vault, MicroNode
    from cairn.compile import compile_session

    vault   = Vault()
    out_dir = Path.home() / ".cairn" / "protocols" / session_id

    print(f"cairn: session ending — {session_id}")

    # ── 1. final compile ──────────────────────────────────────────────────────
    try:
        t0    = time.perf_counter()
        path  = compile_session(vault, session_id, out_dir)
        nodes = vault.session_nodes(session_id)
        ms    = int((time.perf_counter() - t0) * 1000)
        print(f"cairn: compiled {len(nodes)} nodes → PROTOCOL.md ({ms}ms)")
        print(f"       {path}")
    except Exception as e:
        print(f"cairn: compile error — {e}", file=sys.stderr)

    # ── 2. embed all pending (synchronous — session is ending, take the time) ─
    try:
        t1 = time.perf_counter()
        n  = vault.embed_pending()
        ms = int((time.perf_counter() - t1) * 1000)
        if n > 0:
            print(f"cairn: embedded {n} nodes ({ms}ms)")
        else:
            print(f"cairn: all nodes already embedded")
    except ImportError:
        print("cairn: sentence-transformers not installed — skipping embed")
        print("       run: python -m pip install sentence-transformers")
    except Exception as e:
        print(f"cairn: embed error — {e}", file=sys.stderr)

    # ── 2.5 demote stale warm nodes to cold ──────────────────────────────────
    try:
        from cairn.schedule import demote_cold
        all_nodes = vault.session_nodes(session_id)
        # referenced_ids = all parent pointers (nodes that were cited)
        referenced_ids = {r["parent"] for r in all_nodes if r["parent"]}
        demoted = demote_cold(vault, session_id, referenced_ids, min_sessions_old=3)
        if demoted > 0:
            print(f"cairn: demoted {demoted} stale warm nodes → cold tier")
    except Exception as e:
        print(f"cairn: demote_cold error — {e}", file=sys.stderr)

    # ── 3. write session-end marker ───────────────────────────────────────────
    try:
        vault.write(MicroNode(
            session = session_id,
            kind    = "interrupt",
            query   = "session ended cleanly",
            tags    = ["session-end"],
        ))
    except Exception:
        pass

    # ── 4. clear last_node state (prevents cross-session parent contamination) ─
    try:
        cairn_dir = Path.home() / ".cairn"
        # clear last_node.txt so next session starts without stale parent
        last_node = cairn_dir / "last_node.txt"
        if last_node.exists():
            last_node.unlink()
        # reset inject counter state so new session gets fresh heartbeat timing
        inject_state = cairn_dir / "inject_state.json"
        if inject_state.exists():
            try:
                import json
                state = json.loads(inject_state.read_text())
                state["last_inject_at"] = state.get("call_counter", 0)
                state["warm_blocks_sent"] = 0   # reset token-fill gate for next session
                inject_state.write_text(json.dumps(state))
            except Exception:
                pass
    except Exception:
        pass

    print(f"cairn: done — load PROTOCOL.md at next session start")
    sys.exit(0)


if __name__ == "__main__":
    main()
