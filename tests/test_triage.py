"""Tests for cairn/triage.py — the r6 two-stage emerging classifier.

Mirrors the test_garden_projects.py harness (throwaway ~/.cairn, importlib.reload so the
module-global PROJECTS re-reads the temp home). No pytest-asyncio: triage is a pure
sync module, so its tests reach it directly the way test_garden_p3.py reaches book_data.

The three GUARD FIXTURES are the load-bearing ones. They exist because an earlier
revision evaluated positive evidence BEFORE the name guards and promoted `wescott` and
`cairn-launch` to standalone. Each guarded cluster below carries >= CLUSTER_MIN members,
so the P3 bar is MET and only the guard can stop it — a fixture whose cluster failed the
bar anyway would prove nothing. The SpotCMYK control pins the other side: the guard must
not over-block a legitimate new project.

    Run: python -m pytest tests/test_triage.py -q
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cairn.vault import Vault, MicroNode


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway ~/.cairn so projects.json / accounts.json never touch the real one."""
    h = tmp_path / "home"
    (h / ".cairn").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _seed_projects(home: Path, extra=None):
    """Declare projects the way the dashboard does. `wescott-co` gives §8.2 an equality
    collision to find (core drops the corporate suffix); `cairn` gives §8.1 a prefix."""
    d = {
        "cairn": ["Cairn", "the memory system itself"],
        "wescott-co": ["Wescott & Co.", "the hallmark identifier"],
        "skills": ["Skills & Frameworks", "the session infrastructure"],
    }
    if extra:
        d.update(extra)
    (home / ".cairn" / "projects.json").write_text(json.dumps(d), encoding="utf-8")


def _reload(home: Path):
    """Reload garden (rebinds its import-time PROJECTS global against the temp home) and
    then triage, so triage reads the reloaded garden."""
    import cairn.garden as garden
    importlib.reload(garden)
    import cairn.triage as triage
    importlib.reload(triage)
    return triage


def _n(vault, tags, kind="insight", n=6, sessions=2):
    """Write n nodes each carrying ALL of `tags`, spread over `sessions` sessions and 2
    days — genuine families must clear garden's structural hygiene (>=6 nodes, >=2
    sessions, >=2 days). Because every node carries every tag, each pair of families
    shares every node: J=1.0 and shared=n, a genuinely cohesive cluster."""
    base = datetime.now(timezone.utc)
    for i in range(n):
        vault.write(MicroNode(session="s%d" % (i % sessions), kind=kind,
                              query="%s node %d" % (tags[0], i), model="test",
                              tags=list(tags),
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))


def _mixed(vault, tag, kinds):
    """Write one node per entry in `kinds`, spread over 2 sessions and 2 days — for the
    §4 gates, where the KIND MIX is the thing under test."""
    base = datetime.now(timezone.utc)
    for i, k in enumerate(kinds):
        vault.write(MicroNode(session="s%d" % (i % 2), kind=k, model="test",
                              query="%s %d" % (tag, i), tags=[tag],
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))


def _card(data, tag):
    for c in data["cards"]:
        if c["tag"] == tag or tag in c["members"]:
            return c
    return None


# ------------------------------------------------------------------ GUARD FIXTURE 1

def test_cairn_prefix_cluster_cannot_bypass_guard_via_p3(home, vault):
    """A cohesive cluster CONTAINING a cairn-* family must reach Stage 2 through the
    prefix guard — it must not become a NEW PROJECT through P3."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["fixture-alpha", "cairn-fixture-beta", "fixture-gamma"])

    data = triage.triage_data(vault)
    card = _card(data, "cairn-fixture-beta")

    assert card is not None
    assert len(card["members"]) >= triage.CLUSTER_MIN, "P3 bar must be MET, else this proves nothing"
    assert card["stage"] == 2
    assert card["gate"] == "S2/name-guard"
    assert "prefix-hint->cairn" in card["reason"]
    assert data["counts"]["emerging"] == 0


# ------------------------------------------------------------------ GUARD FIXTURE 2

def test_wescott_collision_cluster_cannot_bypass_guard_via_p3(home, vault):
    """A cohesive cluster CONTAINING a name-collision family must reach Stage 2 through
    the collision guard, not become a NEW PROJECT through P3."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["fixture-delta", "wescott", "fixture-epsilon"])

    data = triage.triage_data(vault)
    card = _card(data, "wescott")

    assert card is not None
    assert len(card["members"]) >= triage.CLUSTER_MIN, "P3 bar must be MET, else this proves nothing"
    assert card["stage"] == 2
    assert card["gate"] == "S2/name-guard"
    assert "collision->wescott-co" in card["reason"]
    assert data["counts"]["emerging"] == 0


# ------------------------------------------------------------------ GUARD FIXTURE 3 (control)

def test_spotcmyk_control_still_promotes(home, vault):
    """CONTROL: a SpotCMYK-shaped cluster with no name guard must STILL become exactly ONE
    Stage-1 NEW PROJECT card. This is what proves the guards do not over-block."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["spotcmyk", "cmyk", "splatter", "parallax", "hero"])

    data = triage.triage_data(vault)
    card = _card(data, "spotcmyk")

    assert card is not None
    assert card["stage"] == 1
    assert card["gate"] == "S1.5c"
    assert card["disposition"].startswith("NEW PROJECT")
    assert sorted(card["members"]) == ["cmyk", "hero", "parallax", "splatter", "spotcmyk"]
    assert data["counts"]["emerging"] == 1, "a cluster counts exactly ONCE"
    assert data["counts"]["cards"] == 1


# ------------------------------------------------------------------ P1 registry state

def test_p1_accepts_only_current_unfiled_nominations(home, vault):
    """§6/P1 — proposed|blessed|revived nominate; passed|archived do not; and a NESTED row
    does not, even though nest() leaves status='proposed'. That last one is the trap a
    status-only filter walks into."""
    _seed_projects(home)
    triage = _reload(home)
    from cairn import registry

    registry.propose(vault, "Alpha Thing")
    registry.propose(vault, "Beta Thing")
    registry.act(vault, "beta-thing", "pass")
    registry.propose(vault, "Gamma Thing")
    registry.nest(vault, "gamma-thing", "cairn")
    registry.propose(vault, "Delta Thing")
    registry.act(vault, "delta-thing", "bless")

    slugs = triage._p1_slugs(vault)

    assert "alpha-thing" in slugs, "a live proposal nominates"
    assert "delta-thing" in slugs, "blessed nominates"
    assert "beta-thing" not in slugs, "passed is not a nomination"
    assert "gamma-thing" not in slugs, "a NESTED row is filed under a parent — not a nomination"


def test_p1_revived_nominates_and_archived_does_not(home, vault):
    _seed_projects(home)
    triage = _reload(home)
    from cairn import registry

    registry.propose(vault, "Ghost Thing")
    registry.act(vault, "ghost-thing", "archive")
    assert "ghost-thing" not in triage._p1_slugs(vault)

    registry.act(vault, "ghost-thing", "revive")
    assert "ghost-thing" in triage._p1_slugs(vault), "newest row wins — revived nominates"


# ------------------------------------------------------------------ P2 is entity-only

def test_p2_fires_on_entity_cotag(home, vault):
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["lonetopic", "entity:acme-widgets"])

    card = _card(triage.triage_data(vault), "lonetopic")
    assert card["stage"] == 1
    assert card["gate"] == "S1.6"
    assert card["disposition"].startswith("STANDALONE")


def test_p2_does_not_fire_on_a_domain_in_the_text(home, vault):
    """§6 — domains/paths/URLs/deliverables are NOT deterministic P2 evidence. In a Cairn
    vault every domain is Cairn's own, so a domain mention is Cairn evidence, not evidence
    of independent identity. It needs attribution -> deferred to P4 -> Stage 2."""
    _seed_projects(home)
    triage = _reload(home)
    base = datetime.now(timezone.utc)
    for i in range(8):
        vault.write(MicroNode(session="s%d" % (i % 2), kind="decision", model="test",
                              query="domaintopic ships at cairnmemory.com see https://x.io",
                              tags=["domaintopic"],
                              timestamp=(base - timedelta(days=i % 2)).isoformat()))

    card = _card(triage.triage_data(vault), "domaintopic")
    assert card["stage"] == 2, "a domain in the text is not independent-identity evidence"
    assert triage.triage_data(vault)["counts"]["emerging"] == 0


# ------------------------------------------------------------------ positive evidence only

def test_recurrence_and_scale_alone_never_reach_standalone(home, vault):
    """The fleet-child pin: high recurrence and spread, ZERO decisions, no entity, no
    registry row -> Stage 2. Recurrence, spread, mass and scale are not evidence of
    standalone identity."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["fleetchild"], n=14, sessions=7)

    data = triage.triage_data(vault)
    card = _card(data, "fleetchild")
    assert card["stage"] == 2
    assert card["gate"] == "S2"
    assert data["counts"]["emerging"] == 0


# ------------------------------------------------------------------ §8 name evidence

def test_collision_is_equality_only(home, vault):
    """core("wescott") == core("wescott-co") fires; core("cairn-launch") != core("cairn")
    does not — cairn-launch takes the §8.1 child-hint path instead."""
    _seed_projects(home)
    triage = _reload(home)

    assert triage._collision_home("wescott") == "wescott-co"
    assert triage._collision_home("cairn-launch") is None
    assert triage._prefix_hint("cairn-launch") == "cairn"
    assert triage._project_name_guard("wescott") == ("collision", "wescott-co")
    assert triage._project_name_guard("cairn-launch") == ("prefix-hint", "cairn")


def test_unconfirmed_prefix_hint_goes_to_stage_2_not_standalone(home, vault):
    """§8.1 — a cairn-* family with no accepted home carries the hint into review. It is
    never standalone and never auto-filed."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["cairn-launch", "entity:some-vendor"])

    card = _card(triage.triage_data(vault), "cairn-launch")
    assert card["stage"] == 2
    assert card["gate"] == "S2/prefix-hint"
    assert "overrides P1/P2" in card["reason"], "the hint must beat the P2 entity evidence"


def test_prefix_corroborates_an_accepted_home(home, vault):
    """§8.1 — with an accepted cairn home the prefix RAISES the band one step (capped
    high) and the family files as a child. Corroboration, never decision."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["cairn", "cairn-launch"], n=10)
    for i in range(40):        # ballast: keeps base(cairn) small enough to score a lift
        _n(vault, ["filler-%d" % i], n=1)

    card = _card(triage.triage_data(vault), "cairn-launch")
    assert card["stage"] == 1
    assert card["gate"] == "S1.4"
    assert card["parent"] == "cairn"
    assert card["band"] == "high"
    assert "corroborates" in card["reason"]


# ------------------------------------------------------------------ §4 identity

def test_identity_uses_top_level_keys_and_is_override_exempt(home, vault):
    """§4/S1.1 — accounts.json handles are the TOP-LEVEL KEYS. A `handle`-field reader
    returns the label and misses the key, and that miss filed 72 memories of identity
    noise as project work at high band. Identity never passes the evidence override."""
    (home / ".cairn" / "accounts.json").write_text(
        json.dumps({"slapperme": {"label": "Slappered me", "maker": "Claude"}}),
        encoding="utf-8")
    _seed_projects(home)
    triage = _reload(home)
    # Heavy decision mass + entity cotags: an override-eligible family, if it were eligible.
    _n(vault, ["slapperme", "entity:acme", "entity:beta"], kind="decision", n=9)

    card = _card(triage.triage_data(vault), "slapperme")
    assert card["stage"] == 1
    assert card["gate"] == "S1.1"
    assert card["band"] == "high"
    assert card["disposition"] == "non-project (identity)"


# ------------------------------------------------------------------ stage separation

def test_stage_two_is_excluded_from_the_emerging_count(home, vault):
    """§2 — the Emerging count holds ONLY Stage-1 standalone + strong new-project
    clusters. Every family lands in exactly one disposition."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["spotcmyk", "cmyk", "splatter", "parallax", "hero"])   # -> 1 new project
    _n(vault, ["fleetchild"], n=14, sessions=7)                       # -> Stage 2

    data = triage.triage_data(vault)
    counts = data["counts"]
    assert counts["emerging"] == 1
    assert counts["needs_review"] >= 1
    assert counts["cards"] == counts["stage1"] + counts["needs_review"]
    for c in data["cards"]:
        assert c["stage"] in (1, 2)
        assert c["disposition"]


def test_triage_writes_nothing(home, vault):
    """Presentation-only: the classifier computes and returns. No node, no registry row."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["spotcmyk", "cmyk", "splatter"])
    before = vault.conn.execute("SELECT count(*) FROM nodes").fetchone()[0]

    triage.triage_data(vault)

    after = vault.conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    assert after == before


# ------------------------------------------------------------------ P2 attribution

def test_p2_rejects_an_entity_attributable_to_a_declared_project(home, vault):
    """§6 — `entity:cairn` is CAIRN's evidence, not evidence of an independent identity.
    If a declared project's own entity could nominate, the domain bug r6 removed would
    simply reappear one level down."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["sometopic", "entity:cairn"])

    assert triage._entity_is_declared("entity:cairn") is True
    card = _card(triage.triage_data(vault), "sometopic")
    assert card["stage"] == 2
    assert card["gate"] == "S2"
    assert triage.triage_data(vault)["counts"]["emerging"] == 0


def test_p2_still_accepts_an_unrelated_entity(home, vault):
    """The other side of the same rule: an entity NOT attributable to any declared
    project remains real independent evidence and still nominates."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["sometopic", "entity:acme-widgets"])

    assert triage._entity_is_declared("entity:acme-widgets") is False
    card = _card(triage.triage_data(vault), "sometopic")
    assert card["stage"] == 1
    assert card["gate"] == "S1.6"
    assert card["disposition"].startswith("STANDALONE")


# ------------------------------------------------------------------ cluster card arithmetic

def test_cluster_card_n_is_the_unique_union_not_the_sum(home, vault):
    """§7 — cluster members share nodes BY CONSTRUCTION; that co-occurrence is why they
    clustered. Summing member counts double-counts every shared node."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["spotcmyk", "cmyk", "splatter", "parallax", "hero"], n=6)

    card = _card(triage.triage_data(vault), "spotcmyk")
    assert len(card["members"]) == 5
    assert card["n"] == 6, "five tags sharing six memories is a card of 6, not 30"


# ------------------------------------------------------------------ §7.1 alias corroboration

def test_uncorroborated_high_j_pair_is_not_aliased(home, vault):
    """§7.1 — acorn/whistle scored J=1.00 and are not synonyms. High Jaccard ALONE never
    aliases; without name corroboration the pair falls to Stage 2."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["acorn", "whistle"])

    assert triage._alias_name_ok("acorn", "whistle") is False
    card = _card(triage.triage_data(vault), "acorn")
    assert card["gate"] != "S1.5a"
    assert card["stage"] == 2
    assert triage.triage_data(vault)["counts"]["emerging"] == 0


def test_corroborated_high_j_pair_is_aliased(home, vault):
    """The control for the rule above: same J, but the names corroborate, so it aliases."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["listing-outlaw", "listingoutlaw"])

    assert triage._alias_name_ok("listing-outlaw", "listingoutlaw") is True
    card = _card(triage.triage_data(vault), "listing-outlaw")
    assert card["gate"] == "S1.5a"
    assert card["stage"] == 1
    assert card["band"] == "high"
    assert card["disposition"].startswith("alias/compound")


# ------------------------------------------------------------------ §5.3 home selection

def test_home_unresolved_when_no_majority_and_specificity_below_floor(home, vault):
    """§5.3 — the 30%/15%-shaped case. Neither contender holds a majority and the lift
    winner is below the specificity floor, so NOTHING is decisive: both contenders are
    recorded and the family goes to review. SHARE_SPEC is required in BOTH branches
    precisely so a tiny parent cannot win on specificity alone."""
    _seed_projects(home)
    triage = _reload(home)
    cands = [{"p": "smallproj", "s": 5, "share": 0.25, "LL": 0.93},
             {"p": "bigproj", "s": 6, "share": 0.30, "LL": -1.15}]

    hit, band, why = triage._home(cands)
    assert hit is None
    assert band is None
    assert "both contenders recorded" in why


def test_home_dominance_uses_share_never_the_lift_margin(home, vault):
    """§5.4(b) — when the majority parent overrides the lift winner, the lift margin
    points the WRONG way and must never set the band. The band comes from the share."""
    _seed_projects(home)
    triage = _reload(home)
    cands = [{"p": "spec", "s": 5, "share": 0.40, "LL": 3.0},
             {"p": "maj", "s": 9, "share": 0.70, "LL": 2.0}]

    hit, band, why = triage._home(cands)
    assert hit["p"] == "maj", "the 70% majority takes it, not the lift winner"
    assert band == "high"
    assert "DOMINANCE" in why


def test_home_specificity_can_override_a_dominant_majority(home, vault):
    """§5.3 — a big enough lift margin (>= D_DOM) with the specificity floor met beats
    even a dominant majority."""
    _seed_projects(home)
    triage = _reload(home)
    cands = [{"p": "spec", "s": 8, "share": 0.45, "LL": 5.0},
             {"p": "maj", "s": 10, "share": 0.60, "LL": 2.0}]

    hit, band, why = triage._home(cands)
    assert hit["p"] == "spec"
    assert "specificity override" in why


# ------------------------------------------------------------------ §4 evidence override

def test_process_gate_is_high_without_evidence(home, vault):
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["review"], kind="insight", n=6)          # mass 0, entities 0

    card = _card(triage.triage_data(vault), "review")
    assert card["gate"] == "S1.3"
    assert card["band"] == "high"
    assert card["disposition"] == "non-project (process)"


def test_process_evidence_override_demotes_to_confirm(home, vault):
    """§4 — real decision mass does not make process vocabulary a project, but it does
    stop the gate from being confident: the verdict drops to a low-band confirm."""
    _seed_projects(home)
    triage = _reload(home)
    _mixed(vault, "review", ["decision"] * 6)           # mass 6 >= OVR_MASS

    card = _card(triage.triage_data(vault), "review")
    assert card["gate"] == "S1.3"
    assert card["band"] == "low"
    assert card["disposition"] == "likely non-project — confirm"


def test_machinery_gate_is_high_without_evidence(home, vault):
    _seed_projects(home)
    triage = _reload(home)
    _mixed(vault, "toolnoise", ["tool_call"] * 6)       # kind-ratio 1.00

    card = _card(triage.triage_data(vault), "toolnoise")
    assert card["gate"] == "S1.2"
    assert card["band"] == "high"
    assert card["disposition"] == "non-project (machinery)"


def test_machinery_evidence_override_demotes_to_confirm(home, vault):
    """The conversation guard still passes (decision+procedure+resolved = 2 <= MACH_MASS),
    but total mass including open_item reaches OVR_MASS, so the band drops to a confirm."""
    _seed_projects(home)
    triage = _reload(home)
    _mixed(vault, "toolnoise", ["tool_call"] * 7 + ["decision"] * 2 + ["open_item"])

    card = _card(triage.triage_data(vault), "toolnoise")
    assert card["gate"] == "S1.2"
    assert card["band"] == "low"
    assert card["disposition"] == "likely non-project — confirm"


# ------------------------------------------------------------------ §3 family union

def test_spelling_variants_form_one_family_with_a_deduped_union(home, vault):
    """§3 — spelling variants are ONE family: display is the most-frequent spelling and
    the union is de-duped by node id, so a node carrying both spellings counts once."""
    _seed_projects(home)
    triage = _reload(home)
    _n(vault, ["widget"], n=5)
    _n(vault, ["widgets"], n=3)
    _n(vault, ["widget", "widgets"], n=2)               # must count ONCE

    data = triage.triage_data(vault)
    card = _card(data, "widget")
    assert data["counts"]["signals"] == 1, "widget + widgets are one family, not two"
    assert card["tag"] == "widget", "display = most-frequent spelling (7 rows vs 5)"
    assert card["n"] == 10, "union de-duped by id: 5 + 3 + 2 = 10, not 12"
