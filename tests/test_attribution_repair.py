"""
tests/test_attribution_repair.py — the v0.3.1 attribution-repair patch.

Covers, one group per fix:
  Slice 1  fix-session referee = session EXISTENCE (not a regex), all forms
           preserved, the codex-id corruption closed, extras rejected.
  Slice 2  `account doctor` sees Codex sessions and stays strictly read-only
           (never slug_register -> never writes accounts.json).
  Slice 3b _multi_account_machine is maker-aware: Claude Desktop folders cannot
           trigger a GPT/Codex multi-account verdict.
  Issue 2  a Codex login-id no longer HARD-locks; on a 2+ GPT-account machine it
           is an unlocked guess (so the warning can fire), and a validated
           CAIRN_ACCOUNT re-locks it.
  Slice 3c orient_account_warning fires only for a real multi-account + unlocked
           session — direct Claude-UUID and Codex-MCP cases — and never for a
           single-account, declared, or unknown-maker session.
  Resolver maker_for_session maps session-id SHAPES honestly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
import cairn.accounts as acctmod
from cairn.vault import (_live_account, _live_account_locked, _resolve_account,
                         _multi_account_machine, orient_account_warning)
from cairn.accounts import maker_for_session
from cairn.__main__ import cmd_account

UUID_CUR = "e5e5e5e5-0000-4000-8000-000000000005"
CL1 = "a1a1a1a1-0000-4000-8000-000000000001"
CL2 = "b2b2b2b2-0000-4000-8000-000000000002"
CODEX_SID = "codex-019fabc00000000000000000000000"
MCP_CODEX_SID = "mcp-codex-mcp-client-2026-07-15"
GPT1 = "gpt-account-1111"
GPT2 = "gpt-account-2222"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.delenv("CAIRN_HARNESS", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    yield
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()


# ── helpers ───────────────────────────────────────────────────────────────────
def _accounts(tmp_path, mapping: dict):
    """mapping: slug -> (maker, stable_id)."""
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cairn" / "accounts.json").write_text(json.dumps(
        {slug: {"label": slug, "maker": mk, "stable_id": sid}
         for slug, (mk, sid) in mapping.items()}), encoding="utf-8")


def _seed(tmp_path, sid, account, locked=0):
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    v.conn.execute(
        "INSERT INTO sessions (id, started_at, account, account_locked) VALUES (?,?,?,?)",
        (sid, "2026-07-05T00:00:00", account, locked))
    v.conn.commit()
    return v


def _row(tmp_path, sid):
    return vaultmod.Vault(db_path=tmp_path / "cairn.db").conn.execute(
        "SELECT account, account_locked FROM sessions WHERE id=?", (sid,)).fetchone()


def _codex_auth(tmp_path, account_id):
    d = tmp_path / ".codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "auth.json").write_text(
        json.dumps({"tokens": {"account_id": account_id}}), encoding="utf-8")
    acctmod._IDENTITY_MEMO.clear()


def _desktop_accounts(tmp_path, *account_uuids):
    root = (tmp_path / "Local" / "Packages" / "Claude_t" / "LocalCache" /
            "Roaming" / "Claude" / "claude-code-sessions")
    for au in account_uuids:
        (root / au / "org").mkdir(parents=True, exist_ok=True)
    acctmod._DESKTOP_MEMO.clear()


# ── Slice 1: fix-session referee = existence, forms preserved ──────────────────
def test_codex_id_two_arg_cannot_touch_current_session(tmp_path, monkeypatch, capsys):
    # THE corruption regression: inside a Claude session, a codex-* arg1 must NOT
    # silently relabel+lock the current session.
    monkeypatch.setenv("CLAUDE_SESSION_ID", UUID_CUR)
    _seed(tmp_path, UUID_CUR, "corp", locked=0)
    cmd_account(["fix-session", "codex-019fZZZZ", "x"])
    out = capsys.readouterr().out.lower()
    assert "refusing" in out
    r = _row(tmp_path, UUID_CUR)
    assert (r["account"], r["account_locked"]) == ("corp", 0)   # UNCHANGED


def test_codex_two_arg_targets_existing_codex_session(tmp_path):
    # The previously-IMPOSSIBLE repair: a codex-* session can now be targeted by id.
    _seed(tmp_path, CODEX_SID, "old")
    cmd_account(["fix-session", CODEX_SID, "gptwork"])
    r = _row(tmp_path, CODEX_SID)
    assert (r["account"], r["account_locked"]) == ("gptwork", 1)


def test_unknown_session_looking_one_arg_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_SESSION_ID", UUID_CUR)
    _seed(tmp_path, UUID_CUR, "corp")
    cmd_account(["fix-session", "codex-nope"])           # one arg, looks like a session, absent
    out = capsys.readouterr().out.lower()
    assert "refusing" in out
    assert (_row(tmp_path, UUID_CUR)["account"], _row(tmp_path, UUID_CUR)["account_locked"]) \
        == ("corp", 0)                                   # current session untouched


def test_plain_name_one_arg_still_labels_current(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", UUID_CUR)
    _seed(tmp_path, UUID_CUR, "corp")
    cmd_account(["fix-session", "mywork"])               # plain, not session-shaped
    r = _row(tmp_path, UUID_CUR)
    assert (r["account"], r["account_locked"]) == ("mywork", 1)


def test_two_arg_unknown_session_fails_closed(tmp_path, monkeypatch, capsys):
    # a UUID arg1 that isn't in the vault also fails closed (never current session)
    monkeypatch.setenv("CLAUDE_SESSION_ID", UUID_CUR)
    _seed(tmp_path, UUID_CUR, "corp")
    cmd_account(["fix-session", "ffffffff-0000-4000-8000-00000000ffff", "x"])
    assert "refusing" in capsys.readouterr().out.lower()
    assert _row(tmp_path, UUID_CUR)["account"] == "corp"


def test_rejects_extra_args(tmp_path, capsys):
    _seed(tmp_path, UUID_CUR, "corp")
    cmd_account(["fix-session", UUID_CUR, "a", "b"])
    assert "too many arguments" in capsys.readouterr().out.lower()
    assert _row(tmp_path, UUID_CUR)["account"] == "corp"   # nothing changed


# ── Slice 2: doctor sees Codex, strictly read-only ─────────────────────────────
def test_doctor_sees_codex_and_writes_nothing(tmp_path, capsys):
    _accounts(tmp_path, {"corp": ("Claude", CL1)})
    _codex_auth(tmp_path, "gpt-unregistered-999")        # id NOT in accounts.json
    before = (tmp_path / ".cairn" / "accounts.json").read_bytes()
    cmd_account(["doctor"])
    after = (tmp_path / ".cairn" / "accounts.json").read_bytes()
    assert after == before                               # doctor wrote nothing
    out = capsys.readouterr().out
    assert "Codex auth" in out
    assert "(unregistered id)" in out                    # read-only lookup, not mint
    assert "read-only — nothing changed" in out
    assert "gpt-unregistered-999" not in after.decode()  # never registered


# ── Slice 3b: maker-aware multi-account (no cross-maker leak) ───────────────────
def test_desktop_folders_are_claude_only_not_gpt(tmp_path):
    _desktop_accounts(tmp_path, CL1, CL2)                # two Claude Desktop accounts
    _accounts(tmp_path, {})                              # no registered accounts
    assert _multi_account_machine("Claude") is True      # Claude sees them
    assert _multi_account_machine("GPT") is False        # GPT must NOT


# ── Issue 2 + Slice 3c: Codex lock unlocked on multi-GPT, warning fires ─────────
def test_two_gpt_accounts_unlocked_then_warns_then_declared_locks(tmp_path, monkeypatch):
    _accounts(tmp_path, {"gwork": ("GPT", GPT1), "gpersonal": ("GPT", GPT2)})
    _codex_auth(tmp_path, GPT1)                          # login file names one of them
    # no CAIRN_ACCOUNT -> step-4 login-id -> unlocked guess (was hard-locked before)
    slug, locked = _resolve_account(CODEX_SID)
    assert slug and locked is False
    assert orient_account_warning(CODEX_SID)             # non-empty -> warning fires
    # validated declaration re-locks -> no warning
    monkeypatch.setenv("CAIRN_ACCOUNT", "gwork")
    vaultmod._ACCOUNT_MEMO.clear()
    assert _live_account_locked(CODEX_SID) is True
    assert orient_account_warning(CODEX_SID) == ""


def test_warning_fires_for_claude_uuid_multi_account(tmp_path):
    _accounts(tmp_path, {"corp": ("Claude", CL1), "acme": ("Claude", CL2)})
    w = orient_account_warning(UUID_CUR)                 # bare uuid = Claude Code session
    assert w and "Claude" in w and "best guess" in w


def test_warning_fires_for_codex_mcp_multi_account(tmp_path):
    _accounts(tmp_path, {"gwork": ("GPT", GPT1), "gpersonal": ("GPT", GPT2)})
    _codex_auth(tmp_path, GPT1)
    w = orient_account_warning(MCP_CODEX_SID)            # mcp-codex-* = GPT
    assert w and "GPT" in w


def test_warning_silent_single_account(tmp_path):
    _accounts(tmp_path, {"corp": ("Claude", CL1)})       # one account only
    assert orient_account_warning(UUID_CUR) == ""


def test_warning_silent_unknown_maker(tmp_path):
    _accounts(tmp_path, {"corp": ("Claude", CL1), "acme": ("Claude", CL2)})
    # a session id whose maker can't be proven never warns (honest, no false alarm)
    assert orient_account_warning("mcp-local-agent-mode-cairn-2026-07-15") == ""
    assert orient_account_warning("session-2026-07-15-1200") == ""


# ── the session-to-maker resolver itself ───────────────────────────────────────
def test_maker_for_session_shapes():
    assert maker_for_session(CODEX_SID) == "GPT"
    assert maker_for_session("mcp-codex-mcp-client-2026-07-15") == "GPT"
    assert maker_for_session(UUID_CUR) == "Claude"       # bare uuid = Claude Code
    assert maker_for_session("import-claude-2026") == "Claude"
    assert maker_for_session("import-codex-2026") == "GPT"
    assert maker_for_session("import-x") == "Unknown"    # unknown src, not defaulted
    assert maker_for_session("mcp-local-agent-mode-cairn-x") == "Unknown"
    assert maker_for_session("session-2026-07-15-1200") == "Unknown"
    assert maker_for_session("") == "Unknown"
