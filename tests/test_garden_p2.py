"""
tests/test_garden_p2.py — Garden IA P2: Desk consolidation · conversation reader
· write-side nudges · projects-list alias rollup.

Locks in the 2026-07-02 IA redesign Phase 2 so it can't quietly regress:

  1. DESK = the one attention home
     • sections returned: dated / open / TEND / watch / inbox (+ counts.tend)
     • Tend MERGES the review queue + fading + loose threads + from-the-deep,
       DEDUPED by id, each row carrying WHY it surfaced (reasons[] + primary
       reason). A node already on the Desk (open loop / watch / inbox) never
       double-shows in Tend. hidden_ids respected.
  2. session/turns reader endpoint
     • GET /api/garden/session/{id}/turns returns turns ASC by time, full
       fidelity (episodic_text > output_preview > query), hidden filtered,
       LIMIT respected in shape (active only).
  3. promote-node (turn→shelf)
     • POST /api/garden/promote-node writes a NEW idea|open_item node,
       parent=source id, tags = source family tags + 'promoted', source
       UNTOUCHED (append-only). Refuses a bad kind (400) / missing node (404).
  4. due-button server compatibility
     • the capture pipeline parses a 'due:YYYY-MM-DD' token out of the TEXT
       (what the date chip appends) into a due: tag — no new field needed.
  5. projects LIST alias rollup (P1 leftover)
     • /api/garden/projects list counts UNION primary + alias tags (the detail
       view already did) — a promoted family's counts include its alias nodes.

Run: python -m pytest tests/test_garden_p2.py -q
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


class _Req:
    """Minimal request stand-in (rate limiter only touches client.host)."""
    client = type("C", (), {"host": "127.0.0.1"})()
    headers: dict = {}


def _run(coro):
    return asyncio.run(coro)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _reload_garden(home: Path):
    import cairn.garden as garden
    importlib.reload(garden)
    return garden


def _app_handlers(garden, vault):
    from fastapi import FastAPI
    app = FastAPI()
    garden.register_garden(app, vault, lambda: "s")
    return {getattr(r, "path", ""): r.endpoint for r in app.routes}


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── 1. DESK: sections + Tend dedup + reasons ──────────────────────────────────

class TestDeskTend:
    def test_tend_merges_reasons_and_dedupes(self, home, vault):
        garden = _reload_garden(home)
        # a loose thread: a question with NO active children
        thread = vault.write(MicroNode(session="s", kind="question",
                                       query="what is the golden angle?", model="test"))
        # a from-the-deep candidate: high-importance insight, long unseen AND
        # genuinely old — the review engines age-gate: nothing younger than
        # 3 days may be claimed as "forgotten".
        deep = vault.write(MicroNode(session="s", kind="insight",
                                     query="stdlib-only is the moat", model="test",
                                     importance=9, timestamp=_iso_days_ago(90)))
        vault.conn.execute("UPDATE nodes SET last_injected=? WHERE id=?",
                            (_iso_days_ago(90), deep.id))
        # a fading node: shown (uncited) >= 2, long unseen — insert ledger rows
        fade = vault.write(MicroNode(session="s", kind="insight",
                                     query="fading memory here", model="test",
                                     importance=7))
        now_old = _iso_days_ago(60)
        for _ in range(3):
            vault.conn.execute(
                "INSERT INTO attention_ledger (node_id, channel, shown_at, cited) "
                "VALUES (?,?,?,0)", (fade.id, "inject", now_old))
        vault.conn.execute("UPDATE nodes SET last_injected=? WHERE id=?",
                            (now_old, fade.id))
        vault.conn.commit()

        handlers = _app_handlers(garden, vault)
        d = handlers["/api/garden/desk"]()
        assert "tend" in d and "tend" in d["counts"]
        tend = {n["id"]: n for n in d["tend"]}
        # every tend row is deduped and carries at least one reason
        assert len(tend) == len(d["tend"]), "tend must be deduped by id"
        for n in d["tend"]:
            assert n.get("reasons"), "each tend row shows WHY it surfaced"
            assert n["reason"] == n["reasons"][0]
        # the loose thread surfaced with the right reason
        assert thread.id in tend
        assert "loose thread" in tend[thread.id]["reasons"]
        # the deep cut surfaced (overdue review and/or from the deep — both valid)
        assert deep.id in tend
        assert tend[deep.id]["reasons"]  # some forgetting-engine reason
        # the fading node surfaced and is labelled fading
        assert fade.id in tend
        assert "fading" in tend[fade.id]["reasons"]

    def test_tend_excludes_desk_open_loops(self, home, vault):
        garden = _reload_garden(home)
        # an open_item is an OPEN LOOP (its own Desk section) — it must NOT also
        # appear in Tend even though the review query would pick it up.
        oi = vault.write(MicroNode(session="s", kind="open_item",
                                   query="ship the thing", model="test",
                                   importance=9))
        vault.conn.execute("UPDATE nodes SET last_injected=? WHERE id=?",
                            (_iso_days_ago(90), oi.id))
        vault.conn.commit()
        handlers = _app_handlers(garden, vault)
        d = handlers["/api/garden/desk"]()
        assert oi.id in {n["id"] for n in d["open"]}
        assert oi.id not in {n["id"] for n in d["tend"]}, \
            "an open loop must not double-show in Tend"

    def test_tend_respects_hidden(self, home, vault):
        garden = _reload_garden(home)
        q = vault.write(MicroNode(session="s", kind="question",
                                  query="hidden loose thread", model="test"))
        vault.archive(q.id)   # set aside → hidden from human surfaces
        handlers = _app_handlers(garden, vault)
        d = handlers["/api/garden/desk"]()
        assert q.id not in {n["id"] for n in d["tend"]}


# ── 2. session/turns reader endpoint ──────────────────────────────────────────

class TestSessionTurns:
    def test_turns_ordered_full_fidelity_hidden_filtered(self, home, vault):
        garden = _reload_garden(home)
        s = "sess-reader-1"
        t1 = vault.write(MicroNode(session=s, kind="conversation_turn",
                                   query="short user line", speaker="user",
                                   model="human"))
        # a turn whose full text lives in episodic_text (overflowed the cap) —
        # set via episodic_full (the constructor arg the write-gate stores into
        # the episodic_text column verbatim).
        big = "X" * 5000
        t2 = vault.write(MicroNode(session=s, kind="conversation_turn",
                                   query="agent reply (display)",
                                   output_preview="agent reply (display)",
                                   episodic_full=big, speaker="agent",
                                   model="opus"))
        hidden = vault.write(MicroNode(session=s, kind="conversation_turn",
                                       query="hidden turn", speaker="user",
                                       model="human"))
        vault.archive(hidden.id)

        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/session/{session_id}/turns"](s)
        ids = [t["id"] for t in res["turns"]]
        assert hidden.id not in ids, "hidden turn must be filtered"
        assert ids == [t1.id, t2.id], "turns must be ASC by time"
        # full fidelity: the big turn returns its uncapped episodic_text (the
        # write-gate stores episodic_full verbatim, prefixed "agent said: …") —
        # the complete 5000-char body survives, not the display cap.
        big_turn = next(t for t in res["turns"] if t["id"] == t2.id)
        assert big in big_turn["text"], "full turn text must survive uncapped"
        assert len(big_turn["text"]) >= 5000
        assert big_turn["speaker"] == "agent" and big_turn["model"] == "opus"
        # short turn: episodic_text is the natural-language memory form
        # ("user said: …") — full fidelity, the real turn content preserved.
        assert "short user line" in res["turns"][0]["text"]
        assert res["turns"][0]["speaker"] == "user"
        assert res["count"] == 2

    def test_turns_empty_session(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/session/{session_id}/turns"]("nope")
        assert res["turns"] == [] and res["count"] == 0


# ── 3. promote-node (turn → shelf) ────────────────────────────────────────────

class TestPromoteNode:
    def test_creates_linked_node_source_untouched(self, home, vault):
        garden = _reload_garden(home)
        src = vault.write(MicroNode(
            session="s", kind="conversation_turn",
            query="we should build a card-matching game due:2026-09-01",
            speaker="user", model="human", tags=["acme", "kw:silver"]))
        handlers = _app_handlers(garden, vault)
        res = _run(handlers["/api/garden/promote-node"](
            {"id": src.id, "kind": "idea"}, _Req()))
        assert res["kind"] == "idea"
        assert res["parent"] == src.id
        # a NEW node exists, chained to the source
        new = vault.get(res["id"])
        assert new is not None and new["id"] != src.id
        assert new["parent"] == src.id
        assert new["kind"] == "idea"
        tags = json.loads(new["tags"] or "[]")
        assert "promoted" in tags
        assert "acme" in tags               # family tag inherited
        assert "kw:silver" not in tags         # machine stratum dropped
        # source untouched (append-only) — still a conversation_turn, still active
        after = vault.get(src.id)
        assert after["kind"] == "conversation_turn"
        assert after["status"] == "active"
        # the raw due: token was stripped from the promoted gist
        assert "due:2026-09-01" not in (new["query"] or "")

    def test_open_item_kind(self, home, vault):
        garden = _reload_garden(home)
        src = vault.write(MicroNode(session="s", kind="insight",
                                    query="loose end to chase", model="test"))
        handlers = _app_handlers(garden, vault)
        res = _run(handlers["/api/garden/promote-node"](
            {"id": src.id, "kind": "open_item"}, _Req()))
        new = vault.get(res["id"])
        assert new["kind"] == "open_item" and new["parent"] == src.id

    def test_refuses_bad_kind(self, home, vault):
        garden = _reload_garden(home)
        src = vault.write(MicroNode(session="s", kind="insight",
                                    query="x", model="test"))
        handlers = _app_handlers(garden, vault)
        for bad in ("decision", "warning", "", "note", "turn"):
            res = _run(handlers["/api/garden/promote-node"](
                {"id": src.id, "kind": bad}, _Req()))
            assert getattr(res, "status_code", None) == 400, \
                f"kind={bad!r} must be rejected"

    def test_missing_node_404(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        res = _run(handlers["/api/garden/promote-node"](
            {"id": "nope", "kind": "idea"}, _Req()))
        assert getattr(res, "status_code", None) == 404


# ── 4. due-button server compatibility ────────────────────────────────────────

class TestDueCaptureCompat:
    def test_due_token_in_text_becomes_tag(self, home, vault):
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        # the date chip appends 'due:YYYY-MM-DD' to the capture TEXT — verify the
        # existing pipeline still parses it into a due: tag (no server change).
        res = _run(handlers["/api/garden/capture"](
            {"text": "call the assayer due:2026-08-15", "kind": "open_item"}, _Req()))
        node = vault.get(res["id"])
        tags = json.loads(node["tags"] or "[]")
        assert "due:2026-08-15" in tags
        # and the Desk picks it up as a dated item
        d = handlers["/api/garden/desk"]()
        dated = {n["id"]: n for n in d["dated"]}
        assert res["id"] in dated
        assert dated[res["id"]]["due"] == "2026-08-15"


# ── 5. projects LIST alias rollup (P1 leftover) ───────────────────────────────

class TestProjectsListAliasRollup:
    def test_list_counts_union_aliases(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "acme": ["Acme & Co.", "silver", ["acmes", "kw:silver"]],
        }), encoding="utf-8")
        garden = _reload_garden(home)
        # nodes under the primary tag AND under two aliases
        vault.write(MicroNode(session="s", kind="decision", query="d1",
                              model="test", tags=["acme"]))
        vault.write(MicroNode(session="s", kind="open_item", query="o1",
                              model="test", tags=["acmes"]))
        vault.write(MicroNode(session="s", kind="insight", query="i1",
                              model="test", tags=["kw:silver"]))
        handlers = _app_handlers(garden, vault)
        projects = handlers["/api/garden/projects"]()["projects"]
        w = next(p for p in projects if p["tag"] == "acme")
        # the LIST rollup now unions the family — all 3 nodes counted
        assert w["total"] == 3, f"alias nodes must fold into the list total, got {w['total']}"
        assert w["decisions"] == 1
        assert w["open"] == 1

    def test_node_double_tagged_counts_once(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "acme": ["Acme & Co.", "silver", ["acmes"]],
        }), encoding="utf-8")
        garden = _reload_garden(home)
        # one node carrying BOTH the primary and an alias — must count once
        vault.write(MicroNode(session="s", kind="decision", query="both",
                              model="test", tags=["acme", "acmes"]))
        handlers = _app_handlers(garden, vault)
        projects = handlers["/api/garden/projects"]()["projects"]
        w = next(p for p in projects if p["tag"] == "acme")
        assert w["total"] == 1, f"a double-tagged node must count once, got {w['total']}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
