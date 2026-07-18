"""Gate 2 — the read-only triage sections on /api/garden/projects and the Projects tab.

Mirrors the test_garden_projects.py harness (throwaway ~/.cairn, direct endpoint calls off
the route table, importlib.reload so the module-global PROJECTS re-reads the temp home).

NO SNAPSHOT COUNTS. The live vault moves as real work lands, so every count asserted here
is derived from a controlled fixture built in-test. The calibration figures (111/102/1/29,
then 112/103/1/29) are historical evidence about one vault at one moment — not constants.

    Run: python -m pytest tests/test_garden_triage_sections.py -q
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cairn.vault import Vault, MicroNode


class _Req:
    """Minimal request stand-in: same-origin by construction, rate limiter reads host."""
    client = type("C", (), {"host": "127.0.0.1"})()
    headers: dict = {}


def _run(coro):
    """Drive an async endpoint without pytest-asyncio (stdlib-only law)."""
    return asyncio.run(coro)


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


def _seed_projects(home: Path, extra=None):
    d = {"cairn": ["Cairn", "the memory system itself"],
         "skills": ["Skills & Frameworks", "the session infrastructure"]}
    if extra:
        d.update(extra)
    (home / ".cairn" / "projects.json").write_text(json.dumps(d), encoding="utf-8")


def _reload(home: Path):
    import cairn.garden as garden
    importlib.reload(garden)
    import cairn.triage as triage
    importlib.reload(triage)
    return garden


def _handlers(garden, vault):
    from fastapi import FastAPI
    app = FastAPI()
    garden.register_garden(app, vault, lambda: "s")
    return {getattr(r, "path", ""): r.endpoint for r in app.routes}


def _n(vault, tags, kind="insight", n=6, sessions=2):
    base = datetime.now(timezone.utc)
    for i in range(n):
        vault.write(MicroNode(session="s%d" % (i % sessions), kind=kind,
                              query="%s node %d" % (tags[0], i), model="test",
                              tags=list(tags),
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))


def _seed_all(vault, home):
    """One controlled vault that puts at least one card in every section.

    The 60 filler nodes are ballast, not padding: without them `cairn` owns most of the
    vault, its baseline swamps the log-lift, and the affinity gate correctly refuses to
    file anything — leaving `likely_under` and `skills` empty. A real vault has a long
    tail; the fixture needs one too."""
    _seed_projects(home)
    _n(vault, ["spotcmyk", "cmyk", "splatter", "parallax", "hero"], n=6)  # cluster union
    _n(vault, ["spotcmyk"], n=4)                       # headline dominance, no tie
    _n(vault, ["cairn", "cairnkid"], n=10)             # -> likely_under
    _n(vault, ["skills", "howtothing"], kind="procedure", n=10)   # -> skills (global)
    _n(vault, ["cairn", "cairnhowto"], kind="procedure", n=10)    # -> skills (under X)
    _n(vault, ["review"], n=6)                         # -> filtered (process vocabulary)
    _n(vault, ["fleetchild"], n=14, sessions=7)        # -> review (no positive evidence)
    for i in range(60):
        _n(vault, ["filler-%d" % i], n=1)


def _payload(home, vault):
    garden = _reload(home)
    return _handlers(garden, vault)["/api/garden/projects"]()


def _find(rows, tag):
    for r in rows:
        if r["tag"] == tag or tag in (r.get("members") or []):
            return r
    return None


# ------------------------------------------------------------------ payload shape

def test_triage_is_additive_and_legacy_keys_are_untouched(home, vault):
    """Backward compatibility: `projects` and `dismissed` keep their exact semantics.
    Consumers that never heard of triage must not notice this gate."""
    _seed_all(vault, home)
    d = _payload(home, vault)

    assert set(d.keys()) == {"projects", "dismissed", "triage"}
    assert isinstance(d["projects"], list) and isinstance(d["dismissed"], list)
    declared = [p for p in d["projects"] if not p["emerging"]]
    assert {p["tag"] for p in declared} == {"cairn", "skills"}
    for p in declared:
        assert set(p.keys()) == {
            "tag", "name", "desc", "emerging", "total", "open", "decisions",
            "procedures", "warnings", "maturity", "last_ts", "last_gist"}
        assert "aliases" not in p, "declared cards never carried aliases; still must not"
    assert d["projects"], "the raw emerging list is still served for old consumers"


def test_sections_partition_every_card_exactly_once(home, vault):
    """The invariant: every classified card lands in exactly one section, and nothing
    falls through. `unsectioned` is the tell-tale — it must be empty."""
    _seed_all(vault, home)
    tri = _payload(home, vault)["triage"]

    assert set(tri["sections"]) == {
        "emerging", "likely_under", "skills", "filtered", "review"}
    assert tri["unsectioned"] == [], "a verdict matched no section"
    total = sum(len(v) for v in tri["sections"].values())
    assert total == tri["counts"]["cards"] - tri["hidden_by_dismiss"]
    keys = [c["key"] for v in tri["sections"].values() for c in v]
    assert len(keys) == len(set(keys)), "a card appeared in two sections"


def test_section_membership_matches_the_classifier(home, vault):
    _seed_all(vault, home)
    sec = _payload(home, vault)["triage"]["sections"]

    assert _find(sec["emerging"], "spotcmyk")
    assert _find(sec["likely_under"], "cairnkid")
    assert _find(sec["skills"], "howtothing")     # global skill (absorb)
    assert _find(sec["skills"], "cairnhowto")     # project-specific skill under cairn
    assert _find(sec["filtered"], "review")
    assert _find(sec["review"], "fleetchild")


def test_section_counts_are_derived_from_the_payload(home, vault):
    """Ruling 3: counts render from the current payload, never a pinned snapshot."""
    _seed_all(vault, home)
    tri = _payload(home, vault)["triage"]

    assert tri["section_counts"] == {k: len(v) for k, v in tri["sections"].items()}
    assert tri["section_counts"]["emerging"] == 1
    assert tri["section_counts"]["skills"] == 2


# ------------------------------------------------------------------ SpotCMYK, one card

def test_spotcmyk_is_one_card_with_members_and_the_union_count(home, vault):
    """Requirement 4 — five tags, ONE card, members listed, `n` = unique union memories.

    The union here is 10: six shared nodes plus four spotcmyk-only. Summing the members
    would read 34. The number is fixture-derived on purpose — live it is 35, and that
    figure is evidence about a moment, not a constant."""
    _seed_all(vault, home)
    sec = _payload(home, vault)["triage"]["sections"]

    cards = [c for c in sec["emerging"] if "spotcmyk" in c["members"]]
    assert len(cards) == 1, "the cluster must contribute exactly ONE card"
    card = cards[0]
    assert card["tag"] == "spotcmyk", "headline = highest-support member"
    assert sorted(card["members"]) == ["cmyk", "hero", "parallax", "splatter", "spotcmyk"]
    assert card["n"] == 10, "unique union memories, not the sum of the members"
    assert card["disposition"].startswith("NEW PROJECT")
    assert card["reason"], "the card must carry its evidence"


def test_emerging_section_holds_only_positive_evidence(home, vault):
    _seed_all(vault, home)
    sec = _payload(home, vault)["triage"]["sections"]

    for c in sec["emerging"]:
        assert c["stage"] == 1
        assert c["disposition"].startswith(("STANDALONE", "NEW PROJECT"))
    assert not _find(sec["emerging"], "fleetchild"), "recurrence alone is not evidence"


# ------------------------------------------------------------------ stage-2 exclusion

def test_stage_two_is_excluded_from_the_emerging_count(home, vault):
    """The headline count holds Stage-1 candidates only. Review is parked, never counted."""
    _seed_all(vault, home)
    tri = _payload(home, vault)["triage"]

    assert all(c["stage"] == 2 for c in tri["sections"]["review"])
    assert tri["counts"]["emerging"] == tri["section_counts"]["emerging"]
    assert tri["counts"]["emerging"] == 1
    assert tri["section_counts"]["review"] >= 1
    review_tags = {c["tag"] for c in tri["sections"]["review"]}
    emerging_tags = {c["tag"] for c in tri["sections"]["emerging"]}
    assert not (review_tags & emerging_tags)


# ------------------------------------------------------------------ grouping

def test_likely_under_carries_the_parent_for_grouping(home, vault):
    """The drawer groups by parent client-side; the payload must supply one per card."""
    _seed_all(vault, home)
    sec = _payload(home, vault)["triage"]["sections"]

    assert sec["likely_under"], "fixture should file at least one child"
    for c in sec["likely_under"]:
        assert c["parent"], "every likely-under card needs a parent to group by"
    assert _find(sec["likely_under"], "cairnkid")["parent"] == "cairn"


def test_cards_carry_reason_and_gate_for_inspection(home, vault):
    """Requirement 5 — every recommendation shows its reason/evidence."""
    _seed_all(vault, home)
    tri = _payload(home, vault)["triage"]

    for rows in tri["sections"].values():
        for c in rows:
            assert c["reason"], "%s has no evidence" % c["tag"]
            assert c["gate"], "%s has no gate" % c["tag"]
            assert c["disposition"]


# ------------------------------------------------------------------ dismiss path

def test_dismiss_does_not_compute_triage(home, vault, monkeypatch):
    """Ruling 4 — the write path must not pay for a full classification it never reads.

    Proven by sabotage: break triage_data, then show the GET dies and dismiss does not."""
    _seed_all(vault, home)
    garden = _reload(home)
    H = _handlers(garden, vault)

    import cairn.triage as triage_mod

    def boom(*a, **k):
        raise AssertionError("triage_data must not run on the dismiss path")

    monkeypatch.setattr(triage_mod, "triage_data", boom)

    with pytest.raises(AssertionError):
        H["/api/garden/projects"]()          # proves the sabotage is live

    res = _run(H["/api/garden/dismiss-project"]({"tag": "fleetchild"}, _Req()))
    assert res.get("dismissed") is True, "dismiss must still work with triage broken"


def test_dismissed_families_stay_out_of_the_triage_drawers(home, vault):
    """Dismiss is an existing control this gate must preserve. The classifier cannot see
    a dismissal (it writes no node), so the sections must honour the tab's visible set —
    otherwise Gate 2 resurrects every card the owner ever hid."""
    _seed_all(vault, home)
    garden = _reload(home)
    H = _handlers(garden, vault)
    assert _find(H["/api/garden/projects"]()["triage"]["sections"]["review"], "fleetchild")

    _run(H["/api/garden/dismiss-project"]({"tag": "fleetchild"}, _Req()))

    tri = H["/api/garden/projects"]()["triage"]
    assert not _find(tri["sections"]["review"], "fleetchild"), "hidden card came back"
    assert tri["hidden_by_dismiss"] >= 1


# ------------------------------------------------------------------ presentation

def test_projects_tab_renders_the_five_sections_and_no_gate3_actions(home, vault):
    """The page is built client-side, so this pins the shipped JS/CSS that builds the
    drawers. Real rendering is covered by the browser smoke test."""
    _seed_all(vault, home)
    garden = _reload(home)
    html = garden.GARDEN_HTML

    for key in ("emerging", "likely_under", "skills", "filtered", "review"):
        assert "sec.%s" % key in html or "'%s'" % key in html, "no builder for %s" % key
    assert 'id="triage-likely_under"' in html
    assert 'id="sec-emerging"' in html
    assert "grouped by parent" in html
    assert "needs semantic review" in html
    assert ".triage-drawer[open] summary::before" in html, "caret must flip like .book-older"
    assert "triage-unsectioned" in html, "unsectioned cards must surface, never drop"
    # Correction 2 — alias/compound cards carry the 'alias / same topic' label.
    assert "section_label" in html, "the alias / same-topic label must render"
    # Correction 4 — the Emerging lookup resolves the legacy card through any member,
    # not only the exact headline.
    assert "display_tag" in html, "emerging must prefer the server-resolved display card"
    assert ".includes(x.tag)" in html, "emerging must fall back to any cluster member"
    # Gate 3 owns confirm/bless/mark-non-project — no such control may ship here.
    for banned in ("triageConfirm", "triageBless", "markNonProject", "triageAct"):
        assert banned not in html, "%s is Gate 3 behavior" % banned


def test_existing_proposed_file_under_controls_are_preserved(home, vault):
    """Requirement 2 — the Proposed lane's file-under from 609e2b6 is untouched."""
    _seed_all(vault, home)
    garden = _reload(home)
    html = garden.GARDEN_HTML

    assert "fileUnderPick" in html and "fileUnderGo" in html
    assert "pfu-go-" in html and "fu-go-" in html
    assert "file under…" in html
    assert "registryAct" in html
    assert "openPromote" in html and "dismissProject" in html
    H = _handlers(garden, vault)
    for route in ("/api/garden/projects", "/api/garden/promote",
                  "/api/garden/dismiss-project", "/api/garden/registry",
                  "/api/garden/registry/act",
                  "/api/garden/file-under",              # emerging -> declared parent
                  "/api/garden/registry/file-under"):    # the 609e2b6 Proposed lane
        assert route in H, "%s disappeared" % route


# ------------------------------------------------------------------ correction 2

def test_alias_compound_routes_to_filtered_not_unsectioned(home, vault):
    """Correction 2 — S1.5a alias/compound ('these two tags are one topic') is a reachable
    Stage-1 verdict. It must file into Filtered topics, labeled 'alias / same topic', and
    NEVER fall to `unsectioned`, which stays a safety alarm for verdicts no section claims.

    Fixture: two DISTINCT families (color / colour differ under _family_key) that always
    co-occur, so Jaccard = 1.0 >= J_ALIAS, and are name-corroborated (levenshtein 1 <= 2).
    No project tags, so the affinity gate abstains and the pair reaches the cluster gate."""
    _seed_projects(home)
    _n(vault, ["color", "colour"], n=6)          # one 2-member alias cluster
    for i in range(60):                          # tail ballast, as in _seed_all
        _n(vault, ["afiller-%d" % i], n=1)
    tri = _payload(home, vault)["triage"]

    alias = [c for c in tri["sections"]["filtered"]
             if str(c["disposition"]).startswith("alias/compound")]
    assert len(alias) == 1, "the alias/compound verdict must file into Filtered topics"
    assert alias[0]["gate"] == "S1.5a"
    assert alias[0]["stage"] == 1
    assert alias[0]["section_label"] == "alias / same topic"
    assert tri["unsectioned"] == [], "a supported verdict must never fall to unsectioned"
    assert not any(str(c["disposition"]).startswith("alias/compound")
                   for c in tri["sections"]["emerging"]), "alias is not an emerging candidate"


# ------------------------------------------------------------------ correction 3

def test_visible_cards_reconciles_with_hidden_and_raw_totals(home, vault):
    """Correction 3 — visible_cards makes raw-vs-visible explicit: it is the sum of the five
    section counts and reconciles against the raw classified total and the dismissals, so
    visible_cards + hidden_by_dismiss == counts.cards whenever unsectioned is empty."""
    _seed_all(vault, home)
    garden = _reload(home)
    H = _handlers(garden, vault)

    tri = H["/api/garden/projects"]()["triage"]
    assert tri["visible_cards"] == sum(tri["section_counts"].values())
    assert tri["unsectioned"] == []
    assert tri["visible_cards"] + tri["hidden_by_dismiss"] == tri["counts"]["cards"]

    # dismiss a card: visible falls, hidden rises, the raw classified total is unchanged.
    _run(H["/api/garden/dismiss-project"]({"tag": "fleetchild"}, _Req()))
    tri2 = H["/api/garden/projects"]()["triage"]
    assert tri2["hidden_by_dismiss"] >= 1
    assert tri2["visible_cards"] == sum(tri2["section_counts"].values())
    assert tri2["visible_cards"] + tri2["hidden_by_dismiss"] == tri2["counts"]["cards"]
    # the always-true full accounting, unsectioned included.
    assert (tri2["visible_cards"] + len(tri2["unsectioned"])
            + tri2["hidden_by_dismiss"] == tri2["counts"]["cards"])


# ------------------------------------------------------------------ correction 4

def test_emerging_cluster_renders_through_a_visible_member_when_headline_dismissed(home, vault):
    """Correction 4 — an Emerging cluster card must locate its legacy display card through
    ANY member, not only the exact headline. When the headline family is dismissed the
    cluster survives via its other members, and the payload names a still-visible member to
    render through (display_tag) so the card is never blanked.

    SpotCMYK's headline is 'spotcmyk' (highest support). Dismissing THAT family leaves the
    cluster visible through cmyk/hero/parallax/splatter, whose legacy cards remain."""
    _seed_all(vault, home)
    garden = _reload(home)
    H = _handlers(garden, vault)

    # before: headline is visible, so no fallback tag is needed or emitted.
    card0 = _find(H["/api/garden/projects"]()["triage"]["sections"]["emerging"], "spotcmyk")
    assert card0 and card0["tag"] == "spotcmyk"
    assert "display_tag" not in card0, "a visible headline needs no fallback"

    # dismiss the HEADLINE family; the classifier still ranks it (a dismissal writes no node).
    _run(H["/api/garden/dismiss-project"]({"tag": "spotcmyk"}, _Req()))
    d = H["/api/garden/projects"]()
    legacy = {p["tag"] for p in d["projects"] if p.get("emerging")}
    assert "spotcmyk" not in legacy, "the dismissed headline has no legacy card"

    card = _find(d["triage"]["sections"]["emerging"], "cmyk")
    assert card, "the cluster must still render through a surviving member"
    assert card["tag"] == "spotcmyk", "the classifier headline is unchanged by a dismissal"
    dt = card.get("display_tag")
    assert dt and dt != "spotcmyk", "a non-headline display tag must be supplied"
    assert dt in card["members"], "the display tag is one of the cluster members"
    assert dt in legacy, "and it points at a legacy card the tab is still showing"
