"""
tests/test_garden_file_under.py — file-under on the PROPOSED lane (v4-final).

Target chooses the operation:
  • Skills & Frameworks  → ABSORB (fold into Skills; audit-only Filed history)
  • any other approved project → NEST (leaves Proposed; separable child)
One-home is owner-arbitrated: a filing that would move a member off another
project — or convert an alias already folded into the SELECTED parent (the
v4-final self-parent fix) — STOPS and asks; only confirm_move performs the move.

Mirrors the test_garden_projects.py harness (throwaway ~/.cairn, direct endpoint
calls via _run, rejects surface as JSONResponse.status_code).

Run: python -m pytest tests/test_garden_file_under.py -q
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


class _Req:
    client = type("C", (), {"host": "127.0.0.1"})()
    headers: dict = {}


def _run(coro):
    """Drive an async endpoint without pytest-asyncio (stdlib-only law)."""
    return asyncio.run(coro)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway ~/.cairn so projects.json never touches the real one."""
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _reload_garden(home: Path):
    import cairn.garden as garden
    importlib.reload(garden)
    return garden


def _app_handlers(garden, vault):
    from fastapi import FastAPI
    app = FastAPI()
    garden.register_garden(app, vault, lambda: "s")
    return {getattr(r, "path", ""): r.endpoint for r in app.routes}


def _n(vault, tag, kind="insight", n=1):
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc)
    for i in range(n):
        vault.write(MicroNode(session=f"s{i % 2}", kind=kind,
                              query=f"{tag} node {i}", model="test", tags=[tag],
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))


def _seed_projects(home: Path, extra=None):
    """Write a temp projects.json: a Skills & Frameworks library (the ABSORB
    target, keyed 'skills') + a plain approved parent. `extra` merges/overrides."""
    data = {
        "skills": ["Skills & Frameworks", "the session infrastructure"],
        "parentproj": ["Parent Project", "an approved project"],
    }
    if extra:
        data.update(extra)
    pf = home / ".cairn" / "projects.json"
    pf.write_text(json.dumps(data), encoding="utf-8")
    return pf


def _file_under(handlers, slug, project, confirm=False):
    payload = {"slug": slug, "project": project}
    if confirm:
        payload["confirm_move"] = True
    return _run(handlers["/api/garden/registry/file-under"](payload, _Req()))


# ── ABSORB (Skills & Frameworks) ─────────────────────────────────────────────

class TestAbsorb:
    def test_absorb_folds_and_audit_logs(self, home, vault):
        from cairn.registry import propose, rows
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Loop Helper", aliases=["kw:loophelper"], evidence=5)
        handlers = _app_handlers(garden, vault)

        res = _file_under(handlers, "loop-helper", "skills")
        assert res["filing_mode"] == "absorb" and res["filed"] is True
        # slug + aliases folded into skills' alias list
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "loop-helper" in data["skills"][2]
        assert "kw:loophelper" in data["skills"][2]
        # registry row retired: parent=skills, filing_mode=absorb, archived
        st = rows(vault)["loop-helper"]
        assert st["parent"] == "skills"
        assert st["filing_mode"] == "absorb"
        assert st["status"] == "archived"
        # audit-only: no restore/unfile endpoint exists in v1
        assert "/api/garden/registry/unfile" not in handlers
        assert "/api/garden/registry/restore" not in handlers

    def test_record_absorbed_filing_single_append(self, home, vault):
        """Fix 1: exactly ONE new registry node, not via act()/ACTIONS, and the
        underlying MEMORY nodes stay active (filing records proposal state only)."""
        from cairn.registry import propose, record_absorbed_filing, rows, ACTIONS
        _seed_projects(home)
        _reload_garden(home)
        propose(vault, "Absorb Me", evidence=3)
        _n(vault, "absorb-me", kind="insight", n=1)   # a real memory node

        before = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE tags LIKE '%\"registry-row\"%'"
        ).fetchone()[0]
        st = record_absorbed_filing(vault, "absorb-me", "skills", by="human")
        after = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE tags LIKE '%\"registry-row\"%'"
        ).fetchone()[0]

        assert after - before == 1, "exactly one registry node appended"
        assert st["parent"] == "skills" and st["filing_mode"] == "absorb"
        assert st["status"] == "archived"
        assert "absorb" not in ACTIONS   # the verb set was never extended
        # every underlying memory node still active — nothing archived/voided
        active = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE status='active' "
            "AND tags LIKE '%\"absorb-me\"%' AND tags NOT LIKE '%\"registry-row\"%'"
        ).fetchone()[0]
        assert active == 1

    def test_already_filed_409(self, home, vault):
        from cairn.registry import propose
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Once Only", evidence=2)
        handlers = _app_handlers(garden, vault)
        assert _file_under(handlers, "once-only", "skills")["filed"] is True
        res2 = _file_under(handlers, "once-only", "parentproj")
        assert getattr(res2, "status_code", None) == 409   # never double-file

    def test_absorb_fold_is_add_if_absent(self, home, vault):
        """Idempotent fold: a slug already in skills' aliases is not doubled."""
        from cairn.registry import propose
        _seed_projects(home, extra={
            "skills": ["Skills & Frameworks", "lib", ["retry-row"]]})
        garden = _reload_garden(home)
        propose(vault, "Retry Row", evidence=1)
        handlers = _app_handlers(garden, vault)
        assert _file_under(handlers, "retry-row", "skills")["filing_mode"] == "absorb"
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert data["skills"][2].count("retry-row") == 1


# ── NEST (any other approved project) ────────────────────────────────────────

class TestNest:
    def test_nest_moves_out_of_proposed(self, home, vault):
        from cairn.registry import propose, rows
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Nest Me", evidence=4)
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "nest-me", "parentproj")
        assert res["filing_mode"] == "nest"
        st = rows(vault)["nest-me"]
        assert st["parent"] == "parentproj"
        assert st["filing_mode"] == "nest"
        assert st["status"] == "proposed"   # unchanged — nest is not a bless

    def test_nest_no_collision_leaves_projects_json_unchanged(self, home, vault):
        """Fix 2: a non-conflicting nest is a registry-only write."""
        from cairn.registry import propose
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Registry Only", evidence=1)
        pf = home / ".cairn" / "projects.json"
        before = pf.read_bytes()
        handlers = _app_handlers(garden, vault)
        assert _file_under(handlers, "registry-only", "parentproj")["filing_mode"] == "nest"
        assert pf.read_bytes() == before, "non-conflicting nest must not touch projects.json"

    def test_nest_confirmed_collision_removes_from_prior(self, home, vault):
        """Fix 2: a confirmed-collision nest strips the member from its prior
        project and does NOT add it to the new parent (nest is separable)."""
        from cairn.registry import propose, rows
        _seed_projects(home, extra={
            "proja": ["Project A", "owns movable", ["movable"]]})
        garden = _reload_garden(home)
        propose(vault, "movable", evidence=2)   # slug == "movable"
        handlers = _app_handlers(garden, vault)
        # no confirm → one-home prompt
        assert getattr(_file_under(handlers, "movable", "parentproj"),
                       "status_code", None) == 409
        # confirm → moved out of A, not folded into parentproj
        res2 = _file_under(handlers, "movable", "parentproj", confirm=True)
        assert res2["filing_mode"] == "nest"
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "movable" not in data["proja"][2]
        parent_aliases = data["parentproj"][2] if len(data["parentproj"]) > 2 else []
        assert "movable" not in parent_aliases   # separable, not folded
        assert rows(vault)["movable"]["parent"] == "parentproj"

    def test_parent_survives_later_bless(self, home, vault):
        from cairn.registry import propose, act, rows
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Grow Up", evidence=3)
        handlers = _app_handlers(garden, vault)
        _file_under(handlers, "grow-up", "parentproj")
        act(vault, "grow-up", "bless")
        st = rows(vault)["grow-up"]
        assert st["status"] == "blessed"
        assert st["parent"] == "parentproj" and st["filing_mode"] == "nest"

    def test_nest_already_folded_prompts_then_converts(self, home, vault):
        """v4-final self-parent fix: a slug already an alias of the target parent
        is double-represented; nest prompts, then converts alias → separable child."""
        from cairn.registry import propose, rows
        _seed_projects(home, extra={
            "parentproj": ["Parent Project", "an approved project", ["dupe"]]})
        garden = _reload_garden(home)
        propose(vault, "dupe", evidence=2)   # slug already under parentproj
        handlers = _app_handlers(garden, vault)
        pf = home / ".cairn" / "projects.json"
        before = pf.read_bytes()
        # no confirm → already_folded prompt, projects.json untouched
        res = _file_under(handlers, "dupe", "parentproj")
        assert getattr(res, "status_code", None) == 409
        assert pf.read_bytes() == before
        # confirm → removed from parent's aliases (no longer double-counted)
        res2 = _file_under(handlers, "dupe", "parentproj", confirm=True)
        assert res2["filing_mode"] == "nest"
        data = json.loads(pf.read_text("utf-8"))
        parent_aliases = data["parentproj"][2] if len(data["parentproj"]) > 2 else []
        assert "dupe" not in parent_aliases
        assert rows(vault)["dupe"]["parent"] == "parentproj"


# ── Child-area non-duplication (Fix 3) ───────────────────────────────────────

class TestChildFilter:
    def test_nest_child_filter_excludes_absorbed_passed_blessed(self, home, vault):
        """The child area keys on parent + filing_mode=='nest' + active status.
        Assert the row shapes are distinguishable so absorbed/passed rows can't
        leak into a parent's nested-child list."""
        from cairn.registry import (propose, nest, record_absorbed_filing,
                                     act, rows)
        _seed_projects(home)
        _reload_garden(home)
        propose(vault, "Active Child", evidence=1)
        nest(vault, "active-child", "parentproj")
        propose(vault, "Gone To Skills", evidence=1)
        record_absorbed_filing(vault, "gone-to-skills", "skills")
        propose(vault, "Was Child", evidence=1)
        nest(vault, "was-child", "parentproj")
        act(vault, "was-child", "pass")

        r = rows(vault)

        def is_active_nest_child(st, parent):
            return (st.get("parent") == parent
                    and st.get("filing_mode") == "nest"
                    and st.get("status") in ("proposed", "revived"))

        assert is_active_nest_child(r["active-child"], "parentproj")
        assert not is_active_nest_child(r["gone-to-skills"], "parentproj")  # absorbed
        assert not is_active_nest_child(r["was-child"], "parentproj")       # passed
        # the absorbed row is archived; the passed row keeps its nest parent
        assert r["gone-to-skills"]["filing_mode"] == "absorb"
        assert r["was-child"]["status"] == "passed"
        assert r["was-child"]["parent"] == "parentproj"


# ── One-home arbitration (both paths) ────────────────────────────────────────

class TestOneHome:
    def test_collision_prompts_not_silent(self, home, vault):
        from cairn.registry import propose
        _seed_projects(home, extra={"proja": ["Project A", "owns tagx", ["tagx"]]})
        garden = _reload_garden(home)
        propose(vault, "tagx", evidence=1)
        pf = home / ".cairn" / "projects.json"
        before = pf.read_bytes()
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "tagx", "skills")   # absorb, tagx lives under A
        assert getattr(res, "status_code", None) == 409
        assert pf.read_bytes() == before                # nothing moved

    def test_collision_confirm_moves(self, home, vault):
        from cairn.registry import propose
        _seed_projects(home, extra={"proja": ["Project A", "owns tagy", ["tagy"]]})
        garden = _reload_garden(home)
        propose(vault, "tagy", evidence=1)
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "tagy", "skills", confirm=True)
        assert res["filing_mode"] == "absorb"
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "tagy" not in data["proja"][2]   # removed from A
        assert "tagy" in data["skills"][2]       # single home now: Skills

    def test_emerging_path_one_home(self, home, vault):
        """The EXISTING /api/garden/file-under prompts + honors confirm_move too."""
        _seed_projects(home, extra={"proja": ["Project A", "owns etag", ["etag"]]})
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        fu = handlers["/api/garden/file-under"]
        res = _run(fu({"tag": "etag", "project": "parentproj"}, _Req()))
        assert getattr(res, "status_code", None) == 409
        res2 = _run(fu({"tag": "etag", "project": "parentproj",
                        "confirm_move": True}, _Req()))
        assert res2["filed"] is True
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "etag" not in data["proja"][2]
        assert "etag" in data["parentproj"][2]


# ── Eligibility rejections ───────────────────────────────────────────────────

class TestEligibility:
    def test_unknown_slug_400(self, home, vault):
        _seed_projects(home)
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "no-such-slug", "skills")
        assert getattr(res, "status_code", None) == 400

    def test_unknown_project_404(self, home, vault):
        from cairn.registry import propose
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Orphan", evidence=1)
        pf = home / ".cairn" / "projects.json"
        before = pf.read_bytes()
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "orphan", "does-not-exist")
        assert getattr(res, "status_code", None) == 404
        assert pf.read_bytes() == before

    def test_project_key_alias_not_folded(self, home, vault):
        """A member equal to a declared project KEY is dropped (can't absorb a
        project into another)."""
        from cairn.registry import propose
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Filer", aliases=["parentproj"], evidence=1)  # alias is a KEY
        handlers = _app_handlers(garden, vault)
        res = _file_under(handlers, "filer", "skills")
        assert res["filing_mode"] == "absorb"
        data = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "filer" in data["skills"][2]
        assert "parentproj" not in data["skills"][2]   # a project key never folds in


# ── First-render ordering + UI wiring (source assertions on GARDEN_HTML) ──────

class TestFirstRenderAndWiring:
    def test_file_under_opts_declared_before_callers(self, home):
        """§3A: options const declared before propHTML and the proposed control
        that reference it (no first-render empty dropdown); old window global gone."""
        garden = _reload_garden(home)
        g = garden.GARDEN_HTML
        assert g.index("const fileUnderOpts") < g.index("const propHTML")
        assert g.index("const fileUnderOpts") < g.index("propFilePick(event")
        assert "window._fileUnderOpts" not in g

    def test_proposed_card_wires_new_endpoint(self, home):
        garden = _reload_garden(home)
        g = garden.GARDEN_HTML
        assert "propFileGo(event" in g and "pfu-go-" in g
        assert "/api/garden/registry/file-under" in g
        # the audit-only Filed history and the nested-child affordance exist
        assert "absorbed into Skills & Frameworks" in g
        assert "nestKids" in g


# ── Bounded corrections (Codex final pass) ───────────────────────────────────

class TestBoundedCorrections:
    def test_rejects_non_undecided_rows(self, home, vault):
        """Only proposed/revived rows are fileable; passed/archived/blessed are not."""
        from cairn.registry import propose, act
        _seed_projects(home)
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        for name, action in [("Passed Row", "pass"),
                             ("Archived Row", "archive"),
                             ("Blessed Row", "bless")]:
            slug = name.lower().replace(" ", "-")
            propose(vault, name, evidence=1)
            act(vault, slug, action)
            res = _file_under(handlers, slug, "skills")
            assert getattr(res, "status_code", None) == 409, \
                f"a {action}ed row must be rejected"
        # a REVIVED row is back in the undecided lane → fileable
        propose(vault, "Revived Row", evidence=1)
        act(vault, "revived-row", "pass")
        act(vault, "revived-row", "revive")
        res = _file_under(handlers, "revived-row", "parentproj")
        assert isinstance(res, dict) and res.get("filing_mode") == "nest"

    def test_combined_conflict_one_truthful_prompt(self, home, vault):
        """A nest where one member lives under ANOTHER project AND the slug is
        already folded into the SELECTED parent returns ONE 409 listing BOTH
        consequences; confirm performs both."""
        from cairn.registry import propose, rows
        _seed_projects(home, extra={
            "parentproj": ["Parent Project", "approved", ["combo"]],      # convert
            "proja": ["Project A", "owns otheralias", ["otheralias"]],    # move
        })
        garden = _reload_garden(home)
        propose(vault, "combo", aliases=["otheralias"], evidence=2)
        handlers = _app_handlers(garden, vault)
        pf = home / ".cairn" / "projects.json"
        before = pf.read_bytes()

        res = _file_under(handlers, "combo", "parentproj")
        assert getattr(res, "status_code", None) == 409
        body = json.loads(res.body)
        assert body["error"] == "confirm_move_required"
        assert body["move"] == {"otheralias": "proja"}
        assert body["convert"] == {"combo": "parentproj"}
        assert pf.read_bytes() == before   # nothing moved without confirm

        res2 = _file_under(handlers, "combo", "parentproj", confirm=True)
        assert res2["filing_mode"] == "nest"
        data = json.loads(pf.read_text("utf-8"))
        assert "otheralias" not in data["proja"][2]           # moved out of A
        parent_aliases = data["parentproj"][2] if len(data["parentproj"]) > 2 else []
        assert "combo" not in parent_aliases                   # converted to child
        assert rows(vault)["combo"]["parent"] == "parentproj"

    def test_absorb_partial_failure_then_retry(self, home, vault, monkeypatch):
        """Injected crash: the projects.json fold succeeds, then the registry
        append fails → 500. A retry reaches the same end state with no double-fold
        or double-archive (two non-atomic stores, idempotent recovery)."""
        import cairn.registry as reg
        from cairn.registry import propose, rows
        _seed_projects(home)
        garden = _reload_garden(home)
        propose(vault, "Crashy", evidence=1)
        handlers = _app_handlers(garden, vault)

        real = reg.record_absorbed_filing
        state = {"n": 0}

        def boom(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("injected registry failure after config write")
            return real(*a, **k)

        monkeypatch.setattr(reg, "record_absorbed_filing", boom)

        res1 = _file_under(handlers, "crashy", "skills")
        assert getattr(res1, "status_code", None) == 500
        data1 = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert "crashy" in data1["skills"][2]            # config fold DID happen
        assert not rows(vault)["crashy"].get("parent")   # registry NOT yet updated

        # retry → same end state, no double-fold, row now absorbed
        res2 = _file_under(handlers, "crashy", "skills")
        assert res2["filing_mode"] == "absorb"
        data2 = json.loads((home / ".cairn" / "projects.json").read_text("utf-8"))
        assert data2["skills"][2].count("crashy") == 1   # folded exactly once
        assert rows(vault)["crashy"]["parent"] == "skills"
