"""
tests/test_account_cli.py — attribution v2 (Spec A / A6): the `cairn account`
CLI. Lists galaxies; renames DISPLAY labels ONLY (stable key/id untouched, so
same-named accounts never merge and nodes keep their origin); never deletes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
from cairn.vault import MicroNode
from cairn.__main__ import cmd_account


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    vaultmod._ACCOUNT_MEMO.clear()
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    vaultmod._ACCOUNT_MEMO.clear()


def _reg(home) -> dict:
    return json.loads((home / ".cairn" / "accounts.json").read_text())


def test_rename_changes_display_only(home):
    (home / ".cairn" / "accounts.json").write_text(json.dumps(
        {"nova": {"label": "Nova", "maker": "GPT", "stable_id": "acct-xyz",
                  "email_mask": "no***@x.com"}}))
    cmd_account(["rename", "nova", "Nova Prime"])
    reg = _reg(home)
    assert reg["nova"]["label"] == "Nova Prime"      # display changed
    assert reg["nova"]["stable_id"] == "acct-xyz"    # KEY untouched -> no merge
    assert list(reg.keys()) == ["nova"]              # nothing created, nothing deleted


def test_rename_empty_name_rejected(home, capsys):
    (home / ".cairn" / "accounts.json").write_text(json.dumps(
        {"nova": {"label": "Nova", "stable_id": "x"}}))
    cmd_account(["rename", "nova", "   "])
    assert _reg(home)["nova"]["label"] == "Nova"     # unchanged
    assert "non-empty" in capsys.readouterr().out


def test_rename_unknown_key_reports(home, capsys):
    (home / ".cairn" / "accounts.json").write_text(json.dumps({}))
    cmd_account(["rename", "ghost", "Nope"])
    assert "no account 'ghost'" in capsys.readouterr().out
    assert _reg(home) == {}                          # nothing conjured


def test_rename_labels_unregistered_vault_galaxy(home):
    # a galaxy that exists in the vault but was never registered can be labeled;
    # this CREATES a label-only registry entry (no stable_id needed)
    v = vaultmod.Vault()
    v.write(MicroNode(session="codex-z", kind="note", query="hi", model="test"))
    v.conn.execute("UPDATE sessions SET account=?, harness=? WHERE id=?",
                   ("legacyacct", "codex", "codex-z"))
    v.conn.commit()
    cmd_account(["rename", "legacyacct", "Old Codex"])
    assert _reg(home)["legacyacct"]["label"] == "Old Codex"


def test_list_shows_registered_and_counts(home, capsys):
    (home / ".cairn" / "accounts.json").write_text(json.dumps(
        {"nova": {"label": "Nova Prime", "maker": "GPT", "stable_id": "x"}}))
    cmd_account([])
    out = capsys.readouterr().out
    assert "Nova Prime" in out and "GPT" in out
    assert "0 nodes" in out


def test_bare_vault_respects_monkeypatched_root(home):
    """TRIPWIRE: a bare Vault() must land under the (monkeypatched) VAULT_ROOT,
    never the real ~/.cairn. Regression guard for the import-time-frozen default
    (`db_path=DB_PATH`) that silently defeated VAULT_ROOT patching and leaked
    test writes into the live vault. If this fails, tests are polluting real data."""
    v = vaultmod.Vault()
    assert str(v.db_path).startswith(str(home)), \
        f"bare Vault() escaped isolation: {v.db_path} not under {home}"                          # registered, no data yet
