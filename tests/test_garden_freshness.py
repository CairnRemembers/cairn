"""
tests/test_garden_freshness.py — the 4 Garden-freshness fixes (2026-07-01 audit).

Each fix has a focused test against a throwaway vault. The Garden "felt outdated"
for four separate reasons; these lock in the corrected behavior so it can't quietly
regress:

  1. fading re-rank      — book.hub_data ranks fading by neglect (importance +
                           FSRS overdue), NOT by raw lifetime shown-count, so an
                           ancient max-shown node stops squatting the top slot.
  2. since-last-visit    — the hub delta carries a SEPARATE capped conversation-
                           turn count so a talk-heavy day no longer reads "0 new".
  3. page_one dormancy   — projects with zero nodes in 14d render as DORMANT, not
                           silently under ACTIVE.
  4. project tags        — capture.resolve_project_tag derives a project tag from
                           cwd at write time: env wins, then map file, then folder
                           name vs projects.json keys, else nothing (never guess).

Run: python -m pytest tests/test_garden_freshness.py -q
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn.vault import Vault, MicroNode


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


# ── Fix 1: fading re-rank ───────────────────────────────────────────────────────

def _add_ledger(v, node_id, shown, cited=0):
    """Give a node `shown` uncited attention-ledger receipts (the 'fading' input)."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(shown):
        v.conn.execute(
            "INSERT INTO attention_ledger (node_id, session, channel, position, "
            "trigger, shown_at, cited) VALUES (?,?,?,?,?,?,?)",
            (node_id, "s", "test", i, "", now, cited))
    v.conn.commit()


class TestFadingRerank:
    def test_neglect_beats_raw_shown_count(self, vault):
        """An ancient max-shown but recently-reinjected node must NOT outrank a
        high-importance node that has genuinely gone stale."""
        from cairn.book import hub_data

        # The old top-squatter: shown a TON, but re-injected just now → not stale.
        squatter = vault.write(MicroNode(
            session="s", kind="insight", query="ancient max-shown node",
            model="test", importance=7))
        _add_ledger(vault, squatter.id, shown=50)
        vault.set_stability(squatter.id, 5.0,
                            last_injected=datetime.now(timezone.utc).isoformat())

        # The genuinely fading node: fewer shows, high importance, injected long
        # ago relative to its stability → high neglect score.
        stale = vault.write(MicroNode(
            session="s", kind="decision", query="valuable but going stale",
            model="test", importance=9))
        _add_ledger(vault, stale.id, shown=3)
        vault.set_stability(stale.id, 1.0, last_injected=_iso_days_ago(60))

        fading = hub_data(vault)["fading"]
        ids = [f["id"] for f in fading]
        assert stale.id in ids, "the genuinely-stale node must surface"
        assert ids.index(stale.id) < ids.index(squatter.id), (
            "stale/important node must outrank the recently-reinjected squatter")

    def test_shape_preserved(self, vault):
        """Size/shape identical: <=5 items, HAVING cnt>=2 gate, `shown` payload."""
        from cairn.book import hub_data

        # 7 eligible nodes (>=2 shows each) — expect exactly 5 back.
        for i in range(7):
            n = vault.write(MicroNode(session="s", kind="insight",
                                      query=f"node {i}", model="test"))
            _add_ledger(vault, n.id, shown=3)
        # 1 node with only a single show — must be excluded by HAVING cnt>=2.
        one = vault.write(MicroNode(session="s", kind="insight",
                                    query="shown once", model="test"))
        _add_ledger(vault, one.id, shown=1)

        fading = hub_data(vault)["fading"]
        assert len(fading) == 5, f"LIMIT 5 must hold, got {len(fading)}"
        assert one.id not in [f["id"] for f in fading], "HAVING cnt>=2 must gate"
        assert all(set(f.keys()) == {"id", "gist", "shown"} for f in fading), (
            "payload keys must stay id/gist/shown for the UI")
        assert all(f["shown"] >= 2 for f in fading)


# ── Fix 2: since-your-last-visit carries a separate turns count ─────────────────

class TestSinceLastVisitTurns:
    def test_turns_counted_separately(self, tmp_path, monkeypatch):
        """A conversation-heavy delta reports turns separately from the
        meaning-kind count — so the day doesn't read '0 new'."""
        # isolate ~/.cairn so garden_visit.json is written to the temp home
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        v = Vault(db_path=tmp_path / "test.db")

        # Seed a baseline marker in the past so everything counts as "since".
        (home / ".cairn").mkdir(parents=True, exist_ok=True)
        old_seen = _iso_days_ago(1)
        (home / ".cairn" / "garden_visit.json").write_text(
            json.dumps({"marker": _iso_days_ago(2), "last_seen": old_seen}),
            encoding="utf-8")

        # 5 conversation turns, 1 meaning-kind capture.
        for i in range(5):
            v.write(MicroNode(session="s", kind="conversation_turn",
                              query=f"turn {i}", speaker="user", model="test"))
        v.write(MicroNode(session="s", kind="decision",
                          query="a real decision", model="test"))

        # Drive the endpoint's since-last-visit logic. Register the routes onto a
        # throwaway app and call the hub handler directly.
        from fastapi import FastAPI
        import cairn.garden as garden
        app = FastAPI()
        garden.register_garden(app, v, lambda: "s")
        # find the hub route handler
        handler = next(r.endpoint for r in app.routes
                       if getattr(r, "path", "") == "/api/garden/hub")
        data = handler()
        slv = data["since_last_visit"]
        assert slv["count"] == 1, f"meaning-kind count stays 1, got {slv['count']}"
        assert slv["turns"] == 5, f"turns counted separately, got {slv['turns']}"

    def test_turns_capped(self, tmp_path, monkeypatch):
        """The turns count is capped so a runaway day keeps the label terse."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        v = Vault(db_path=tmp_path / "test.db")
        (home / ".cairn").mkdir(parents=True, exist_ok=True)
        (home / ".cairn" / "garden_visit.json").write_text(
            json.dumps({"marker": _iso_days_ago(2), "last_seen": _iso_days_ago(1)}),
            encoding="utf-8")

        for i in range(120):
            v.write(MicroNode(session="s", kind="conversation_turn",
                              query=f"turn {i}", speaker="user", model="test"))

        from fastapi import FastAPI
        import cairn.garden as garden
        app = FastAPI()
        garden.register_garden(app, v, lambda: "s")
        handler = next(r.endpoint for r in app.routes
                       if getattr(r, "path", "") == "/api/garden/hub")
        slv = handler()["since_last_visit"]
        assert slv["turns"] == 99, f"turns must cap at 99, got {slv['turns']}"


# ── Fix 3: page_one dormant rendering ───────────────────────────────────────────

class TestPageOneDormant:
    def test_active_vs_dormant(self, vault):
        """Projects with recent nodes list under ACTIVE with a count; projects
        with none collapse to a DORMANT line — not silently 'active'."""
        from cairn import book

        # Two declared projects; only one has a recent node.
        projects = {"live": ("Live One", "has recent work"),
                    "dead": ("Dead One", "no recent work")}
        # patch _projects for a deterministic set
        orig = book._projects
        book._projects = lambda: projects
        try:
            vault.write(MicroNode(session="s", kind="decision",
                                  query="fresh work", model="test",
                                  tags=["live"]))
            head = book.page_one(vault)
        finally:
            book._projects = orig

        assert "Live One" in head
        # live project carries its 14d count under ACTIVE
        assert "Live One - has recent work (1 nodes/14d)" in head
        # dead project appears on the DORMANT line, NOT with a count
        assert "DORMANT:" in head
        dormant_line = next(l for l in head.splitlines() if l.startswith("DORMANT:"))
        assert "Dead One" in dormant_line
        assert "Live One" not in dormant_line

    def test_all_dormant_shows_none_active(self, vault):
        """When nothing is active in 14d, ACTIVE says so rather than lying."""
        from cairn import book
        projects = {"dead": ("Dead One", "nothing recent")}
        orig = book._projects
        book._projects = lambda: projects
        try:
            head = book.page_one(vault)
        finally:
            book._projects = orig
        assert "(none active in 14d)" in head
        assert "DORMANT: Dead One" in head


# ── Fix 4: capture-time project-tag resolution ──────────────────────────────────

def _fresh_home():
    """Point ~/.cairn at a temp dir and reload capture so its Path.home()
    lookups (projects.json / project_map.json) resolve there."""
    tmp = tempfile.mkdtemp(prefix="cairn_proj_")
    os.environ["HOME"] = tmp
    os.environ["USERPROFILE"] = tmp
    import cairn.vault as vault_mod
    importlib.reload(vault_mod)
    import cairn.capture as capture_mod
    importlib.reload(capture_mod)
    return Path(tmp), capture_mod


def _write_projects(home: Path, mapping: dict):
    d = home / ".cairn"
    d.mkdir(parents=True, exist_ok=True)
    (d / "projects.json").write_text(json.dumps(mapping), encoding="utf-8")


class TestResolveProjectTag:
    def teardown_method(self):
        os.environ.pop("CAIRN_PROJECT", None)

    def test_env_wins(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.setenv("CAIRN_PROJECT", "explicit-project")
        # even with a cwd that would match a project key, env takes precedence
        assert capture.resolve_project_tag("/some/cairn") == "explicit-project"

    def test_map_file_folder_component(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        (home / ".cairn" / "project_map.json").write_text(
            json.dumps({"widgets-app": "widgets"}), encoding="utf-8")
        # folder component matches the map key → mapped tag
        assert capture.resolve_project_tag(
            "C:/Users/me/dev/widgets-app") == "widgets"

    def test_map_file_substring(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        (home / ".cairn" / "project_map.json").write_text(
            json.dumps({"kitchen": "cookery"}), encoding="utf-8")
        # substring anywhere in the cwd path → mapped tag
        assert capture.resolve_project_tag(
            "/home/me/kitchen-hub/src") == "cookery"

    def test_folder_name_match(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"],
                               "widgets": ["Widgets", "y"]})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        # cwd folder name matches a projects.json key
        assert capture.resolve_project_tag(
            "C:/Users/me/dev/cairn") == "cairn"

    def test_parent_folder_match(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        # a subdir under the project still resolves via the parent (up to 2 levels)
        assert capture.resolve_project_tag(
            "C:/Users/me/dev/cairn/tests") == "cairn"

    def test_no_match_returns_none(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        # unrelated cwd, no map file → NO tag (never guess)
        assert capture.resolve_project_tag("/tmp/random/place") is None

    def test_never_creates_files(self, monkeypatch):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.delenv("CAIRN_PROJECT", raising=False)
        capture.resolve_project_tag("/tmp/nope")
        # the resolver must READ config only, never write project_map.json
        assert not (home / ".cairn" / "project_map.json").exists()

    def test_write_turn_appends_tag(self, monkeypatch, tmp_path):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.setenv("CAIRN_PROJECT", "cairn")
        from cairn.vault import Vault as V2
        v = V2(db_path=tmp_path / "t.db")
        node = capture.write_turn("a genuinely salient user directive here",
                                  speaker="user", session="s", vault=v)
        tags = json.loads(v.conn.execute(
            "SELECT tags FROM nodes WHERE id=?", (node.id,)).fetchone()["tags"])
        assert tags == ["conversation", "user", "cairn"], tags

    def test_write_turn_no_tag_when_no_match(self, monkeypatch, tmp_path):
        home, capture = _fresh_home()
        _write_projects(home, {"cairn": ["Cairn", "x"]})
        monkeypatch.setenv("CAIRN_PROJECT", "")   # empty → falls through
        monkeypatch.chdir(tmp_path)               # cwd won't match any key
        from cairn.vault import Vault as V2
        v = V2(db_path=tmp_path / "t.db")
        node = capture.write_turn("another salient directive, long enough to keep",
                                  speaker="user", session="s", vault=v)
        tags = json.loads(v.conn.execute(
            "SELECT tags FROM nodes WHERE id=?", (node.id,)).fetchone()["tags"])
        assert tags == ["conversation", "user"], tags


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
