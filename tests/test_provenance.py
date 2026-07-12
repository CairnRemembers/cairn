"""
tests/test_provenance.py — attribution v2 (Spec A / A5): retrieval carries
provenance. fetch/drift hit lines gain a "· <account>/<harness>" tag, and
fetch takes an optional case-insensitive account= filter. Origin is decoration
only: unknown stays silent, never invented. The account/harness resolution
itself is proven in test_account_stamp; here we force the sessions row and test
only the join + render in retrieve.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
from cairn.vault import Vault, MicroNode
from cairn import retrieve


@pytest.fixture
def vault(tmp_path, monkeypatch):
    # isolate HOME so a stray write can't read the real machine's me.json;
    # we force provenance explicitly below regardless.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CAIRN_ACCOUNT", raising=False)
    monkeypatch.delenv("CAIRN_HARNESS", raising=False)
    vaultmod._ACCOUNT_MEMO.clear()
    v = Vault(db_path=tmp_path / "p.db")
    yield v
    vaultmod._ACCOUNT_MEMO.clear()


def _stamp(v, session, account, harness):
    """Force a sessions row's provenance, independent of live resolution."""
    v.conn.execute("UPDATE sessions SET account=?, harness=? WHERE id=?",
                   (account, harness, session))
    v.conn.commit()


def _hit(node, session, body="body text here"):
    return {"id": node.id, "kind": "decision", "gist": node.query,
            "query": node.query, "score": 0.9, "session": session,
            "tags": "[]", "output_preview": body}


def test_fetch_pack_shows_origin(vault, monkeypatch):
    n = vault.write(MicroNode(session="codex-t1", kind="decision",
                              query="pick sqlite", model="test"))
    _stamp(vault, "codex-t1", "Nova", "codex")
    monkeypatch.setattr(Vault, "query_episodic",
                        lambda self, q, k=20: [_hit(n, "codex-t1")])
    pack = retrieve.fetch_pack("anything", vault=vault)
    assert pack["results"][0]["origin"] == "Nova/codex"
    assert "· Nova/codex" in retrieve.render_pack(pack)


def test_origin_absent_is_silent(vault, monkeypatch):
    # no account, no harness on the row -> honest 'unknown': no tag rendered
    n = vault.write(MicroNode(session="mystery", kind="note",
                              query="hi", model="test"))
    _stamp(vault, "mystery", None, None)
    monkeypatch.setattr(Vault, "query_episodic",
                        lambda self, q, k=20: [_hit(n, "mystery")])
    pack = retrieve.fetch_pack("anything", vault=vault)
    assert pack["results"][0]["origin"] == ""
    assert "·" not in retrieve.render_pack(pack)


def test_partial_origin_harness_only(vault, monkeypatch):
    # harness known, account unknown -> tag is just the harness, no leading slash
    n = vault.write(MicroNode(session="codex-x", kind="note",
                              query="q", model="test"))
    _stamp(vault, "codex-x", None, "codex")
    monkeypatch.setattr(Vault, "query_episodic",
                        lambda self, q, k=20: [_hit(n, "codex-x")])
    pack = retrieve.fetch_pack("anything", vault=vault)
    assert pack["results"][0]["origin"] == "codex"
    assert "· codex" in retrieve.render_pack(pack)


def test_account_filter_narrows(vault, monkeypatch):
    a = vault.write(MicroNode(session="codex-a", kind="note",
                              query="from nova", model="test"))
    b = vault.write(MicroNode(session="claude-b", kind="note",
                              query="from atlas", model="test"))
    _stamp(vault, "codex-a", "Nova", "codex")
    _stamp(vault, "claude-b", "Atlas", "claude-code")
    monkeypatch.setattr(Vault, "query_episodic",
                        lambda self, q, k=20: [_hit(a, "codex-a"),
                                               _hit(b, "claude-b")])
    assert retrieve.fetch_pack("x", vault=vault)["count"] == 2      # no filter
    pack = retrieve.fetch_pack("x", vault=vault, account="nova")    # case-insensitive
    assert pack["count"] == 1
    assert pack["results"][0]["origin"] == "Nova/codex"


def test_render_drift_shows_origin():
    # render half in isolation — drift_pack populates origin via the same helper
    pack = {"query": "q", "seeds": [],
            "results": [{"id": "x1", "kind": "insight", "source": "codex-a",
                         "gist": "an adjacent idea", "topic": "", "score": 0.5,
                         "hops": 2, "origin": "Nova/codex"}]}
    assert "· Nova/codex" in retrieve.render_drift(pack)
