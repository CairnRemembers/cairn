"""
tests/test_setup.py — the consent walk's detection helpers. The walk itself
is interactive; what must never lie is its reading of the machine state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.__main__ import (_claude_present, _claude_connected_global,
                            _codex_present, _codex_hook_installed,
                            record_consent, _consent_get, _consent_lookup)
from cairn.accounts import resolve_slug_for_setup, set_handle


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    # attribution-v2 identity readers cache per process; clear so each tmp-home test is fresh
    try:
        from cairn import accounts
        accounts._IDENTITY_MEMO.clear()
    except Exception:
        pass
    return tmp_path


def test_nothing_detected_on_bare_machine(home):
    assert not _claude_present()
    assert not _codex_present()


def test_claude_detected_but_not_connected(home):
    (home / ".claude").mkdir()
    assert _claude_present()
    assert not _claude_connected_global()


def test_claude_connected_when_hooks_present(home):
    d = home / ".claude"; d.mkdir()
    (d / "settings.json").write_text('{"hooks": {"Stop": [{"command": "cairn hook"}]}}')
    assert _claude_connected_global()


def test_codex_detected_and_hook_state(home):
    d = home / ".codex"; d.mkdir()
    (d / "config.toml").write_text('notify = ["something-else"]')
    assert _codex_present()
    assert not _codex_hook_installed()
    (d / "config.toml").write_text('notify = ["python","-m","cairn","codex-hook"]')
    assert _codex_hook_installed()


# ── attribution v2 (Spec A / A4): consent keyed by harness x account ──

def test_consent_keyed_per_harness_account(home):
    record_consent("Claude Code", "work", "yes")
    record_consent("Claude Code", "personal", "no")
    c = _consent_get()
    # two accounts of one harness stay distinct — never smashed together
    assert _consent_lookup(c, "Claude Code", "work")["answer"] == "yes"
    assert _consent_lookup(c, "Claude Code", "personal")["answer"] == "no"


def test_consent_legacy_bare_key_honored(home):
    import json
    p = home / ".cairn"; p.mkdir(parents=True, exist_ok=True)
    # a pre-upgrade record keyed by the bare harness name
    (p / "consent.json").write_text(json.dumps({"OpenAI Codex": {"answer": "no", "at": "2026-07-01T00:00"}}))
    c = _consent_get()
    # a new per-account lookup finds no per-account key -> honors the legacy answer (no re-hound)
    assert _consent_lookup(c, "OpenAI Codex", "anyone")["answer"] == "no"


def test_resolve_slug_env_then_handle(home, monkeypatch):
    import json
    monkeypatch.setenv("CAIRN_ACCOUNT", "WorkAcct")
    assert resolve_slug_for_setup("Claude Code") == "workacct"       # env wins
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    p = home / ".cairn"; p.mkdir(parents=True, exist_ok=True)
    (p / "me.json").write_text(json.dumps({"handle": "HomeAcct"}))
    assert resolve_slug_for_setup("Claude Code") == "homeacct"       # handle next


def test_resolve_slug_from_claude_identity_registers_stable_id(home):
    import json
    # no env, no me.json handle -> read the harness-native id, register a slug keyed to it
    (home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "uuid-abc-123", "emailAddress": "dev@example.com",
                          "organizationName": "Org"}}))
    slug = resolve_slug_for_setup("Claude Code")
    assert slug == "dev"                                             # label hint = email localpart
    reg = json.loads((home / ".cairn" / "accounts.json").read_text())
    assert reg["dev"]["stable_id"] == "uuid-abc-123"                 # the id is the durable key


def test_set_handle_persists_and_resolves(home):
    import json
    p = home / ".cairn"; p.mkdir(parents=True, exist_ok=True)
    (p / "me.json").write_text(json.dumps({"channels": {"codex-": "GptWork"}}))
    # the ask-when-default answer sticks as the handle (normalized), channels kept
    assert set_handle("My Work Box") == "myworkbox"
    cfg = json.loads((p / "me.json").read_text())
    assert cfg["handle"] == "myworkbox"
    assert cfg["channels"] == {"codex-": "GptWork"}
    # resolution now returns it -> setup never re-asks
    assert resolve_slug_for_setup("Claude Code") == "myworkbox"
