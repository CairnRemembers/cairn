"""
5a — tool-calls-as-turn-metadata.

Proves the live-capture rework end to end against a temp vault:
  1. PostToolUse appends to the per-session buffer instead of writing a node.
  2. turn_hook's drain() empties the buffer and hands the records to write_turn.
  3. The agent conversation_turn carries the tools as JSON metadata.
  4. ZERO standalone tool_call nodes exist — the explosion is gone.

No hooks fire here (they only load at session start); we exercise the same
functions the hooks call, with the same buffer file, against an isolated DB.
"""
import json, os, sys, tempfile, importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_cairn_home():
    """Point ~/.cairn at a temp dir so the real vault is never touched."""
    tmp = tempfile.mkdtemp(prefix="cairn5a_")
    os.environ["HOME"] = tmp
    os.environ["USERPROFILE"] = tmp        # Windows home
    # re-import modules so their module-level Path.home() constants rebind
    import cairn.vault as vault_mod
    importlib.reload(vault_mod)
    import cairn.pending as pending_mod
    importlib.reload(pending_mod)
    import cairn.capture as capture_mod
    importlib.reload(capture_mod)
    return tmp, vault_mod, pending_mod, capture_mod


def test_tool_calls_bake_into_turn():
    tmp, vault_mod, pending, capture = _fresh_cairn_home()
    sess = "test-session-5a"

    # ── 1. simulate three PostToolUse calls → buffer appends (no nodes) ───────
    for rec in [
        {"tool": "Grep",  "query": "needle",        "preview": "3 hits", "result_count": 3, "latency_ms": 12, "ts": "t1"},
        {"tool": "Read",  "query": "foo.py",         "preview": "...",    "result_count": 80, "latency_ms": 5, "ts": "t2"},
        {"tool": "Bash",  "query": "run the tests",  "preview": "ok",     "result_count": 1, "latency_ms": 999, "ts": "t3"},
    ]:
        pending.append(sess, rec)

    buffered = pending.read(sess)
    assert len(buffered) == 3, f"expected 3 buffered, got {len(buffered)}"

    v = vault_mod.Vault()
    pre = v.conn.execute("SELECT COUNT(*) FROM nodes WHERE kind='tool_call'").fetchone()[0]
    assert pre == 0, "no tool_call nodes should exist from buffering"

    # ── 2. turn_hook drains + attaches to the agent turn ─────────────────────
    drained = pending.drain(sess)
    assert len(drained) == 3, f"drain should return 3, got {len(drained)}"
    assert pending.read(sess) == [], "buffer must be empty after drain"

    capture.write_turn("I searched, read foo.py, and ran the tests — all green.",
                       speaker="agent", session=sess, vault=v,
                       model="claude-opus-4-8", tool_calls=drained)

    # ── 3. the turn carries the tools as JSON metadata ───────────────────────
    row = v.conn.execute(
        "SELECT tool_calls FROM nodes WHERE kind='conversation_turn' AND speaker='agent'"
    ).fetchone()
    assert row is not None, "agent turn node must exist"
    tcs = json.loads(row["tool_calls"])
    assert [t["tool"] for t in tcs] == ["Grep", "Read", "Bash"], tcs
    assert tcs[2]["latency_ms"] == 999

    # ── 4. the explosion is gone — zero standalone tool_call nodes ───────────
    post = v.conn.execute("SELECT COUNT(*) FROM nodes WHERE kind='tool_call'").fetchone()[0]
    assert post == 0, f"expected ZERO tool_call nodes, found {post}"

    print("PASS: 3 tools buffered -> drained -> baked into 1 turn, 0 tool_call nodes")


def test_drain_resets_between_turns():
    """A second turn must not inherit the first turn's tools."""
    tmp, vault_mod, pending, capture = _fresh_cairn_home()
    sess = "test-session-5a-b"
    v = vault_mod.Vault()

    pending.append(sess, {"tool": "Read", "query": "a.py", "ts": "t1"})
    t1 = pending.drain(sess)
    capture.write_turn("turn one reads a.py here, long enough to be salient.",
                       speaker="agent", session=sess, vault=v, tool_calls=t1)

    # second turn, different tools
    pending.append(sess, {"tool": "Edit", "query": "b.py", "ts": "t2"})
    t2 = pending.drain(sess)
    assert [t["tool"] for t in t2] == ["Edit"], t2

    capture.write_turn("turn two edits b.py here, also nice and salient now.",
                       speaker="agent", session=sess, vault=v, tool_calls=t2)

    rows = v.conn.execute(
        "SELECT tool_calls FROM nodes WHERE kind='conversation_turn' ORDER BY timestamp"
    ).fetchall()
    tools_per_turn = [[t["tool"] for t in json.loads(r["tool_calls"])] for r in rows if r["tool_calls"]]
    assert ["Read"] in tools_per_turn and ["Edit"] in tools_per_turn, tools_per_turn
    print("PASS: each turn carries only its own tools")


if __name__ == "__main__":
    test_tool_calls_bake_into_turn()
    test_drain_resets_between_turns()
    print("\nALL 5a TESTS PASSED")
