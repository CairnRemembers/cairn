"""
tests/test_desktop_proof.py — Claude Desktop per-session PROOF reader
(accounts.desktop_account): match the cairn session id to the Desktop store's
cliSessionId and return the owning account. The packaged-path discovery, the
exact+unique guard, the pending bucket for unknown accounts, and the
non-uuid-id skip are all load-bearing for the attribution-v2 confidence model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.accounts as acct

CORP = "b2b2b2b2-0000-4000-8000-000000000002"
SLAP  = "a1a1a1a1-0000-4000-8000-000000000001"
ORG_S = "d4d4d4d4-0000-4000-8000-000000000004"
ORG_L = "c3c3c3c3-0000-4000-8000-000000000003"
SID   = "e5e5e5e5-0000-4000-8000-000000000005"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    # isolate HOME (accounts.json registry) and the Desktop store roots
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cairn" / "accounts.json").write_text(json.dumps({
        "corp":     {"label": "Claude-Corp", "maker": "Claude", "stable_id": CORP},
        "acme": {"label": "Acme", "maker": "Claude", "stable_id": SLAP},
    }), encoding="utf-8")
    acct._DESKTOP_MEMO.clear()
    yield
    acct._DESKTOP_MEMO.clear()


def _store(tmp_path) -> Path:
    return (tmp_path / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" /
            "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions")


def _mk(tmp_path, account_uuid, org_uuid, deskid, cli_session_id):
    d = _store(tmp_path) / account_uuid / org_uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"local_{deskid}.json").write_text(json.dumps(
        {"sessionId": f"local_{deskid}", "cliSessionId": cli_session_id,
         "cwd": "C:/x", "model": "claude-opus-4-8"}), encoding="utf-8")


def test_exact_match_maps_to_registered_slug(tmp_path):
    _mk(tmp_path, SLAP, ORG_L, "aaaa1111-1111-1111-1111-111111111111", SID)
    r = acct.desktop_account(SID)
    assert r and r["slug"] == "acme"
    assert r["account_uuid"] == SLAP and r["org_uuid"] == ORG_L


def test_no_match_falls_through(tmp_path):
    _mk(tmp_path, SLAP, ORG_L, "aaaa1111-1111-1111-1111-111111111111", "some-other-cli-id")
    assert acct.desktop_account(SID) is None


def test_ambiguous_two_account_folders_falls_through(tmp_path):
    # same cliSessionId under two different account folders -> never guess
    _mk(tmp_path, SLAP,  ORG_L, "aaaa1111-1111-1111-1111-111111111111", SID)
    _mk(tmp_path, CORP, ORG_S, "bbbb2222-2222-2222-2222-222222222222", SID)
    assert acct.desktop_account(SID) is None


def test_unknown_account_uuid_gets_pending_bucket(tmp_path):
    unknown = "99999999-0000-0000-0000-000000000000"
    _mk(tmp_path, unknown, "org-x", "cccc3333-3333-3333-3333-333333333333", SID)
    r = acct.desktop_account(SID)
    assert r and r["slug"] == "claude-99999999" and r["account_uuid"] == unknown


def test_non_uuid_session_ids_are_ignored(tmp_path):
    _mk(tmp_path, SLAP, ORG_L, "aaaa1111-1111-1111-1111-111111111111", SID)
    assert acct.desktop_account("codex-abc123") is None
    assert acct.desktop_account("mcp-codex-mcp-client-2026-07-05") is None
    assert acct.desktop_account("import-claude-2025-01") is None
    assert acct.desktop_account("") is None


def test_reparse_appdata_path_when_unreadable_is_skipped(tmp_path):
    # no packaged store, APPDATA points nowhere real -> no roots -> None (no crash)
    r = acct.desktop_account(SID)
    assert r is None
