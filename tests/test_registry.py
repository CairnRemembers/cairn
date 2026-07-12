"""
tests/test_registry.py — the project registry ledger (cairn/registry.py).

Contract under test: append-only fold (newest row per slug wins), agents
propose / humans act, no action ever deletes, bless/pass/revive vocabulary,
and the compiled FINISH-LINES.md is derived — nodes stay canonical.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault
from cairn.registry import (slugify, rows, propose, act,
                            compile_finish_lines)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return Vault(db_path=tmp_path / "test.db")


def test_slugify():
    assert slugify("Widget Co (Auto)") == "widget-co-auto"
    assert slugify("  ") == ""


def test_propose_and_fold(vault):
    nid = propose(vault, "Gadget Box", aliases=["kw:gadgetbox"],
                  evidence=32, span="2024-07..2026-05",
                  why="a sample consumer brand", account="gpt")
    assert nid
    st = rows(vault)["gadget-box"]
    assert st["status"] == "proposed"
    assert st["evidence"] == 32
    assert st["account"] == "gpt"


def test_propose_refuses_duplicates(vault):
    assert propose(vault, "Diffingo", evidence=49)
    assert propose(vault, "Diffingo", evidence=49) is None


def test_act_transitions_append_only(vault):
    propose(vault, "Cogs", evidence=27)
    st = act(vault, "cogs", "bless", reason="real android app")
    assert st["status"] == "blessed"
    st = act(vault, "cogs", "archive")
    assert st["status"] == "archived"
    st = act(vault, "cogs", "revive")
    assert st["status"] == "revived"
    # the whole history is still in the ledger — nothing edited or deleted
    n = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE '%\"registry-row\"%'"
    ).fetchone()[0]
    assert n == 4  # propose + 3 actions


def test_act_unknown_slug_or_action(vault):
    assert act(vault, "never-proposed", "bless") is None
    propose(vault, "Gadgets", evidence=19)
    assert act(vault, "gadgets", "delete") is None  # no such verb, ever


def test_registry_nodes_wear_the_register_tag(vault):
    propose(vault, "Gears", evidence=52)
    tags = vault.conn.execute(
        "SELECT tags FROM nodes WHERE tags LIKE '%\"registry-row\"%'"
    ).fetchone()[0]
    # 'registry' is a PROCESS_TAGS word: proposals stay off Today/Fresh and
    # live in the Projects tab instead — by design, not accident.
    assert '"registry"' in tags


def test_compile_finish_lines(vault, tmp_path):
    propose(vault, "Gizmos", evidence=157, why="property maintenance saas")
    propose(vault, "Sprockets", evidence=7)
    act(vault, "sprockets", "pass", reason="testing")
    out = tmp_path / "FINISH-LINES.md"
    n = compile_finish_lines(vault, out)
    assert n == 2
    text = out.read_text(encoding="utf-8")
    assert "Gizmos" in text and "proposed" in text
    assert "Sprockets" in text and "passed" in text  # passed ≠ gone
