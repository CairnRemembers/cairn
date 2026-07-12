"""
tests/test_dashboard_projects.py — the atlas derives its project groups from the
USER'S OWN ~/.cairn/projects.json, never a hardcoded owner list. A fresh vault
(no projects.json) shows NO baked-in project tags and groups by account until
the user declares their own. Privacy + correctness guard for public launch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
dashboard = pytest.importorskip("cairn.dashboard")


def _fresh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)


def test_fresh_vault_shows_no_owner_projects(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)          # NO projects.json written
    tags = dashboard._atlas_project_tags()
    # fresh vault -> ZERO project groups. The empty result IS the guarantee:
    # any hardcoded owner project would appear here, so no names need naming.
    assert tags == []
    colors = dashboard._atlas_project_colors(tags)
    assert set(colors) == {"import", "_none"}   # only generic system groups


def test_atlas_groups_derive_from_user_projects(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    (tmp_path / ".cairn" / "projects.json").write_text(
        json.dumps({"myapp": ["MyApp", "a note app"],
                    "budget": ["Budget", "money tracker"]}), encoding="utf-8")
    tags = dashboard._atlas_project_tags()
    assert set(tags) == {"myapp", "budget"}     # the user's OWN declared tags
    colors = dashboard._atlas_project_colors(tags)
    assert colors["myapp"] and colors["budget"] # each declared tag gets a color
    assert "import" in colors and "_none" in colors
