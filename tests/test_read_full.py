"""
The verbatim retrieval surface — cairn_read (MCP) and `cairn read` (CLI).

Storage being lossless (test_lossless_turns.py) only matters if the text is
actually REACHABLE: the old MCP read capped every field at 4,000 chars with no
override, and the CLI had no read command at all — text past the cap was
readable only by raw SQLite. These tests pin the new contract: a generous
default budget, a max_chars dial that reaches the whole turn, and truncation
notices that say how much more exists.
"""
import pytest

import cairn.vault as vaultmod
from cairn.capture import write_turn, PREVIEW_CAP
from cairn import mcp_server


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    # the MCP server memoizes its Vault — point it at the isolated one
    monkeypatch.setattr(mcp_server, "_VAULT", None, raising=False)


def _long_turn(v, chars=30_000):
    text = ("deep reasoning line. " * (chars // 21))[:chars - 10] + " FINAL-TAIL"
    node = write_turn(text, speaker="agent", session="s-read", vault=v)
    return text, node


def test_mcp_read_default_budget_covers_a_long_turn(tmp_path):
    """A turn past the old 4,000 cap comes back complete under the default."""
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    text, node = _long_turn(v, chars=20_000)
    out = mcp_server._tool_read({"ids": [node.id]})
    assert "FINAL-TAIL" in out                     # the tail is reachable
    assert "[... truncated" not in out             # nothing silently clipped


def test_mcp_read_max_chars_reaches_everything(tmp_path):
    """Past the default budget, the truncation notice names the total and
    max_chars pulls the whole thing."""
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    text, node = _long_turn(v, chars=30_000)

    clipped = mcp_server._tool_read({"ids": [node.id]})
    assert "truncated at 24000" in clipped         # honest notice
    assert "pass max_chars=" in clipped            # says how to get the rest

    full = mcp_server._tool_read({"ids": [node.id], "max_chars": len(text) + 200})
    assert "FINAL-TAIL" in full
    assert "[... truncated" not in full


def test_mcp_read_max_chars_can_skim(tmp_path):
    """Lower budgets work too — the dial goes both ways."""
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    _, node = _long_turn(v, chars=20_000)
    out = mcp_server._tool_read({"ids": [node.id], "max_chars": 500})
    assert "truncated at 500" in out


def test_mcp_read_one_canonical_body_no_duplicates(tmp_path):
    """Codex finding: query ⊂ preview ⊂ episodic tripled the payload. A turn
    must come back as ONE body — the derived/prefix fields stay suppressed."""
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    _, node = _long_turn(v, chars=20_000)
    out = mcp_server._tool_read({"ids": [node.id]})
    assert out.count("FINAL-TAIL") == 1            # body once, not thrice
    assert "(adds content beyond text)" not in out  # no redundant extras
    assert "fields:" in out                         # sizes still inventoried


def test_mcp_read_total_budget_across_ids(tmp_path):
    """Codex finding: eight long turns could stack into a context bomb — a
    TOTAL budget must stop the pile-up and name what went unread."""
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    ids = []
    for i in range(4):
        text = (f"turn{i} " + "filler words here. " * 1400)[:26_000] + f" TAIL-{i}"
        node = write_turn(text, speaker="agent", session="s-budget", vault=v)
        ids.append(node.id)
    out = mcp_server._tool_read({"ids": ids})
    assert "body budget" in out                     # ceiling engaged
    assert ids[3] in out                            # unread id is named
    assert "TAIL-3" not in out                      # and its body not emitted


def test_cli_read_prints_full_text(tmp_path, capsys):
    """The new `cairn read` command: complete text by default, no cap."""
    from cairn.__main__ import cmd_read
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    text, node = _long_turn(v, chars=PREVIEW_CAP + 6_000)
    cmd_read([node.id])
    out = capsys.readouterr().out
    assert "FINAL-TAIL" in out
    assert "capped at" not in out


def test_cli_read_max_chars_flag(tmp_path, capsys):
    from cairn.__main__ import cmd_read
    v = vaultmod.Vault(db_path=tmp_path / "cairn.db")
    _, node = _long_turn(v, chars=12_000)
    cmd_read([node.id, "--max-chars=400"])
    out = capsys.readouterr().out
    assert "capped at 400" in out
