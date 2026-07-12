"""
tests/test_runnability_fixes.py — guards for the new-user runnability fixes.

Two invariants a fresh install depends on:
  - query_episodic degrades to keyword search when the vault has no embeddings
    yet (fresh install / no embedder / no network) instead of loading the model
    and crashing — so fetch/search/dashboard work on day one.
  - `cairn doctor` looks up the REAL packaged distribution name (from pyproject),
    not a stale one — the old 'cairn-memory' lookup made every clean install
    falsely report "metadata not found".

Run: python -m pytest tests/test_runnability_fixes.py -q   (no embedder, no net)
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault, MicroNode

ROOT = Path(__file__).parent.parent


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _write(v, query, kind="decision", session="s1"):
    return v.write(MicroNode(session=session, kind=kind, query=query, model="test"))


class TestKeywordFallback:
    """A fresh vault has no embeddings. query_episodic must keyword-fallback
    (its docstring's promise) rather than load the model and risk a crash."""

    def test_no_embeddings_falls_back_without_loading_model(self, vault):
        _write(vault, "chose sqlite over lancedb for local-first storage")
        hits = vault.query_episodic("sqlite")
        assert vault._embedder is None, "must not load the model when no vectors exist"
        assert hits, "keyword fallback should find the matching node"
        assert any("sqlite" in (h.get("query") or "") for h in hits)

    def test_fallback_rows_are_get_safe_dicts(self, vault):
        _write(vault, "postgres migration notes and rollback plan")
        hits = vault.query_episodic("postgres")
        assert hits and isinstance(hits[0], dict)
        assert hits[0].get("id") and hits[0].get("kind")
        assert hits[0].get("score", None) is not None   # callers read .get("score", 0)

    def test_no_match_returns_empty_list(self, vault):
        _write(vault, "unrelated content here")
        assert vault.query_episodic("zzq_no_such_term_xyz") == []

    def test_void_nodes_excluded_from_fallback(self, vault):
        n = _write(vault, "sqlite decision to be voided")
        vault.void(n.id)
        assert vault.query_episodic("sqlite") == []


def test_doctor_queries_the_real_dist_name():
    name = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    src = (ROOT / "cairn" / "__main__.py").read_text(encoding="utf-8")
    assert f"version('{name}')" in src, f"doctor must query version('{name}')"
    assert "cairn-memory" not in src, \
        "stale dist name 'cairn-memory' still referenced in __main__.py"
