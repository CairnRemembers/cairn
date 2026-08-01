"""
tests/test_void_audit.py — atomic void accountability (candidate).

Every successful void writes exactly ONE immutable audit node in the same
transaction. If the audit write fails, the void must not land. No-op voids
(missing / already-void targets) write no audit. The audit records path and
provenance as DECLARATION, not authentication.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    return h


def _vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _seed(v, text="a claim"):
    return v.write(MicroNode(session="s", kind="warning", query=text,
                             output_preview=text, model="t", tags=["x"])).id


def _audits(v, target):
    return v.conn.execute(
        "SELECT * FROM nodes WHERE kind='void_audit' AND tags LIKE ?",
        (f'%"target:{target}"%',)).fetchall()


def test_successful_void_writes_exactly_one_audit(home, tmp_path):
    v = _vault(tmp_path)
    nid = _seed(v)
    before = v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert v.void(nid, source="cli") is True
    assert v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before + 1
    audits = _audits(v, nid)
    assert len(audits) == 1
    assert "declaration, not authentication" in audits[0]["query"]
    assert v.conn.execute("SELECT status FROM nodes WHERE id=?",
                          (nid,)).fetchone()["status"] == "void"


def test_noop_void_writes_no_audit(home, tmp_path):
    v = _vault(tmp_path)
    nid = _seed(v)
    v.void(nid)
    n_after_first = v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert v.void(nid) is False                      # already void
    assert v.void("ffffffffffff") is False           # missing
    assert v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == n_after_first
    assert len(_audits(v, nid)) == 1                 # still exactly one


def test_audit_failure_rolls_back_the_void(home, tmp_path):
    v = _vault(tmp_path)
    nid = _seed(v)
    real_write = v.write

    def exploding_write(node, commit=True):
        if getattr(node, "kind", "") == "void_audit":
            raise RuntimeError("audit write refused")
        return real_write(node, commit=commit)

    v.write = exploding_write
    with pytest.raises(RuntimeError):
        v.void(nid, source="cli")
    v.write = real_write
    # the void did NOT land — fresh connection ground truth
    v2 = Vault(db_path=tmp_path / "test.db")
    assert v2.conn.execute("SELECT status FROM nodes WHERE id=?",
                           (nid,)).fetchone()["status"] == "active"
    assert len(_audits(v2, nid)) == 0


def test_audit_node_is_immutable(home, tmp_path):
    import sqlite3
    v = _vault(tmp_path)
    nid = _seed(v)
    v.void(nid, source="cli")
    aid = _audits(v, nid)[0]["id"]
    with pytest.raises(sqlite3.DatabaseError):
        v.conn.execute("UPDATE nodes SET query='scrubbed' WHERE id=?", (aid,))


def test_garden_void_path_records_provenance(home, tmp_path):
    import importlib
    import cairn.garden as garden
    importlib.reload(garden)
    from fastapi import FastAPI
    import asyncio
    v = _vault(tmp_path)
    nid = _seed(v)
    app = FastAPI()
    garden.register_garden(app, v, lambda: "s")
    handlers = {getattr(r, "path", ""): r.endpoint for r in app.routes}

    class _Req:
        headers = {"origin": "http://localhost:7331"}
        client = type("C", (), {"host": "127.0.0.1"})()

    out = asyncio.run(handlers["/api/garden/node/{node_id}/void"](nid, _Req()))
    assert out["status"] == "void"
    a = _audits(v, nid)
    assert len(a) == 1
    assert "garden:/void" in a[0]["query"]
    assert "origin=http://localhost:7331" in a[0]["query"]
