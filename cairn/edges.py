"""
cairn/edges.py — the connective tissue.

Materializes the vault's graph structure into the `edges` table instead of
recomputing pairwise similarity on every dashboard request:

  chain     — parent lineage (how reasoning actually flowed)
  dendrite  — consolidation membership (synthesis -> its episodes)
  semantic  — embedding kNN, tiered by similarity:
                strong >= 0.78   (same thought, different day)
                medium >= 0.70   (sub-context, related work)
                weak   >= 0.62   (distant relation, faint echo)

kNN instead of threshold-all-pairs is the hairball fix: a threshold gives
every same-topic conversation a dense clique (imported backfill turns are
near-identical to their neighbors); top-k gives each node at most k best
edges, so structure emerges instead of fog.

On top of the edge graph: label propagation (self-built, charter: no deps)
discovers topic communities across sessions, and a tf-idf pass over member
gists names them. Result lands in nodes.community as 'c<n>|<label>'.

Everything here is DERIVED data — safe to wipe and rebuild from nodes at any
time. The append-only invariant lives in nodes; edges is a cache of structure.

numpy is used when available (it ships with the embedder); a pure-Python
fallback keeps small vaults working without it.
"""
from __future__ import annotations

import json
import math
import re
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

from cairn.vault import Vault
from cairn.accounts import galaxy_label

DIM         = 384      # all-MiniLM-L6-v2
KNN_K       = 6        # best neighbors kept per node
TIER_STRONG = 0.78
TIER_MEDIUM = 0.70
TIER_WEAK   = 0.62     # floor — below this, no edge at all

# community detection runs over edges that carry real signal; weak semantic
# edges are lenses for the eye, not structure for the algorithm
COMMUNITY_EDGE_TIERS = ("strong", "medium")
MIN_COMMUNITY_SIZE   = 4
MAX_LP_ITERATIONS    = 12

# Super-hub guard for community detection. A cross-topic consolidation/synthesis
# node (e.g. a "[consolidated x… across … sessions]" whole-background summary)
# carries dendrite/semantic edges to episodes from dozens of unrelated projects.
# Left in, its single label floods label propagation and collapses them all into
# one monster community. Nodes past the cut are dropped from DETECTION ONLY —
# every edge is kept for retrieval + drift; the node just can't dictate everyone's
# topic, and it lands community=NULL (honest: a whole-background node has no one
# topic). Small/normal vaults never trip this — the cut is deliberately extreme.
HUB_MIN_GRAPH  = 400    # don't hunt hubs in tiny graphs
HUB_ABS_FLOOR  = 250    # this many community edges is already a lot
HUB_FRAC       = 0.025  # …or wired to > 2.5% of the whole graph
HUB_MED_MULT   = 8      # …and a clear degree outlier (× median)

_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are",
    "was", "were", "not", "but", "have", "has", "had", "can", "could",
    "would", "should", "will", "its", "it's", "into", "from", "out",
    "about", "what", "when", "where", "how", "why", "who", "all", "any",
    "than", "then", "them", "they", "there", "their", "these", "those",
    "just", "like", "get", "got", "one", "two", "use", "used", "using",
    "now", "new", "way", "also", "more", "some", "want", "wants", "need",
    "needs", "let", "lets", "see", "say", "says", "said", "yes", "yeah",
    "okay", "right", "think", "thing", "things", "make", "makes", "made",
    "really", "still", "here", "over", "back", "user", "agent", "turn",
    "session", "node", "nodes", "doesn", "don", "didn", "isn", "wasn",
    "i'm", "you're", "it's", "that's", "there's", "what's", "let's",
    "i've", "you've", "we're", "they're", "can't", "won't", "don't",
    "good", "great", "fair", "sure", "well", "actually", "basically",
    "look", "looking", "looks", "going", "gonna", "come", "comes",
    "know", "knows", "knew", "yeah", "okay", "anything", "everything",
    "something", "nothing", "anybody", "everybody", "someone", "anyone",
    "else", "asking", "asked", "tell", "tells", "told", "give", "gives",
    "take", "takes", "much", "many", "very", "even", "only", "first",
    "last", "next", "before", "after", "because", "while", "every",
    "each", "other", "another", "same", "different", "supported",
    "current", "check", "checked", "people", "person", "time", "times",
    "day", "days", "today", "thanks", "thank", "please", "maybe",
    "probably", "definitely", "exactly", "literally", "totally",
    # conversational filler / weak connectives
    "that's", "here's", "there's", "it's", "what's", "let's",
    "able", "need", "needed", "keeps", "keep", "doing", "done",
    "lot", "bit", "bit", "bit", "kinda", "sorta", "though", "already",
    "via", "per", "add", "added", "adds", "few", "own", "seem",
    "seems", "seemed", "across", "around", "against", "between",
    "within", "without", "since", "until", "once", "whether", "both",
    "either", "neither", "always", "never", "often", "usually",
    "however", "therefore", "thus", "hence", "instead", "otherwise",
    "actually", "simply", "mostly", "mostly", "fairly", "quite",
    "almost", "often", "part", "parts", "call", "calls", "called",
    "put", "puts", "try", "tries", "tried", "show", "shows", "shown",
    "found", "find", "finds", "work", "works", "worked", "working",
    "move", "moves", "moved", "point", "points", "case", "cases",
    "able", "again", "along", "off", "too", "up", "down", "with",
    "into", "onto", "upon", "out", "and", "not", "but", "yet", "nor",
    "the", "its", "our", "their", "his", "her", "mine", "your",
    "been", "being", "had", "have", "has", "will", "was", "were",
    # modal/auxiliary verb forms that produce label noise
    "does", "did", "doing", "done", "does", "gets", "got", "get",
    "puts", "put", "runs", "ran", "run", "goes", "went", "gone",
    "came", "come", "comes", "through", "off", "the",
    "means", "mean", "meant", "very", "just", "only", "also",
    "however", "therefore", "thus", "since", "though", "although",
    "while", "when", "where", "which", "then", "than", "that", "this",
}

MEANING_KINDS = {"decision", "warning", "insight", "idea", "open_item",
                 "procedure", "resolved", "hypothesis", "question", "blocker"}


def _tier(sim: float) -> str:
    if sim >= TIER_STRONG:
        return "strong"
    if sim >= TIER_MEDIUM:
        return "medium"
    return "weak"


def _decode(blob) -> Optional[tuple]:
    try:
        if blob is None or len(blob) != DIM * 4:
            return None
        return struct.unpack(f"{DIM}f", blob)
    except (struct.error, TypeError):
        return None


# ── semantic kNN ───────────────────────────────────────────────────────────────

def _knn_numpy(ids: list[str], vecs, k: int) -> dict:
    """Block-wise top-k cosine. Returns {(a,b) canonical: sim}."""
    import numpy as np
    mat = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    pairs: dict[tuple[str, str], float] = {}
    n = len(ids)
    BLOCK = 512
    for start in range(0, n, BLOCK):
        block = mat[start:start + BLOCK]
        sims = block @ mat.T                      # (block, n)
        for bi in range(block.shape[0]):
            i = start + bi
            row = sims[bi]
            row[i] = -1.0                          # no self-edges
            kk = min(k, n - 1)
            if kk <= 0:
                continue
            top = np.argpartition(row, -kk)[-kk:]
            for j in top:
                sim = float(row[j])
                if sim < TIER_WEAK:
                    continue
                a, b = (ids[i], ids[j]) if ids[i] < ids[j] else (ids[j], ids[i])
                if pairs.get((a, b), -1.0) < sim:
                    pairs[(a, b)] = sim
    return pairs


def _knn_pure(ids: list[str], vecs: list[tuple], k: int) -> dict:
    """No-numpy fallback. O(N^2) — fine for small vaults, slow past ~2k nodes."""
    norms = [math.sqrt(sum(x * x for x in v)) or 1.0 for v in vecs]
    pairs: dict[tuple[str, str], float] = {}
    n = len(ids)
    for i in range(n):
        sims = []
        vi, ni = vecs[i], norms[i]
        for j in range(n):
            if i == j:
                continue
            s = sum(a * b for a, b in zip(vi, vecs[j])) / (ni * norms[j])
            if s >= TIER_WEAK:
                sims.append((s, j))
        sims.sort(reverse=True)
        for s, j in sims[:k]:
            a, b = (ids[i], ids[j]) if ids[i] < ids[j] else (ids[j], ids[i])
            if pairs.get((a, b), -1.0) < s:
                pairs[(a, b)] = s
    return pairs


# ── entity edges (the HippoRAG bridge) ──────────────────────────────────────
# Links nodes that share a SPECIFIC named entity, so same-topic claims sitting
# just below the semantic floor still connect — entities rescue the topical
# links cosine misses, within a topic AND across 'solar systems'. False bridges
# are held off by a stoplist, a STAR (not all-pairs) so a recurring entity stays
# linear, and a proportional session cap (an entity spanning too much of the
# corpus is generic). Always on — a no-op until distilled claims (which carry
# entity: tags) exist, so a raw-only vault's edge set is unchanged.
_ENTITY_STOP = {"user", "agent", "cairn", "system", "ai", "model", "claude",
                "gpt", "gemini", "it", "this", "that", "thing", "things",
                "project", "app", "code", "session", "node", "nodes", "data",
                "file", "files", "tool", "tools", "human", "you", "stuff"}

def _canon_entity(e: str) -> str:
    return " ".join((e or "").lower().split())

def _entity_edges(rows, now, max_session_frac: float = 0.25, max_pairs: int = 4000):
    """Build entity-bridge edges from nodes' `entity:<name>` tags — the HippoRAG
    bridge that rescues topical links cosine misses (within a topic AND across
    'solar systems'). Returns (src, dst, 'entity', None, 1.0, now) tuples. Two
    guards keep it a clean web instead of a hairball:
      • STAR, not all-pairs: each entity's claims hang off ONE hub claim, so a
        recurring entity (e.g. a 50-convo project) stays LINEAR (n-1 edges), never
        quadratic — and whenever the hub and a leaf are different conversations,
        that edge IS a cross-conversation bridge.
      • PROPORTIONAL generic guard: an entity spanning more than max_session_frac
        of all conversations is too broad to mean a topic, so it's skipped. This
        scales with the corpus, so a real big project bridges but a generic term
        spread across everything does not."""
    from collections import defaultdict
    ent_nodes = defaultdict(list)        # entity -> [claim ids] (first seen = hub)
    ent_sess  = defaultdict(set)         # entity -> {sessions it appears in}
    for r in rows:
        tags = r["tags"] or "[]"
        if "entity:" not in tags:
            continue
        try:
            seen = set()
            for t in json.loads(tags):
                if isinstance(t, str) and t.startswith("entity:"):
                    e = _canon_entity(t[7:])
                    if len(e) >= 3 and e not in _ENTITY_STOP and e not in seen:
                        ent_nodes[e].append(r["id"])
                        ent_sess[e].add(r["session"])
                        seen.add(e)
        except Exception:
            pass
    n_sess = len({r["session"] for r in rows}) or 1
    session_cap = max(2, int(max_session_frac * n_sess))
    out, pairs = [], 0
    for e, nids in ent_nodes.items():
        nids = list(dict.fromkeys(nids))
        if len(nids) < 2:                    # need >=2 claims sharing it to link
            continue
        if len(ent_sess[e]) > session_cap:   # spans too much of the corpus → generic, skip
            continue
        hub = nids[0]                         # STAR: all claims hang off one hub (linear)
        for other in nids[1:]:
            out.append((hub, other, "entity", None, 1.0, now))
            pairs += 1
            if pairs >= max_pairs:
                return out
    return out


# ── the build ─────────────────────────────────────────────────────────────────

def build_edges(vault: Optional[Vault] = None, k: int = KNN_K) -> dict:
    """
    Rebuild the edges table from scratch: chain + dendrite + semantic kNN.
    Idempotent — derived data, wiped and recomputed each run.
    """
    v = vault or Vault()
    rows = v.conn.execute("""
        SELECT id, parent, tags, embedding, session FROM nodes
        WHERE status = 'active'
    """).fetchall()
    id_set = {r["id"] for r in rows}
    now = datetime.now(timezone.utc).isoformat()

    edges: list[tuple] = []   # (src, dst, type, tier, weight, created_at)

    for r in rows:
        if r["parent"] and r["parent"] in id_set:
            edges.append((r["parent"], r["id"], "chain", None, 1.0, now))
        tags = r["tags"] or "[]"
        if "member:" in tags:
            try:
                for t in json.loads(tags):
                    if isinstance(t, str) and t.startswith("member:"):
                        m = t.split(":", 1)[1]
                        if m in id_set:
                            edges.append((r["id"], m, "dendrite", None, 1.0, now))
            except Exception:
                pass

    ids, vecs = [], []
    for r in rows:
        emb = _decode(r["embedding"])
        if emb is not None:
            ids.append(r["id"])
            vecs.append(emb)

    if len(ids) >= 2:
        try:
            pairs = _knn_numpy(ids, vecs, k)
        except ImportError:
            pairs = _knn_pure(ids, vecs, k)
        for (a, b), sim in pairs.items():
            edges.append((a, b, "semantic", _tier(sim), round(sim, 4), now))

    edges += _entity_edges(rows, now)        # entity bridges — validated, always on

    with v.conn:
        v.conn.execute("DELETE FROM edges")
        v.conn.executemany(
            "INSERT OR REPLACE INTO edges (src, dst, type, tier, weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", edges)

    by_type = Counter(e[2] for e in edges)
    by_tier = Counter(e[3] for e in edges if e[2] == "semantic")
    return {
        "total":    len(edges),
        "chain":    by_type.get("chain", 0),
        "dendrite": by_type.get("dendrite", 0),
        "semantic": by_type.get("semantic", 0),
        "entity":   by_type.get("entity", 0),
        "strong":   by_tier.get("strong", 0),
        "medium":   by_tier.get("medium", 0),
        "weak":     by_tier.get("weak", 0),
        "embedded": len(ids),
    }


# ── communities — label propagation + tf-idf naming ──────────────────────────

def detect_communities(vault: Optional[Vault] = None) -> dict:
    """
    Discover cross-session topic communities over the structural graph
    (chains + dendrites + strong/medium semantic edges), then name each one
    from its members' gists. Writes nodes.community = 'c<n>|<label>'.
    """
    v = vault or Vault()
    rows = v.conn.execute("""
        SELECT src, dst, type, tier, weight FROM edges
        WHERE type IN ('chain', 'dendrite')
           OR (type = 'semantic' AND tier IN (?, ?))
    """, COMMUNITY_EDGE_TIERS).fetchall()

    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        w = float(r["weight"] or 1.0)
        adj[r["src"]].append((r["dst"], w))
        adj[r["dst"]].append((r["src"], w))

    if not adj:
        return {"communities": 0, "labeled_nodes": 0}

    # drop pathological super-hubs so a cross-topic consolidation node can't
    # bridge dozens of unrelated projects into one community (see HUB_* above).
    hubs_excluded = 0
    if len(adj) >= HUB_MIN_GRAPH:
        deg = {nid: len(nbrs) for nid, nbrs in adj.items()}
        vals = sorted(deg.values())
        med = vals[len(vals) // 2] or 1
        cut = max(HUB_ABS_FLOOR, HUB_FRAC * len(adj), HUB_MED_MULT * med)
        hubs = {nid for nid, d in deg.items() if d >= cut}
        if hubs:
            hubs_excluded = len(hubs)
            adj = {nid: [(nb, w) for nb, w in nbrs if nb not in hubs]
                   for nid, nbrs in adj.items() if nid not in hubs}
            if not adj:
                return {"communities": 0, "labeled_nodes": 0,
                        "hubs_excluded": hubs_excluded}

    # label propagation: every node starts as its own label; each pass adopts
    # the weight-dominant label among neighbors. Deterministic: sorted order,
    # ties break to the smallest label.
    label = {nid: nid for nid in adj}
    order = sorted(adj.keys())
    for _ in range(MAX_LP_ITERATIONS):
        changed = 0
        for nid in order:
            scores: dict[str, float] = defaultdict(float)
            for nb, w in adj[nid]:
                scores[label[nb]] += w
            if not scores:
                continue
            top_w = max(scores.values())
            winner = min(l for l, w in scores.items() if w == top_w)
            if winner != label[nid]:
                label[nid] = winner
                changed += 1
        if changed == 0:
            break

    groups: dict[str, list[str]] = defaultdict(list)
    for nid, lb in label.items():
        groups[lb].append(nid)
    communities = [m for m in groups.values() if len(m) >= MIN_COMMUNITY_SIZE]
    communities.sort(key=len, reverse=True)

    names = _name_communities(v, communities)

    with v.conn:
        v.conn.execute("UPDATE nodes SET community = NULL "
                       "WHERE community IS NOT NULL")
        for ci, members in enumerate(communities):
            tag = f"c{ci}|{names[ci]}"
            v.conn.executemany(
                "UPDATE nodes SET community = ? WHERE id = ?",
                [(tag, m) for m in members])

    return {
        "communities":   len(communities),
        "labeled_nodes": sum(len(m) for m in communities),
        "largest":       len(communities[0]) if communities else 0,
        "hubs_excluded": hubs_excluded,
        "names":         names[:12],
    }


def _name_communities(v: Vault, communities: list[list[str]]) -> list[str]:
    """
    tf-idf over member gists: the 3 most distinctive terms name the topic.
    Knowledge-kind nodes count triple — a decision should name a topic
    before small talk does — and terms spread across >35% of communities
    are background hum, not names.

    Improvements over v1:
    - Bigram extraction: adjacent token pairs are scored alongside unigrams.
      If the top bigram beats the top unigram, the label leads with it.
    - Fragment filtering: tokens < 3 chars are dropped entirely; tokens 3-4
      chars are only kept if they appear >= 3 times in that community (kills
      typo shards like 'lly', 'kts' from a single mis-tokenised word).
    - Expanded _STOPWORDS removes conversational filler that produces
      meaningless labels like "whale grade memories".
    """
    # ── per-community unigram and bigram counters ─────────────────────────────
    uni_docs:  list[Counter] = []   # {term: weighted_count}
    bi_docs:   list[Counter] = []   # {"term1 term2": weighted_count}
    # raw (unweighted) frequency per community — used for the 3-4-char filter
    raw_uni:   list[Counter] = []

    for members in communities:
        terms:    Counter = Counter()
        bigrams:  Counter = Counter()
        raw:      Counter = Counter()
        qmarks = ",".join("?" * len(members))
        for r in v.conn.execute(
                f"SELECT kind, gist, query FROM nodes WHERE id IN ({qmarks})",
                members):
            text = (r["gist"] or r["query"] or "")[:300].lower()
            mult = 3 if r["kind"] in MEANING_KINDS else 1

            # tokenise — minimum length 3 enforced here
            tokens = [tok for tok in re.findall(r"[a-z][a-z0-9'\-]{2,}", text)
                      if tok not in _STOPWORDS]

            for tok in tokens:
                raw[tok] += 1   # raw count (unweighted) for fragment filter
                terms[tok] += mult

            # bigrams from the filtered token stream
            for a, b in zip(tokens, tokens[1:]):
                bigrams[f"{a} {b}"] += mult

        # apply the 3-4-char fragment filter:
        # keep a short token only if its raw count in this community >= 3
        filtered_terms: Counter = Counter()
        for tok, cnt in terms.items():
            if len(tok) <= 4 and raw[tok] < 3:
                continue
            filtered_terms[tok] = cnt

        uni_docs.append(filtered_terms)
        bi_docs.append(bigrams)
        raw_uni.append(raw)

    n_docs = len(uni_docs) or 1

    # ── document-frequency tables ─────────────────────────────────────────────
    uni_df:  Counter = Counter()
    bi_df:   Counter = Counter()
    for d in uni_docs:
        for term in d:
            uni_df[term] += 1
    for d in bi_docs:
        for bg in d:
            bi_df[bg] += 1

    df_cap = max(3, int(n_docs * 0.35))

    # ── score and assemble labels ─────────────────────────────────────────────
    names = []
    for uni_d, bi_d in zip(uni_docs, bi_docs):
        # score unigrams
        uni_scored = sorted(
            ((cnt * math.log(1 + n_docs / (1 + uni_df[t])), t)
             for t, cnt in uni_d.items() if uni_df[t] <= df_cap),
            reverse=True)

        # score bigrams (same idf formula; bigram df_cap is the same)
        bi_scored = sorted(
            ((cnt * math.log(1 + n_docs / (1 + bi_df[bg])), bg)
             for bg, cnt in bi_d.items() if bi_df[bg] <= df_cap),
            reverse=True)

        top_uni_score = uni_scored[0][0] if uni_scored else 0.0
        top_bi_score  = bi_scored[0][0]  if bi_scored  else 0.0

        chosen_words: list[str] = []

        if top_bi_score > top_uni_score and bi_scored:
            # lead with bigram, then add up to 1 more distinct unigram
            best_bigram = bi_scored[0][1]
            bigram_words = set(best_bigram.split())
            chosen_words.append(best_bigram)
            for _, t in uni_scored:
                if t not in bigram_words:
                    chosen_words.append(t)
                    break   # max 1 extra → 3 words total
        else:
            # no winning bigram — fall back to top-3 unigrams (original logic)
            chosen_words = [t for _, t in uni_scored[:3]]

        names.append(" ".join(chosen_words) or "untitled")

    return names


def compute_atlas(vault: Optional[Vault] = None) -> int:
    """
    Precompute a STABLE spatial position for every active node — the atlas.

    Fractal phyllotaxis: topic communities sit on a golden-angle spiral
    (i-th community at angle i*2pi/phi^2, radius growing with sqrt(i)), and
    each community's members sit on their own golden-angle spiral inside it,
    most-important nodes at the center. Same constant the context scheduler
    rotates by — the geometry of the map and the geometry of attention are
    one idea.

    Deterministic given the same communities: the map never re-scrambles,
    so a human can build spatial memory of their own vault (force layouts —
    Obsidian's included — randomize every load and never let you).
    Coordinates land in nodes.map_x/map_y; the dashboard draws them on
    canvas with zero physics. Derived data, rewritten each build.
    """
    v = vault or Vault()
    GA = 2.0 * math.pi * 0.3819660112501051   # golden angle
    rows = v.conn.execute(
        "SELECT n.id, n.community, n.importance, s.account "
        "FROM nodes n LEFT JOIN sessions s ON n.session = s.id "
        "WHERE n.status = 'active'").fetchall()   # active only — void nodes never get coords
    if not rows:
        return 0

    def _galaxy(acct):
        return galaxy_label(acct)

    # ── stable places (Phase 3 — opt-in via CAIRN_STABLE_PLACES) ─────────────
    # _layout below orders communities by SIZE-RANK and PACKS them, so a district's
    # position depends on the whole set — one import re-scrambles the map (charter
    # law #1 violated). Fix: give each community a birth-order sector LOCKED at
    # first sighting and persisted in `places`, keyed to a STABLE seed (its min
    # member id, robust to community relabeling). angle = born_seq*GA forever, so
    # existing districts never move as the vault grows. OFF by default → live
    # atlas unchanged; flipping it on is the ONE deliberate, watched re-layout.
    import os
    stable = bool(os.environ.get("CAIRN_STABLE_PLACES"))
    cid_seq: dict[str, int] = {}
    if stable:
        from datetime import datetime, timezone
        v.conn.execute("CREATE TABLE IF NOT EXISTS places "
                       "(seed TEXT PRIMARY KEY, born_seq INTEGER, created_at TEXT)")
        seq_of = {pr["seed"]: pr["born_seq"]
                  for pr in v.conn.execute("SELECT seed, born_seq FROM places")}
        nxt = (max(seq_of.values()) + 1) if seq_of else 0
        comm_members: dict[str, list] = defaultdict(list)
        for r in rows:
            c0 = (r["community"] or "").partition("|")[0]
            if c0:
                comm_members[c0].append(r["id"])
        fresh, now = [], datetime.now(timezone.utc).isoformat()
        for c0, ids in comm_members.items():
            seed = min(ids)                       # stable representative of the member set
            if seed not in seq_of:
                seq_of[seed] = nxt; fresh.append((seed, nxt, now)); nxt += 1
            cid_seq[c0] = seq_of[seed]
        if fresh:
            with v.conn:
                v.conn.executemany(
                    "INSERT OR IGNORE INTO places(seed,born_seq,created_at) VALUES(?,?,?)", fresh)

    def _layout(members_all):
        """Lay out ONE galaxy as a fractal phyllotaxis centered ~origin —
        communities on a golden spiral, members spiraled inside each, orphans as
        a dust halo (the exact single-vault map, per source). Returns
        (list[(x,y,id)], radius)."""
        groups: dict[str, list] = defaultdict(list)
        for r in members_all:
            cid = (r["community"] or "").partition("|")[0] or "_none"
            groups[cid].append(r)
        if stable:
            ordered = sorted(((c, m) for c, m in groups.items() if c != "_none"),
                             key=lambda kv: cid_seq.get(kv[0], 1 << 30))
        else:
            ordered = sorted(((c, m) for c, m in groups.items() if c != "_none"),
                             key=lambda kv: -len(kv[1]))
        SPACING = 26.0
        PAD     = SPACING * 1.25
        local: list[tuple] = []
        cum_area = 0.0
        placed: list[tuple] = []
        for i, (cid, members) in enumerate(ordered):
            rc = SPACING * math.sqrt(len(members)) / 1.55
            if stable:
                # sector + radius LOCKED to birth order — adding nodes never moves
                # an existing district (the re-scramble fix). Pure phyllotaxis.
                bs = cid_seq.get(cid, i)
                th = bs * GA
                R = SPACING * 1.30 * math.sqrt(bs + 0.5)
                cx, cy = R * math.cos(th), R * math.sin(th)
            elif i == 0:
                cx = cy = 0.0
            else:
                R = max(0.0, math.sqrt(cum_area / math.pi) - rc * 0.4)
                th = i * GA
                for _ in range(40):
                    cx, cy = R * math.cos(th), R * math.sin(th)
                    if all(math.hypot(cx - px, cy - py) >= (rc + prc + PAD * 0.5)
                           for px, py, prc in placed):
                        break
                    R += SPACING * 1.5
            placed.append((cx, cy, rc))
            cum_area += math.pi * (rc + PAD) ** 2
            members.sort(key=lambda r: -(r["importance"] or 5))
            n = len(members)
            for j, m in enumerate(members):
                rr = rc * math.sqrt((j + 0.5) / n)
                local.append((cx + rr * math.cos(j * GA),
                              cy + rr * math.sin(j * GA), m["id"]))
        orphans = groups.get("_none", [])
        body = max((math.hypot(px, py) + prc for px, py, prc in placed), default=80.0)
        # scattered lone stars — a sparse, DEEP halo, not a tight ring. A stable
        # per-id hash spreads each orphan across a wide radial band (0.85-1.30 of
        # the body) and jitters its angle off the golden spiral, so they read as a
        # starfield surrounding their galaxy instead of a clean annulus hugging it.
        # Deterministic (hash of id) so the map never re-scrambles between rebuilds.
        for j, m in enumerate(orphans):
            hv = 2166136261
            for ch in str(m["id"]):
                hv = ((hv ^ ord(ch)) * 16777619) & 0xffffffff
            f1 = (hv & 0xffff) / 0xffff
            f2 = ((hv >> 16) & 0xffff) / 0xffff
            # lone stars OUTSIDE the recent rings (which sit ~1.0-1.2x the body):
            # a sparse deep halo at 1.22-1.62x, angle-jittered so it never reads as
            # a clean ring. Deterministic per id so the sky never re-scrambles.
            rr = body * (1.30 + 0.42 * f1)
            ang = j * GA + (f2 - 0.5) * 2.2
            local.append((rr * math.cos(ang), rr * math.sin(ang), m["id"]))
        rad = max((math.hypot(x, y) for x, y, _ in local), default=80.0)
        return local, rad

    # GALAXIES: lay out each source as its own phyllotaxis cluster-atlas, then
    # offset onto a ring so they read as distinct worlds. Cross-source edges
    # span between them as bridges. Baked into map_x/map_y → client renders with
    # zero physics (fast at 60k+).
    by_galaxy: dict[str, list] = defaultdict(list)
    for r in rows:
        by_galaxy[_galaxy(r["account"])].append(r)
    gkeys = sorted(by_galaxy.keys())
    laid = {k: _layout(by_galaxy[k]) for k in gkeys}

    pos: list[tuple] = []
    if len(gkeys) <= 1:
        for k in gkeys:
            for x, y, i in laid[k][0]:
                pos.append((round(x, 2), round(y, 2), i))
    else:
        # PACK onto the hub ring as tight as possible: ringR is the smallest radius
        # that keeps every ADJACENT pair's full extents (orphan halo + recency rings,
        # via laid[k][1]) from crossing — so a small galaxy sits right up against a
        # big neighbor instead of being flung out to the biggest galaxy's radius
        # (equal-radius spacing pushed the small galaxies way too far apart).
        rads = [laid[k][1] for k in gkeys]
        half = math.sin(math.pi / len(gkeys)) or 1.0
        need = max((rads[i] + rads[(i + 1) % len(gkeys)]) / (2 * half) for i in range(len(gkeys)))
        ringR = need * 1.08   # a small breathing gap so halos don't quite touch
        if stable:
            # lock the galaxy ring once — otherwise growing ONE galaxy translates
            # every galaxy's center (the 2nd half of the re-scramble: ringR is
            # max-radius-derived). Persisted, reused; the map grows at the rim, the
            # centers stay put.
            v.conn.execute("CREATE TABLE IF NOT EXISTS atlas_meta (k TEXT PRIMARY KEY, v REAL)")
            _rr = v.conn.execute("SELECT v FROM atlas_meta WHERE k='ringR'").fetchone()
            if _rr and _rr["v"]:
                # STABLE-BUT-BREATHING (owner's catch: growing piles crossed
                # into neighbors' space because the ring was frozen forever).
                # Never shrink — the centers stay directionally put — but when
                # the galaxies outgrow the old ring, it breathes OUT so every
                # halo keeps its room. A uniform radial step, not a re-scramble.
                ringR = max(_rr["v"], ringR)
                if ringR > _rr["v"]:
                    with v.conn:
                        v.conn.execute("INSERT OR REPLACE INTO atlas_meta(k,v) VALUES('ringR',?)", (ringR,))
            else:
                with v.conn:
                    v.conn.execute("INSERT OR REPLACE INTO atlas_meta(k,v) VALUES('ringR',?)", (ringR,))
        for idx, k in enumerate(gkeys):
            ang = (idx / len(gkeys)) * 2 * math.pi - math.pi / 2
            gx, gy = ringR * math.cos(ang), ringR * math.sin(ang)
            for x, y, i in laid[k][0]:
                pos.append((round(x + gx, 2), round(y + gy, 2), i))

    # Ensure atlas_meta exists OUTSIDE the coords transaction — DDL implicitly
    # commits the open transaction on Python < 3.12, which would split the UPDATE
    # from the rev bump. Keeping the `with` block pure-DML makes them atomic on all
    # versions (the table also gets created in the multi-galaxy branch above; both
    # are IF NOT EXISTS, so this is a harmless no-op when it already exists).
    v.conn.execute("CREATE TABLE IF NOT EXISTS atlas_meta (k TEXT PRIMARY KEY, v REAL)")
    with v.conn:
        v.conn.executemany(
            "UPDATE nodes SET map_x = ?, map_y = ? WHERE id = ?", pos)
        # Bump a monotonic atlas revision so an already-open dashboard can tell a
        # COORDINATE-ONLY rebuild happened even when node/edge counts are unchanged
        # (e.g. after an account was reassigned and the galaxies re-separated), and
        # CLEAR the stale flag — this rebuild reflects the current account layout.
        v.conn.execute("INSERT INTO atlas_meta(k, v) VALUES('rev', 1) "
                       "ON CONFLICT(k) DO UPDATE SET v = v + 1")
        v.conn.execute("INSERT INTO atlas_meta(k, v) VALUES('stale', 0) "
                       "ON CONFLICT(k) DO UPDATE SET v = 0")
    return len(pos)


import threading as _threading
_atlas_rebuild_lock = _threading.Lock()


def rebuild_atlas_if_stale(vault: Optional[Vault] = None) -> bool:
    """Lazy self-heal for the derived atlas: if an account change flagged it
    'stale', recompute coordinates ONCE and clear the flag. GUARDED + idempotent —
    a non-blocking lock means concurrent dashboard polls can never launch duplicate
    rebuilds (the loser serves current coords; its next poll sees the bumped rev).
    Returns True iff a rebuild ran. Fail-safe: any error is a no-op so the atlas
    request never breaks."""
    v = vault or Vault()
    try:
        row = v.conn.execute("SELECT v FROM atlas_meta WHERE k='stale'").fetchone()
    except Exception:
        return False
    if not (row and row["v"]):
        return False
    if not _atlas_rebuild_lock.acquire(blocking=False):
        return False                      # another request is already rebuilding
    try:
        row = v.conn.execute("SELECT v FROM atlas_meta WHERE k='stale'").fetchone()
        if not (row and row["v"]):        # a concurrent rebuild already cleared it
            return False
        compute_atlas(v)                  # rebuilds coords, clears stale, bumps rev
        return True
    except Exception:
        return False
    finally:
        _atlas_rebuild_lock.release()


def build_all(vault: Optional[Vault] = None, k: int = KNN_K) -> dict:
    """Edges, communities, names, atlas — the full connective-tissue pass."""
    v = vault or Vault()
    rep = build_edges(v, k=k)
    rep.update(detect_communities(v))
    rep["atlas"] = compute_atlas(v)
    return rep
