"""
tests/test_supersede_zero_mutation.py — the owner-ratified zero-mutation
supersede contract (Step 2).

`cairn note --supersedes=<id>` must:
  * nonexistent target  -> clear nonzero error, ZERO database mutations;
  * already-void target -> clear nonzero error, ZERO database mutations;
  * active target       -> exactly ONE successor row, target retained as
                           status='void', successor carries supersedes:<id>;
  * any failure mid-flight -> NO partial mutation (successor+void are one
                           transaction; both land or neither does).

All tests run against throwaway vaults in tmp homes (HOME/USERPROFILE
monkeypatched, mirroring the repo's existing garden test harness).

Run: python -m pytest tests/test_supersede_zero_mutation.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway ~/.cairn so nothing touches the real vault."""
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    return h


def _fresh_main():
    """(Re)import cairn.__main__ under the patched HOME."""
    import importlib
    import cairn.vault as vault_mod
    importlib.reload(vault_mod)
    import cairn.__main__ as main_mod
    importlib.reload(main_mod)
    return main_mod


def _vault(main_mod):
    from cairn.vault import Vault
    return Vault()


def _rows(v) -> int:
    return v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def _seed(v, text="the original claim") -> str:
    from cairn.vault import MicroNode
    node = v.write(MicroNode(session="s", kind="warning", query=text,
                             output_preview=text, model="test",
                             tags=["seed"]))
    return node.id


# ── failure paths: zero mutation ─────────────────────────────────────────────

def test_nonexistent_target_errors_with_zero_mutation(home, capsys):
    m = _fresh_main()
    v = _vault(m)
    _seed(v)
    before = _rows(v)
    with pytest.raises(SystemExit) as e:
        m.cmd_note(["--supersedes=ffffffffffff", "successor text"])
    assert e.value.code == 1
    assert "not found" in capsys.readouterr().err
    assert _rows(v) == before                      # zero new rows
    assert not (home / ".cairn" / "last_node.txt").exists()


def test_already_void_target_errors_with_zero_mutation(home, capsys):
    m = _fresh_main()
    v = _vault(m)
    vid = _seed(v)
    v.void(vid)
    before = _rows(v)
    with pytest.raises(SystemExit) as e:
        m.cmd_note([f"--supersedes={vid}", "second successor attempt"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "already retired" in err
    assert "successor" not in err          # no unsupported successor claim
    assert _rows(v) == before
    row = v.conn.execute("SELECT status FROM nodes WHERE id=?",
                         (vid,)).fetchone()
    assert row["status"] == "void"                 # unchanged


# ── success path: exactly one successor + lineage + void ─────────────────────

def test_active_target_single_successor_void_and_lineage(home, capsys):
    m = _fresh_main()
    v = _vault(m)
    vid = _seed(v)
    before = _rows(v)
    m.cmd_note([f"--supersedes={vid}", "the corrected claim"])
    out = capsys.readouterr().out
    assert "retired (void)" in out
    assert _rows(v) == before + 1                  # exactly one successor
    assert v.conn.execute("SELECT status FROM nodes WHERE id=?",
                          (vid,)).fetchone()["status"] == "void"
    succ = v.conn.execute(
        "SELECT id, tags FROM nodes WHERE tags LIKE ?",
        (f'%supersedes:{vid}%',)).fetchall()
    assert len(succ) == 1                          # exact lineage, once
    assert (home / ".cairn" / "last_node.txt").read_text() == succ[0]["id"]


def test_plain_note_without_supersedes_is_unchanged_behavior(home, capsys):
    m = _fresh_main()
    v = _vault(m)
    before = _rows(v)
    m.cmd_note(["--kind=insight", "an ordinary note"])
    assert _rows(v) == before + 1
    assert "wrote insight" in capsys.readouterr().out


# ── atomicity: no partial mutation when a half fails ─────────────────────────

class _ZeroRowConn:
    """Proxy forcing the guarded UPDATE to match zero rows by voiding the
    target through the SAME connection first — i.e. inside the command's own
    open transaction. This is NOT an external-concurrency simulation; it
    exercises the ATOMIC ROLLBACK path: rowcount guard fires, rollback reverts
    both the in-transaction void and the successor."""

    def __init__(self, real, victim_id):
        self._real = real
        self._vid = victim_id

    def execute(self, sql, *a, **k):
        if "UPDATE nodes SET status='void'" in sql:
            self._real.execute(
                "UPDATE nodes SET status='void' WHERE id=?", (self._vid,))
            return self._real.execute(sql, *a, **k)
        return self._real.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_guarded_update_zero_rows_atomic_rollback(home, capsys, monkeypatch):
    """Atomic-rollback mechanics: when the guarded UPDATE matches zero rows,
    everything in the transaction (successor AND the same-connection void)
    rolls back — the vault returns to its pre-command state."""
    m = _fresh_main()
    v_probe = _vault(m)
    vid = _seed(v_probe, "guarded claim")
    before = _rows(v_probe)

    from cairn.vault import Vault as RealVault

    class ZeroRowVault(RealVault):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.conn = _ZeroRowConn(self.conn, vid)

    monkeypatch.setattr(m, "Vault", ZeroRowVault)
    with pytest.raises(SystemExit) as e:
        m.cmd_note([f"--supersedes={vid}", "successor that must not land"])
    assert e.value.code == 1
    assert "changed state during write" in capsys.readouterr().err

    # fresh un-proxied connection: full rollback, no partial mutation —
    # target back to ACTIVE (its void was in-transaction), no successor.
    v_check = _vault(m)
    assert _rows(v_check) == before
    assert v_check.conn.execute(
        "SELECT status FROM nodes WHERE id=?", (vid,)).fetchone()["status"] \
        == "active"
    assert v_check.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE ?",
        (f'%supersedes:{vid}%',)).fetchone()[0] == 0


def test_true_two_connection_race_fails_closed(home, capsys, monkeypatch):
    """FAITHFUL external-concurrency test: after initial validation passes but
    BEFORE the candidate write begins, a SEPARATE sqlite connection voids and
    COMMITS the target. The command must fail closed; the external void (a
    committed foreign transaction) must survive; nothing else changes."""
    m = _fresh_main()
    v_probe = _vault(m)
    vid = _seed(v_probe, "externally raced claim")
    before_nodes = _rows(v_probe)
    before_sessions = v_probe.conn.execute(
        "SELECT COUNT(*) FROM sessions").fetchone()[0]
    db_path = v_probe.db_path

    state = home / ".cairn" / "last_node.txt"
    state.write_text("sentinel-unchanged")

    import sqlite3 as _sq
    from cairn.vault import Vault as RealVault

    class ExternallyRacedVault(RealVault):
        def write(self, node, commit=True):
            if any(str(t).startswith("supersedes:")
                   for t in (node.tags or [])):
                # the race: a genuinely separate connection voids + commits
                # between validation and the write beginning.
                ext = _sq.connect(str(db_path))
                try:
                    ext.execute(
                        "UPDATE nodes SET status='void' WHERE id=?", (vid,))
                    ext.commit()
                finally:
                    ext.close()
            return super().write(node, commit=commit)

    monkeypatch.setattr(m, "Vault", ExternallyRacedVault)
    with pytest.raises(SystemExit) as e:
        m.cmd_note([f"--supersedes={vid}", "successor that must not land"])
    assert e.value.code == 1
    assert "changed state during write" in capsys.readouterr().err

    v_check = _vault(m)
    # the EXTERNAL void is a committed foreign transaction — it must remain
    assert v_check.conn.execute(
        "SELECT status FROM nodes WHERE id=?", (vid,)).fetchone()["status"] \
        == "void"
    # no successor, node count unchanged, session count unchanged
    assert v_check.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE ?",
        (f'%supersedes:{vid}%',)).fetchone()[0] == 0
    assert _rows(v_check) == before_nodes
    assert v_check.conn.execute(
        "SELECT COUNT(*) FROM sessions").fetchone()[0] == before_sessions
    # last_node.txt untouched by the failed command
    assert state.read_text() == "sentinel-unchanged"


def test_write_failure_leaves_target_active(home, monkeypatch):
    """If the successor write itself explodes, the target must stay active
    (nothing half-done)."""
    m = _fresh_main()
    v = _vault(m)
    vid = _seed(v, "must stay active")
    before = _rows(v)

    from cairn.vault import Vault as RealVault

    class ExplodingVault(RealVault):
        def write(self, node, commit=True):
            if any(str(t).startswith("supersedes:") for t in (node.tags or [])):
                raise RuntimeError("simulated write failure")
            return super().write(node, commit=commit)

    monkeypatch.setattr(m, "Vault", ExplodingVault)
    with pytest.raises(RuntimeError):
        m.cmd_note([f"--supersedes={vid}", "doomed successor"])

    v_check = _vault(m)
    assert _rows(v_check) == before
    assert v_check.conn.execute(
        "SELECT status FROM nodes WHERE id=?",
        (vid,)).fetchone()["status"] == "active"
