"""
The v2 rules-shim refresh (cmd_connect) — preservation + idempotency.

Codex review requirement (2026-07-14): the new refresh-in-place logic writes
into files cairn does not own (a project's AGENTS.md may hold the user's own
rules), so it must be PROVEN to (a) touch only the marked cairn block,
(b) leave everything outside the markers byte-identical, and (c) be
idempotent — a second connect changes nothing.
"""
import pytest

import cairn.vault as vaultmod
from cairn.__main__ import cmd_connect

SHIMS = ["AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules"]


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)


def test_refresh_preserves_user_content_around_stale_block(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    pre = ("# House rules\nkeep this line exactly\n\n"
           "<!-- cairn:start -->\nOLD V1 BLOCK CONTENT\n<!-- cairn:end -->\n\n"
           "# Tail rules\nalso keep this exactly\n")
    (proj / "AGENTS.md").write_text(pre, encoding="utf-8")

    cmd_connect([str(proj)])

    txt = (proj / "AGENTS.md").read_text(encoding="utf-8")
    assert "keep this line exactly" in txt          # content above survives
    assert "also keep this exactly" in txt          # content below survives
    assert "cairn:start v2" in txt                  # block upgraded
    assert "OLD V1 BLOCK CONTENT" not in txt        # stale block replaced
    assert txt.count("<!-- cairn:start") == 1       # exactly one block


def test_connect_is_idempotent_across_all_shims(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    cmd_connect([str(proj)])                        # first run creates shims
    before = {t: (proj / t).read_text(encoding="utf-8") for t in SHIMS}
    assert all("cairn:start v2" in b for b in before.values())

    cmd_connect([str(proj)])                        # second run: no-op
    after = {t: (proj / t).read_text(encoding="utf-8") for t in SHIMS}
    assert before == after


def test_lf_file_outside_bytes_preserved_exactly(tmp_path):
    """Codex byte-level finding: text-mode rewrite CRLF'd whole LF files on
    Windows. Outside the marked block, bytes must survive IDENTICAL — proven
    with read_bytes(), not substring checks."""
    proj = tmp_path / "proj"
    proj.mkdir()
    top = b"# House rules\nkeep this line exactly\n\n"
    old = b"<!-- cairn:start -->\nOLD V1\n<!-- cairn:end -->\n"
    bot = b"\n# Tail rules\nalso keep this exactly\n"
    (proj / "AGENTS.md").write_bytes(top + old + bot)

    cmd_connect([str(proj)])

    raw = (proj / "AGENTS.md").read_bytes()
    assert raw.startswith(top)                      # bytes above: identical
    assert raw.endswith(bot)                        # bytes below: identical
    assert b"\r\n" not in raw                       # LF file stays pure LF
    assert b"cairn:start v2" in raw


def test_crlf_file_keeps_crlf_and_block_matches(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    top = b"# Windows rules\r\nkeep me\r\n\r\n"
    old = b"<!-- cairn:start -->\r\nOLD V1\r\n<!-- cairn:end -->\r\n"
    bot = b"\r\n# Tail\r\nkeep me too\r\n"
    (proj / "AGENTS.md").write_bytes(top + old + bot)

    cmd_connect([str(proj)])

    raw = (proj / "AGENTS.md").read_bytes()
    assert raw.startswith(top) and raw.endswith(bot)
    assert b"cairn:start v2" in raw
    # pure CRLF file: every newline is CRLF, in the new block too
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_bom_preserved_once(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bom = b"\xef\xbb\xbf"
    (proj / "AGENTS.md").write_bytes(
        bom + b"# Rules\nmine\n\n<!-- cairn:start -->\nOLD\n<!-- cairn:end -->\n")

    cmd_connect([str(proj)])

    raw = (proj / "AGENTS.md").read_bytes()
    assert raw.startswith(bom)
    assert raw.count(bom) == 1                      # kept, never duplicated
    assert b"cairn:start v2" in raw


def test_non_utf8_file_left_byte_identical(tmp_path):
    """Codex round-4 finding: errors="replace" silently corrupted non-UTF-8
    files (UTF-16 fixture: 132 bytes → 1,520 bytes of mojibake). The writer
    must fail CLOSED: an undecodable file stays byte-for-byte untouched."""
    proj = tmp_path / "proj"
    proj.mkdir()
    original = "# My rules\r\nkeep every byte of this\r\n".encode("utf-16")
    (proj / "AGENTS.md").write_bytes(original)

    cmd_connect([str(proj)])

    assert (proj / "AGENTS.md").read_bytes() == original   # untouched, exactly
    # the other shims (fresh files) are still written normally
    assert (proj / "GEMINI.md").exists()


def test_unmarked_hand_rolled_block_left_alone(tmp_path):
    """A hand-written Cairn section without markers is never touched."""
    proj = tmp_path / "proj"
    proj.mkdir()
    hand = "# Mine\nMy own Cairn memory notes, no markers here.\n"
    (proj / "AGENTS.md").write_text(hand, encoding="utf-8")
    cmd_connect([str(proj)])
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == hand
