"""
tests/test_relation_notes.py — annotate-only relation flags (candidate).

`cairn note --corrects=<id> / --narrows=<id> / --applies-after=<id> /
--conflicts-with=<id> / --resolves=<id>` must:
  * validate the target exactly like supersede (exists, not void) and fail
    closed with ZERO mutations otherwise;
  * on success, write ONE note carrying the relation tag — and the target
    STAYS ACTIVE: no void, no status change, no demotion (annotate-only);
  * Vault.incoming_relations resolves the reverse direction, including the
    symmetric half of conflicts-with.

All tests run against throwaway vaults in tmp homes (same harness as
tests/test_supersede_zero_mutation.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway ~/.cairn so nothing touches the real vault."""
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    return h


def _fresh_main():
    import importlib
    import cairn.vault as vault_mod
    importlib.reload(vault_mod)
    import cairn.__main__ as main_mod
    importlib.reload(main_mod)
    return main_mod


def _vault():
    from cairn.vault import Vault
    return Vault()


def _rows(v) -> int:
    return v.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def _seed(v, text="the original claim", kind="warning") -> str:
    from cairn.vault import MicroNode
    node = v.write(MicroNode(session="s", kind=kind, query=text,
                             output_preview=text, model="test",
                             tags=["seed"]))
    return node.id


REL_FLAGS = ["--corrects", "--narrows", "--applies-after", "--conflicts-with"]


# ── failure paths: zero mutation, mirroring the supersede gate ──────────────

@pytest.mark.parametrize("flag", REL_FLAGS + ["--resolves"])
def test_nonexistent_target_errors_with_zero_mutation(home, capsys, flag):
    main = _fresh_main()
    v = _vault()
    before = _rows(v)
    with pytest.raises(SystemExit) as exc:
        main.cmd_note([f"{flag}=ffffffffffff", "some text"])
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err
    assert _rows(_vault()) == before
    assert not (home / ".cairn" / "last_node.txt").exists()


@pytest.mark.parametrize("flag", REL_FLAGS)
def test_void_target_errors_with_zero_mutation(home, capsys, flag):
    main = _fresh_main()
    v = _vault()
    vid = _seed(v)
    v.void(vid)
    before = _rows(v)
    with pytest.raises(SystemExit) as exc:
        main.cmd_note([f"{flag}={vid}", "some text"])
    assert exc.value.code == 1
    assert "already retired" in capsys.readouterr().err
    assert _rows(_vault()) == before


# ── success path: annotate-only — target stays active ───────────────────────

@pytest.mark.parametrize("flag", REL_FLAGS)
def test_success_writes_tag_and_target_stays_active(home, capsys, flag):
    main = _fresh_main()
    v = _vault()
    vid = _seed(v)
    before = _rows(v)
    main.cmd_note([f"{flag}={vid}", "the relation note"])
    out = capsys.readouterr().out
    assert "annotate-only, target stays active" in out
    v2 = _vault()
    assert _rows(v2) == before + 1
    # target untouched: still active
    assert v2.conn.execute("SELECT status FROM nodes WHERE id=?",
                           (vid,)).fetchone()["status"] == "active"
    # exactly one node carries the relation tag
    rel = flag[2:]
    hits = v2.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE ?",
        (f'%"{rel}:{vid}"%',)).fetchone()[0]
    assert hits == 1


def test_target_remains_on_ranked_surfaces(home):
    """Annotate-only invariant: a corrected node is NOT excluded anywhere."""
    main = _fresh_main()
    v = _vault()
    vid = _seed(v, "warning about the throttle body", kind="warning")
    main.cmd_note([f"--corrects={vid}", "actually the throttle body was fine"])
    v2 = _vault()
    # the keyword fallback path (no embeddings in a fresh vault) must still
    # return the corrected target — nothing hides it
    hits = v2.query_episodic("throttle body", k=10)
    assert any(h["id"] == vid for h in hits)


# ── reverse lookup ──────────────────────────────────────────────────────────

def test_incoming_relations_maps_all_types(home):
    main = _fresh_main()
    v = _vault()
    a = _seed(v, "claim A")
    b = _seed(v, "claim B")
    main.cmd_note([f"--corrects={a}", "fix for A"])
    main.cmd_note([f"--narrows={b}", "narrower B"])
    v2 = _vault()
    rel = v2.incoming_relations([a, b])
    assert ("corrects", ) == tuple(r[0] for r in rel[a]),  rel
    assert ("narrows", ) == tuple(r[0] for r in rel[b]), rel


def test_conflicts_with_is_symmetric(home):
    main = _fresh_main()
    v = _vault()
    a = _seed(v, "claim A")
    main.cmd_note([f"--conflicts-with={a}", "counter-claim"])
    v2 = _vault()
    src = v2.conn.execute(
        "SELECT id FROM nodes WHERE tags LIKE ?",
        (f'%"conflicts-with:{a}"%',)).fetchone()["id"]
    # target side sees the conflict…
    rel = v2.incoming_relations([a])
    assert ("conflicts-with", src) in rel[a]
    # …and the source side sees it too (symmetric)
    rel2 = v2.incoming_relations([src])
    assert ("conflicts-with", a) in rel2[src]


def test_voided_relation_source_is_ignored(home):
    """A relation note that was itself retired stops annotating."""
    main = _fresh_main()
    v = _vault()
    a = _seed(v, "claim A")
    main.cmd_note([f"--corrects={a}", "bad correction"])
    v2 = _vault()
    src = v2.conn.execute(
        "SELECT id FROM nodes WHERE tags LIKE ?",
        (f'%"corrects:{a}"%',)).fetchone()["id"]
    v2.void(src)
    assert a not in v2.incoming_relations([a])


def test_plain_note_unchanged(home, capsys):
    """Regression guard: notes without relation flags behave exactly as before."""
    main = _fresh_main()
    v = _vault()
    before = _rows(v)
    main.cmd_note(["--kind=insight", "plain note"])
    assert "wrote insight" in capsys.readouterr().out
    assert _rows(_vault()) == before + 1
