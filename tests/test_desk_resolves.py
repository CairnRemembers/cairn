"""
tests/test_desk_resolves.py — structured Desk resolution semantics (candidate).

The Desk's soft-clear signal is now the structured `resolves:<id>` tag on a
resolved node. The old bare-12-hex-text-mention convention is honored ONLY
for resolved nodes written before RESOLVED_MENTION_CUTOFF (bounded
grandfather). Regression pins: a git-hash-shaped token in a NEW resolved
note can no longer suppress an unrelated card.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CAIRN_CAPTURE", "0")
    return h


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _desk(home, vault, cutoff="2020-01-01"):
    """Handler with a pinned cutoff so tests are date-independent: the
    default puts EVERY node in the structured era (no grandfather)."""
    import cairn.garden as garden
    importlib.reload(garden)
    garden.RESOLVED_MENTION_CUTOFF = cutoff
    from fastapi import FastAPI
    app = FastAPI()
    garden.register_garden(app, vault, lambda: "s")
    handlers = {getattr(r, "path", ""): r.endpoint for r in app.routes}
    return handlers["/api/garden/desk"]


def _warn(vault, text="watch the flux capacitor"):
    return vault.write(MicroNode(session="s", kind="warning", query=text,
                                 output_preview=text, model="test",
                                 tags=["t"])).id


def _resolved(vault, text, tags=None):
    return vault.write(MicroNode(session="s", kind="resolved", query=text,
                                 output_preview=text, model="test",
                                 tags=(tags or []))).id


def _watch_ids(desk_fn):
    """The Watch section's node ids — where warnings live on the Desk."""
    return {n["id"] for n in desk_fn().get("watch", [])}


def test_new_text_mention_no_longer_suppresses(home, vault):
    w = _warn(vault)
    desk = _desk(home, vault)
    assert w in _watch_ids(desk)
    _resolved(vault, f"handled the thing about {w} earlier")   # post-cutoff
    assert w in _watch_ids(desk)          # mention alone is not authority anymore


def test_structured_resolves_tag_suppresses(home, vault):
    w = _warn(vault)
    _resolved(vault, "closed it properly", tags=[f"resolves:{w}"])
    desk = _desk(home, vault)
    assert w not in _watch_ids(desk)
    # append-only: the warning itself is untouched, still active
    assert vault.conn.execute("SELECT status FROM nodes WHERE id=?",
                              (w,)).fetchone()["status"] == "active"


def test_git_hash_shaped_token_cannot_suppress(home, vault):
    w = _warn(vault, "unrelated open warning")
    # a new resolved note quoting a 12-hex git-hash prefix identical in shape
    # to a node id — and even equal to this node's id — must not clear it
    _resolved(vault, f"cherry-picked commit {w} onto the release branch")
    desk = _desk(home, vault)
    assert w in _watch_ids(desk)


def test_pre_cutoff_mentions_still_grandfathered(home, vault):
    w = _warn(vault)
    # simulate a resolved node from the pre-structured era via direct INSERT
    # (append-only trigger only guards UPDATEs)
    vault.conn.execute(
        "INSERT INTO nodes (id, session, kind, timestamp, query, "
        "output_preview, tags) VALUES (?,?,?,?,?,?,?)",
        ("aaaaaaaaaaaa", "s", "resolved", "2026-07-15T12:00:00+00:00",
         f"fixed the issue {w} last month", f"fixed the issue {w} last month",
         "[]"))
    vault.conn.commit()
    desk = _desk(home, vault, cutoff="2026-08-02")
    assert w not in _watch_ids(desk)      # old corpus keeps working


def test_imported_historical_node_cannot_reopen_grandfather(home, vault):
    """A resolved node IMPORTED after the boundary arrives with an old
    timestamp — timestamp alone must not grant it text-mention power."""
    w = _warn(vault)
    vault.conn.execute(
        "INSERT INTO nodes (id, session, kind, timestamp, query, "
        "output_preview, tags) VALUES (?,?,?,?,?,?,?)",
        ("bbbbbbbbbbbb", "import-gpt-2026-09-01", "resolved",
         "2026-07-15T12:00:00+00:00",
         f"fixed {w} ages ago", f"fixed {w} ages ago", "[]"))
    vault.conn.commit()
    desk = _desk(home, vault, cutoff="2026-08-02")
    assert w in _watch_ids(desk)          # import cannot reopen the bypass


def test_actual_import_session_shape_cannot_reopen_grandfather(home,
                                                               tmp_path):
    """The REAL `cairn import-session` path: arbitrary --session name (dodges
    every prefix rule), historical timestamp, kind=resolved — but the command
    force-adds the 'backfill' tag, and that tag must kill the grandfather.
    Everything runs in the HOME vault, the one the importer writes to."""
    import importlib, json as _json
    import cairn.__main__ as main
    importlib.reload(main)
    from cairn.vault import Vault as _V, MicroNode
    hv = _V()
    w = hv.write(MicroNode(session="s", kind="warning",
                           query="live warning", output_preview="live warning",
                           model="t", tags=["t"])).id
    jsonl = tmp_path / "old.jsonl"
    jsonl.write_text(_json.dumps({
        "when": "2026-07-10T09:00:00+00:00", "kind": "resolved",
        "text": f"sorted the issue {w} back then", "tags": [],
        "speaker": "agent"}) + "\n", encoding="utf-8")
    # innocent-looking session name — no import-/backfill- prefix to catch
    main.cmd_import_session([str(jsonl), "--session=native-looking-chat",
                             "--date=2026-07-10"])
    desk = _desk(home, hv, cutoff="2026-08-02")
    assert w in _watch_ids(desk)       # historical import cannot clear it


def test_rowid_boundary_closes_the_set_even_against_native_shapes(home, vault):
    """The marker boundary: two byte-identical hostile rows (native session,
    pre-cutoff timestamp, no import provenance) — one inserted BEFORE the
    marker grandfathers, the one inserted AFTER does not. Insert order is
    the one thing a writer cannot forge."""
    from cairn.vault import MicroNode
    w1 = _warn(vault, "warning one")
    w2 = _warn(vault, "warning two")
    vault.conn.execute(
        "INSERT INTO nodes (id, session, kind, timestamp, query, "
        "output_preview, tags) VALUES (?,?,?,?,?,?,?)",
        ("ddddddddd001", "s", "resolved", "2026-07-15T12:00:00+00:00",
         f"fixed {w1} then", f"fixed {w1} then", "[]"))
    vault.conn.commit()
    # integration moment: the boundary marker
    vault.write(MicroNode(session="desk-boundary", kind="procedure",
                          query="desk mention grandfather boundary",
                          output_preview="boundary", model="system",
                          tags=["mention-grandfather-boundary"]))
    # hostile post-marker insert, same shape as the legitimate one
    vault.conn.execute(
        "INSERT INTO nodes (id, session, kind, timestamp, query, "
        "output_preview, tags) VALUES (?,?,?,?,?,?,?)",
        ("ddddddddd002", "s", "resolved", "2026-07-15T12:00:00+00:00",
         f"fixed {w2} then", f"fixed {w2} then", "[]"))
    vault.conn.commit()
    desk = _desk(home, vault, cutoff="2026-08-02")
    ids = _watch_ids(desk)
    assert w1 not in ids               # pre-marker row keeps its power
    assert w2 in ids                   # post-marker row has none


def test_distilled_historical_node_cannot_reopen_grandfather(home, vault):
    w = _warn(vault)
    vault.conn.execute(
        "INSERT INTO nodes (id, session, kind, timestamp, query, "
        "output_preview, tags) VALUES (?,?,?,?,?,?,?)",
        ("cccccccccccc", "s", "resolved", "2026-07-15T12:00:00+00:00",
         f"fixed {w} ages ago", f"fixed {w} ages ago",
         '["prov:distilled"]'))
    vault.conn.commit()
    desk = _desk(home, vault, cutoff="2026-08-02")
    assert w in _watch_ids(desk)
