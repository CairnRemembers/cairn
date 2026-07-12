"""
tests/test_local_agent_reader.py — Claude Desktop / Cowork local-agent import.

Synthetic transcript (no filesystem/MSIX dependency): proves the reader captures
real user/agent turns, DROPS Cowork "brief mode" runtime nudges injected on the
user channel (+ their acks), salience-gates bare pleasantries, and is idempotent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.local_agent_reader as lar
from cairn.vault import Vault


def _rec(rtype, content=None, model=None, uuid="", ts="2026-07-07T12:00:00.000Z",
         sid="sess-1", role=None):
    r = {"type": rtype, "uuid": uuid, "timestamp": ts, "sessionId": sid}
    if rtype in ("user", "assistant"):
        msg = {"role": role or rtype}
        if content is not None:
            msg["content"] = content
        if model:
            msg["model"] = model
        r["message"] = msg
    return r


def _write_transcript(root: Path, sid: str, records: list) -> Path:
    # mirror the real shape: <root>/…/.claude/projects/<enc>/<sid>.jsonl
    d = root / "s" / "inner" / "agent" / "local_ditto_inner" / ".claude" / "projects" / "enc"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return root


def test_capture_filters_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(lar, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(lar, "CAIRN_HOME", tmp_path)

    sid = "sess-1"
    A = "claude-opus-4-8"
    records = [
        _rec("user", "Hello", uuid="u0", ts="2026-07-07T12:00:00.000Z", sid=sid),
        _rec("assistant", [{"type": "text", "text": "Hey there — what can I help you with across your projects today?"}],
             model=A, uuid="a0", ts="2026-07-07T12:00:03.000Z", sid=sid),
        _rec("user", "What is the status of the launch and the punch-list items?",
             uuid="u1", ts="2026-07-07T12:01:00.000Z", sid=sid),
        _rec("assistant", [{"type": "text", "text": "The launch path has four blockers and the punch list is tracked in the vault."}],
             model=A, uuid="a1", ts="2026-07-07T12:01:05.000Z", sid=sid),
        # a tool round-trip: tool_use assistant + tool_result echoed on the user
        # channel — must NOT split the turn or become a user node.
        _rec("assistant", [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
             model=A, uuid="a1b", ts="2026-07-07T12:01:06.000Z", sid=sid),
        _rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
             uuid="u1b", ts="2026-07-07T12:01:07.000Z", sid=sid),
        # Cowork brief-mode runtime nudge (+ terse ack) — the whole turn is dropped.
        _rec("user", "You ended the turn without calling SendUserMessage. In brief mode you must use SendUserMessage.",
             uuid="u2", ts="2026-07-07T12:02:00.000Z", sid=sid),
        _rec("assistant", [{"type": "text", "text": "Standing by."}],
             model=A, uuid="a2", ts="2026-07-07T12:02:03.000Z", sid=sid),
    ]
    root = _write_transcript(tmp_path / "store", sid, records)
    v = Vault(db_path=str(tmp_path / "t.db"))

    rep = lar.read_local_agent_sessions(
        root=root, vault=v, dry_run=False,
        include_before="2000-01-01T00:00:00+00:00")

    rows = v.conn.execute(
        "SELECT speaker, model, query FROM nodes WHERE kind='conversation_turn' "
        "ORDER BY timestamp").fetchall()
    texts = [r["query"] for r in rows]

    # real conversation captured, with the right model on the agent side
    assert any("status of the launch" in t for t in texts)
    assert any("four blockers" in t for t in texts)
    assert any(r["model"] == A for r in rows if r["speaker"] == "agent")
    # session keyed local-agent-<sid>
    sess = v.conn.execute(
        "SELECT DISTINCT session FROM nodes WHERE kind='conversation_turn'").fetchone()
    assert sess["session"] == f"local-agent-{sid}"
    # noise filtered
    assert not any("SendUserMessage" in t for t in texts)      # harness inject
    assert not any("Standing by" in t for t in texts)          # its ack
    assert not any(t == "Hello" for t in texts)                # bare pleasantry
    assert not any("tool_result" in t for t in texts)          # tool echo not a turn

    # idempotent: a second pass writes nothing
    rep2 = lar.read_local_agent_sessions(
        root=root, vault=v, dry_run=False,
        include_before="2000-01-01T00:00:00+00:00")
    assert rep2["written_nodes"] == 0
    assert rep2["already_captured"] >= 1


def test_no_store_is_clean_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(lar, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(lar, "CAIRN_HOME", tmp_path)
    v = Vault(db_path=str(tmp_path / "t.db"))
    rep = lar.read_local_agent_sessions(root=tmp_path / "does-not-exist", vault=v,
                                        dry_run=True)
    assert rep["files_scanned"] == 0
    assert rep["written_nodes"] == 0
