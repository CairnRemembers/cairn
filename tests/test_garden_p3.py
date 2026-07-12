"""
tests/test_garden_p3.py — Garden IA P3: Index Topics · Book ordering + older
projects · Archive drill-in · the just-landed strip · the tag expander ·
the humanized Brain status bar.

Locks in the 2026-07-02 IA redesign Phase 3 so it can't quietly regress:

  1. TOPICS (plan C4)
     • topics_data reads named community clusters ('c3|golden angle scheduler'),
       skips unnamed / number-only communities, counts meaning-kind members.
     • topic_members lists a topic's meaning-kind nodes (hidden respected).
     • index_data carries topics + consolidated (Topic-hubs fold-in) and the
       A–Z tag list applies the machine-strata denylist (entity:/kw:/… out;
       DISPLAY only — the vault keeps every tag).
  2. BOOK (plan C8)
     • project chapters ordered by LAST-TOUCH DESC (tool_call doesn't count).
     • older_projects: undeclared tag families with real mass, machine strata
       stripped, spelling variants folded, last-activity ordered.
     • volume_sessions: an Archive Volume's session list (drill-in → the P2
       conversation reader).
  3. JUST LANDED (plan C8)
     • /api/garden/justlanded: newest ~10 active nodes of ANY kind, each with
       an embedded flag (False → the ○ marker), process markers
       (tool_call/interrupt/context_stamp) skipped, hidden + imports excluded.
  4. TAG EXPANDER (owner's brain-tags feedback)
     • client prefix list mirrors the server's _NOT_PROJECT_PREFIXES exactly,
       in BOTH garden and dashboard HTML; consts declared before callers.
  5. STATUS BAR (Brain humanization)
     • served HTML carries plain-language labels (hot/warm/cold, Attention,
       memories) + a one-sentence tooltip on EVERY segment + the ○ pending
       marker + short session id with full id in the tooltip. Every metric
       stays — presentation only.

Run: python -m pytest tests/test_garden_p3.py -q
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
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


def _set_community(vault, node_id: str, community: str):
    vault.conn.execute("UPDATE nodes SET community=? WHERE id=?",
                       (community, node_id))
    vault.conn.commit()


def _set_ts(vault, node_id: str, ts: str):
    vault.conn.execute("UPDATE nodes SET timestamp=? WHERE id=?", (ts, node_id))
    vault.conn.commit()


# ── 1. TOPICS: named communities → Index ──────────────────────────────────────

class TestTopics:
    def test_named_communities_counted_unnamed_skipped(self, home, vault):
        from cairn.book import topics_data
        a = vault.write(MicroNode(session="s", kind="decision",
                                  query="use the golden angle", model="test"))
        b = vault.write(MicroNode(session="s", kind="insight",
                                  query="rotation beats decay", model="test"))
        c = vault.write(MicroNode(session="s", kind="decision",
                                  query="orphan in unnamed cluster", model="test"))
        d = vault.write(MicroNode(session="s", kind="decision",
                                  query="numeric label is not a name", model="test"))
        _set_community(vault, a.id, "c3|golden angle scheduler")
        _set_community(vault, b.id, "c3|golden angle scheduler")
        _set_community(vault, c.id, "c7|")          # unnamed
        _set_community(vault, d.id, "c9|4")         # number-only label
        topics = topics_data(vault)
        labels = {t["label"] for t in topics}
        assert "golden angle scheduler" in labels
        assert not any(t["cid"] in ("c7", "c9") for t in topics), \
            "unnamed / number-only communities must be skipped"
        t = next(t for t in topics if t["cid"] == "c3")
        assert t["count"] == 2 and t["total"] == 2

    def test_meaning_kind_gate(self, home, vault):
        """A topic whose members are ALL conversation chatter never shows;
        chatter members don't inflate a real topic's meaning count."""
        from cairn.book import topics_data
        a = vault.write(MicroNode(session="s", kind="conversation_turn",
                                  query="chit chat", model="test"))
        b = vault.write(MicroNode(session="s", kind="decision",
                                  query="real call", model="test"))
        c = vault.write(MicroNode(session="s", kind="conversation_turn",
                                  query="more chat", model="test"))
        _set_community(vault, a.id, "c1|pure chatter")
        _set_community(vault, b.id, "c2|real topic")
        _set_community(vault, c.id, "c2|real topic")
        topics = {t["cid"]: t for t in topics_data(vault)}
        assert "c1" not in topics, "an all-chatter cluster is not a human topic"
        assert topics["c2"]["count"] == 1 and topics["c2"]["total"] == 2

    def test_topic_members_meaning_only_hidden_respected(self, home, vault):
        garden = _reload_garden(home)
        a = vault.write(MicroNode(session="s", kind="decision",
                                  query="member one", model="test"))
        b = vault.write(MicroNode(session="s", kind="conversation_turn",
                                  query="chatter member", model="test"))
        c = vault.write(MicroNode(session="s", kind="insight",
                                  query="hidden member", model="test"))
        for n in (a, b, c):
            _set_community(vault, n.id, "c5|silver hallmarks")
        vault.archive(c.id)
        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/topic/{cid}"]("c5")
        ids = {n["id"] for n in res["nodes"]}
        assert a.id in ids
        assert b.id not in ids, "conversation_turn is not a topic member row"
        assert c.id not in ids, "hidden nodes must not surface"
        assert res["label"] == "silver hallmarks"
        assert res["count"] == len(res["nodes"])

    def test_index_tags_machine_strata_filtered(self, home, vault):
        """The A–Z tag list drops entity:/kw:/… strata (DISPLAY only)."""
        from cairn.book import index_data
        for _ in range(2):
            vault.write(MicroNode(session="s", kind="insight", query="x",
                                  model="test",
                                  tags=["acme", "entity:Acme's",
                                        "kw:silver", "prov:distilled",
                                        "by:sonnet", "stance:claim"]))
        tags = {t["tag"] for t in index_data(vault)["tags"]}
        assert "acme" in tags
        for machine in ("entity:Acme's", "kw:silver", "prov:distilled",
                        "by:sonnet", "stance:claim"):
            assert machine not in tags, f"machine stratum leaked: {machine}"

    def test_index_carries_topics_and_consolidated(self, home, vault):
        """Topics + the Topic-hubs page's consolidated section live in the
        Index now (fold-in, plan C4)."""
        from cairn.book import index_data
        a = vault.write(MicroNode(session="s", kind="decision",
                                  query="topic seed", model="test"))
        _set_community(vault, a.id, "c0|cairn memory design")
        vault.write(MicroNode(session="s", kind="insight",
                              query="synthesized while you slept", model="test",
                              tags=["consolidated"]))
        d = index_data(vault)
        assert any(t["label"] == "cairn memory design" for t in d["topics"])
        assert any("synthesized" in c["gist"] for c in d["consolidated"])

    def test_hubs_endpoint_still_exists(self, home, vault):
        """The fold-in must NOT delete /hubs (links may still point there)."""
        garden = _reload_garden(home)
        handlers = _app_handlers(garden, vault)
        assert "/api/garden/hubs" in handlers
        d = handlers["/api/garden/hubs"]()
        assert "tags" in d and "consolidated" in d


# ── 2. BOOK: last-touch ordering · older projects · volume drill-in ───────────

class TestBookOrdering:
    def test_projects_ordered_by_last_touch_desc(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({
            "alpha": ["Alpha", "first in config"],
            "beta":  ["Beta", "second in config"],
        }), encoding="utf-8")
        from cairn.book import book_data
        a = vault.write(MicroNode(session="s", kind="decision", query="old a",
                                  model="test", tags=["alpha"]))
        _set_ts(vault, a.id, _iso_days_ago(5))
        # a NEW tool_call on alpha must NOT count as a human touch
        tc = vault.write(MicroNode(session="s", kind="tool_call", query="plumb",
                                   model="test", tags=["alpha"]))
        b = vault.write(MicroNode(session="s", kind="decision", query="fresh b",
                                  model="test", tags=["beta"]))
        _set_ts(vault, b.id, _iso_days_ago(1))
        d = book_data(vault)
        order = [p["tag"] for p in d["projects"]]
        assert order.index("beta") < order.index("alpha"), \
            f"most recently touched project must lead, got {order}"
        for p in d["projects"]:
            assert "last_ts" in p

    def test_older_projects_present_cleaned_ordered(self, home, vault):
        pf = home / ".cairn" / "projects.json"
        pf.write_text(json.dumps({"cairn": ["Cairn", "the memory system"]}),
                      encoding="utf-8")
        from cairn.book import book_data
        # a real undeclared family: 3 'acme' + 3 'acmes' = 6 (variants fold)
        # — spread over 2 sessions + 2 days so it passes the structural
        # hygiene rule (one-session/one-day exhaust never qualifies).
        for i in range(3):
            n = vault.write(MicroNode(session=f"s{i % 2}", kind="insight",
                                      query=f"w{i}", model="test",
                                      tags=["acme"]))
            _set_ts(vault, n.id, _iso_days_ago(3 + (i % 2)))
        for i in range(3):
            n = vault.write(MicroNode(session=f"s{i % 2}", kind="insight",
                                      query=f"ws{i}", model="test",
                                      tags=["acmes"]))
            _set_ts(vault, n.id, _iso_days_ago(3 + (i % 2)))
        # a fresher small family: 6 nodes, newer, same 2x2 spread
        for i in range(6):
            n = vault.write(MicroNode(session=f"s{i % 2}", kind="insight",
                                      query=f"g{i}", model="test",
                                      tags=["golfclub"]))
            _set_ts(vault, n.id, _iso_days_ago(1 + (i % 2)))
        # machine strata with mass must NEVER appear
        for i in range(6):
            vault.write(MicroNode(session="s", kind="insight",
                                  query=f"k{i}", model="test",
                                  tags=["kw:noise"]))
        # a declared tag never re-appears as older
        for i in range(6):
            vault.write(MicroNode(session="s", kind="insight",
                                  query=f"c{i}", model="test", tags=["cairn"]))
        d = book_data(vault)
        older = d["older_projects"]
        names = [p["tag"] for p in older]
        assert "golfclub" in names
        assert "acme" in names or "acmes" in names
        w = next(p for p in older if p["tag"] in ("acme", "acmes"))
        assert w["total"] == 6, "spelling variants must fold into one family"
        assert not any(t.startswith("kw:") for t in names), \
            "machine strata must not appear as older projects"
        assert "cairn" not in names, "declared projects never re-appear as older"
        assert names.index("golfclub") < names.index(w["tag"]), \
            "older projects must be last-activity ordered (newest first)"

    def test_below_mass_family_excluded(self, home, vault):
        from cairn.book import book_data
        for i in range(5):   # below the >=6 threshold
            vault.write(MicroNode(session="s", kind="insight",
                                  query=f"t{i}", model="test", tags=["tiny"]))
        d = book_data(vault)
        assert "tiny" not in [p["tag"] for p in d["older_projects"]]


class TestVolumeDrillIn:
    def test_volume_sessions_listed_newest_first(self, home, vault):
        garden = _reload_garden(home)
        for i in range(3):
            n = vault.write(MicroNode(session="import-s1",
                                      kind="conversation_turn",
                                      query=f"turn {i}", model="claude"))
            _set_ts(vault, n.id, _iso_days_ago(10 - i))
        n2 = vault.write(MicroNode(session="import-s2",
                                   kind="conversation_turn",
                                   query="newer convo", model="claude"))
        _set_ts(vault, n2.id, _iso_days_ago(1))
        vault.conn.execute(
            "UPDATE sessions SET account='claudeA' WHERE id LIKE 'import-%'")
        vault.conn.commit()
        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/volume/{account}/sessions"]("claudeA")
        assert res["count"] == 2
        assert [s["id"] for s in res["sessions"]] == ["import-s2", "import-s1"], \
            "sessions must be newest-first"
        s1 = next(s for s in res["sessions"] if s["id"] == "import-s1")
        assert s1["nodes"] == 3 and s1["first"] and s1["last"]

    def test_session_rows_open_the_reader(self, home, vault):
        """A volume's session row drills into the P2 conversation reader."""
        garden = _reload_garden(home)
        vault.write(MicroNode(session="import-s9", kind="conversation_turn",
                              query="hello from the archive", speaker="user",
                              model="human"))
        vault.conn.execute(
            "UPDATE sessions SET account='claudeB' WHERE id='import-s9'")
        vault.conn.commit()
        handlers = _app_handlers(garden, vault)
        vol = handlers["/api/garden/volume/{account}/sessions"]("claudeB")
        sid = vol["sessions"][0]["id"]
        reader = handlers["/api/garden/session/{session_id}/turns"](sid)
        assert reader["count"] == 1
        assert "hello from the archive" in reader["turns"][0]["text"]


# ── 3. JUST LANDED: the live tail, humanized ──────────────────────────────────

class TestJustLanded:
    def test_shape_flags_and_exclusions(self, home, vault):
        garden = _reload_garden(home)
        woven = vault.write(MicroNode(session="s", kind="decision",
                                      query="embedded already", model="test"))
        vault.conn.execute("UPDATE nodes SET embedding=? WHERE id=?",
                           (b"\x00" * 16, woven.id))
        fresh = vault.write(MicroNode(session="s", kind="idea",
                                      query="not yet woven", model="test"))
        # process markers must be skipped
        for kind in ("tool_call", "interrupt", "context_stamp"):
            vault.write(MicroNode(session="s", kind=kind, query=kind,
                                  model="test"))
        hid = vault.write(MicroNode(session="s", kind="insight",
                                    query="set aside", model="test"))
        vault.archive(hid.id)
        imp = vault.write(MicroNode(session="import-x", kind="insight",
                                    query="archive import", model="test"))
        vault.conn.commit()

        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/justlanded"]()
        by_id = {n["id"]: n for n in res["nodes"]}
        assert fresh.id in by_id and woven.id in by_id
        assert by_id[fresh.id]["embedded"] is False, "no embedding → ○"
        assert by_id[woven.id]["embedded"] is True, "embedding present → ·"
        kinds = {n["kind"] for n in res["nodes"]}
        assert not kinds & {"tool_call", "interrupt", "context_stamp"}, \
            "process markers must never land in the strip"
        assert hid.id not in by_id, "hidden ids must be respected"
        assert imp.id not in by_id, "import archive stays out of the live tail"
        for n in res["nodes"]:
            for key in ("id", "kind", "gist", "speaker", "ts", "embedded"):
                assert key in n, f"strip row missing {key}"

    def test_newest_first_capped_at_ten(self, home, vault):
        garden = _reload_garden(home)
        ids = []
        for i in range(14):
            n = vault.write(MicroNode(session="s", kind="insight",
                                      query=f"n{i}", model="test"))
            _set_ts(vault, n.id, _iso_days_ago(14 - i))
            ids.append(n.id)
        handlers = _app_handlers(garden, vault)
        res = handlers["/api/garden/justlanded"]()
        assert res["count"] == 10, "the strip is ~10 rows, newest only"
        got = [n["id"] for n in res["nodes"]]
        assert got == list(reversed(ids))[:10], "rows must be newest-first"


# ── 4. TAG EXPANDER: client/server prefix parity + declaration order ──────────

def _js_prefix_list(html: str) -> set:
    m = re.search(r"const MACHINE_TAG_PREFIXES = \[(.*?)\];", html, re.S)
    assert m, "MACHINE_TAG_PREFIXES const missing from served HTML"
    return set(re.findall(r"'([^']+)'", m.group(1)))


class TestTagExpander:
    def test_garden_prefixes_mirror_server(self, home):
        garden = _reload_garden(home)
        js = _js_prefix_list(garden.GARDEN_HTML)
        assert js == set(garden._NOT_PROJECT_PREFIXES), \
            f"client/server prefix drift: js={js}"

    def test_dashboard_prefixes_mirror_server(self, home):
        garden = _reload_garden(home)
        from cairn.dashboard import DASHBOARD_HTML
        js = _js_prefix_list(DASHBOARD_HTML)
        assert js == set(garden._NOT_PROJECT_PREFIXES), \
            f"dashboard prefix drift: js={js}"

    def test_declared_before_callers(self, home):
        """House JS rule: consts/registries before their top-level callers."""
        garden = _reload_garden(home)
        from cairn.dashboard import DASHBOARD_HTML
        g = garden.GARDEN_HTML
        assert g.index("const MACHINE_TAG_PREFIXES") \
            < g.index("function tagChipsHTML") \
            < g.index("tagChipsHTML(n.tags)")
        d = DASHBOARD_HTML
        assert d.index("const MACHINE_TAG_PREFIXES") \
            < d.index("function tagFoldHTML") \
            < d.index("tagFoldHTML(n.tags)")

    def test_expander_markup_present(self, home):
        garden = _reload_garden(home)
        from cairn.dashboard import DASHBOARD_HTML
        for html in (garden.GARDEN_HTML, DASHBOARD_HTML):
            assert "provenance ${machine.length}" in html, \
                "the provenance N ▸ expander must exist in both faces"


# ── 5. STATUS BAR: humanized labels + tooltips (Brain) ────────────────────────

class TestStatusBarHumanized:
    @pytest.fixture()
    def html(self):
        from cairn.dashboard import DASHBOARD_HTML
        return DASHBOARD_HTML

    def test_plain_language_tier_badges(self, html):
        assert ">hot 0</span>" in html
        assert ">warm 0</span>" in html
        assert ">cold 0</span>" in html
        assert ">H:0</span>" not in html
        assert ">W:0</span>" not in html
        assert ">C:0</span>" not in html
        # JS writes the words too, not initials
        assert "'hot '" in html and "'warm '" in html and "'cold '" in html

    def test_every_segment_has_a_tooltip(self, html):
        bar = html[html.index('<div id="statusbar">'):]
        bar = bar[:bar.index("</div>\n\n<script>")] if "</div>\n\n<script>" in bar \
            else bar[:bar.index("<script>")]
        for m in re.finditer(r'<div class="stat"[^>]*>', bar):
            assert "title=" in m.group(0), \
                f"a status segment lost its tooltip: {m.group(0)}"

    def test_specified_tooltips_verbatim(self, html):
        assert ("Memories pushed into the model's context this session / "
                "the gate's cap.") in html
        assert ("Of memories shown to the model, the share it actually used; "
                "underattended = shown but never used.") in html
        assert ("New memories not yet woven into search — the nightly sleep "
                "embeds them.") in html

    def test_session_short_id_plus_start_full_id_in_tooltip(self, html):
        assert 'id="stat-sess-wrap"' in html
        assert "s.session_started" in html, "JS must read the start time"
        assert "Full id: ' + s.session" in html, "tooltip must carry the full id"
        assert ".slice(0, 8)" in html, "the visible id is short"

    def test_embedded_pending_gets_the_ring(self, html):
        assert 'id="stat-emb-pend"' in html
        assert "' ○ ' + pend + ' pending'" in html, \
            "the pending count wears the ○ freshness marker"

    def test_attention_label_and_vault_words(self, html):
        assert "Attention <span" in html
        assert "Attn eff" not in html
        assert "memories ·" in html, "Vault counts read as memories, not nodes"

    def test_every_metric_still_present(self, html):
        """Humanization is presentation-only: every metric element survives."""
        for el_id in ("stat-sess", "stat-nodes", "stat-hot", "stat-warm",
                      "stat-cold", "stat-str", "stat-tok", "ctx-bar", "ctx-pct",
                      "inj-bar", "inj-pct", "stat-attn", "stat-total",
                      "stat-sessions", "stat-emb"):
            assert f'id="{el_id}"' in html, f"metric lost: {el_id}"

    def test_api_status_serves_session_started(self):
        """The additive field the humanized bar reads — source-level lock."""
        src = (Path(__file__).resolve().parent.parent
               / "cairn" / "dashboard.py").read_text(encoding="utf-8")
        assert '"session_started": session_started' in src


# ── 6. page_one output is UNCHANGED (hard constraint) ─────────────────────────

class TestPageOneUntouched:
    def test_page_one_shape(self, home, vault):
        from cairn.book import page_one
        out = page_one(vault)
        assert out.startswith("== CAIRN - PAGE ONE ==")
        assert "LAWS:" in out and "NAVIGATE:" in out
        assert out.endswith("== last session's protocol follows ==")
        assert len(out.splitlines()) <= 34


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ── 7. DEEP-GATHER: find projects buried in machine tags (plan P3.5) ──────────

class TestGather:
    """The reunification path for backfilled projects whose only identity is
    kw:/entity: distill tags (Gizmos ~250 nodes, zero bare human tags). The
    denylist gates emerging DISCOVERY; gather is the deliberate search that
    includes the machine strata so they can be blessed as aliases."""

    def _seed(self, vault):
        for i in range(5):
            vault.write(MicroNode(session="s", kind="conversation_turn",
                                  query=f"gizmos work {i}", model="test",
                                  tags=["kw:gizmos"]))
        for i in range(3):
            vault.write(MicroNode(session="s", kind="insight",
                                  query=f"upkeep insight {i}", model="test",
                                  tags=["entity:Gizmos", "kw:gizmos"]))
        vault.write(MicroNode(session="s", kind="decision",
                              query="domain decision", model="test",
                              tags=["entity:gizmos.com"]))
        vault.write(MicroNode(session="s", kind="idea",
                              query="unrelated", model="test",
                              tags=["gardening"]))
        vault.conn.commit()

    def test_variants_found_counts_and_distinct_total(self, home, vault):
        garden = _reload_garden(home)
        self._seed(vault)
        h = _app_handlers(garden, vault)
        out = h["/api/garden/gather"](q="gizmos")
        tags = {c["tag"]: c["count"] for c in out["candidates"]}
        assert tags.get("kw:gizmos") == 8
        assert tags.get("entity:Gizmos") == 3
        assert tags.get("entity:gizmos.com") == 1
        assert "gardening" not in tags
        # 5 + 3 + 1 distinct nodes (the 3 double-tagged count once)
        assert out["total"] == 9
        assert out["samples"], "sample gists ride along"

    def test_normalization_meets_spaced_variants(self, home, vault):
        garden = _reload_garden(home)
        for i in range(4):
            vault.write(MicroNode(session="s", kind="conversation_turn",
                                  query=f"car club chat {i}", model="test",
                                  tags=["entity:Sample Widget Club"]))
        vault.write(MicroNode(session="s", kind="insight",
                              query="club insight", model="test",
                              tags=["kw:sample widget club"]))
        vault.conn.commit()
        h = _app_handlers(garden, vault)
        out = h["/api/garden/gather"](q="sample widget")
        tags = {c["tag"] for c in out["candidates"]}
        assert "entity:Sample Widget Club" in tags
        assert "kw:sample widget club" in tags
        assert out["total"] == 5

    def test_min_length_guard(self, home, vault):
        garden = _reload_garden(home)
        h = _app_handlers(garden, vault)
        out = h["/api/garden/gather"](q="ez")
        assert out["candidates"] == [] and out["total"] == 0

    def test_promote_accepts_machine_tag_aliases(self, home, vault):
        import asyncio
        garden = _reload_garden(home)
        self._seed(vault)
        h = _app_handlers(garden, vault)

        class _Req:
            client = type("C", (), {"host": "127.0.0.1"})()
            headers: dict = {}

        res = asyncio.run(h["/api/garden/promote"](
            {"tag": "gizmos", "name": "Gizmos", "blurb": "a sample app",
             "aliases": ["kw:gizmos", "entity:Gizmos",
                         "entity:gizmos.com"]}, _Req()))
        assert isinstance(res, dict) and res.get("promoted") is True
        assert "kw:gizmos" in res["aliases"], "machine tags valid as aliases"
        # the declared family now unions everything in the projects list
        projects = h["/api/garden/projects"]()
        items = projects.get("projects", projects) \
            if isinstance(projects, dict) else projects
        fam = next(p for p in items if p["tag"] == "gizmos")
        assert fam["total"] == 9, "list rollup unions the machine-tag aliases"
        assert fam.get("emerging") in (False, None)

    def test_gather_ui_wired_in_template(self, home):
        garden = _reload_garden(home)
        g = garden.GARDEN_HTML
        assert "gather-q" in g and "doGather" in g
        assert "declareGathered" in g
        assert g.index("api/garden/gather") > 0
