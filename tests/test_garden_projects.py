"""
tests/test_garden_projects.py — Garden IA P1: the Projects/Promote/Flagged pass.

Locks in the 2026-07-02 IA redesign Phase 1 so it can't quietly regress:

  1. emerging denylist   — machine-tag strata (kw:/entity:/prov:/by:/stance:/
                           account:/turn:/member:/due:) + literal plumbing
                           (claim/import/conversation/…) are NEVER offered as
                           emerging project candidates, even above the >=6 node
                           threshold; a genuine >=6 topic tag still survives.
  2. family normalization — spelling variants (acme / Acmes / acme's)
                           collapse to ONE emerging family, displayed under the
                           most-frequent original spelling, counted over the union.
  3. promote-to-project  — POST /api/garden/promote writes ~/.cairn/projects.json
                           (create-if-missing), REFUSES an existing key (409),
                           writes a 3-element [name, blurb, [aliases]] value, and
                           hot-reloads the module PROJECTS global (no restart).
  4. loader compat       — both garden._load_projects and book._projects tolerate
                           2- AND 3-element values (v[0]/v[1] work either way);
                           page_one/book_data don't crash on 3-element values.
  5. aliases in queries  — a declared project matches its primary tag OR any alias
                           tag (garden /project/{tag} + book_data chapters).
  6. flagged view        — /api/garden/flagged returns active flagged=1 nodes,
                           newest first, hidden_ids respected.
  7. 'not a project'     — POST /api/garden/dismiss-project hides an emerging
                           family via ~/.cairn/dismissed.json (config, NOT the
                           vault: no node archived); the card re-surfaces only
                           on genuinely new evidence; /undismiss-project (and
                           the toast undo) restores it; other/approved
                           projects are untouched.

Run: python -m pytest tests/test_garden_projects.py -q
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


class _Req:
    """Minimal request stand-in: the promote handler only touches
    request.client.host (rate limiter) and is same-origin by construction."""
    client = type("C", (), {"host": "127.0.0.1"})()
    headers: dict = {}


def _run(coro):
    """Drive an async endpoint without pytest-asyncio (stdlib-only law)."""
    return asyncio.run(coro)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway ~/.cairn so projects.json / promote never touch the real one."""
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _reload_garden(home: Path):
    """Reload garden so its module-global PROJECTS re-reads the temp home's
    projects.json, and return the fresh module."""
    import cairn.garden as garden
    importlib.reload(garden)
    return garden


def _app_handlers(garden, vault):
    from fastapi import FastAPI
    app = FastAPI()
    garden.register_garden(app, vault, lambda: "s")
    return {getattr(r, "path", ""): r.endpoint for r in app.routes}


def _n(vault, tag, kind="insight", n=1):
    # Seeds spread across 2 sessions and 2 days: genuine families must satisfy
    # the structural hygiene rule (>=2 sessions AND >=2 days — one session's
    # work-exhaust never qualifies as a project family).
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc)
    for i in range(n):
        vault.write(MicroNode(session=f"s{i % 2}", kind=kind,
                              query=f"{tag} node {i}", model="test", tags=[tag],
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))


def test_single_session_exhaust_never_a_family(home, vault):
    """The hygiene rule itself: 10 nodes, one session, one night — no family."""
    garden = _reload_garden(home)
    for i in range(10):
        vault.write(MicroNode(session="one-build-night", kind="decision",
                              query=f"work note {i}", model="test",
                              tags=["megaexhaust"]))
    handlers = _app_handlers(garden, vault)
    emerging = {p["tag"] for p in handlers["/api/garden/projects"]()["projects"]
                if p.get("emerging")}
    assert "megaexhaust" not in emerging


# ── 1. emerging denylist ─────────────────────────────────────────────────────

class TestEmergingDenylist:
    def test_noise_strata_excluded_genuine_survives(self, home, vault):
        garden = _reload_garden(home)
        # noise strata — each WELL above the >=6 threshold; none may appear.
        for noisy in ("kw:silver", "entity:Acme", "prov:distilled",
                      "by:sonnet", "stance:supports", "account:main",
                      "turn:42", "member:abc123", "due:2026-07-01",
                      "claim", "import", "conversation", "user", "agent",
                      "mcp", "codex", "claude", "human", "distilled",
                      "consolidated", "test", "codex-test"):
            _n(vault, noisy, n=8)
        # a GENUINE emerging topic with real mass — must survive.
        _n(vault, "widgets", n=7)

        handlers = _app_handlers(garden, vault)
        projects = handlers["/api/garden/projects"]()["projects"]
        emerging = {p["tag"] for p in projects if p.get("emerging")}
        assert "widgets" in emerging, "a genuine >=6 tag must survive as emerging"
        # not one noise stratum leaked through
        for noisy in ("kw:silver", "entity:Acme", "prov:distilled",
                      "by:sonnet", "stance:supports", "claim", "import",
                      "conversation", "distilled", "test"):
            assert noisy not in {p["tag"] for p in projects}, \
                f"noise stratum {noisy!r} leaked into projects"

    def test_below_threshold_excluded(self, home, vault):
        garden = _reload_garden(home)
        _n(vault, "smallidea", n=5)   # 5 < 6 → not emerging
        handlers = _app_handlers(garden, vault)
        projects = handlers["/api/garden/projects"]()["projects"]
        assert "smallidea" not in {p["tag"] for p in projects}


# ── 2. family normalization ──────────────────────────────────────────────────

class TestFamilyNormalization:
    def test_variants_merge_to_one_family(self, home, vault):
        garden = _reload_garden(home)
        # 4 + 3 = 7 nodes across two spellings → one family over threshold.
        _n(vault, "acme", n=4)
        _n(vault, "Acmes", n=3)   # possessive/plural + case variant
        handlers = _app_handlers(garden, vault)
        projects = handlers["/api/garden/projects"]()["projects"]
        fams = [p for p in projects if p.get("emerging")]
        acmes = [p for p in fams if garden._family_key(p["tag"]) == "acme"]
        assert len(acmes) == 1, f"variants must be ONE family, got {len(acmes)}"
        card = acmes[0]
        assert card["total"] == 7, f"family counts the union, got {card['total']}"
        # display spelling = the most-frequent original (acme, 4 > 3)
        assert card["tag"] == "acme"
        assert "Acmes" in card.get("aliases", [])

    def test_family_key(self, home):
        garden = _reload_garden(home)
        assert garden._family_key("Acme's") == "acme"
        assert garden._family_key("acmes") == "acme"
        assert garden._family_key("ACME") == "acme"
        # short plural-looking words aren't over-stripped
        assert garden._family_key("cars") == "car"


# ── 3. promote-to-project ────────────────────────────────────────────────────

class TestPromote:
    def test_promote_creates_and_hot_reloads(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        promote = handlers["/api/garden/promote"]

        res = _run(promote(
            {"tag": "acme", "name": "Acme & Co.",
             "blurb": "hallmark ID + eye-training game",
             "aliases": ["acmes", "kw:silver", "kw:hallmark"]}, _Req()))
        assert res["promoted"] is True
        # file written with a 3-element value
        pf = home / ".cairn" / "projects.json"
        data = json.loads(pf.read_text(encoding="utf-8"))
        assert data["acme"][0] == "Acme & Co."
        assert data["acme"][2] == ["acmes", "kw:silver", "kw:hallmark"]
        # module PROJECTS hot-reloaded (no restart) — readers see it now
        assert "acme" in garden.PROJECTS
        assert garden._project_match_tags("acme") == \
            ["acme", "acmes", "kw:silver", "kw:hallmark"]

    def test_promote_refuses_existing_key(self, home, vault):
        garden = _reload_garden(home)
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({"cairn": ["Cairn", "the system"]}),
                      encoding="utf-8")
        handlers = _app_handlers(garden, vault)

        res = _run(handlers["/api/garden/promote"](
            {"tag": "cairn", "name": "Nope", "blurb": "x"}, _Req()))
        # JSONResponse with 409 — never overwrite a declared project
        assert getattr(res, "status_code", None) == 409
        # file unchanged
        data = json.loads(pf.read_text(encoding="utf-8"))
        assert data["cairn"] == ["Cairn", "the system"]

    def test_promote_rejects_bad_tag(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)

        for bad in ('has"quote', "has<angle>", "with/slash", "", "  "):
            res = _run(handlers["/api/garden/promote"](
                {"tag": bad, "name": "x", "blurb": "y"}, _Req()))
            assert getattr(res, "status_code", None) == 400, \
                f"{bad!r} must be rejected"


# ── 4. loader compatibility (2- and 3-element values) ────────────────────────

class TestLoaderCompat:
    def test_both_loaders_tolerate_3_element(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "two":   ["Two", "a 2-element value"],
            "three": ["Three", "a 3-element value", ["alias-a", "alias-b"]],
        }), encoding="utf-8")

        garden = _reload_garden(home)
        import cairn.book as book
        importlib.reload(book)

        gp = garden._load_projects()
        bp = book._projects()
        # v[0]/v[1] work for either length
        assert gp["two"][0] == "Two" and gp["two"][1] == "a 2-element value"
        assert gp["three"][0] == "Three" and gp["three"][1] == "a 3-element value"
        assert bp["three"][0] == "Three" and bp["three"][1] == "a 3-element value"
        # aliases exposed
        assert garden._project_aliases("three") == ["alias-a", "alias-b"]
        assert garden._project_aliases("two") == []

    def test_page_one_and_book_dont_crash_on_3_element(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "acme": ["Acme & Co.", "silver", ["acmes", "kw:silver"]],
        }), encoding="utf-8")
        import cairn.book as book
        importlib.reload(book)
        # neither raises on the 3-element value
        head = book.page_one(vault)
        assert "== CAIRN - PAGE ONE ==" in head
        data = book.book_data(vault)
        assert any(p["tag"] == "acme" for p in data["projects"])


# ── 5. aliases in project queries ────────────────────────────────────────────

class TestAliasQueries:
    def test_project_view_matches_aliases(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "acme": ["Acme & Co.", "silver", ["acmes", "kw:silver"]],
        }), encoding="utf-8")
        garden = _reload_garden(home)
        # nodes under the primary tag AND under two different aliases
        _n(vault, "acme", kind="decision", n=1)
        _n(vault, "acmes", kind="open_item", n=1)
        _n(vault, "kw:silver", kind="insight", n=1)

        handlers = _app_handlers(garden, vault)
        d = handlers["/api/garden/project/{tag}"]("acme")
        # decisions (primary) + attention (open_item alias) + knowledge (kw:silver)
        assert len(d["decisions"]) == 1
        assert len(d["attention"]) == 1      # the acmes open_item
        assert len(d["knowledge"]) == 1      # the kw:silver insight

    def test_book_chapters_match_aliases(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "acme": ["Acme & Co.", "silver", ["kw:silver"]],
        }), encoding="utf-8")
        import cairn.book as book
        importlib.reload(book)
        _n(vault, "acme", kind="decision", n=1)
        _n(vault, "kw:silver", kind="decision", n=1)
        data = book.book_data(vault)
        acme = next(p for p in data["projects"] if p["tag"] == "acme")
        # both decision nodes (primary + alias) counted in the Decisions chapter
        dec = next(c for c in acme["chapters"] if c["cid"] == "decision")
        assert dec["count"] == 2, f"aliases must fold in, got {dec['count']}"
        assert acme["total"] == 2


# ── 6. flagged view ──────────────────────────────────────────────────────────

class TestFlaggedView:
    def test_flagged_newest_first_hidden_respected(self, home, vault):
        garden = _reload_garden(home)
        a = vault.write(MicroNode(session="s", kind="insight", query="flag me A",
                                  model="test"))
        b = vault.write(MicroNode(session="s", kind="insight", query="flag me B",
                                  model="test"))
        unflagged = vault.write(MicroNode(session="s", kind="insight",
                                          query="not flagged", model="test"))
        hidden = vault.write(MicroNode(session="s", kind="insight",
                                       query="flagged but archived", model="test"))
        for nid in (a.id, b.id, hidden.id):
            vault.flag(nid)
        vault.archive(hidden.id)   # archived → out of the flagged view

        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/flagged"]()
        ids = [n["id"] for n in res["nodes"]]
        assert a.id in ids and b.id in ids
        assert unflagged.id not in ids, "unflagged node must not appear"
        assert hidden.id not in ids, "archived node must be hidden"
        # newest first (b written after a)
        assert ids.index(b.id) < ids.index(a.id)
        assert res["count"] == len(ids)


# ── 7. 'not a project' — dismiss / reappear / restore ────────────────────────

class TestNotAProject:
    def _emerging(self, handlers):
        res = handlers["/api/garden/projects"]()
        return res, {p["tag"] for p in res["projects"] if p.get("emerging")}

    def test_dismiss_hides_family_and_persists(self, home, vault):
        garden = _reload_garden(home)
        _n(vault, "widgets", n=7)
        handlers = _app_handlers(garden, vault)

        _, emerging = self._emerging(handlers)
        assert "widgets" in emerging, "a genuine family must start visible"

        res = _run(handlers["/api/garden/dismiss-project"](
            {"tag": "widgets"}, _Req()))
        assert res["dismissed"] is True and res["key"] == "widget"

        # config file written — NOT the vault; snapshot captured
        df = home / ".cairn" / "dismissed.json"
        data = json.loads(df.read_text(encoding="utf-8"))
        assert "widget" in data
        assert data["widget"]["count"] == 7
        assert data["widget"]["name"] == "widgets"

        # gone from emerging, and a re-render (refresh) keeps it gone
        for _ in range(2):
            payload, emerging = self._emerging(handlers)
            assert "widgets" not in emerging
        # it surfaces in the 'hidden' list instead (for the show-hidden UI)
        assert any(x["key"] == "widget" for x in payload["dismissed"])

    def test_no_nodes_archived_by_dismiss(self, home, vault):
        garden = _reload_garden(home)
        _n(vault, "widgets", n=7)
        handlers = _app_handlers(garden, vault)
        _run(handlers["/api/garden/dismiss-project"]({"tag": "widgets"}, _Req()))
        # append-only law: every memory is still active — nothing archived/voided
        statuses = [r["status"] for r in
                    vault.conn.execute("SELECT status FROM nodes").fetchall()]
        assert statuses and all(s == "active" for s in statuses)

    def test_reappears_on_new_evidence(self, home, vault):
        garden = _reload_garden(home)
        _n(vault, "widgets", n=7)
        handlers = _app_handlers(garden, vault)
        _run(handlers["/api/garden/dismiss-project"]({"tag": "widgets"}, _Req()))
        _, emerging = self._emerging(handlers)
        assert "widgets" not in emerging

        # genuinely new work lands on the family -> it earns its way back
        _n(vault, "widgets", n=3)   # count grows past the dismissal snapshot
        payload, emerging = self._emerging(handlers)
        assert "widgets" in emerging, "new evidence must re-surface the card"
        # ...and it is no longer double-listed under 'hidden'
        assert not any(x["key"] == "widget" for x in payload["dismissed"])

    def test_undismiss_restores(self, home, vault):
        garden = _reload_garden(home)
        _n(vault, "widgets", n=7)
        handlers = _app_handlers(garden, vault)
        _run(handlers["/api/garden/dismiss-project"]({"tag": "widgets"}, _Req()))
        _, emerging = self._emerging(handlers)
        assert "widgets" not in emerging

        res = _run(handlers["/api/garden/undismiss-project"](
            {"key": "widget"}, _Req()))
        assert res["restored"] is True
        _, emerging = self._emerging(handlers)
        assert "widgets" in emerging
        # store emptied
        df = home / ".cairn" / "dismissed.json"
        assert json.loads(df.read_text(encoding="utf-8")) == {}

    def test_dismiss_one_leaves_others_and_approved(self, home, vault):
        # a declared project + two emerging families
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({"cairn": ["Cairn", "the system"]}),
                      encoding="utf-8")
        garden = _reload_garden(home)
        _n(vault, "cairn", kind="decision", n=3)   # feeds the declared project
        _n(vault, "widgets", n=7)
        _n(vault, "gadgets", n=7)
        handlers = _app_handlers(garden, vault)

        _run(handlers["/api/garden/dismiss-project"]({"tag": "widgets"}, _Req()))
        payload, emerging = self._emerging(handlers)
        assert "widgets" not in emerging          # the dismissed one is gone
        assert "gadgets" in emerging              # a fresh candidate still shows
        # approved project unaffected (present, and not treated as emerging)
        cairn = next((p for p in payload["projects"]
                      if p["tag"] == "cairn"), None)
        assert cairn is not None and not cairn.get("emerging")

    def test_bad_input_rejected(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        for bad in ('has"quote', "with/slash", "", "  "):
            res = _run(handlers["/api/garden/dismiss-project"](
                {"tag": bad}, _Req()))
            assert getattr(res, "status_code", None) == 400, \
                f"{bad!r} must be rejected"
        # undismiss a key that was never dismissed -> 404
        res = _run(handlers["/api/garden/undismiss-project"](
            {"key": "nope"}, _Req()))
        assert getattr(res, "status_code", None) == 404


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
