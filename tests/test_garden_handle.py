"""
tests/test_garden_handle.py — the Garden handle setter must PRESERVE the me.json
`channels` map. A bare {"handle": h} write silently wiped channels, collapsing
Codex/other-harness account routing (codex- -> BigCo). Regression guard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

garden = pytest.importorskip("cairn.garden")   # skip if dashboard deps absent


def test_set_handle_preserves_channels(tmp_path, monkeypatch):
    mf = tmp_path / "me.json"
    mf.write_text(json.dumps(
        {"handle": "Old", "channels": {"codex-": "BigCo"}}), encoding="utf-8")
    monkeypatch.setattr(garden, "_ME_FILE", mf)

    saved = garden._set_my_handle("NewName")

    after = json.loads(mf.read_text(encoding="utf-8"))
    assert saved == "NewName"
    assert after["handle"] == "NewName"
    assert after["channels"] == {"codex-": "BigCo"}   # NOT wiped


def test_set_handle_creates_file_when_absent(tmp_path, monkeypatch):
    mf = tmp_path / "me.json"
    monkeypatch.setattr(garden, "_ME_FILE", mf)
    saved = garden._set_my_handle("Solo")
    assert saved == "Solo"
    assert json.loads(mf.read_text(encoding="utf-8"))["handle"] == "Solo"


def test_set_handle_rejects_bad_handle_and_leaves_file(tmp_path, monkeypatch):
    mf = tmp_path / "me.json"
    mf.write_text(json.dumps({"channels": {"codex-": "BigCo"}}), encoding="utf-8")
    monkeypatch.setattr(garden, "_ME_FILE", mf)
    assert garden._set_my_handle("!!!bad") is None
    # rejected write must not have touched the existing channels
    assert json.loads(mf.read_text(encoding="utf-8"))["channels"] == {"codex-": "BigCo"}
