"""
HOOK-LEVEL regression pin for the retired _AGENT_MAX=4000 pre-slice.

Codex review finding (2026-07-14): the first lossless test exercised
capture.write_turn directly, so it would have passed even with the old
turn_hook pre-slice still in place — the bug lived one layer up. This test
drives turn_hook.main() itself with a real Stop-event payload and a real
transcript JSONL, exactly the path live Claude capture takes. With the old
`atext[:_AGENT_MAX]` slice present, the tail assertion below fails.
"""
import io
import json
import sys

import pytest

import cairn.vault as vaultmod
from cairn import turn_hook


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CAIRN_CAPTURE", raising=False)
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)


def _run_hook(tmp_path, monkeypatch, agent_text):
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user",
                     "content": "please refactor the whole capture layer"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [{"type": "text", "text": agent_text}],
                     "usage": {"input_tokens": 11, "output_tokens": 22}}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries),
                          encoding="utf-8")
    payload = {"session_id": "s-hooktest", "transcript_path": str(transcript)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    try:
        turn_hook.main()
    except SystemExit:
        pass


def test_hook_path_preserves_long_agent_turn(tmp_path, monkeypatch):
    """The full pipeline: Stop event → transcript parse → write_turn → vault.
    A ~22k-char agent turn must land complete (tail included)."""
    big = ("deliberate reasoning sentence. " * 700) + "HOOK-TAIL-MARKER"
    assert len(big) > 20000
    _run_hook(tmp_path, monkeypatch, big)

    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    row = v.conn.execute(
        "SELECT output_preview, episodic_text FROM nodes "
        "WHERE kind='conversation_turn' AND speaker='agent'").fetchone()
    assert row is not None, "hook did not capture the agent turn"
    assert row["episodic_text"].endswith("HOOK-TAIL-MARKER")   # lossless
    assert len(row["output_preview"]) == 8000                  # bounded preview


def test_hook_path_past_the_old_cap(tmp_path, monkeypatch):
    """The exact historical failure: 4,000 < turn <= 8,000 chars. The old
    pre-slice chopped it at 4,000; now it must arrive whole in the preview."""
    mid = ("m" * 6000) + " OLD-CAP-TAIL"
    _run_hook(tmp_path, monkeypatch, mid)

    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    row = v.conn.execute(
        "SELECT output_preview FROM nodes "
        "WHERE kind='conversation_turn' AND speaker='agent'").fetchone()
    assert row is not None
    assert row["output_preview"].endswith("OLD-CAP-TAIL")
    assert len(row["output_preview"]) > 4000
