"""
tests/test_page_one_claim_rule.py — the three-layer reminder on PAGE ONE.

The CLAIM CHECK line is recited at every orient, the 34-line cap is
unchanged, and the required tail (NAVIGATE + CLAIM CHECK + protocol marker)
survives even a landscape big enough to hit the cap — landscape trims,
laws/warnings/nav never do.
"""
from __future__ import annotations

import json
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


def _book():
    import importlib
    import cairn.book as book
    importlib.reload(book)
    return book


def test_claim_rule_present_and_cap_held(home, tmp_path):
    book = _book()
    v = Vault(db_path=tmp_path / "t.db")
    v.write(MicroNode(session="s", kind="warning", query="w1",
                      output_preview="w1", model="t", tags=["x"]))
    out = book.page_one(v)
    lines = out.splitlines()
    assert len(lines) <= 34
    assert any(l.startswith("CLAIM CHECK:") for l in lines)
    assert any(l.startswith("NAVIGATE:") for l in lines)
    assert any(l.startswith("LAWS:") for l in lines)
    assert lines[-1] == "== last session's protocol follows =="
    # single line each — token growth bounded to one line
    assert sum(1 for l in lines if l.startswith("CLAIM CHECK:")) == 1


def test_all_required_sections_survive_a_huge_landscape(home, tmp_path):
    """40 active projects PLUS warnings: laws, warnings, navigation, claim
    check, and the protocol marker must ALL survive; only the landscape
    trims, and it says so honestly."""
    book = _book()
    v = Vault(db_path=tmp_path / "t.db")
    projects = {f"tag{i}": [f"Project {i}", "desc"] for i in range(40)}
    (home / ".cairn" / "projects.json").write_text(json.dumps(projects),
                                                   encoding="utf-8")
    for i in range(40):
        v.write(MicroNode(session="s", kind="insight", query=f"n{i}",
                          output_preview=f"n{i}", model="t",
                          tags=[f"tag{i}"]))
    v.write(MicroNode(session="s", kind="warning", query="the big warning",
                      output_preview="the big warning", model="t",
                      tags=["w"]))
    out = book.page_one(v)
    lines = out.splitlines()
    assert len(lines) <= 34
    assert any(l.startswith("LAWS:") for l in lines)
    assert any(l.startswith("WARNINGS:") for l in lines)
    assert any("the big warning" in l for l in lines)
    assert any(l.startswith("NAVIGATE:") for l in lines)
    assert any(l.startswith("CLAIM CHECK:") for l in lines)
    assert lines[-1] == "== last session's protocol follows =="
    assert any("more project line(s)" in l for l in lines)  # honest trim marker
