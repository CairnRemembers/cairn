"""cairn_read — full-text node reads by id over MCP.

The gap this tool closes, characterized live 2026-07-02: every model-facing
read surface returned truncated gists, and fetch/search can't see un-embedded
(fresher-than-last-sleep) nodes at all. cairn_read is plain row lookup —
no embeddings involved — so "read what was written a minute ago, in full"
works. These tests pin that contract.
"""
import pytest

import cairn.mcp_server as mcp
from cairn.vault import Vault, MicroNode


LONG_TEXT = ("The golden angle rotation guarantees no memory is permanently "
             "exiled — coverage over urgency → the exploration half of the "
             "scheduler. " * 4).strip()          # >90 chars, has unicode


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = Vault(db_path=str(tmp_path / "test.db"))
    monkeypatch.setattr(mcp, "_VAULT", v)
    yield v
    v.conn.close()


def _write(v, **kw):
    defaults = dict(session="test-mcp-read", kind="decision", query=LONG_TEXT,
                    model="test", agent_role="worker", memory_tier=1,
                    tags=["test"])
    defaults.update(kw)
    return v.write(MicroNode(**defaults))


class TestCairnRead:
    def test_full_text_no_embeddings_needed(self, vault):
        node = _write(vault)
        out = mcp._tool_read({"ids": [node.id]})
        assert node.id in out
        assert LONG_TEXT[:200] in out          # full text, not a 90-char gist
        assert "decision" in out

    def test_prefix_lookup(self, vault):
        node = _write(vault)
        out = mcp._tool_read({"ids": [node.id[:8]]})
        assert node.id in out
        assert LONG_TEXT[:120] in out

    def test_not_found(self, vault):
        out = mcp._tool_read({"ids": ["zzzzzzzzzzzz"]})
        assert "not found" in out

    def test_voided_node_shown_with_banner(self, vault):
        node = _write(vault)
        vault.void(node.id)
        out = mcp._tool_read({"ids": [node.id]})
        assert "status=void" in out            # loud banner
        assert LONG_TEXT[:120] in out          # content still readable

    def test_string_ids_accepted(self, vault):
        a = _write(vault)
        b = _write(vault, query="second node full text for the string-ids case")
        out = mcp._tool_read({"ids": f"{a.id}, {b.id}"})
        assert a.id in out and b.id in out

    def test_no_ids(self, vault):
        assert "no ids" in mcp._tool_read({"ids": []})

    def test_handle_dispatch(self, vault):
        node = _write(vault)
        resp = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "cairn_read",
                                      "arguments": {"ids": [node.id]}}})
        text = resp["result"]["content"][0]["text"]
        assert node.id in text and LONG_TEXT[:120] in text

    def test_advertised(self, vault):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                           "params": {}})
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "cairn_read" in names


class TestRecentPairsWithRead:
    def test_recent_includes_ids(self, vault):
        node = _write(vault)
        out = mcp._tool_recent({"limit": 5})
        assert node.id in out                  # ids present → read can follow up
        assert "cairn_read" in out             # the pointer to the follow-up


class TestCairnLogs:
    def test_tail_includes_all_kinds(self, vault):
        turn = _write(vault, kind="conversation_turn", query="hello whale tail overload",
                      session="codex-thread-1", speaker="user")
        note = _write(vault, kind="decision", query="a decision made moments ago")
        out = mcp._tool_logs({"limit": 10})
        assert turn.id in out and note.id in out   # turns AND meaning-kinds
        assert "conversation_turn" in out

    def test_contains_search_on_live_text(self, vault):
        _write(vault, query="nothing relevant here")
        hit = _write(vault, kind="conversation_turn", session="codex-t",
                     query="My deep take: Cairn is real. What I like most...")
        out = mcp._tool_logs({"contains": "what i like"})   # case-insensitive
        assert hit.id in out
        assert "nothing relevant" not in out

    def test_unembedded_marker_and_filter(self, vault):
        node = _write(vault)                       # temp vault → never embedded
        out = mcp._tool_logs({"unembedded_only": True})
        assert node.id in out
        assert "○" in out                          # the not-yet-embedded mark

    def test_session_prefix_filter(self, vault):
        codex = _write(vault, kind="conversation_turn", session="codex-abc", speaker="agent")
        other = _write(vault, session="claude-session")
        out = mcp._tool_logs({"session": "codex-"})
        assert codex.id in out and other.id not in out

    def test_advertised(self, vault):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                           "params": {}})
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "cairn_logs" in names
