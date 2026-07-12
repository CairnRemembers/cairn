"""
tests/test_mcp_attribution.py — honest model labels + session scoping for
MCP clients (Lane C). Contract: the initialize handshake's clientInfo names
the writer; CAIRN_MODEL env overrides; a NON-Claude client never inherits
the active Claude session from last_session.txt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn import mcp_server


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("CAIRN_MODEL", raising=False)
    monkeypatch.delenv("CAIRN_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    mcp_server._CLIENT_INFO.clear()
    yield
    mcp_server._CLIENT_INFO.clear()


def test_initialize_captures_client_info():
    mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "Codex CLI",
                                                 "version": "0.15"}}})
    assert mcp_server._client_label() == "codex-cli"


def test_env_override_beats_client_info(monkeypatch):
    mcp_server._CLIENT_INFO.update({"name": "codex-cli"})
    monkeypatch.setenv("CAIRN_MODEL", "gpt-5.5")
    assert mcp_server._client_label() == "gpt-5.5"


def test_unknown_client_stays_generic():
    assert mcp_server._client_label() == "mcp-client"


def test_non_claude_client_gets_scoped_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn" / "last_session.txt").write_text("claude-abc-123")
    mcp_server._CLIENT_INFO.update({"name": "codex-cli"})
    sid = mcp_server._session()
    assert sid.startswith("mcp-codex-cli-")
    assert "claude-abc-123" not in sid


def test_claude_client_keeps_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn" / "last_session.txt").write_text("claude-abc-123")
    mcp_server._CLIENT_INFO.update({"name": "claude-code"})
    assert mcp_server._session() == "claude-abc-123"


def test_env_session_always_wins(monkeypatch):
    mcp_server._CLIENT_INFO.update({"name": "codex-cli"})
    monkeypatch.setenv("CAIRN_SESSION", "explicit-session")
    assert mcp_server._session() == "explicit-session"


def test_serve_starts_and_self_declares_harness(monkeypatch):
    """Regression: serve() referenced `os` without importing it -> NameError on
    startup, which killed the whole MCP server. This EXECUTES serve() (empty
    stdin makes the read loop exit at once) so that class of bug can't slip by
    a green suite again."""
    import io
    monkeypatch.delenv("CAIRN_HARNESS", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    try:
        mcp_server.serve()                        # must not raise
        assert os.environ.get("CAIRN_HARNESS") == "mcp"   # self-declared (R1)
    finally:
        os.environ.pop("CAIRN_HARNESS", None)     # don't leak into other tests
