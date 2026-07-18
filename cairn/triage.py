"""cairn/triage.py — emerging-topic triage: decide only what the evidence decides.

The Projects tab surfaces recurring tag-families that nobody declared a project. For two
revisions the question "is this a project?" was answered with thresholds — recurrence,
spread, decision-mass, scale — and two read-only calibration runs against a live vault
proved that ceiling: a tag with fourteen nodes and ZERO decisions was still crowned a
standalone project, purely on spread. Threshold tuning stopped there.

This module is the replacement, and its shape is a ruling rather than a heuristic:

    STAGE 1 — DETERMINISTIC. Emit a disposition ONLY where the evidence decides.
    STAGE 2 — NEEDS SEMANTIC REVIEW. Everything else, parked, EXCLUDED from the count.

Stage 1 never guesses. A gate that is not decisive routes the family to Stage 2, and that
is a success, not a failure — it is the honest answer to an undecidable question. So the
Emerging Projects count holds exactly two things: Stage-1 standalone candidates (§6) and
Stage-1 strong new-project clusters (§7.3). Nothing else is counted.

Read-only and pure. It computes and returns; it writes nothing — no vault node, no
registry row, no projects.json, no file. Nothing is filed until the owner confirms a
disposition, and that surface is not implemented here.

Real-time: Stage 1 reads only tags, kinds, sessions and timestamps, plus the identity,
vocabulary and name checks. No embeddings, no network, no completed sleep — unslept nodes
classify like any other. Stage 2's assists (embeddings, gists, a connected agent) are
optional and never required; with no assist available an entry simply stays unresolved.

Family identity is imported from garden rather than re-derived. book.py chose the other
way — mirroring garden's denylist to stay a leaf — and the two sets have since drifted
apart. A family here must be byte-identical to the card the owner already sees, so this
module imports the real `_family_key` and denylist instead of keeping a second copy.

Spec:      cairn-specs/SPEC-garden-emerging-triage-v1-FINAL.md (r6, two-stage)
Evidence:  cairn-specs/garden-emerging-r6-evidence/ (frozen calibration; not a runner)

    from cairn.triage import triage_data
    d = triage_data(vault)      # {"cards": [...], "counts": {...}}
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import cairn.garden as garden
from cairn import registry
from cairn.garden import _family_key, _is_machine_tag, _NOT_PROJECTS

# --- §15 constants. The read-only dry run is the calibration gate; these are its numbers.
S_MIN = 5                 # §5.1 candidate gate — minimum support
SIGMA_MIN = 0.15          # §5.1 candidate gate — minimum share
K = 3                     # §5.2 smoothing shrink
D_MARGIN = 1.0            # §5.3 accept margin
SHARE_DOM = 0.50          # §5.3 parent-share dominance
SHARE_SPEC = 0.40         # §5.3 specificity floor — required in BOTH branches
D_DOM = 2.0               # §5.3 lift margin needed to beat a dominant majority
DOM_HI = 0.70             # §5.4(b) dominance high band
METHOD_MIN = 0.30         # §5.5 skill routing
J_MIN = 0.30              # §7 edge threshold
J_ALIAS = 0.85            # §7.1 alias threshold
ALIAS_HI = 0.95           # §7.1 alias high band
D_MAX = 6                 # §7 hub-tag degree cap
ALIAS_EDIT = 2            # §7.1 levenshtein tolerance
CLUSTER_MIN = 3           # §7.3 new-project member bar
P2_MIN = 1                # §6 artifact-token bar
NAME_MIN = 4              # §8 minimum core length for a name check
MACH_MASS = 2             # §4 machinery conversation guard
MACH_ENT = 1
MACH_HI = 0.85
PROC_HI_MASS = 1          # §4 process high band
OVR_MASS = 3              # §4 evidence-override (process/machinery ONLY)
OVR_ENT = 2

# garden.py's structural-hygiene rule for what even surfaces as a family. Mirrored so this
# module's family set is identical to the card the owner sees — a real project spans TIME
# and CONVERSATIONS; one session's work-exhaust never qualifies however many nodes it drops.
FAM_MIN_NODES = 6
FAM_MIN_SESSIONS = 2
FAM_MIN_DAYS = 2

SUFFIXES = frozenset({"co", "corp", "inc", "llc", "ltd", "company", "group",
                      "studio", "studios", "lab", "labs"})
PROC_VOCAB = frozenset({"uncommitted", "not-committed", "committed-not-pushed", "read-only",
                        "built", "suite-green", "complete", "review", "ground-truth",
                        "verification"})
MODEL_IDS = frozenset({"opus", "opus-4-8", "sonnet", "haiku", "claude", "fable-5",
                       "singu", "sol", "gpt"})

# §6 / P1. registry.py's lifecycle: proposed|blessed|revived NOMINATE a project;
# passed|archived do not. nest() files a row under a parent WITHOUT touching status, so a
# status-only test would let a nested row nominate — the filing check is load-bearing.
P1_STATES = frozenset({"proposed", "blessed", "revived"})
P1_FILED = frozenset({"nest", "absorb"})

SKILLS_LABEL = "Skills & Frameworks"


# ---------------------------------------------------------------- name primitives (§3)

def _norm(x) -> str:
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


def _tokens(x) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", str(x).lower()) if t]


def _core(x) -> list:
    """tokens(x) minus trailing corporate suffixes — so wescott-co reduces to wescott."""
    t = _tokens(x)
    while t and t[-1] in SUFFIXES:
        t = t[:-1]
    return t


def _stem(x) -> str:
    s = _norm(x)
    return s[:-1] if s.endswith("s") and len(s) > 3 else s


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > ALIAS_EDIT:
        return ALIAS_EDIT + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _alias_name_ok(a: str, b: str) -> bool:
    """§7.1 name corroboration. High Jaccard ALONE is not synonymy: acorn/whistle and
    ai/lead-research both scored J=1.00 and are not the same thing."""
    if set(_tokens(a)) & set(_tokens(b)):
        return True
    na, nb = _norm(a), _norm(b)
    if na and nb and (na in nb or nb in na):
        return True
    if _stem(a) == _stem(b):
        return True
    return _lev(na, nb) <= ALIAS_EDIT


def _prefix_hint(tag: str):
    """§8.1. Does `tag` begin with a declared project's core? cairn-launch -> cairn.
    CORROBORATING child evidence only: it never files by itself and never makes a topic
    standalone. Reads garden.PROJECTS at call time so a reload is picked up."""
    tt = _tokens(tag)
    for p in garden.PROJECTS:
        cp = _core(p)
        if cp and len(tt) > len(cp) and tt[:len(cp)] == cp and len(_norm("".join(cp))) >= NAME_MIN:
            return p
    return None


def _collision_home(tag: str):
    """§8.2 name collision, equality-only after separator + corporate-suffix stripping.
    core("wescott") == core("wescott-co") -> fires. core("cairn-launch") != core("cairn")
    -> does not fire; that takes the §8.1 child-hint path instead."""
    ct = _core(tag)
    if len(_norm("".join(ct))) < NAME_MIN:
        return None
    for p, val in garden.PROJECTS.items():
        cands = {tuple(_core(p))}
        if val:
            cands.add(tuple(_core(val[0])))
        cands |= {tuple(_core(a)) for a in garden._project_match_tags(p)}
        if tuple(ct) in cands:
            return p
    return None


def _entity_is_declared(ent: str) -> bool:
    """§6/P2 — is this structured entity just a declared project under another name?

    `entity:cairn` is Cairn's OWN evidence, not evidence of an independent identity. This
    is the domain reasoning one level down: letting a declared project's entity nominate a
    standalone rebuilds exactly the bug that removed domains from P2. Attribution is by
    name / slug / alias equality (§8.2's core comparison), so an unrelated structured
    entity such as `entity:acme-widgets` still nominates."""
    name = ent.split(":", 1)[1] if ":" in ent else ent
    return _collision_home(name) is not None


def _project_name_guard(tag: str):
    """The §8 name evidence that PRECEDES P1/P2 and P3 promotion alike. Returns
    ("collision"|"prefix-hint", project) or None. A topic whose own name is evidence it
    belongs to — or is confusable with — an existing project is never standalone and never
    a new project; it goes to review carrying that evidence."""
    hit = _collision_home(tag)
    if hit:
        return ("collision", hit)
    hint = _prefix_hint(tag)
    if hint:
        return ("prefix-hint", hint)
    return None


# ---------------------------------------------------------------- identity + registry

def _identity_names() -> set:
    """§4 / S1.1, exact extraction. accounts.json is {"<handle>": {"label":…, "maker":…}}
    — the handles are the TOP-LEVEL KEYS, not a "handle" field. A reader that looked for a
    "handle" field returned the label instead and missed the key; the miss filed 72
    memories of pure identity noise as project work at high confidence."""
    handles = set()
    try:
        acc = json.loads((Path.home() / ".cairn" / "accounts.json").read_text(encoding="utf-8"))
    except Exception:
        acc = {}
    if isinstance(acc, dict):
        for k, v in acc.items():
            if isinstance(k, str):
                handles.add(k.lower())
            if isinstance(v, dict):
                for fld in ("label", "maker"):
                    if isinstance(v.get(fld), str):
                        handles.add(v[fld].lower())
    ident = handles | set(MODEL_IDS) | {"for-" + h for h in handles}
    return {_norm(i) for i in ident if i}


def _p1_slugs(vault) -> set:
    """§6 / P1 — slugs whose CURRENT registry row nominates a project.

    registry.rows() already folds the append-only ledger to the newest row per slug, so
    this reads current state rather than history: a slug proposed and later passed is not
    P1 evidence. A row filed under a parent is not a nomination either — it is the
    opposite — and nest() leaves status='proposed' while setting the parent, which is why
    the filing check cannot be skipped."""
    out = set()
    try:
        rows = registry.rows(vault)
    except Exception:
        return out
    for slug, st in (rows or {}).items():
        if not isinstance(st, dict):
            continue
        last = st.get("last_action")
        action = str((last or {}).get("action") or "").lower() if isinstance(last, dict) else ""
        status = str(st.get("status") or "").lower()
        filing = str(st.get("filing_mode") or "").lower()
        if status in P1_STATES and action not in P1_FILED and filing not in P1_FILED:
            out.add(slug)
    return out


# ---------------------------------------------------------------- the read-only index

class _Index:
    """One pass over active nodes plus the derived indexes every gate needs.

    Read-only by construction: it only ever SELECTs. Held for the life of one triage_data
    call — never cached across writes, since a new node can change any family's shape."""

    def __init__(self, vault):
        self.nodes = []
        # Tags, kinds, sessions, timestamps — nothing else. No node text is read: P2 is
        # `entity:` cotags only, so there is no domain/path/URL scan to feed (§6).
        for r in vault.conn.execute(
                "SELECT id, session, tags, kind, timestamp "
                "FROM nodes WHERE status='active'"):
            try:
                tags = [t for t in json.loads(r["tags"] or "[]") if isinstance(t, str)]
            except Exception:
                tags = []
            self.nodes.append({
                "id": r["id"], "session": r["session"] or "", "tags": tags,
                "kind": r["kind"] or "", "ts": r["timestamp"] or "",
            })
        self.by_id = {n["id"]: n for n in self.nodes}
        self.n_total = len(self.nodes)
        self.c0 = 10.0 / self.n_total if self.n_total else 0.0

        self.by_tag = defaultdict(list)
        for n in self.nodes:
            for t in n["tags"]:
                if not t.startswith("member:"):
                    self.by_tag[t].append(n)

        self.projects = dict(garden.PROJECTS)
        self.declared = set()
        for p in self.projects:
            self.declared.update(garden._project_match_tags(p))
        self.pnodes = {}
        for p in self.projects:
            ids = set()
            for mt in garden._project_match_tags(p):
                for n in self.by_tag.get(mt, []):
                    ids.add(n["id"])
            self.pnodes[p] = ids
        self.base = {p: (len(self.pnodes[p]) / self.n_total if self.n_total else 0.0)
                     for p in self.projects}
        self.skills = next((k for k, v in self.projects.items()
                            if v and str(v[0]).strip() == SKILLS_LABEL), "skills")

    def bstar(self, p) -> float:
        """§5.2 baseline floor — floors ONLY micro-projects (<10 nodes); cairn/skills untouched."""
        return max(self.base.get(p, 0.0), self.c0)

    def entities(self, ids) -> set:
        out = set()
        for i in ids:
            for t in self.by_id[i]["tags"]:
                if t.startswith("entity:"):
                    out.add(t)
        return out

    def artifacts(self, ids) -> set:
        """§6 / P2 — INDEPENDENT structured entity evidence. Two exclusions, one reason.

        Domains, URLs, repository paths and named deliverables are excluded outright: a
        naive domain rule fires constantly because in a Cairn vault every domain and path
        is Cairn's own, and it wrongly crowned fleet-child (mass 0), goose, routing and
        landing-page. Is this domain OURS or THIS TOPIC'S? — not decidable without
        semantic context, so it defers to P4.

        That same question survives INSIDE `entity:`, which is why §6 requires cotags
        "NOT attributable to a declared project". `entity:cairn` is Cairn's evidence; if
        it could nominate, the domain bug would simply reappear one level down."""
        return {e for e in self.entities(ids) if not _entity_is_declared(e)}

    def kinds(self, fam) -> Counter:
        return Counter(n["kind"] for n in fam["nodes"])

    def candidates(self, ids, n) -> list:
        """§5.1 gate + §5.2 baseline-anchored smoothed log-lift.

        The support/share gate — not the smoothing, not the floor — is the guard against
        thin-support over-attribution."""
        out = []
        for p in self.projects:
            s = len(ids & self.pnodes[p])
            if s == 0:
                continue
            share = s / n
            if s >= S_MIN and share >= SIGMA_MIN:
                bs = self.bstar(p)
                smoothed = (s + K * bs) / (n + K)
                out.append({"p": p, "s": s, "share": share,
                            "LL": math.log2(smoothed / bs) if bs > 0 else 0.0})
        return sorted(out, key=lambda x: -x["LL"])


def _families(idx: _Index) -> dict:
    """Group spelling variants into families, then apply garden's structural-hygiene rule
    so this module's family set matches the live card exactly."""
    raw = defaultdict(lambda: {"spellings": {}, "tags": []})
    for tag, rows in idx.by_tag.items():
        if tag in idx.declared or tag in _NOT_PROJECTS or _is_machine_tag(tag):
            continue
        key = _family_key(tag)
        if not key:
            continue
        f = raw[key]
        f["spellings"][tag] = f["spellings"].get(tag, 0) + len(rows)
        f["tags"].append(tag)

    fams = {}
    for key, f in raw.items():
        seen, union = set(), []
        for t in f["tags"]:
            for n in idx.by_tag.get(t, []):
                if n["id"] in seen:
                    continue
                seen.add(n["id"])
                union.append(n)
        sessions = {n["session"] for n in union if n["session"]}
        days = {n["ts"][:10] for n in union}
        if (len(union) < FAM_MIN_NODES or len(sessions) < FAM_MIN_SESSIONS
                or len(days) < FAM_MIN_DAYS):
            continue
        fams[key] = {
            "display": max(f["spellings"].items(), key=lambda kv: kv[1])[0],
            "tags": f["tags"], "nodes": union, "ids": seen,
            "sessions": sessions, "days": days,
        }
    return fams


# ---------------------------------------------------------------- bands (§5.4)

def _band_normal(ll, margin, s, single) -> str:
    if ll >= 3 and (single or margin >= 1.5) and s >= 8:
        return "high"
    if ll >= 2 and (single or margin >= 1.0) and s >= 5:
        return "medium"
    return "low"


def _band_dominance(share, ll, s) -> str:
    """§5.4(b). When the majority parent overrides the lift winner the lift margin points
    the WRONG way and must never be used — the band comes from the share separation."""
    if share >= DOM_HI and ll >= 2 and s >= 8:
        return "high"
    if share >= SHARE_DOM and ll >= 1 and s >= 5:
        return "medium"
    return "low"


def _home(cands: list):
    """§5.3 home selection + parent-share dominance safeguard.
    Returns (home, band, why) or (None, None, why) when nothing is decisive."""
    if not cands:
        return None, None, ""
    single = len(cands) == 1
    lift = cands[0]
    shr = max(cands, key=lambda x: x["share"])
    if lift["p"] == shr["p"]:
        margin = float("inf") if single else lift["LL"] - cands[1]["LL"]
        if margin >= D_MARGIN:
            why = "%.0f%% co-occurrence with %s (LL %.1f; %s)" % (
                lift["share"] * 100, lift["p"], lift["LL"],
                "single gated candidate" if single else "margin %.1f" % margin)
            return lift, _band_normal(lift["LL"], margin, lift["s"], single), why
        return None, None, "thin margin %.1f — not decisive" % margin
    maj, spec = shr, lift
    dd = spec["LL"] - maj["LL"]
    if maj["share"] >= SHARE_DOM:
        if spec["share"] >= SHARE_SPEC and dd >= D_DOM:
            return spec, _band_normal(spec["LL"], dd, spec["s"], False), "specificity override"
        why = "DOMINANCE: %s %.0f%% vs %s %.0f%% (band from share)" % (
            maj["p"], maj["share"] * 100, spec["p"], spec["share"] * 100)
        return maj, _band_dominance(maj["share"], maj["LL"], maj["s"]), why
    if spec["share"] >= SHARE_SPEC and dd >= D_MARGIN:
        return spec, _band_normal(spec["LL"], dd, spec["s"], False), "no majority; specificity holds mass"
    # SHARE_SPEC is required in BOTH branches — a tiny parent never wins on specificity
    # alone (30% vs 15% -> Stage 2, both contenders recorded).
    return None, None, "no majority and specificity below %.0f%% — both contenders recorded" % (SHARE_SPEC * 100)


# ---------------------------------------------------------------- Stage 1 gates

def _noise_gate(fam, idx: _Index):
    """§4 — identity, process vocabulary, machinery. Runs BEFORE affinity, because Cairn
    co-occurs with almost everything at session level."""
    tag = fam["display"]
    n = len(fam["nodes"])
    kc = idx.kinds(fam)
    ents = len(idx.entities(fam["ids"]))
    mass = kc["decision"] + kc["procedure"] + kc["resolved"] + kc["open_item"]
    override = mass >= OVR_MASS or ents >= OVR_ENT

    if _norm(tag) in _identity_names():
        # DEFINITIVE and override-EXEMPT: identity is never a project whatever its mass.
        return {"disposition": "non-project (identity)", "parent": None, "band": "high",
                "gate": "S1.1", "stage": 1,
                "reason": "accounts.json key/label/maker or model id — definitive, "
                          "override-exempt (mass %d ignored)" % mass}
    if tag in PROC_VOCAB:
        band = "low" if override else ("high" if (mass <= PROC_HI_MASS and ents == 0) else "medium")
        return {"disposition": "likely non-project — confirm" if override else "non-project (process)",
                "parent": None, "band": band, "gate": "S1.3", "stage": 1,
                "reason": "process vocabulary; mass=%d, entity=%d" % (mass, ents)}
    r_mach = (kc["context_stamp"] + kc["tool_call"]) / n
    r_conv = kc["conversation_turn"] / n
    guard = (kc["decision"] + kc["procedure"] + kc["resolved"]) <= MACH_MASS and ents <= MACH_ENT
    if (r_mach >= 0.70 or r_conv >= 0.70) and guard:
        ratio = max(r_mach, r_conv)
        band = "low" if override else ("high" if ratio >= MACH_HI else "medium")
        return {"disposition": "likely non-project — confirm" if override else "non-project (machinery)",
                "parent": None, "band": band, "gate": "S1.2", "stage": 1,
                "reason": "kind-ratio %.2f, conversation guard passed" % ratio}
    return None


def _affinity_gate(fam, idx: _Index):
    """§5 — project child / project-specific skill / global skill, accepted homes only."""
    n = len(fam["nodes"])
    cands = idx.candidates(fam["ids"], n)
    home, band, why = _home(cands)
    if not home or band not in ("high", "medium"):
        return None
    hint = _prefix_hint(fam["display"])
    if hint == home["p"] and band == "medium":
        # §8.1 — the prefix CORROBORATES an accepted home; may raise one step, capped high.
        band = "high"
        why += "; name-prefix '%s-' corroborates child-of-%s (band raised)" % (hint, hint)
    kc = idx.kinds(fam)
    method = (kc["procedure"] + kc["hypothesis"]) / n
    if home["p"] == idx.skills:
        disp = "global skill (absorb)"
    elif method >= METHOD_MIN:
        disp = "project-specific skill under %s" % home["p"]
    else:
        disp = "project child under %s" % home["p"]
    return {"disposition": disp, "parent": home["p"], "band": band, "gate": "S1.4",
            "stage": 1, "reason": why + "; method %.2f" % method}


def _classify_cluster(cid, members, ids, min_j, idx: _Index):
    """THE cluster classification and P3 promotion decision (§7).

    Gate order, exactly:
        S1.5a alias -> S1.5b existing-project children -> NAME GUARD -> S1.5c P3 -> Stage 2

    The name guard sits BEFORE promotion on purpose. An earlier revision tested positive
    evidence first and promoted `wescott` and `cairn-launch` to standalone — precisely
    what §8.1/§8.2 forbid — so collision and unconfirmed-prefix evidence overrides P1/P2
    and P3 alike. The tests drive THIS function; there is no second implementation.

    members: ordered list of (key, display, n), highest-support first.
    Returns {key: disposition-dict}; every member of a cluster shares one disposition."""
    keys = [k for k, _, _ in members]
    names = [d for _, d, _ in members]
    disp_of = {k: d for k, d, _ in members}
    n_of = {k: n for k, _, n in members}
    n_union = len(ids)
    artifacts = len(idx.artifacts(ids))

    def emit(**kw):
        return {k: dict(kw, tag=disp_of[k], n=n_of[k], cluster=cid) for k in keys}

    # --- §7.1 alias / compound: BOTH high J AND name corroboration
    if len(keys) == 2 and min_j >= J_ALIAS and _alias_name_ok(names[0], names[1]):
        return emit(disposition="alias/compound -> canonical '%s'" % names[0], parent=None,
                    band="high" if min_j >= ALIAS_HI else "medium", gate="S1.5a", stage=1,
                    reason="J=%.2f and name-corroborated" % min_j)

    # --- §7.2 existing-project children: the FULL §5 machinery on the cluster union
    home, band, _why = _home(idx.candidates(ids, n_union))
    if home and band in ("high", "medium"):
        return emit(disposition="cluster -> children of %s" % home["p"], parent=home["p"],
                    band=band, gate="S1.5b", stage=1,
                    reason="union (%d tags, %d memories) passes full gates to %s" % (
                        len(keys), n_union, home["p"]))

    # --- §8.1/§8.2 NAME PRECEDENCE — evaluated BEFORE the P3 promotion below.
    guards = [(nm, g) for nm, g in ((nm, _project_name_guard(nm)) for nm in names) if g]
    if guards:
        text = ", ".join("%s:%s->%s" % (nm, g[0], g[1]) for nm, g in guards)
        return emit(disposition="needs semantic review (name-guarded cluster)", parent=None,
                    band=None, gate="S2/name-guard", stage=2,
                    reason="cluster cannot use P3 new-project promotion because collision/"
                           "prefix precedence applies first: %s" % text)

    # --- §7.3 P3 new project
    if len(keys) >= CLUSTER_MIN or artifacts >= P2_MIN:
        return emit(disposition="NEW PROJECT cluster -> '%s'" % names[0], parent=None,
                    band="medium", gate="S1.5c", stage=1,
                    reason="strong cluster: %d tags (%s), union %d memories, artifacts=%d "
                           "(bar: members>=%d OR artifacts>=%d)" % (
                               len(keys), ", ".join(names), n_union, artifacts,
                               CLUSTER_MIN, P2_MIN))
    return emit(disposition="needs semantic review (weak cluster)", parent=None, band=None,
                gate="S2", stage=2,
                reason="cluster of %d tags (%s), union %d memories, artifacts=%d — below "
                       "the new-project bar" % (len(keys), ", ".join(names), n_union, artifacts))


def _clusters(pool, fams, idx: _Index) -> dict:
    """§7 graph: keep edge (A,B) iff J >= J_min AND shared >= 4; drop hub tags with degree
    > D_max; clusters are connected components. Single-linkage, never mutual-best-match —
    which fragmented SpotCMYK by dropping its own anchor."""
    edges = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a, b = fams[pool[i]]["ids"], fams[pool[j]]["ids"]
            union = len(a | b)
            shared = len(a & b)
            jj = shared / union if union else 0.0
            if jj >= J_MIN and shared >= 4:
                edges.append((pool[i], pool[j], jj))
    degree = Counter()
    for a, b, _ in edges:
        degree[a] += 1
        degree[b] += 1
    hubs = {t for t in pool if degree[t] > D_MAX}
    adj = defaultdict(set)
    for a, b, _ in edges:
        if a in hubs or b in hubs:
            continue
        adj[a].add(b)
        adj[b].add(a)

    seen, comps = set(), []
    for t in pool:
        if t in seen or t not in adj:
            continue
        stack, comp = [t], set()
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            seen.add(x)
            stack.extend(adj[x] - comp)
        if len(comp) > 1:
            # The secondary key is load-bearing, not tidiness. `comp` is a SET, and
            # Python randomizes str hashing per process, so on a node-count TIE a
            # count-only sort inherits set-iteration order and resolves differently on
            # every run: acorn/whistle (14 nodes each) swapped the card's headline
            # between refreshes, and splatter/parallax (9 each) reordered SpotCMYK's
            # members. Ties break on the family key so the card is reproducible.
            comps.append(sorted(comp, key=lambda k: (-len(fams[k]["nodes"]), k)))

    out = {}
    for ci, comp in enumerate(comps):
        pairs = [jj for a, b, jj in edges if a in comp and b in comp]
        min_j = min(pairs) if pairs else 0.0
        ids = set()
        for m in comp:
            ids |= fams[m]["ids"]
        members = [(m, fams[m]["display"], len(fams[m]["nodes"])) for m in comp]
        out.update(_classify_cluster("C%d" % ci, members, ids, min_j, idx))
    return out


def _standalone_gate(fam, idx: _Index, p1_slugs, key):
    """§6 — STANDALONE requires POSITIVE project evidence. Recurrence, spread, mass and
    scale are NOT evidence of standalone identity: they may corroborate a positive signal,
    they can never establish one.

    Precedence: collision -> prefix-hint-unconfirmed -> P1 -> P2 -> P3 -> P4 -> Stage 2.
    A name collision or an unconfirmed prefix hint OVERRIDES P1/P2 — a topic whose own
    name says it belongs to an existing project is never standalone."""
    tag = fam["display"]
    guard = _project_name_guard(tag)
    if guard and guard[0] == "collision":
        return {"disposition": "needs semantic review", "parent": None, "band": None,
                "gate": "S2/collision", "stage": 2,
                "reason": "name collision: core('%s') == core('%s') and '%s' is not an "
                          "accepted home — never standalone (overrides P1/P2)" % (
                              tag, guard[1], guard[1])}
    if guard and guard[0] == "prefix-hint":
        return {"disposition": "needs semantic review", "parent": None, "band": None,
                "gate": "S2/prefix-hint", "stage": 2,
                "reason": "name-prefix '%s-' is corroborating child-of-'%s' evidence, but no "
                          "accepted home confirms it — never standalone (overrides P1/P2)" % (
                              guard[1], guard[1])}
    arts = idx.artifacts(fam["ids"])
    p1 = registry.slugify(tag) in p1_slugs or key in p1_slugs
    p2 = len(arts) >= P2_MIN
    evidence = []
    if p1:
        evidence.append("P1 current registry nomination")
    if p2:
        evidence.append("P2 independent entity evidence %s" % sorted(arts)[:3])
    if evidence:
        return {"disposition": "STANDALONE candidate (keep Emerging)", "parent": None,
                "band": "medium", "gate": "S1.6", "stage": 1,
                "reason": "positive project evidence: " + "; ".join(evidence)}
    return {"disposition": "needs semantic review", "parent": None, "band": None,
            "gate": "S2", "stage": 2,
            "reason": "no positive project evidence (P1 registry=no, P2 independent "
                      "entities=no, P3 strong cluster=no, P4 semantic review=not run)"}


# ---------------------------------------------------------------- presentation (§10)

def _collapse_cards(res: dict, fams: dict) -> list:
    """A cluster contributes EXACTLY ONE card to every count and renders as one card
    listing its members.

    `n` is the count of UNIQUE union memories, never the sum of the members' counts.
    Cluster members share nodes BY CONSTRUCTION — that co-occurrence is the whole reason
    they clustered — so summing double-counts every shared node: five tags sharing six
    memories is a card of 6, not 30. The tests drive this function too."""
    cards = {}
    union = defaultdict(set)
    for key, v in res.items():
        ck = v.get("cluster") or key
        union[ck] |= fams[key]["ids"]
        if ck not in cards:
            card = dict(v)
            card["members"] = [v["tag"]]
            card["key"] = ck
            cards[ck] = card
        else:
            cards[ck]["members"].append(v["tag"])
    for ck, card in cards.items():
        card["n"] = len(union[ck])
    return sorted(cards.values(), key=lambda c: -c["n"])


def triage_data(vault) -> dict:
    """Classify every emerging tag-family. Read-only: returns a payload, writes nothing.

    {"cards": [{tag, members, disposition, parent, band, gate, stage, reason, n}, ...],
     "counts": {signals, cards, emerging, needs_review, stage1}}

    `emerging` counts ONLY Stage-1 standalone candidates and Stage-1 strong new-project
    clusters. Stage 2 is excluded from it by design."""
    idx = _Index(vault)
    fams = _families(idx)
    p1_slugs = _p1_slugs(vault)

    res = {}
    undecided = []
    for key, fam in fams.items():
        verdict = _noise_gate(fam, idx) or _affinity_gate(fam, idx)
        if verdict:
            res[key] = dict(verdict, tag=fam["display"], n=len(fam["nodes"]))
        else:
            undecided.append(key)

    res.update(_clusters(undecided, fams, idx))

    for key in undecided:
        if key in res:
            continue
        fam = fams[key]
        res[key] = dict(_standalone_gate(fam, idx, p1_slugs, key),
                        tag=fam["display"], n=len(fam["nodes"]))

    cards = _collapse_cards(res, fams)
    emerging = [c for c in cards
                if c["disposition"].startswith(("STANDALONE", "NEW PROJECT"))]
    needs_review = [c for c in cards if c["stage"] == 2]
    return {
        "cards": cards,
        "counts": {
            "signals": len(fams),
            "cards": len(cards),
            "emerging": len(emerging),
            "needs_review": len(needs_review),
            "stage1": len(cards) - len(needs_review),
        },
    }
