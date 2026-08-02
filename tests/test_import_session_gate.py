"""
tests/test_import_session_gate.py — the import door fails closed (Gate A repair).

`cairn import-session` was the one write path that accepted reserved relation
tags verbatim, bypassing the MCP/CLI relation-integrity gates: a crafted JSONL
line carrying resolves:<id> could soft-clear a live Desk warning. The repair
preflights the ENTIRE file before the vault is opened — any reserved or
non-string tag rejects the whole import with zero database mutations, never a
silent strip. Companion fix: `resolves` joins the reverse-relation lookup so a
properly resolved warning renders "resolved by [id]".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cairn.vault import Vault, MicroNode, RESERVED_RELATION_PREFIXES


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    # Env vars alone do NOT isolate: cairn.vault.VAULT_ROOT binds at first
    # import (2026-08-01 contamination incident) — rebind the module globals.
    import cairn.vault as _cv
    monkeypatch.setattr(_cv, "VAULT_ROOT", h / ".cairn")
    monkeypatch.setattr(_cv, "DB_PATH", h / ".cairn" / "cairn.db")
    return h


def _jsonl(tmp_path, records):
    f = tmp_path / "import.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                 encoding="utf-8")
    return f


def _import(path, session="imported-chat"):
    import cairn.__main__ as main
    main.cmd_import_session([str(path), f"--session={session}",
                             "--date=2026-07-10"])


def _import_rejected(path, capsys, session="imported-chat"):
    """A rejected import must exit NONZERO with the error on STDERR."""
    with pytest.raises(SystemExit) as e:
        _import(path, session=session)
    assert e.value.code not in (0, None)
    return capsys.readouterr().err


def _counts(vault):
    n = vault.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    s = vault.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return n, s


CLEAN = {"when": "2026-07-10T09:00:00+00:00", "kind": "note",
         "text": "an innocent historical note", "tags": ["harmless"],
         "speaker": "agent"}


# ── rejection: every reserved prefix, whole file, zero mutations ─────────────

@pytest.mark.parametrize("prefix", RESERVED_RELATION_PREFIXES)
def test_reserved_tag_rejects_entire_import(home, tmp_path, capsys, prefix):
    vault = Vault()
    w = vault.write(MicroNode(session="s", kind="warning", query="live one",
                              output_preview="live one", model="t",
                              tags=["t"])).id
    before = _counts(vault)
    evil = dict(CLEAN, kind="resolved", text=f"handled {w} long ago",
                tags=[f"{prefix}:{w}"])
    err = _import_rejected(_jsonl(tmp_path, [CLEAN, evil]), capsys)
    assert "REJECTED" in err and f"{prefix}:" in err
    # the WHOLE file is refused — the clean line must not slip in either
    assert _counts(vault) == before
    assert vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE session='imported-chat'"
    ).fetchone()[0] == 0


def test_non_string_tag_rejects_entire_import(home, tmp_path, capsys):
    vault = Vault()
    before = _counts(vault)
    err = _import_rejected(_jsonl(tmp_path, [dict(CLEAN, tags=["ok", 42]),
                                             dict(CLEAN, tags=[["nested"]])]),
                           capsys)
    assert "REJECTED" in err and "non-string tag" in err
    assert _counts(vault) == before


def test_non_list_tags_field_rejects_entire_import(home, tmp_path, capsys):
    # a string/dict tags field would be silently DISCARDED at write time —
    # that's a downgrade; the whole import must be refused instead
    vault = Vault()
    before = _counts(vault)
    err = _import_rejected(
        _jsonl(tmp_path, [dict(CLEAN, tags="resolves:aaaaaaaaaaaa"),
                          dict(CLEAN, tags={"k": "v"})]), capsys)
    assert "REJECTED" in err and "must be a list" in err
    assert "str" in err and "dict" in err
    assert _counts(vault) == before


def test_null_or_absent_tags_field_is_fine(home, tmp_path, capsys):
    rec_no_tags = {k: v for k, v in CLEAN.items() if k != "tags"}
    _import(_jsonl(tmp_path, [rec_no_tags, dict(CLEAN, tags=None)]))
    assert "imported 2 node(s)" in capsys.readouterr().out


def test_rejection_happens_before_the_vault_is_opened(home, tmp_path, capsys):
    # Strongest zero-mutation proof: no Vault() was ever constructed, so the
    # database file itself must not exist after a rejected import.
    db = home / ".cairn" / "cairn.db"
    assert not db.exists()
    err = _import_rejected(
        _jsonl(tmp_path, [dict(CLEAN, tags=[f"resolves:aaaaaaaaaaaa"])]), capsys)
    assert "REJECTED" in err
    assert not db.exists()


def test_rejection_never_silently_strips(home, tmp_path, capsys):
    # The forbidden tag must be NAMED in the error — a stripped-and-imported
    # node would be a silent downgrade, which the policy forbids.
    err = _import_rejected(
        _jsonl(tmp_path, [dict(CLEAN, tags=["corrects:bbbbbbbbbbbb"])]), capsys)
    assert "corrects:bbbbbbbbbbbb" in err and "nothing was imported" in err.lower()


# ── the legitimate path still works ──────────────────────────────────────────

def test_legitimate_import_succeeds(home, tmp_path, capsys):
    vault = Vault()
    before = _counts(vault)[0]
    _import(_jsonl(tmp_path, [CLEAN,
                              dict(CLEAN, text="second note", tags=[])]))
    out = capsys.readouterr().out
    assert "imported 2 node(s)" in out
    rows = vault.conn.execute(
        "SELECT tags FROM nodes WHERE session='imported-chat'").fetchall()
    assert len(rows) == 2
    assert _counts(vault)[0] == before + 2
    for r in rows:
        assert "backfill" in json.loads(r["tags"])    # provenance still forced


# ── resolves: reverse lookup + rendering (the missing half) ──────────────────

def test_resolves_reverse_annotation_renders(home):
    vault = Vault()
    w = vault.write(MicroNode(session="s", kind="warning", query="watch it",
                              output_preview="watch it", model="t",
                              tags=["t"])).id
    r = vault.write(MicroNode(session="s", kind="resolved",
                              query="closed properly",
                              output_preview="closed properly", model="t",
                              tags=[f"resolves:{w}"])).id
    rel = vault.incoming_relations([w])
    assert ("resolves", r) in rel.get(w, [])
    ann = vault.relation_annotations([w])
    assert w in ann and "resolved by" in ann[w] and r in ann[w]


def test_resolves_annotation_survives_voided_target(home):
    vault = Vault()
    w = vault.write(MicroNode(session="s", kind="warning", query="old alarm",
                              output_preview="old alarm", model="t",
                              tags=["t"])).id
    vault.write(MicroNode(session="s", kind="resolved", query="done",
                          output_preview="done", model="t",
                          tags=[f"resolves:{w}"]))
    assert vault.void(w, source="test", provenance="gate-repair test")
    rel = vault.incoming_relations([w])               # must not raise
    assert w in rel and rel[w][0][0] == "resolves"
    ann = vault.relation_annotations([w])             # decoration never raises
    assert isinstance(ann, dict)


def test_resolves_pointing_at_missing_target_never_raises(home):
    vault = Vault()
    vault.write(MicroNode(session="s", kind="resolved", query="phantom",
                          output_preview="phantom", model="t",
                          tags=["resolves:ffffffffffff"]))
    rel = vault.incoming_relations(["ffffffffffff"])  # target id never existed
    assert isinstance(rel, dict)
    assert isinstance(vault.relation_annotations(["ffffffffffff"]), dict)


# ── the validated author path: CLI `cairn note --resolves=<id>` ──────────────

def _warn(vault, text="watch the boiler"):
    return vault.write(MicroNode(session="s", kind="warning", query=text,
                                 output_preview=text, model="t",
                                 tags=["t"])).id


def _note(args):
    import cairn.__main__ as main
    main.cmd_note(args)


def _resolves_count(vault, target):
    return vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE ?",
        (f'%"resolves:{target}"%',)).fetchone()[0]


def test_cli_resolves_active_target_succeeds(home, capsys):
    vault = Vault()
    w = _warn(vault)
    _note([f"--resolves={w}", "closed", "it", "properly"])
    capsys.readouterr()
    assert _resolves_count(vault, w) == 1
    # annotate-only: the target STAYS active — resolves never demotes
    assert vault.conn.execute("SELECT status FROM nodes WHERE id=?",
                              (w,)).fetchone()["status"] == "active"


def test_cli_resolves_reverse_rendering(home, capsys):
    vault = Vault()
    w = _warn(vault)
    _note([f"--resolves={w}", "handled", "for", "good"])
    capsys.readouterr()
    ann = vault.relation_annotations([w])
    assert w in ann and "resolved by" in ann[w]


def test_cli_resolves_already_void_target_rejected(home, capsys):
    vault = Vault()
    w = _warn(vault)
    assert vault.void(w, source="test", provenance="cli-resolves test")
    before = vault.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    with pytest.raises(SystemExit) as e:
        _note([f"--resolves={w}", "too", "late"])
    assert e.value.code not in (0, None)
    assert "already" in capsys.readouterr().err      # CLI errors go to stderr
    # fail closed: the note itself must not have been written
    assert vault.conn.execute(
        "SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    assert _resolves_count(vault, w) == 0


def test_cli_resolves_missing_target_zero_mutation(home, capsys):
    vault = Vault()
    before = vault.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    with pytest.raises(SystemExit) as e:
        _note(["--resolves=eeeeeeeeeeee", "ghost", "closure"])
    assert e.value.code not in (0, None)
    assert "not found" in capsys.readouterr().err    # CLI errors go to stderr
    assert vault.conn.execute(
        "SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    assert _resolves_count(vault, "eeeeeeeeeeee") == 0
