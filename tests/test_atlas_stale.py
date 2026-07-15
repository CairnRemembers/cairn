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


def _meta(v, key):
    r = v.conn.execute("SELECT v FROM atlas_meta WHERE k=?", (key,)).fetchone()
    return (r["v"] if r and r["v"] is not None else 0)


def _dirty(v):
    return _meta(v, "dirty_rev")


def _built(v):
    return _meta(v, "built_rev")


def _stale(v):
    # stale ⟺ an account change bumped dirty_rev past the last built_rev (the
    # monotonic generation model that replaced the old 0/1 'stale' flag).
    return 1 if _dirty(v) > _built(v) else 0


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
    v.conn.execute("INSERT INTO atlas_meta(k,v) VALUES('dirty_rev', "
                   "COALESCE((SELECT v FROM atlas_meta WHERE k='built_rev'),0)+1) "
                   "ON CONFLICT(k) DO UPDATE SET v=v+1")
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


# ── Correction regressions (mid-rebuild race, connection isolation, real import) ──

def test_account_change_during_rebuild_stays_dirty(tmp_path, monkeypatch):
    # THE lost-update race: an account change that lands WHILE a rebuild is mid-flight
    # must not be clobbered clean. We model the interleaving deterministically by
    # forcing compute_atlas to build FROM an older generation than the current
    # dirty_rev — exactly what happens when the change arrives after the snapshot.
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sA", "AcctA")
    _seed_nodes(v, "sA", 4, "a")
    compute_atlas(v)
    assert _stale(v) == 0
    g_built = _built(v)                                # the generation baked in
    _seed_session(v, "sB", "AcctB")                    # a new galaxy appears →
    _seed_nodes(v, "sB", 4, "b")                       # dirty_rev bumps past g_built
    assert _stale(v) == 1
    # Force the NEXT compute_atlas to behave like a rebuild that STARTED before AcctB
    # arrived (its snapshot predates the change): it observed the OLD generation.
    import cairn.edges as edgesmod
    gen = {"val": g_built}
    monkeypatch.setattr(edgesmod, "_atlas_dirty_gen", lambda vault: gen["val"])
    compute_atlas(v)                                   # a late, stale-generation rebuild
    # The OLD unconditional stale=0 clear would mark it clean here and the bug would
    # silently return. The generation design must leave it dirty.
    assert _stale(v) == 1, "lost-update: a mid-rebuild account change was clobbered clean"
    # A rebuild that observes the CURRENT generation then clears it.
    gen["val"] = _dirty(v)
    compute_atlas(v)
    assert _stale(v) == 0


class _ExplodingConn:
    """Any use raises — proves rebuild_atlas_if_stale never touches this handle."""
    def execute(self, *a, **k):
        raise AssertionError("rebuild used the caller's shared connection")
    def cursor(self, *a, **k):
        raise AssertionError("rebuild used the caller's shared connection")
    def commit(self, *a, **k):
        raise AssertionError("rebuild used the caller's shared connection")
    def close(self, *a, **k):
        pass


class _VaultFacade:
    """A vault whose .conn explodes on use but whose .db_path is real."""
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = _ExplodingConn()


def test_rebuild_never_touches_the_callers_connection(tmp_path, monkeypatch):
    # The blocker fix's contract: the rebuild must do ALL its DB work on its own
    # short-lived connection, never the caller's shared handle (which the SSE feed
    # reads concurrently). Passing a vault whose .conn explodes on any use, a passing
    # rebuild proves it opened its own connection instead.
    _acct_env(tmp_path, monkeypatch)
    real = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(real, "sA", "A"); _seed_nodes(real, "sA", 4, "a")
    _seed_session(real, "sB", "B"); _seed_nodes(real, "sB", 4, "b")   # new galaxy → stale
    assert _stale(real) == 1
    real.conn.close()                                  # drop our handle entirely
    from cairn.edges import rebuild_atlas_if_stale
    facade = _VaultFacade(str(tmp_path / "cairn.db"))
    assert rebuild_atlas_if_stale(facade) is True      # succeeded via its OWN connection
    v2 = Vault(db_path=str(tmp_path / "cairn.db"))      # fresh reader
    assert _stale(v2) == 0
    ax, ay, _ = _centroid(v2, "A")
    bx, by, _ = _centroid(v2, "B")
    assert math.hypot(ax - bx, ay - by) > 100          # galaxies separated


def test_concurrent_reader_survives_rebuild(tmp_path, monkeypatch):
    # A reader on its OWN connection (like the dashboard SSE feed) hammers SELECTs
    # while rebuilds run repeatedly. With the dedicated-connection design + WAL this
    # never raises; the old shared-connection write raced the reader → InterfaceError.
    import threading
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sA", "A"); _seed_nodes(v, "sA", 20, "a")
    compute_atlas(v)
    _seed_session(v, "sB", "B"); _seed_nodes(v, "sB", 20, "b")
    assert _stale(v) == 1
    from cairn.edges import rebuild_atlas_if_stale
    errors: list = []
    stop = threading.Event()

    def reader():
        rv = Vault(db_path=str(tmp_path / "cairn.db"))
        try:
            while not stop.is_set():
                rv.conn.execute("SELECT id, map_x, map_y FROM nodes "
                                "WHERE status='active'").fetchall()
        except Exception as e:
            errors.append(repr(e))
        finally:
            rv.conn.close()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for _ in range(20):
            v.conn.execute("INSERT INTO atlas_meta(k,v) VALUES('dirty_rev',1) "
                           "ON CONFLICT(k) DO UPDATE SET v=v+1")
            v.conn.commit()
            rebuild_atlas_if_stale(v)
    finally:
        stop.set(); t.join(timeout=5)
    assert not errors, f"reader errored during rebuild: {errors}"
    assert _stale(v) == 0


def test_real_import_export_new_account_invalidates_atlas(tmp_path, monkeypatch):
    # Codex's importer-coverage ask: exercise the REAL importer (import_export →
    # v.write), not a raw session insert. Importing a conversation under a new
    # account must flag the atlas so it self-heals with no manual command.
    import json
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "liveS", "Live")
    _seed_nodes(v, "liveS", 5, "live")
    compute_atlas(v)
    assert _stale(v) == 0
    export = tmp_path / "claude_export.json"
    export.write_text(json.dumps([{
        "name": "an imported conversation",
        "created_at": "2026-07-10T00:00:00Z",
        "chat_messages": [
            {"sender": "human",
             "text": "a substantive imported question worth keeping in the vault",
             "created_at": "2026-07-10T00:00:00Z"},
            {"sender": "assistant",
             "text": "a substantive, specific imported answer that endures over time",
             "created_at": "2026-07-10T00:00:01Z"},
        ],
    }]), encoding="utf-8")
    from cairn.importer import import_export
    rep = import_export(export, source="claude", vault=v, account="ImportedGalaxy")
    assert rep["turns"] >= 1                            # the import really wrote turns
    assert _stale(v) == 1                               # the real importer flagged it
    from cairn.edges import rebuild_atlas_if_stale
    assert rebuild_atlas_if_stale(v) is True
    lx, ly, _ = _centroid(v, "Live")
    ix, iy, _ = _centroid(v, "ImportedGalaxy")
    assert math.hypot(lx - ix, ly - iy) > 100          # imported galaxy separated


# ── round-2 audit fixes: upgrade heal, trigger refresh, out-of-order guard, SSE ──

def test_upgrade_seeds_dirty_for_preexisting_overlapped_vault(tmp_path, monkeypatch):
    # Fix #1: a v0.3.1-shaped vault (no generation keys) that is ALREADY overlapped
    # must be marked dirty by _migrate on upgrade, so the first /api/atlas re-separates
    # it — the headline heal for issue #3. Without the seed, both keys read 0 = clean.
    _acct_env(tmp_path, monkeypatch)
    db = str(tmp_path / "cairn.db")
    v = Vault(db_path=db)
    _seed_session(v, "sA", "AcctA"); _seed_nodes(v, "sA", 4, "a")
    _seed_session(v, "sB", "AcctB"); _seed_nodes(v, "sB", 4, "b")
    compute_atlas(v)
    # Force the pre-upgrade OVERLAP: collapse every node onto one point — the stale
    # single-galaxy state issue #3 describes — so the heal must genuinely re-separate.
    v.conn.execute("UPDATE nodes SET map_x = 0.0, map_y = 0.0")
    # Simulate a pre-generation-model vault: no built_rev/dirty_rev ever written.
    v.conn.execute("DELETE FROM atlas_meta WHERE k IN ('dirty_rev','built_rev')")
    v.conn.commit()
    ax, ay, _ = _centroid(v, "AcctA"); bx, by, _ = _centroid(v, "AcctB")
    assert math.hypot(ax - bx, ay - by) == 0            # galaxies OVERLAP before upgrade
    assert _stale(v) == 0                               # both keys absent -> reads clean (bug)
    v.conn.close()
    v2 = Vault(db_path=db)                              # upgrade open -> _migrate seeds
    assert _stale(v2) == 1, "upgrade did not invalidate a pre-existing overlapped vault"
    from cairn.edges import rebuild_atlas_if_stale
    assert rebuild_atlas_if_stale(v2) is True           # self-heals, no manual command
    assert _stale(v2) == 0
    ax, ay, _ = _centroid(v2, "AcctA"); bx, by, _ = _centroid(v2, "AcctB")
    assert math.hypot(ax - bx, ay - by) > 100           # re-separated from the overlap


def test_upgrade_does_not_seed_single_galaxy_vault(tmp_path, monkeypatch):
    # Fix #1 gate: a single-galaxy vault was never overlapped, so the upgrade seed
    # must NOT fire (no needless rebuild).
    _acct_env(tmp_path, monkeypatch)
    db = str(tmp_path / "cairn.db")
    v = Vault(db_path=db)
    _seed_session(v, "solo", "Solo"); _seed_nodes(v, "solo", 5, "s")
    compute_atlas(v)
    v.conn.execute("DELETE FROM atlas_meta WHERE k IN ('dirty_rev','built_rev')")
    v.conn.commit()
    v.conn.close()
    v2 = Vault(db_path=db)
    assert _stale(v2) == 0                              # single galaxy -> no seed -> clean


def test_migrate_refreshes_both_atlas_triggers(tmp_path, monkeypatch):
    # Fix #2: a DB carrying the 04fe766 boolean-'stale' trigger BODIES must be upgraded
    # by _migrate to the dirty_rev bodies — for BOTH atlas triggers, not only one.
    _acct_env(tmp_path, monkeypatch)
    db = str(tmp_path / "cairn.db")
    v = Vault(db_path=db)
    # Install the OLD 04fe766 boolean-body versions of BOTH atlas triggers.
    v.conn.executescript("""
        DROP TRIGGER IF EXISTS atlas_stale_on_account_change;
        DROP TRIGGER IF EXISTS atlas_stale_on_new_galaxy;
        CREATE TRIGGER atlas_stale_on_account_change
        AFTER UPDATE OF account ON sessions
        FOR EACH ROW WHEN NEW.account IS NOT OLD.account
        BEGIN
            INSERT INTO atlas_meta(k, v) VALUES ('stale', 1) ON CONFLICT(k) DO UPDATE SET v = 1;
        END;
        CREATE TRIGGER atlas_stale_on_new_galaxy
        AFTER INSERT ON sessions
        FOR EACH ROW WHEN NEW.account IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM sessions WHERE account = NEW.account AND id <> NEW.id)
        BEGIN
            INSERT INTO atlas_meta(k, v) VALUES ('stale', 1) ON CONFLICT(k) DO UPDATE SET v = 1;
        END;
    """)
    v.conn.commit()
    v.conn.close()
    v2 = Vault(db_path=db)                              # _migrate must refresh BOTH bodies
    # new_galaxy path: a brand-new-account session INSERT must bump dirty_rev
    d0 = _dirty(v2)
    _seed_session(v2, "s1", "AcctA")
    assert _dirty(v2) > d0, "new_galaxy trigger not refreshed (no dirty_rev on new galaxy)"
    # account_change path: reassigning that session's account must bump dirty_rev
    d1 = _dirty(v2)
    v2.conn.execute("UPDATE sessions SET account='AcctB' WHERE id='s1'")
    v2.conn.commit()
    assert _dirty(v2) > d1, "account_change trigger not refreshed (no dirty_rev on reassign)"


def test_stale_generation_compute_cannot_publish(tmp_path, monkeypatch):
    # Fix #3 (out-of-order guard, DETERMINISTIC — no threads/timing): a compute that
    # observed an OLDER generation than the current dirty_rev must NOT publish
    # coordinates or bump rev. Else a slow older rebuild finishing last would clobber a
    # newer one's coords while marking the atlas falsely clean. We force
    # observed_gen != current dirty_rev by monkeypatching _atlas_dirty_gen to a stale
    # value while the real dirty_rev is ahead.
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sA", "AcctA"); _seed_nodes(v, "sA", 4, "a")
    _seed_session(v, "sB", "AcctB"); _seed_nodes(v, "sB", 4, "b")
    compute_atlas(v)                                    # clean baseline (built == dirty)

    def _coords():
        return {r["id"]: (r["map_x"], r["map_y"]) for r in
                v.conn.execute("SELECT id, map_x, map_y FROM nodes").fetchall()}
    coords_before, rev_before = _coords(), _rev(v)

    # A newer generation lands (dirty_rev advances past what a slow compute observed).
    v.conn.execute("INSERT INTO atlas_meta(k,v) VALUES('dirty_rev',1) "
                   "ON CONFLICT(k) DO UPDATE SET v=v+1")
    v.conn.commit()
    built_now = _built(v)
    assert _dirty(v) > built_now                        # genuinely stale

    # Force compute_atlas to run as a STALE-generation writer: observed_gen = the old
    # built generation, while the real dirty_rev is ahead. The publish guard must skip.
    import cairn.edges as edgesmod
    monkeypatch.setattr(edgesmod, "_atlas_dirty_gen", lambda vault: built_now)
    compute_atlas(v)

    assert _coords() == coords_before, "stale-generation compute clobbered coordinates"
    assert _rev(v) == rev_before, "stale-generation compute advanced the revision"
    assert _dirty(v) > _built(v), "atlas falsely marked clean by a stale compute"


def test_sse_start_ts_never_empty_string_on_failure(tmp_path, monkeypatch):
    # Fix #4: the SSE start-boundary helper must return None (caller retries) — NOT ""
    # — when the read fails, so a transient error can't replay the whole vault as
    # "live" via `WHERE timestamp > ''`.
    from cairn.dashboard import _sse_start_ts

    class _BoomConn:
        def execute(self, *a, **k):
            raise Exception("transient read failure")
    assert _sse_start_ts(_BoomConn()) is None           # never "" on failure

    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "s", "Solo"); _seed_nodes(v, "s", 3, "s")
    ts = _sse_start_ts(v.conn)
    assert ts and ts != ""                              # a real "now" boundary
    # that boundary excludes ALL existing rows -> no history replayed
    n = v.conn.execute("SELECT COUNT(*) c FROM nodes WHERE timestamp > ?",
                       (ts,)).fetchone()["c"]
    assert n == 0


def test_new_galaxy_session_before_nodes_reinvalidates_on_first_node(tmp_path, monkeypatch):
    # Codex final-pass race: the readers commit a new-account session BEFORE its first
    # nodes. If /api/atlas rebuilds in that gap, built_rev catches dirty_rev on an EMPTY
    # galaxy; the later first-node insert must RE-invalidate (via the central first-node
    # trigger) so the node is baked, not left unmapped-and-clean forever. Deterministic.
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "sA", "AcctA"); _seed_nodes(v, "sA", 5, "a")
    compute_atlas(v)
    from cairn.edges import rebuild_atlas_if_stale
    # (1) a NEW-galaxy session is committed with NO nodes (the stamp-before-nodes commit)
    v.conn.execute("INSERT INTO sessions (id, started_at, account, account_locked) "
                   "VALUES ('sB','2026-07-07T00:00:00','AcctB',1)")
    v.conn.commit()
    assert _stale(v) == 1                               # new_galaxy trigger bumped dirty
    # (2) a rebuild runs in the gap — AcctB has no nodes, so it bakes nothing for it
    rebuild_atlas_if_stale(v)
    assert _stale(v) == 0                               # built caught dirty on the empty galaxy
    # (3) the first node of the new galaxy finally arrives (normal write path)
    _seed_nodes(v, "sB", 3, "b")
    assert _stale(v) == 1, "first node of an already-stamped new galaxy did not re-invalidate"
    # ...and a rebuild now bakes it and separates the galaxies with no manual command
    assert rebuild_atlas_if_stale(v) is True
    baked = v.conn.execute("SELECT COUNT(*) c FROM nodes n JOIN sessions s ON n.session=s.id "
                           "WHERE s.account='AcctB' AND n.map_x IS NOT NULL").fetchone()["c"]
    assert baked == 3                                   # new galaxy's nodes now baked
    ax, ay, _ = _centroid(v, "AcctA"); bx, by, _ = _centroid(v, "AcctB")
    assert math.hypot(ax - bx, ay - by) > 100


def test_first_node_of_second_session_in_existing_galaxy_does_not_dirty(tmp_path, monkeypatch):
    # Codex over-correction fix: the first-node trigger must fire only when the ACCOUNT
    # has no nodes ANYWHERE (a genuinely new galaxy) — NOT merely when the SESSION has no
    # nodes. A first node in a SECOND session of an EXISTING galaxy must NOT invalidate
    # (that's a new conversation, not a structural galaxy change).
    _acct_env(tmp_path, monkeypatch)
    v = Vault(db_path=str(tmp_path / "cairn.db"))
    _seed_session(v, "s1", "AcctA"); _seed_nodes(v, "s1", 4, "a")   # galaxy AcctA now has nodes
    compute_atlas(v)
    assert _stale(v) == 0                                            # clean baseline (built == dirty)
    d0 = _dirty(v)
    # a SECOND session for the SAME account (existing galaxy), stamped, no nodes yet
    v.conn.execute("INSERT INTO sessions (id, started_at, account, account_locked) "
                   "VALUES ('s2','2026-07-07T00:00:00','AcctA',1)")
    v.conn.commit()
    assert _stale(v) == 0 and _dirty(v) == d0                        # new_galaxy did NOT fire (AcctA exists)
    # its first node arrives — must NOT dirty the atlas (galaxy already baked)
    _seed_nodes(v, "s2", 3, "a2")
    assert _dirty(v) == d0, "first node of a second session in an existing galaxy over-invalidated"
    assert _stale(v) == 0
    # case-variant of an existing account: its first node must ALSO not be treated as
    # a new galaxy by THIS trigger (LOWER match). Measure the delta around the NODE
    # insert only — the session INSERT may separately fire new_galaxy's exact-match path.
    v.conn.execute("INSERT INTO sessions (id, started_at, account, account_locked) "
                   "VALUES ('s3','2026-07-07T00:00:00','accta',1)")   # lowercase variant of AcctA
    v.conn.commit()
    d1 = _dirty(v)
    _seed_nodes(v, "s3", 2, "a3")
    assert _dirty(v) == d1, "first node of a case-variant of an existing galaxy over-invalidated"
