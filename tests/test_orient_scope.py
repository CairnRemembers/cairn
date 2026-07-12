"""
tests/test_orient_scope.py — orient's page_one is computed LIVE and scoped to
the caller's galaxy: a VAULT-totals header shows whole-vault scale, per-project
14-day counts scope to one account, a fresh vault renders clean, and the live
count beats a stale cached PAGE_ONE.md. (Fixes the global "307" orient wart —
a Codex session was shown every account's activity summed as if it were its own.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from cairn import book
from cairn.vault import Vault


def _seed(v, account, project, n, when="2026-07-08T12:00:00+00:00"):
    """n active nodes tagged <project>, under a session owned by <account>."""
    sess = f"s-{account}-{project}"
    v.conn.execute(
        "INSERT OR IGNORE INTO sessions (id, started_at, account, account_locked) "
        "VALUES (?,?,?,1)", (sess, when, account))
    for i in range(n):
        v.conn.execute(
            "INSERT INTO nodes (id, session, kind, timestamp, tags) VALUES (?,?,?,?,?)",
            (f"{account}-{project}-{i}", sess, "decision", when, json.dumps([project])))
    v.conn.commit()


def _projects_file(tmp_path, mapping):
    d = tmp_path / ".cairn"
    d.mkdir(parents=True, exist_ok=True)
    (d / "projects.json").write_text(json.dumps(mapping), encoding="utf-8")


def test_fresh_vault_renders_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    v = Vault(db_path=tmp_path / "c.db")
    head = book.page_one(v)                       # must not raise on an empty vault
    assert "== CAIRN - PAGE ONE ==" in head
    assert "VAULT: 0 memories" in head            # no owner data, honest zero


def test_project_counts_scope_to_account(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _projects_file(tmp_path, {"widgets": ["Widgets", "a demo project"]})
    v = Vault(db_path=tmp_path / "c.db")
    _seed(v, "Acme", "widgets", 5)   # stored canonical (Title-case), as the write path does
    _seed(v, "Corp", "widgets", 2)
    # NB: query with the LOWERCASE slug the resolver returns — must still match
    glob = book.page_one(v)                        # None -> global sum
    acme = book.page_one(v, account="acme")
    corp = book.page_one(v, account="corp")
    assert "(7 nodes/14d)" in glob                 # 5 + 2 across both galaxies
    assert "ACTIVE:" in glob and "this galaxy" not in glob
    assert "(5 nodes/14d)" in acme and "ACTIVE (this galaxy):" in acme
    assert "(2 nodes/14d)" in corp
    assert "VAULT: 7 memories" in acme             # header stays whole-vault when scoped


def test_live_render_beats_stale_page_one(tmp_path, monkeypatch):
    # the MCP orient path (Codex's) must show the LIVE count, never the stale file
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = tmp_path / ".cairn"
    d.mkdir(parents=True, exist_ok=True)
    (d / "PAGE_ONE.md").write_text(
        "VAULT: 999 memories · 999 sessions\nACTIVE:\n", encoding="utf-8")
    _projects_file(tmp_path, {"widgets": ["Widgets", "x"]})
    v = Vault(db_path=tmp_path / "c.db")
    _seed(v, "Acme", "widgets", 3)   # canonical Title-case storage
    from cairn import mcp_server
    monkeypatch.setattr(mcp_server, "_vault", lambda: v)
    monkeypatch.setenv("CAIRN_ACCOUNT", "acme")
    out = mcp_server._tool_orient({})
    assert "VAULT: 3 memories" in out              # live wins
    assert "999" not in out                        # stale cached file never leaks in
