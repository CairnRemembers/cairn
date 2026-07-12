"""
tests/test_codex_filter.py — codex_hook must ignore Codex's OWN backstage llm
calls (title generation / ambient suggestions / safety-compliance checks) and
never a real chat turn; plus the metadata-only diagnostic log (gated by
CAIRN_CODEX_DEBUG env or a ~/.cairn/CODEX_DEBUG marker file).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.codex_hook as ch

TITLE_USER  = ("You are a helpful assistant. You will be presented with a user "
               "prompt, and your job is to provide a short title.")
SAFETY_USER = ("You are an expert at upholding safety and compliance standards "
               "for Codex ambient suggestions.  I will present ...")
AMBIENT_USER = ("# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for "
                "what this user can do with Codex in this Project")


def test_title_generation_is_a_helper():
    assert ch._internal_helper_reason(TITLE_USER, '{"title":"Inspect routing"}')


def test_safety_check_is_a_helper():
    assert ch._internal_helper_reason(SAFETY_USER, '{"exclude":[]}')


def test_ambient_suggestions_is_a_helper():
    assert ch._internal_helper_reason(AMBIENT_USER, '{"suggestions":[]}')


def test_agent_json_reply_alone_flags_helper():
    assert ch._internal_helper_reason("(some prompt)", '{"suggestions":[{"title":"x"}]}')


def test_real_chat_is_not_a_helper():
    assert ch._internal_helper_reason(
        "hey, can you inspect the codex attribution routing? banana-telescope-42",
        "Sure — here's what I found about the routing ...") is None


def test_empty_is_not_a_helper():
    assert ch._internal_helper_reason("", "") is None


def test_diag_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_CODEX_DEBUG", raising=False)
    monkeypatch.setattr(ch, "CAIRN_HOME", tmp_path)
    ch._diag({"type": "x", "user_len": 5})
    assert not (tmp_path / "codex_hook_diag.log").exists()


def test_diag_on_via_marker_logs_metadata_only(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_CODEX_DEBUG", raising=False)
    monkeypatch.setattr(ch, "CAIRN_HOME", tmp_path)
    (tmp_path / "CODEX_DEBUG").write_text("")            # marker enables diagnostics
    ch._diag({"type": "agent-turn-complete", "decision": "skip",
              "reason": "helper:user-signature", "user_len": 123, "agent_len": 45})
    log = tmp_path / "codex_hook_diag.log"
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["reason"] == "helper:user-signature"
    assert rec["user_len"] == 123 and "ts" in rec
    # metadata ONLY — no transcript content fields
    assert "user_text" not in rec and "agent_text" not in rec


def test_diag_on_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_CODEX_DEBUG", "1")
    monkeypatch.setattr(ch, "CAIRN_HOME", tmp_path)
    assert ch._diag_on() is True
