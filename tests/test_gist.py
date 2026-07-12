"""
tests/test_gist.py — the stored gist (a node's one-line subject) must skip
status/ack openers ("Done.", "On it.") so the gist carries meaning, while
leaving genuine short decisions untouched. Forward-only: only new nodes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from cairn.vault import _gist_from_text


def test_gist_skips_status_openers():
    assert _gist_from_text("Done. Report written and verified") == "Report written and verified"
    assert _gist_from_text("On it. Pulling the numbers now") == "Pulling the numbers now"
    assert _gist_from_text("Report's in. I walked the vault") == "I walked the vault"
    # stacked openers peel all the way to the substance
    assert _gist_from_text("Done. Done. Actually shipped the fix") == "Actually shipped the fix"


def test_gist_keeps_meaningful_short_openers():
    # regression guard (per review): a real short decision must NOT be skipped
    assert _gist_from_text("chose SQLite over Postgres; local-first, zero ops") == "chose SQLite over Postgres"
    assert _gist_from_text("ship it now, before the window closes") == "ship it now, before the window closes"


def test_gist_never_blanks_and_handles_empty():
    assert _gist_from_text("Done.") == "Done."   # whole text is an opener → kept
    assert _gist_from_text("") == ""
