"""
Regression pin: the capture off-switch must be honored by EVERY capture hook.

compact_hook (PreCompact), stop_hook (Stop), and codex_hook (Codex notify) all
write to the vault. Before 2026-07-17 only hook.py / prompt_hook.py / turn_hook.py
checked the mute (CAIRN_CAPTURE=0, or the global ~/.cairn/CAPTURE_OFF marker) —
the other three wrote regardless. So `cairn capture off` reported MUTED while a
compaction, a clean Stop, or a Codex turn still captured to the permanent vault
(codex even survived `cairn disconnect`). That is a hole in a local-first
product's core promise.

These tests drive each hook's main() with the mute set and assert NOTHING is
written. For codex_hook they also assert the notify CHAIN STILL RUNS while muted:
the mute silences Cairn's capture, it must never silence Codex's own plumbing
(fail-safe is that file's whole contract).
"""
import io
import json
import sys

import pytest

import cairn.vault as vaultmod
from cairn import codex_hook, compact_hook, stop_hook
from cairn.__main__ import cmd_orient


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CAIRN_CAPTURE", raising=False)
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    # codex_hook resolves CAIRN_HOME as a module-level constant (its own idiom),
    # frozen at import — before this fixture patches HOME. Repoint it so the
    # CAPTURE_OFF marker check reads the temp home, matching a fresh production
    # process where Path.home() is already correct.
    monkeypatch.setattr(codex_hook, "CAIRN_HOME", tmp_path / ".cairn")
    return tmp_path


def _feed(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _run_exit(hook):
    try:
        hook.main()
    except SystemExit:
        pass


# ── compact_hook + stop_hook: a mute means the DB is never even opened ────────
# The gate sits before `from cairn.vault import Vault`, so a muted run never
# constructs a Vault — the surest proof of "wrote nothing" is that no db exists.

@pytest.mark.parametrize("hook", [compact_hook, stop_hook])
def test_env_mute_writes_nothing(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    _feed(monkeypatch, {"session_id": "s-mute"})
    _run_exit(hook)
    assert not (tmp_path / "cairn.db").exists()


@pytest.mark.parametrize("hook", [compact_hook, stop_hook])
def test_marker_mute_writes_nothing(hook, tmp_path, monkeypatch):
    (tmp_path / ".cairn" / "CAPTURE_OFF").write_text("", encoding="utf-8")
    _feed(monkeypatch, {"session_id": "s-mute"})
    _run_exit(hook)
    assert not (tmp_path / "cairn.db").exists()


# ── codex_hook: a mute writes nothing, but the notify chain STILL runs ────────

def _chain_marker(tmp_path):
    """A tiny program used as the chained notify command; it touches a marker so
    the test can prove the chain ran. Returns (chain_argv_tail, marker_path)."""
    marker = tmp_path / "chain_ran.txt"
    script = tmp_path / "chain_prog.py"
    script.write_text(
        "import pathlib\n"
        f"pathlib.Path(r'{marker}').write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], marker


def _turn_payload():
    return json.dumps({
        "type": "agent-turn-complete",
        "thread-id": "t-mute", "turn-id": "turn-1",
        "input-messages": ["hello there"],
        "last-assistant-message": "hi, this is the agent reply",
    })


@pytest.mark.parametrize("mute", ["env", "marker"])
def test_codex_mute_no_write_but_still_chains(mute, tmp_path, monkeypatch):
    if mute == "env":
        monkeypatch.setenv("CAIRN_CAPTURE", "0")
    else:
        (tmp_path / ".cairn" / "CAPTURE_OFF").write_text("", encoding="utf-8")

    chain, marker = _chain_marker(tmp_path)
    rc = codex_hook.main(["--chain", *chain, "--", _turn_payload()])

    assert rc == 0                               # never fails Codex
    assert not (tmp_path / "cairn.db").exists()  # captured nothing
    assert marker.exists()                       # but Codex's own chain still ran


# ── cmd_orient (SessionStart): a mute skips the WRITE but never the READ ──────
# orient is the 4th automatic writer — it stamps a context node every session
# start. Muting capture must skip that write, but must NOT mute recall: orient
# still prints the inherited-context digest so the model sees its past.

def _node_count(tmp_path):
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    return v.conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]


@pytest.mark.parametrize("mute", ["env", "marker"])
def test_orient_mute_writes_no_node_but_still_prints(mute, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s-orient")
    if mute == "env":
        monkeypatch.setenv("CAIRN_CAPTURE", "0")
    else:
        (tmp_path / ".cairn" / "CAPTURE_OFF").write_text("", encoding="utf-8")

    cmd_orient([])

    out = capsys.readouterr().out
    assert out.strip()                 # recall is NOT muted — orient still prints
    assert _node_count(tmp_path) == 0  # but no context_stamp was written


def test_orient_capture_on_writes_a_stamp(tmp_path, monkeypatch):
    """Positive control: unmuted, orient DOES write its session stamp."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s-orient")
    monkeypatch.delenv("CAIRN_CAPTURE", raising=False)

    cmd_orient([])

    assert _node_count(tmp_path) >= 1


def test_codex_capture_on_writes_and_chains(tmp_path, monkeypatch):
    """Positive control: without a mute the new gate does NOT block normal
    capture — the two conversation turns land and the chain still runs."""
    monkeypatch.delenv("CAIRN_CAPTURE", raising=False)
    chain, marker = _chain_marker(tmp_path)
    rc = codex_hook.main(["--chain", *chain, "--", _turn_payload()])

    assert rc == 0
    assert marker.exists()
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    n = v.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE session='codex-t-mute'"
    ).fetchone()["c"]
    assert n == 2
