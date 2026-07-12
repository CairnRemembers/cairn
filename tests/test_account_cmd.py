"""
tests/test_account_cmd.py — `cairn account doctor` (read-only mismatch report)
and `cairn account fix-session` (human-approved LOCKED repair, reversible backup,
never touches node content).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
import cairn.accounts as acctmod
from cairn.__main__ import cmd_account

SLAP = "a1a1a1a1-0000-4000-8000-000000000001"
SING = "b2b2b2b2-0000-4000-8000-000000000002"
ORG_L = "c3c3c3c3-0000-4000-8000-000000000003"
SID = "e5e5e5e5-0000-4000-8000-000000000005"


def _mk_desktop(home: Path, account_uuid, org_uuid, cli_session_id):
    d = (home / "Local" / "Packages" / "Claude_t" / "LocalCache" / "Roaming" /
         "Claude" / "claude-code-sessions" / account_uuid / org_uuid)
    d.mkdir(parents=True, exist_ok=True)
    fn = "local_" + cli_session_id[:8]           # distinct file per session id
    (d / f"{fn}.json").write_text(json.dumps(
        {"sessionId": fn, "cliSessionId": cli_session_id}), encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    acctmod._DESKTOP_MEMO.clear()
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cairn" / "accounts.json").write_text(json.dumps({
        "corp":     {"label": "Corp", "maker": "Claude", "stable_id": SING},
        "acme": {"label": "Acme", "maker": "Claude", "stable_id": SLAP},
    }), encoding="utf-8")
    yield
    acctmod._DESKTOP_MEMO.clear()


def _seed_session(tmp_path, account, locked=0):
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    v.conn.execute(
        "INSERT INTO sessions (id, started_at, account, account_locked) VALUES (?,?,?,?)",
        (SID, "2026-07-05T00:00:00", account, locked))
    v.conn.commit()
    return v


def test_doctor_reports_mismatch(tmp_path, capsys):
    _mk_desktop(tmp_path, SLAP, ORG_L, SID)     # Desktop proof says acme
    _seed_session(tmp_path, "corp")            # but the DB says corp
    cmd_account(["doctor"])
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert SID[:8] in out
    assert "acme" in out                   # proof suggestion shown


def test_fix_session_repairs_and_locks(tmp_path):
    _mk_desktop(tmp_path, SLAP, ORG_L, SID)
    _seed_session(tmp_path, "corp")
    acctmod._DESKTOP_MEMO.clear()
    cmd_account(["fix-session", SID, "acme"])
    row = vaultmod.Vault(db_path=tmp_path / "cairn.db").conn.execute(
        "SELECT account, account_locked FROM sessions WHERE id=?", (SID,)).fetchone()
    assert row["account"] == "acme"
    assert row["account_locked"] == 1
    # reversible backup was written
    bak = tmp_path / ".cairn" / "restamp-backup-fix-session.jsonl"
    assert bak.exists()
    rec = json.loads(bak.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["old_account"] == "corp" and rec["new_account"] == "acme"


def test_fix_session_recomputes_from_proof_when_no_slug(tmp_path):
    _mk_desktop(tmp_path, SLAP, ORG_L, SID)
    _seed_session(tmp_path, "corp")
    acctmod._DESKTOP_MEMO.clear()
    cmd_account(["fix-session", SID])           # no slug -> use Desktop proof
    row = vaultmod.Vault(db_path=tmp_path / "cairn.db").conn.execute(
        "SELECT account, account_locked FROM sessions WHERE id=?", (SID,)).fetchone()
    assert row["account"] == "acme" and row["account_locked"] == 1


def test_fix_session_never_guesses_without_id(tmp_path, capsys):
    _seed_session(tmp_path, "corp")
    cmd_account(["fix-session"])                # no CLAUDE_SESSION_ID, no arg
    out = capsys.readouterr().out
    assert "no session id" in out.lower()


def test_backfill_dryrun_is_readonly_then_apply(tmp_path, capsys):
    AGREE = "aaaaaaaa-1111-1111-1111-111111111111"
    UNCOV = "bbbbbbbb-2222-2222-2222-222222222222"
    _mk_desktop(tmp_path, SLAP, ORG_L, SID)      # proof acme (DB says corp -> repair)
    _mk_desktop(tmp_path, SLAP, ORG_L, AGREE)    # proof acme (DB says acme -> lock)
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    for sid, acct in [(SID, "corp"), (AGREE, "acme"),
                      (UNCOV, "corp"), ("import-x", "gpt")]:
        v.conn.execute(
            "INSERT INTO sessions (id, started_at, account, account_locked) VALUES (?,?,?,0)",
            (sid, "t", acct))
    v.conn.commit()
    acctmod._DESKTOP_MEMO.clear()

    cmd_account(["backfill"])                     # dry-run
    assert "DRY-RUN" in capsys.readouterr().out
    con = vaultmod.Vault(db_path=tmp_path / "cairn.db").conn
    r = con.execute("SELECT account, account_locked FROM sessions WHERE id=?", (SID,)).fetchone()
    assert (r["account"], r["account_locked"]) == ("corp", 0)     # dry-run changed nothing

    acctmod._DESKTOP_MEMO.clear()
    cmd_account(["backfill", "--apply"])
    con = vaultmod.Vault(db_path=tmp_path / "cairn.db").conn

    def row(sid):
        return con.execute("SELECT account, account_locked FROM sessions WHERE id=?", (sid,)).fetchone()

    assert (row(SID)["account"], row(SID)["account_locked"]) == ("acme", 1)   # repaired + locked
    assert row(AGREE)["account_locked"] == 1                                        # proof-agree locked
    assert (row(UNCOV)["account"], row(UNCOV)["account_locked"]) == ("corp", 0)    # uncovered LEFT
    assert row("import-x")["account_locked"] == 1                                   # explicit import locked
    assert list((tmp_path / ".cairn").glob("backfill-backup-*.json"))              # backup written
