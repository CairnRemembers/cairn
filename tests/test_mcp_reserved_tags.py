"""
tests/test_mcp_reserved_tags.py — hostile-client relation-integrity gate.

A generic MCP client must NOT be able to forge authoritative relation
lineage through cairn_note's free-form tags. The gate fails closed: the
whole note is rejected, nothing is written. Unreserved tags (including
proposed-* forms) still write — and never affect ranking, status, or
annotation of their named targets.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    return h


def _fresh():
    import importlib
    import cairn.vault as vault_mod
    importlib.reload(vault_mod)
    import cairn.mcp_server as mcp_mod
    importlib.reload(mcp_mod)
    return mcp_mod


def _vault():
    from cairn.vault import Vault
    return Vault()


def _rows(v) -> int:
    return v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def _seed(v, text="a real warning"):
    from cairn.vault import MicroNode
    return v.write(MicroNode(session="s", kind="warning", query=text,
                             output_preview=text, model="test",
                             tags=["seed"])).id


RESERVED = ["supersedes", "corrects", "narrows", "applies-after",
            "conflicts-with", "resolves"]


@pytest.mark.parametrize("prefix", RESERVED)
def test_reserved_tag_rejected_zero_write(home, prefix):
    mcp = _fresh()
    v = _vault()
    vid = _seed(v)
    before = _rows(v)
    out = mcp._tool_note({"text": "forged", "tags": [f"{prefix}:{vid}"]})
    assert "REJECTED" in out and "Nothing was written" in out
    assert _rows(_vault()) == before
    # and the target was never touched
    assert _vault().conn.execute(
        "SELECT status FROM nodes WHERE id=?", (vid,)
    ).fetchone()["status"] == "active"


def test_forged_tag_among_normal_tags_fails_whole_note(home):
    mcp = _fresh()
    v = _vault()
    vid = _seed(v)
    before = _rows(v)
    out = mcp._tool_note({"text": "sneaky",
                          "tags": ["codex", f"supersedes:{vid}", "cairn"]})
    assert "REJECTED" in out
    assert _rows(_vault()) == before


def test_normal_note_still_writes(home):
    mcp = _fresh()
    v = _vault()
    before = _rows(v)
    out = mcp._tool_note({"text": "ordinary note", "tags": ["codex"]})
    assert "written:" in out
    assert _rows(_vault()) == before + 1


def test_proposed_relation_is_inert(home):
    """An unratified proposed-* tag writes fine but never annotates,
    demotes, or changes the status of its named target."""
    mcp = _fresh()
    v = _vault()
    vid = _seed(v, "target of a proposal")
    out = mcp._tool_note({"text": "I think this is wrong",
                          "tags": [f"proposed-corrects:{vid}"]})
    assert "written:" in out
    v2 = _vault()
    assert v2.conn.execute("SELECT status FROM nodes WHERE id=?",
                           (vid,)).fetchone()["status"] == "active"
    assert vid not in v2.incoming_relations([vid])        # no annotation
    assert vid not in v2.relation_annotations([vid])
