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
