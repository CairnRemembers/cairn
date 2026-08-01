"""
tests/test_relation_annotations.py — annotate-only rendering (candidate).

Contract: relation annotations are DECORATION on frozen output — when no
relation exists, every surface's output is byte-identical to its
pre-relation output; when one exists, the only difference is the inserted
annotation. Targets are never demoted, hidden, or re-ranked.
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
    import cairn.__main__ as main_mod
    importlib.reload(main_mod)
    import cairn.mcp_server as mcp_mod
    importlib.reload(mcp_mod)
    import cairn.retrieve as ret_mod
    importlib.reload(ret_mod)
    import cairn.book as book_mod
    importlib.reload(book_mod)
    return main_mod, mcp_mod, ret_mod, book_mod


def _vault():
    from cairn.vault import Vault
    return Vault()


def _seed_warning(v, text="warning about the throttle body sticking"):
    from cairn.vault import MicroNode
    return v.write(MicroNode(session="s", kind="warning", query=text,
                             output_preview=text, model="test",
                             tags=["seed"])).id


def _relate(main, vid):
    # text deliberately shares no keywords with the test queries, so the
    # relation note itself never joins keyword-fallback result sets
    main.cmd_note([f"--corrects={vid}", "the earlier claim was wrong"])


# ── search (keyword-fallback path in an embedder-less test vault) ────────────

def test_search_golden_then_annotated(home):
    main, mcp, _, _ = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    pre = mcp._tool_search({"query": "throttle body"})
    assert "⚠ corrected" not in pre                      # golden: no artifacts
    _relate(main, vid)
    post = mcp._tool_search({"query": "throttle body"})
    ann = [l for l in post.splitlines() if "corrected by [" in l]
    assert len(ann) == 1
    # removing the annotation line restores the pre output byte-for-byte
    assert "\n".join(l for l in post.splitlines()
                     if "corrected by [" not in l) == pre


# ── recent ───────────────────────────────────────────────────────────────────

def test_recent_annotates_target_row(home):
    main, mcp, _, _ = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    pre = mcp._tool_recent({})
    assert "corrected by [" not in pre
    _relate(main, vid)                     # relation note kind='note' — not in
    post = mcp._tool_recent({})            # recent's allowlist, rows unchanged
    assert "\n".join(l for l in post.splitlines()
                     if "corrected by [" not in l) == pre
    assert any("corrected by [" in l for l in post.splitlines())


# ── logs ─────────────────────────────────────────────────────────────────────

def test_logs_annotates_filtered_row(home):
    main, mcp, _, _ = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    pre = mcp._tool_logs({"kind": "warning"})
    _relate(main, vid)
    post = mcp._tool_logs({"kind": "warning"})
    assert "\n".join(l for l in post.splitlines()
                     if "corrected by [" not in l) == pre
    assert any("corrected by [" in l for l in post.splitlines())


# ── read: both directions ────────────────────────────────────────────────────

def test_read_shows_incoming_and_outgoing(home):
    main, mcp, _, _ = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    pre = mcp._tool_read({"ids": [vid]})
    assert "corrected by [" not in pre
    _relate(main, vid)
    v2 = _vault()
    src = v2.conn.execute("SELECT id FROM nodes WHERE tags LIKE ?",
                          (f'%"corrects:{vid}"%',)).fetchone()["id"]
    tgt_view = mcp._tool_read({"ids": [vid]})
    assert f"corrected by [{src}]" in tgt_view          # incoming on target
    src_view = mcp._tool_read({"ids": [src]})
    assert f"→ corrects [{vid}]" in src_view            # outgoing on source
    # target is still active — no void label appeared
    assert "status=void" not in tgt_view


# ── fetch pack ───────────────────────────────────────────────────────────────

def test_fetch_pack_carries_relation(home):
    main, _, ret, _ = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    _relate(main, vid)
    pack = ret.fetch_pack("throttle body", vault=v)
    hit = next(r for r in pack["results"] if r["id"] == vid)
    assert "corrected by [" in hit.get("relation", "")
    rendered = ret.render_pack(pack)
    assert "corrected by [" in rendered


# ── PAGE ONE ─────────────────────────────────────────────────────────────────

def test_page_one_inline_suffix_and_cap(home):
    main, _, _, book = _fresh()
    v = _vault()
    vid = _seed_warning(v)
    pre = book.page_one(v)
    assert "corrected by [" not in pre
    _relate(main, vid)
    post = book.page_one(_vault())
    assert "corrected by [" in post
    lines = post.splitlines()
    assert len(lines) <= 34                              # cap never grows
    assert any(l.startswith("NAVIGATE:") for l in lines)  # nav retained
    assert any(l.startswith("LAWS:") for l in lines)      # laws retained
    assert any(l.startswith("WARNINGS:") for l in lines)  # warnings retained
    # suffix rides the warning's own line — no extra line added
    assert len(lines) == len(pre.splitlines())
