"""
tests/test_account_stamp.py — the account (galaxy) attribution ladder AND the
confidence (locked/unlocked) bit.

Ladder (highest first):  CAIRN_ACCOUNT env  >  me.json channels prefix  >
Desktop cliSessionId proof  >  harness login-id  >  guarded handle  >  None.

locked = proven/explicit (validated env, channels, Desktop proof) — never
silently overwritten by a later guess. unlocked = a guess/fallback
(multi-account CLI login-id, guarded handle) that later proof / fix-session heals.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
import cairn.accounts as acctmod
from cairn.vault import (_live_account, _live_account_locked, _live_harness,
                         _resolve_account)

SLAP = "a1a1a1a1-0000-4000-8000-000000000001"
SING = "b2b2b2b2-0000-4000-8000-000000000002"
ORG_L = "c3c3c3c3-0000-4000-8000-000000000003"
ORG_S = "d4d4d4d4-0000-4000-8000-000000000004"


def _desktop_root(home: Path) -> Path:
    return (home / "Local" / "Packages" / "Claude_test" /
            "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions")


def _mk_desktop(home: Path, account_uuid, org_uuid, cli_session_id, deskid="d1"):
    d = _desktop_root(home) / account_uuid / org_uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"local_{deskid}.json").write_text(json.dumps(
        {"sessionId": f"local_{deskid}", "cliSessionId": cli_session_id}), encoding="utf-8")


def _write_accounts(home: Path, mapping: dict):
    """mapping: slug -> stable_id (all maker=Claude)."""
    p = home / ".cairn"
    p.mkdir(parents=True, exist_ok=True)
    (p / "accounts.json").write_text(json.dumps(
        {slug: {"label": slug, "maker": "Claude", "stable_id": sid}
         for slug, sid in mapping.items()}), encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.delenv("CAIRN_HARNESS", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # isolate the Desktop store discovery from the real machine (empty ->
    # single-account by default, so the guarded handle fires in basic tests)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()
    (tmp_path / "me.json").write_text(json.dumps(
        {"handle": "HandleAcct", "channels": {"codex-": "ChannelAcct"}}), encoding="utf-8")
    yield
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()


# ── rung 1: env ───────────────────────────────────────────────────────────────
def test_env_wins_over_everything(monkeypatch):
    monkeypatch.setenv("CAIRN_ACCOUNT", "EnvAcct")
    assert _live_account("codex-123") == "EnvAcct"
    assert _live_account("anything") == "EnvAcct"


def test_env_validated_locks_unknown_labels_only(tmp_path, monkeypatch):
    _write_accounts(tmp_path, {"corp": SING})
    monkeypatch.setenv("CAIRN_ACCOUNT", "corp")
    assert _resolve_account("x") == ("corp", True)          # known slug -> locked
    monkeypatch.setenv("CAIRN_ACCOUNT", "typoacct")
    assert _resolve_account("x") == ("typoacct", False)      # unvalidated -> label, no lock


# ── rung 2: channels (+ the mcp-codex fix) ────────────────────────────────────
def test_channel_prefix_beats_handle():
    assert _resolve_account("codex-abc") == ("ChannelAcct", True)


def test_mcp_codex_routes_through_channel():
    # mcp-codex-mcp-client-* must route to the codex- channel (was falling to handle)
    assert _resolve_account("mcp-codex-mcp-client-2026-07-05") == ("ChannelAcct", True)


# ── rung 3: Desktop proof ─────────────────────────────────────────────────────
def test_desktop_proof_locks(tmp_path):
    sid = "e5e5e5e5-0000-4000-8000-000000000005"
    _write_accounts(tmp_path, {"acme": SLAP, "corp": SING})
    _mk_desktop(tmp_path, SLAP, ORG_L, sid)
    acctmod._DESKTOP_MEMO.clear()
    assert _resolve_account(sid) == ("acme", True)


def test_desktop_proof_beats_stale_cli_login(tmp_path):
    # Desktop file says acme; single-slot ~/.claude.json (stale) says corp.
    sid = "e5e5e5e5-0000-4000-8000-000000000005"
    _write_accounts(tmp_path, {"acme": SLAP, "corp": SING})
    _mk_desktop(tmp_path, SLAP, ORG_L, sid)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": SING, "emailAddress": "si@x.com"}}))
    acctmod._DESKTOP_MEMO.clear(); acctmod._IDENTITY_MEMO.clear()
    assert _resolve_account(sid) == ("acme", True)      # proof wins over stale CLI


# ── rung 4: login-id, guarded by single-vs-multi account ──────────────────────
def test_login_id_unlocked_on_multi_account(tmp_path):
    # two Claude accounts -> multi -> CLI login-id LABELS but does not LOCK
    _write_accounts(tmp_path, {"acme": SLAP, "corp": SING})
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": SING, "emailAddress": "si@x.com"}}))
    acctmod._IDENTITY_MEMO.clear()
    assert _resolve_account("uuid-no-desktop-file-here") == ("corp", False)


def test_login_id_locked_on_single_account(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": SING, "emailAddress": "dev@x.com"}}))
    (tmp_path / "me.json").unlink()   # no handle
    vaultmod._ACCOUNT_MEMO.clear(); acctmod._IDENTITY_MEMO.clear()
    acct, locked = _resolve_account("uuid")
    assert acct and locked is True    # sole account -> authoritative -> locked


def test_login_id_beats_handle(tmp_path):
    # INVERTED from the old ladder: login-id (rung 4) now outranks handle (rung 5)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "uuid-xyz", "emailAddress": "someone@example.com"}}))
    acctmod._IDENTITY_MEMO.clear()
    assert _live_account("claude-session-1") == "someone"


# ── rung 5: guarded handle (single-account only) ──────────────────────────────
def test_handle_is_default_when_login_id_missing():
    # no ~/.claude.json -> login-id misses -> handle fires (single-account), unlocked
    assert _resolve_account("claude-session-1") == ("HandleAcct", False)
    assert _live_account_locked("claude-session-1") is False


def test_handle_disarmed_on_multi_account(tmp_path):
    _write_accounts(tmp_path, {"acme": SLAP, "corp": SING})   # multi
    # no ~/.claude.json -> login-id misses; the machine-global handle must NOT fire
    assert _resolve_account("claude-session-1") == (None, False)    # the handle bug is dead


def test_no_config_no_account(tmp_path):
    (tmp_path / "me.json").unlink()
    vaultmod._ACCOUNT_MEMO.clear()
    assert _live_account("x") is None


# ── the write rule: locked never overwritten by a guess; proof heals a guess ──
def test_locked_label_survives_a_later_guess(tmp_path, monkeypatch):
    _write_accounts(tmp_path, {"acme": SLAP})
    v = vaultmod.Vault(db_path=tmp_path / "w.db")
    monkeypatch.setenv("CAIRN_ACCOUNT", "acme")          # validated -> locked
    v.write(vaultmod.MicroNode(session="s1", kind="conversation_turn",
                               query="hi", speaker="user", model="human"))
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id='s1'").fetchone()
    assert (row["account"], row["account_locked"]) == ("Acme", 1)   # stored canonical (Titlecase)
    # a later unlocked write must NOT change the locked label
    v.write(vaultmod.MicroNode(session="s1", kind="conversation_turn",
                               query="more", speaker="user", model="human"))
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id='s1'").fetchone()
    assert (row["account"], row["account_locked"]) == ("Acme", 1)   # unchanged


def test_desktop_proof_upgrades_unlocked_guess(tmp_path):
    # the timing-race heal: first write (no desktop file) is an unlocked login-id
    # guess; when the Desktop file appears, a later write upgrades to locked proof.
    sid = "e5e5e5e5-0000-4000-8000-000000000005"
    _write_accounts(tmp_path, {"acme": SLAP, "corp": SING})   # multi
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": SING, "emailAddress": "si@x.com"}}))
    v = vaultmod.Vault(db_path=tmp_path / "u.db")
    v.write(vaultmod.MicroNode(session=sid, kind="conversation_turn",
                               query="hi", speaker="user", model="human"))
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id=?", (sid,)).fetchone()
    assert (row["account"], row["account_locked"]) == ("Corp", 0)   # provisional guess (stored canonical)
    _mk_desktop(tmp_path, SLAP, ORG_L, sid)
    acctmod._DESKTOP_MEMO.clear()
    v.write(vaultmod.MicroNode(session=sid, kind="conversation_turn",
                               query="more", speaker="user", model="human"))
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id=?", (sid,)).fetchone()
    assert (row["account"], row["account_locked"]) == ("Acme", 1)   # healed by proof


# ── harness (source tool) stamp — unchanged; provenance only ──────────────────
def test_harness_from_session_prefix():
    assert _live_harness("codex-abc") == "codex"
    assert _live_harness("import-chatgpt-2025-01") == "import-chatgpt"
    assert _live_harness("random-uuid-xyz") is None


def test_harness_env_override(monkeypatch):
    monkeypatch.setenv("CAIRN_HARNESS", "mcp")
    assert _live_harness("codex-abc") == "mcp"


def test_harness_claude_code_from_session_signal(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-1")
    assert _live_harness("some-claude-uuid") == "claude-code"


def test_harness_stamped_on_session_row(tmp_path):
    v = vaultmod.Vault(db_path=tmp_path / "h.db")
    v.write(vaultmod.MicroNode(session="codex-t1", kind="conversation_turn",
                               query="hi", speaker="user", model="human"))
    row = v.conn.execute("SELECT harness FROM sessions WHERE id=?", ("codex-t1",)).fetchone()
    assert row["harness"] == "codex"
