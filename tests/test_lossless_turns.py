"""
Lossless live-turn capture — the "one layer" contract.

Every conversation turn's COMPLETE text must survive capture, regardless of
harness or length: output_preview is a bounded display slice; text past
PREVIEW_CAP rides episodic_full into episodic_text (the same pattern
codex_hook and the importers already use). These tests pin the contract for
live Claude capture (capture.write_turn) — the writer that used to drop
agent tails at 4,000 chars (_AGENT_MAX, retired 2026-07-14).

Also pins what must NOT change (ruling R2, deferred): mid-length turns
(<= PREVIEW_CAP) keep today's embed behavior — episodic_text derived from
the first ~500 chars — because widening the embedded slice is an
embedding-behavior-wide owner decision that stays parked.
"""
import pytest

import cairn.vault as vaultmod
from cairn import capture
from cairn.capture import write_turn, PREVIEW_CAP


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)


def _v(tmp_path):
    return vaultmod.Vault(db_path=tmp_path / "cairn.db")


def _row(v, node_id):
    return v.conn.execute(
        "SELECT query, output_preview, episodic_text FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()


# ── the contract: long turns survive complete ────────────────────────────────

def test_long_agent_turn_is_lossless(tmp_path):
    """An agent turn far past PREVIEW_CAP keeps its complete text."""
    v = _v(tmp_path)
    text = "start-marker " + ("reasoning sentence. " * 1500) + "END-TAIL-MARKER"
    assert len(text) > PREVIEW_CAP  # the case _AGENT_MAX used to destroy

    node = write_turn(text, speaker="agent", session="s-lossless", vault=v)
    row = _row(v, node.id)

    # display slice is bounded…
    assert row["output_preview"] == text[:PREVIEW_CAP]
    # …and the COMPLETE text (tail included) survives in episodic_text
    assert row["episodic_text"].endswith("END-TAIL-MARKER")
    assert text in row["episodic_text"]


def test_long_user_turn_is_lossless(tmp_path):
    """Same contract for the user side (write_turn parity)."""
    v = _v(tmp_path)
    text = ("u" * (PREVIEW_CAP + 5000)) + " USER-TAIL"
    node = write_turn(text, speaker="user", session="s-lossless", vault=v)
    row = _row(v, node.id)
    assert row["output_preview"] == text[:PREVIEW_CAP]
    assert row["episodic_text"].endswith("USER-TAIL")


def test_old_agent_cap_case_now_survives(tmp_path):
    """The exact regression: a turn between 4,000 (old _AGENT_MAX) and
    PREVIEW_CAP used to lose its tail at the door. Now output_preview holds
    it whole."""
    v = _v(tmp_path)
    text = ("x" * 6000) + " MID-TAIL"
    node = write_turn(text, speaker="agent", session="s-lossless", vault=v)
    row = _row(v, node.id)
    assert row["output_preview"] == text          # complete, one field
    assert len(row["output_preview"]) > 4000      # past the retired cap


# ── what must NOT change ─────────────────────────────────────────────────────

def test_short_turn_behavior_unchanged(tmp_path):
    """Short turns: full text in output_preview, no overflow machinery."""
    v = _v(tmp_path)
    text = "we chose sqlite over postgres — local-first, zero ops"
    node = write_turn(text, speaker="agent", session="s-lossless", vault=v)
    row = _row(v, node.id)
    assert row["output_preview"] == text
    assert text[:80] in row["episodic_text"]


def test_r2_embed_slice_stays_deferred(tmp_path):
    """Ruling R2 pin: a mid-length turn (<= PREVIEW_CAP) still derives its
    embedded episodic_text from the ~500-char slice — storage is lossless via
    output_preview, but the embed width does NOT silently widen here."""
    v = _v(tmp_path)
    text = ("a" * 600) + " BEYOND-EMBED-SLICE"
    node = write_turn(text, speaker="agent", session="s-lossless", vault=v)
    row = _row(v, node.id)
    assert row["output_preview"] == text                 # stored complete
    assert "BEYOND-EMBED-SLICE" not in row["episodic_text"]  # embed slice unchanged


def test_preview_cap_matches_codex_hook(tmp_path):
    """One house number: live capture and the Codex hook share the cap, so
    'lossless' means the same thing on every path."""
    from cairn.codex_hook import TRUNC_PREVIEW
    assert PREVIEW_CAP == TRUNC_PREVIEW
