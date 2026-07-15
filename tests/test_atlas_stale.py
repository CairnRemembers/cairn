"""
tests/test_atlas_stale.py — GitHub issue: the dashboard can render multiple
account galaxies as one until the atlas is manually rebuilt.

Proves the fix's acceptance criteria:
  - compute_atlas() gives each account its own spatial cluster (no overlap),
  - it bumps a monotonic atlas revision (so an open dashboard can notice a
    coordinate-only rebuild even when node/edge counts are unchanged),
  - repairing an account (cairn account fix-session) AUTO-realigns the atlas —
    no manual `cairn edges` needed,
  - single-account layout is deterministic/unchanged,
  - the realign NEVER mutates node content (only map_x/map_y).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
import cairn.accounts as acctmod
from cairn.vault import Vault, MicroNode
from cairn.edges import compute_atlas


def _seed_session(v, sid, account, locked=0):
    v.conn.execute(
        "INSERT OR REPLACE INTO sessions (id, started_at, account, account_locked) "
        "VALUES (?,?,?,?)", (sid, "2026-07-07T00:00:00", account, locked))
    v.conn.commit()


def _seed_nodes(v, sid, n, prefix):
    for i in range(n):
        v.write(MicroNode(session=sid, kind="insight",
                          query=f"{prefix} node {i} — a durable fact worth keeping",
                          model="human", agent_role="worker", memory_tier=1,
                          tags=[prefix]))


def _rev(v):
    try:
        r = v.conn.execute("SELECT v FROM atlas_meta WHERE k='rev'").fetchone()
        return r["v"] if r else 0
    except Exception:
        return 0


def _centroid(v, account):
    rows = v.conn.execute(
        "SELECT n.map_x mx, n.map_y my FROM nodes n JOIN sessions s ON n.session=s.id "
        "WHERE s.account=? AND n.map_x IS NOT NULL", (account,)).fetchall()
    k = len(rows)
    assert k, f"no positioned nodes for {account}"
    return (sum(r["mx"] for r in rows) / k, sum(r["my"] for r in rows) / k, k)


def test_compute_atlas_separates_accounts_and_bumps_rev(tmp_path):
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sessA", "AcctA")
    _seed_session(v, "sessB", "AcctB")
    _seed_nodes(v, "sessA", 6, "A")
    _seed_nodes(v, "sessB", 6, "B")
    r0 = _rev(v)
    compute_atlas(v)
    assert _rev(v) == r0 + 1                          # revision bumped once
    ax, ay, an = _centroid(v, "AcctA")
    bx, by, bn = _centroid(v, "AcctB")
    assert an == 6 and bn == 6                        # every node positioned
    dist = math.hypot(ax - bx, ay - by)
    assert dist > 100, f"galaxies overlap: centroids only {dist:.0f} apart"
    compute_atlas(v)
    assert _rev(v) == r0 + 2                          # bumps again each run


def test_single_account_layout_is_stable(tmp_path):
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "solo", "Solo")
    _seed_nodes(v, "solo", 8, "S")
    compute_atlas(v)
    pos1 = {r["id"]: (r["map_x"], r["map_y"]) for r in
            v.conn.execute("SELECT id, map_x, map_y FROM nodes").fetchall()}
    compute_atlas(v)
    pos2 = {r["id"]: (r["map_x"], r["map_y"]) for r in
            v.conn.execute("SELECT id, map_x, map_y FROM nodes").fetchall()}
    assert pos1 == pos2 and all(p[0] is not None for p in pos1.values())


def test_compute_atlas_never_mutates_content(tmp_path):
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sessA", "AcctA")
    _seed_nodes(v, "sessA", 4, "A")
    cols = "id, query, episodic_text, status, kind, tags"
    before = {r["id"]: tuple(r) for r in
              v.conn.execute(f"SELECT {cols} FROM nodes").fetchall()}
    compute_atlas(v)
    after = {r["id"]: tuple(r) for r in
             v.conn.execute(f"SELECT {cols} FROM nodes").fetchall()}
    assert before == after                            # only map_x/map_y touched


def test_fix_session_auto_realigns_atlas(tmp_path, monkeypatch):
    # reassigning a session's account (a second galaxy appears) must realign the
    # atlas automatically — the exact GitHub-issue trigger, no manual `cairn edges`.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)

    S1 = "a1a1a1a1-0000-4000-8000-000000000001"
    S2 = "b2b2b2b2-0000-4000-8000-000000000002"
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, S1, "Acct", locked=0)
    _seed_session(v, S2, "Acct", locked=0)            # both one galaxy at first
    _seed_nodes(v, S1, 6, "one")
    _seed_nodes(v, S2, 6, "two")
    compute_atlas(v)                                  # baked as a single galaxy
    r0 = _rev(v)

    from cairn.__main__ import cmd_account
    cmd_account(["fix-session", S2, "Other"])         # S2 -> a NEW galaxy

    v2 = Vault(db_path=str(tmp_path / "cairn.db"))     # fresh conn to read committed state
    assert v2.conn.execute("SELECT account FROM sessions WHERE id=?", (S2,)).fetchone()[0] == "Other"
    assert _rev(v2) > r0, "atlas was not realigned after the account change"
    ax, ay, _ = _centroid(v2, "Acct")
    bx, by, _ = _centroid(v2, "Other")
    assert math.hypot(ax - bx, ay - by) > 100, "galaxies still overlap after repair"


def _stale(v):
    r = v.conn.execute("SELECT v FROM atlas_meta WHERE k='stale'").fetchone()
    return (r["v"] if r else 0)


def _acct_env(tmp_path, monkeypatch, *registered):
    """Isolate HOME/vault, register account slugs so CAIRN_ACCOUNT locks them."""
    import json as _json
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    vaultmod._ACCOUNT_MEMO.clear()
    acctmod._DESKTOP_MEMO.clear()
    acctmod._IDENTITY_MEMO.clear()
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cairn" / "accounts.json").write_text(_json.dumps(
        {s.lower(): {"label": s, "maker": "Claude", "stable_id": s.lower()}
         for s in registered}), encoding="utf-8")


# ── normal-capture path: a NEW second account appears → auto-separates, no cmd ──
def test_new_account_via_capture_separates_on_next_atlas_request(tmp_path, monkeypatch):
    _acct_env(tmp_path, monkeypatch, "Claude", "Gpt")
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    # Phase 1 — Claude captures + a rebuild bakes it (as an earlier sleep would).
    monkeypatch.setenv("CAIRN_ACCOUNT", "Claude")
    _seed_nodes(v, "claudeS", 8, "c")
    compute_atlas(v)
    assert _stale(v) == 0                              # baked, flag cleared
    # Phase 2 — the GPT account writes for the FIRST time via ordinary capture.
    monkeypatch.setenv("CAIRN_ACCOUNT", "Gpt")
    vaultmod._ACCOUNT_MEMO.clear()
    _seed_nodes(v, "gptS", 8, "g")
    assert _stale(v) == 1                              # trigger flagged it — no manual step
    r_before = v.conn.execute(
        "SELECT COUNT(*) c FROM nodes n JOIN sessions s ON n.session=s.id "
        "WHERE s.account='Gpt' AND n.map_x IS NULL").fetchone()["c"]
    assert r_before == 8                               # GPT nodes un-baked → would overlap at center
    rev0 = _rev(v)
    # THE ATLAS REQUEST — no cairn edges / atlas / sleep. This is what /api/atlas calls.
    from cairn.edges import rebuild_atlas_if_stale
    assert rebuild_atlas_if_stale(v) is True
    assert _stale(v) == 0 and _rev(v) == rev0 + 1      # rebuilt once, flag cleared, rev bumped
    cx, cy, cn = _centroid(v, "Claude")
    gx, gy, gn = _centroid(v, "Gpt")
    assert cn == 8 and gn == 8                         # GPT now baked
    assert math.hypot(cx - gx, cy - gy) > 100          # galaxies AUTO-separated


def test_guess_heals_to_proven_account_flags_stale(tmp_path, monkeypatch):
    _acct_env(tmp_path, monkeypatch, "Bbb")
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "s1", "Aaa", locked=0)           # an unlocked GUESS
    _seed_nodes(v, "s1", 4, "a")
    compute_atlas(v)
    assert _stale(v) == 0
    monkeypatch.setenv("CAIRN_ACCOUNT", "Bbb")         # proof/declared, registered → locked
    vaultmod._ACCOUNT_MEMO.clear()
    v.write(MicroNode(session="s1", kind="insight", query="a healed turn worth keeping",
                      model="human", agent_role="worker", memory_tier=1, tags=["h"]))
    healed = v.conn.execute("SELECT account FROM sessions WHERE id='s1'").fetchone()["account"]
    assert healed == "Bbb"                             # guess healed to the proven account
    assert _stale(v) == 1                              # and the atlas was flagged


def test_import_style_new_account_flags_stale(tmp_path, monkeypatch):
    # an import introducing a new account lands as a sessions INSERT → trigger fires.
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sA", "Existing")
    _seed_nodes(v, "sA", 3, "a")
    compute_atlas(v)
    assert _stale(v) == 0
    _seed_session(v, "import-x", "Imported")           # a fresh galaxy via (import) insert
    assert _stale(v) == 1


def test_new_session_in_existing_galaxy_not_flagged(tmp_path, monkeypatch):
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "s1", "Solo")
    _seed_nodes(v, "s1", 4, "a")
    compute_atlas(v)
    assert _stale(v) == 0
    _seed_session(v, "s2", "Solo")                     # SAME galaxy, different session
    assert _stale(v) == 0                              # no new galaxy → not flagged, no needless rebuild


def test_rebuild_atlas_if_stale_idempotent_and_guarded(tmp_path, monkeypatch):
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "s", "Solo")
    _seed_nodes(v, "s", 5, "s")
    compute_atlas(v)
    from cairn.edges import rebuild_atlas_if_stale, _atlas_rebuild_lock
    r0 = _rev(v)
    assert rebuild_atlas_if_stale(v) is False and _rev(v) == r0        # not stale → no-op
    v.conn.execute("INSERT INTO atlas_meta(k,v) VALUES('stale',1) "
                   "ON CONFLICT(k) DO UPDATE SET v=1")
    v.conn.commit()
    _atlas_rebuild_lock.acquire()                                      # simulate a concurrent rebuild
    try:
        assert rebuild_atlas_if_stale(v) is False                     # guarded → skipped
        assert _stale(v) == 1 and _rev(v) == r0                        # no duplicate rebuild
    finally:
        _atlas_rebuild_lock.release()
    assert rebuild_atlas_if_stale(v) is True                           # lock free → rebuild once
    assert _stale(v) == 0 and _rev(v) == r0 + 1


def test_cmd_atlas_recomputes(tmp_path, monkeypatch):
    # the lightweight `cairn atlas` command: recompute positions, bump rev, separate.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "gA", "GalA")
    _seed_session(v, "gB", "GalB")
    _seed_nodes(v, "gA", 6, "a")
    _seed_nodes(v, "gB", 6, "b")
    r0 = _rev(v)
    from cairn.__main__ import cmd_atlas
    cmd_atlas([])                                     # Vault() -> VAULT_ROOT/cairn.db
    v2 = Vault(db_path=str(tmp_path / "cairn.db"))
    assert _rev(v2) == r0 + 1                          # recomputed + bumped
    ax, ay, _ = _centroid(v2, "GalA")
    bx, by, _ = _centroid(v2, "GalB")
    assert math.hypot(ax - bx, ay - by) > 100         # separated
