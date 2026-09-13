"""
cairn/book.py — the Book. The project's founding idea, finally built.

The founding idea — Cairn = Index + TOC + Glossary = Logbook = Compass. The
engine got built; the book got forgotten. This module is the
book: a pointer-only navigation layer generated from organs that already
exist — nothing here stores content, everything dereferences.

Four readers, one truth:
  hub_data()    — the Garden's landing hub (attention, activity, open things)
  book_data()   — the Contents page (This Week -> Projects -> Archive Volumes)
  index_data()  — the back-of-book Index (tags, doc cards, defined terms)
  page_one()    — the model's orientation head (~30 lines: laws, landscape,
                  warnings, how to navigate). Prepended by orient + MCP.

Sizing law (IFScale, derived March 2026): <=150 lines per artifact. The book
points; it never carries.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

MEANING_KINDS = ("decision", "warning", "insight", "idea", "open_item",
                 "procedure", "resolved", "hypothesis", "question", "blocker")

# The human-facing recency surfaces (Hub "just captured", since-last-visit) show
# real human content — NOT process markers (tool_call / interrupt / context_stamp).
# One shared allowlist so every recency view stays consistent: noise-vs-signal is
# decided here, once, not re-litigated in each view's WHERE clause.
RECENCY_KINDS = MEANING_KINDS + ("conversation_turn", "artifact")

_LAWS = ("local-first - nothing leaves this machine | append-only - void, "
         "never delete | stdlib + numpy | model-agnostic")

_NAVIGATE = ("cairn fetch \"q\" before re-reading files/history | "
             "cairn wander \"topic\" for adjacent ideas | "
             "cairn read <id> = a node IN FULL (fetch/search show gists) | "
             "note decisions/warnings as you work | "
             "full book: ~/.cairn/BOOK.md")

# The three-layer claim rule, recited to every model at every orient. Rides
# its own single line under NAVIGATE (the page_one line budget accounts for
# it; the 34-line cap is unchanged).
_CLAIM_RULE = ("CLAIM CHECK: logs = tail only, search = history only - "
               "absence in one is NEVER evidence; check tail + full history "
               "+ live source before calling anything new, stale, or decided")


def _projects() -> dict:
    """Declared projects: ~/.cairn/projects.json, same file the Garden reads.
    Values are 2-element [label, blurb] or 3-element [label, blurb,
    [alias-tags…]] — tuple()'d whole, so v[0]/v[1] work for either length and a
    3rd element (aliases) rides along for callers that want it."""
    f = Path.home() / ".cairn" / "projects.json"
    if f.exists():
        try:
            return {k: tuple(v) for k, v in
                    json.loads(f.read_text(encoding="utf-8")).items()
                    if isinstance(v, (list, tuple)) and len(v) >= 2}
        except Exception:
            pass
    return {"cairn": ("Cairn", "episodic memory system"),
            "meta": ("The Vault", "this second brain, its charter and care")}


def _match_tags(tag: str, v) -> list:
    """Primary tag + declared alias tags (the optional 3rd element). A project's
    node queries union over these so a promoted family reads as one project."""
    out = [tag]
    if v and len(v) >= 3 and isinstance(v[2], (list, tuple)):
        for a in v[2]:
            if isinstance(a, str) and a and a not in out:
                out.append(a)
    return out


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── Emerging-family detection for the Book's "older projects" (plan C8) ────────
# Mirrors garden.py's P1 logic (machine-tag denylist + family normalization) so
# the Book reaches the same real-but-undeclared projects the Projects view does,
# without importing garden (book.py stays a leaf: stdlib-only, no cycles).
_BOOK_MACHINE_PREFIXES = ("kw:", "entity:", "prov:", "by:", "stance:",
                          "account:", "turn:", "member:", "due:")
_BOOK_NOT_PROJECTS = {
    "conversation", "user", "agent", "context", "intent", "session-start",
    "garden", "human-capture", "reply", "backfill", "consolidated",
    "compress-event", "pre-compact", "checkpoint", "session-end",
    "agent-authored", "decision", "resolved", "warning", "insight",
    "strategy", "stack", "pipeline", "scraping", "data", "style",
    "white-paper", "hardware", "ip", "verification", "protocol", "dev",
    "origin", "parked", "second-brain", "annotation", "promoted", "inbox",
    "claim", "import", "mcp", "codex", "claude", "human", "distilled",
    "test", "codex-test", "media", "chat", "chat-pin",
    # agent work-exhaust (2026-07-03): build/audit session vocabulary that
    # flooded the owner's project surfaces as fake emerging families. Process
    # words, never projects. Mirrors garden._NOT_PROJECTS — keep in sync.
    "verified", "shipped", "handoff", "registry", "backlog", "sprint",
    "hygiene", "audit", "codex-audit", "audit-brief", "owner-ruling",
    "owner-rulings", "owner-approved", "g2", "g2-early", "phase-0",
    "lane-a", "lane-b", "lane-c", "lane-d", "lane-e", "for-codex",
    "for-all-agents", "ia", "vault", "spec", "correction",
    # second census (owner walkthrough 2026-07-03): survivors of the
    # structural rule that are still work-about-the-work, not his life.
    "ux", "docs", "doc", "dashboard", "brain", "naming", "feedback",
    "idea", "ideas", "capture", "open-items", "open-item", "open_item",
    "compact-event", "session", "launch", "security", "federation",
    "tools", "tooling",
}


def _book_is_machine_tag(tag: str) -> bool:
    return isinstance(tag, str) and tag.startswith(_BOOK_MACHINE_PREFIXES)


def _book_family_key(tag: str) -> str:
    import re as _re
    s = (tag or "").strip().lower()
    s = _re.sub(r"^[^\w]+|[^\w]+$", "", s)
    s = _re.sub(r"'s$", "", s)
    if s.endswith("s") and len(s) > 3:
        s = s[:-1]
    return s


def _older_projects(vault, declared_tags: set) -> list:
    """Real-but-undeclared projects (plan C8): emerging tag FAMILIES with mass,
    machine strata stripped, ordered by LAST ACTIVITY DESC — so every real
    project is reachable from the Book, promoted or not. Each carries the display
    spelling + its full tag family (so the click-through project view unions it).
    """
    c = vault.conn
    by_tag: dict = {}
    for r in c.execute(
            "SELECT id, session, tags, timestamp FROM nodes WHERE status='active'"):
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            continue
        for t in tags:
            if not isinstance(t, str):
                continue
            if t in declared_tags or t in _BOOK_NOT_PROJECTS or _book_is_machine_tag(t):
                continue
            by_tag.setdefault(t, []).append(r)

    families: dict = {}
    for tag, rows in by_tag.items():
        key = _book_family_key(tag)
        if not key:
            continue
        fam = families.setdefault(key, {"spellings": {}, "tags": [], "ids": {},
                                        "sessions": set(), "days": set()})
        fam["spellings"][tag] = fam["spellings"].get(tag, 0) + len(rows)
        fam["tags"].append(tag)
        for r in rows:
            ts = r["timestamp"] or ""
            if ts > fam["ids"].get(r["id"], ""):
                fam["ids"][r["id"]] = ts
            if r["session"]:
                fam["sessions"].add(r["session"])
            fam["days"].add(ts[:10])

    out = []
    for key, fam in families.items():
        # same thresholds as the Projects view: mass PLUS structural spread —
        # a real project spans conversations and days; one session's work-
        # exhaust never qualifies (owner hygiene ruling, 2026-07-03).
        if (len(fam["ids"]) < 6 or len(fam["sessions"]) < 2
                or len(fam["days"]) < 2):
            continue
        display = max(fam["spellings"].items(), key=lambda kv: kv[1])[0]
        last_ts = max(fam["ids"].values()) if fam["ids"] else ""
        out.append({"tag": display, "name": display,
                    "total": len(fam["ids"]), "last_ts": last_ts,
                    "aliases": sorted(t for t in fam["tags"] if t != display)})
    out.sort(key=lambda p: p["last_ts"], reverse=True)
    return out


def _gist(r) -> str:
    g = (r["gist"] or (r["query"] or "")[:110]).replace("\n", " ")
    if len(g) <= 110:
        return g
    cut = g.rfind(" ", 0, 110)          # word boundary, never mid-letter
    return (g[:cut] if cut > 40 else g[:110]) + " …"


def hub_data(vault) -> dict:
    """Everything the Garden's landing hub shows — one call."""
    c = vault.conn
    week = _iso_days_ago(7)
    hidden = vault.hidden_ids()   # archived/snoozed leave the human hub (stay active for the AI)

    open_items = [
        {"id": r["id"], "gist": _gist(r), "ts": r["timestamp"]}
        for r in c.execute(
            "SELECT id, gist, query, timestamp FROM nodes "
            "WHERE status='active' AND kind='open_item' "
            "ORDER BY timestamp DESC LIMIT 8")
        if r["id"] not in hidden]

    # Fading = "valuable AND genuinely going stale", not "oldest + most-shown".
    # The old ORDER BY cnt DESC ranked by raw cumulative injection count, so an
    # ancient max-shown node (e.g. shown=799) sat pinned to the top forever while
    # newly-neglected memories never surfaced. Re-rank by a recency-and-
    # importance-aware NEGLECT score instead: importance leads (surface what's
    # worth keeping), tie-broken by FSRS overdue-pressure — days since the node
    # was last (re)injected, scaled by its stability. A node re-injected recently
    # is NOT fading no matter how high its lifetime shown-count; one that's slipped
    # past its schedule is. `shown` (the injection count) stays in the payload so
    # the UI's "surfaced Nx, never used" copy is byte-identical. Shape unchanged:
    # HAVING cnt>=2, LIMIT 5, same dict keys.
    fading = [
        {"id": r["id"], "gist": _gist(r), "shown": r["cnt"]}
        for r in c.execute("""
            SELECT n.id, n.gist, n.query,
                   COUNT(l.id) AS cnt,
                   ( (julianday('now')
                        - julianday(COALESCE(n.last_injected, '2020-01-01')))
                     / MAX(COALESCE(n.stability_days, 1.0), 0.1) )
                   AS neglect
            FROM nodes n JOIN attention_ledger l ON l.node_id = n.id
            WHERE n.status='active' AND l.cited = 0
            GROUP BY n.id HAVING cnt >= 2
            ORDER BY COALESCE(n.importance, 5) DESC, neglect DESC, cnt DESC
            LIMIT 5""")
        if r["id"] not in hidden]

    ideas = [
        {"id": r["id"], "gist": _gist(r), "ts": r["timestamp"]}
        for r in c.execute(
            "SELECT id, gist, query, timestamp FROM nodes "
            "WHERE status='active' AND kind='idea' "
            "ORDER BY timestamp DESC LIMIT 6")
        if r["id"] not in hidden]

    # cross-project, cross-model activity — who moved what, where
    activity = [
        {"session": r["session"], "model": r["model"], "nodes": r["n"]}
        for r in c.execute("""
            SELECT session, model, COUNT(*) AS n FROM nodes
            WHERE status='active' AND timestamp >= ?
              AND session NOT LIKE 'import-%'
            GROUP BY session, model ORDER BY n DESC LIMIT 10""", (week,))]

    chat = c.execute(
        "SELECT COUNT(*) FROM nodes WHERE status='active' "
        "AND session LIKE 'room:%' AND timestamp >= ?", (week,)).fetchone()[0]

    # due dates: nodes carry a 'due:YYYY-MM-DD' tag (set via capture "due:..."),
    # split into overdue / today / upcoming. Highest-value, lowest-effort hub
    # surface — the data was already in the vault, just never shown here.
    import json as _json
    from datetime import date as _date
    today = _date.today().isoformat()
    overdue, due_today, upcoming = [], [], []
    for r in c.execute(
            "SELECT id, gist, query, tags FROM nodes "
            "WHERE status='active' AND tags LIKE '%due:%' LIMIT 200"):
        if r["id"] in hidden:
            continue
        try:
            tags = _json.loads(r["tags"] or "[]")
        except Exception:
            continue
        d = next((t.split(":", 1)[1] for t in tags
                  if isinstance(t, str) and t.startswith("due:")), None)
        if not d:
            continue
        import re as _re
        g = _re.sub(r"\s*\bdue:\S+", "", _gist(r)).strip()  # drop the raw due: token
        item = {"id": r["id"], "gist": g, "due": d}
        (overdue if d < today else due_today if d == today else upcoming).append(item)
    overdue.sort(key=lambda x: x["due"])
    upcoming.sort(key=lambda x: x["due"])
    due = {"overdue": overdue, "today": due_today, "upcoming": upcoming[:5]}

    # recently captured — the curated lists above are kind-gated and never show
    # conversation_turn (the dominant ambient-capture kind), so new activity was
    # invisible on the landing tab. RECENCY_KINDS surfaces real captures while
    # excluding process markers (tool_call/interrupt/context_stamp) + the import
    # archive — so the Garden tracks new stuff without the harness/system noise.
    _rk = ",".join("?" * len(RECENCY_KINDS))
    just_captured = [
        {"id": r["id"], "kind": r["kind"], "gist": _gist(r),
         "speaker": r["speaker"], "ts": r["timestamp"]}
        for r in c.execute(
            f"SELECT id, kind, gist, query, speaker, timestamp FROM nodes "
            f"WHERE status='active' AND kind IN ({_rk}) "
            f"AND session NOT LIKE 'import-%' "
            f"ORDER BY timestamp DESC LIMIT 10", RECENCY_KINDS)
        if r["id"] not in hidden]

    return {"open_items": open_items, "fading": fading, "ideas": ideas,
            "activity": activity, "chat_week": chat, "due": due,
            "just_captured": just_captured,
            "generated": datetime.now(timezone.utc).isoformat()}


def book_data(vault) -> dict:
    """The Contents page: This Week -> Projects (topic chapters) -> Volumes."""
    c = vault.conn
    week = _iso_days_ago(7)
    kinds = ",".join(f"'{k}'" for k in MEANING_KINDS if k != "warning")  # warnings live on Desk/Review, not the human Book

    this_week = [
        {"id": r["id"], "kind": r["kind"], "gist": _gist(r),
         "ts": r["timestamp"]}
        for r in c.execute(
            f"SELECT id, kind, gist, query, timestamp FROM nodes "
            f"WHERE status='active' AND kind IN ({kinds}) "
            f"AND timestamp >= ? "
            f"ORDER BY importance DESC, timestamp DESC LIMIT 12", (week,))]

    # Chapters by KIND (human-legible: Decisions / How-tos / Open threads / …),
    # not by graph `community` id (machine output, often just a number). The Book
    # is a browsable contents page for a human. Grouping by kind also surfaces
    # tagged-but-not-yet-clustered nodes (the old community filter hid them).
    KIND_CHAPTER = {
        "decision": "Decisions", "insight": "Key insights",
        "open_item": "Open threads", "blocker": "Open threads",
        "procedure": "How-tos", "warning": "Warnings",
        "resolved": "Resolved", "idea": "Ideas",
        "hypothesis": "Hypotheses", "question": "Questions",
    }
    projects = []
    for tag, v in _projects().items():
        name, desc = v[0], v[1]
        mt = _match_tags(tag, v)                     # primary + alias tags
        like = " OR ".join("tags LIKE ?" for _ in mt)
        likeparams = [f'%"{t}"%' for t in mt]
        rows = c.execute(
            f"SELECT kind, COUNT(*) AS n FROM nodes "
            f"WHERE status='active' AND ({like}) AND kind IN ({kinds}) "
            f"GROUP BY kind ORDER BY n DESC", likeparams).fetchall()
        chapters = []
        for r in rows:
            ex = [{"id": e["id"], "kind": e["kind"], "gist": _gist(e)}
                  for e in c.execute(
                      f"SELECT id, kind, gist, query FROM nodes "
                      f"WHERE status='active' AND kind = ? AND ({like}) "
                      f"ORDER BY importance DESC, timestamp DESC LIMIT 3",
                      (r["kind"], *likeparams))]
            chapters.append({"cid": r["kind"],
                             "label": KIND_CHAPTER.get(r["kind"], r["kind"].replace("_", " ").title()),
                             "count": r["n"], "exemplars": ex})
        total = c.execute(
            f"SELECT COUNT(*) FROM nodes WHERE status='active' "
            f"AND ({like})", likeparams).fetchone()[0]
        # last-touch: the newest node timestamp across the whole family (plan C8
        # — the Book orders "where was I" by most-recently-active, not config
        # order). tool_call excluded so plumbing doesn't count as human activity.
        last_ts = c.execute(
            f"SELECT MAX(timestamp) FROM nodes WHERE status='active' "
            f"AND ({like}) AND kind != 'tool_call'", likeparams).fetchone()[0] or ""
        projects.append({"tag": tag, "name": name, "desc": desc,
                         "total": total, "last_ts": last_ts,
                         "chapters": chapters})

    # order by LAST-TOUCH DESC — the most recently active project sits at top.
    projects.sort(key=lambda p: p["last_ts"], reverse=True)

    # Older / other projects (plan C8): real-but-undeclared tag families, cleaned
    # and last-activity ordered, so every project is reachable from the Book. The
    # declared set (primary + aliases) never re-appears here.
    declared_tags = set()
    for tag, v in _projects().items():
        for t in _match_tags(tag, v):
            declared_tags.add(t)
    older_projects = _older_projects(vault, declared_tags)

    volumes = [
        {"account": r["account"], "sessions": r["s"], "nodes": r["n"],
         "first": (r["lo"] or "")[:10], "last": (r["hi"] or "")[:10]}
        for r in c.execute("""
            SELECT COALESCE(s.account,'unlabeled') AS account,
                   COUNT(DISTINCT s.id) AS s, COUNT(n.id) AS n,
                   MIN(n.timestamp) AS lo, MAX(n.timestamp) AS hi
            FROM sessions s JOIN nodes n ON n.session = s.id
            WHERE s.id LIKE 'import-%' AND n.status != 'void'
            GROUP BY COALESCE(s.account,'unlabeled')
            ORDER BY n DESC""")]

    return {"this_week": this_week, "projects": projects,
            "older_projects": older_projects, "volumes": volumes,
            "generated": datetime.now(timezone.utc).isoformat()}


def volume_sessions(vault, account: str) -> dict:
    """One archive volume's sessions (plan C4 Archive drill-in): each session
    row with node count + date span, so a click opens the P2 conversation reader
    (GET /api/garden/session/{id}/turns) for that import. Newest first."""
    c = vault.conn
    acct = account if account and account != "unlabeled" else None
    where = "s.account = ?" if acct is not None else "s.account IS NULL"
    params = (acct,) if acct is not None else ()
    rows = c.execute(f"""
        SELECT s.id AS sid,
               COUNT(n.id) AS nodes,
               MIN(n.timestamp) AS lo, MAX(n.timestamp) AS hi
        FROM sessions s JOIN nodes n ON n.session = s.id
        WHERE s.id LIKE 'import-%' AND n.status != 'void' AND {where}
        GROUP BY s.id
        ORDER BY hi DESC LIMIT 200""", params).fetchall()
    sessions = [{"id": r["sid"], "nodes": r["nodes"],
                 "first": (r["lo"] or "")[:10], "last": (r["hi"] or "")[:10]}
                for r in rows]
    return {"account": account, "sessions": sessions, "count": len(sessions)}


def topics_data(vault) -> list:
    """Named community clusters (nodes.community = 'c<n>|<label>') → Topics.

    The nightly graph pass names cross-session topic communities and stamps each
    member with 'c<idx>|<label>'. This reads that computed-then-thrown-away
    output for the Index Topics section (plan C4): one entry per NAMED community,
    with its meaning-kind member count. UNNAMED communities — a bare 'c3' or
    'c3|' with a numeric-only / empty label — are skipped (machine output with no
    human handle). DISPLAY only; the community column and retrieval are untouched.
    """
    c = vault.conn
    topics: dict = {}   # cid -> {"cid","label","count","total"}
    for r in c.execute(
            "SELECT kind, community, timestamp, "
            "       (tags LIKE '%\"prov:distilled\"%') AS distilled FROM nodes "
            "WHERE status='active' AND community IS NOT NULL AND community != ''"):
        raw = r["community"] or ""
        cid, _, label = raw.partition("|")
        label = label.strip()
        # skip the unnamed: no label at all, or a purely numeric label (no human
        # handle — 'c3|' / 'c3|4' are machine ids, not topics a person browses).
        if not label or label.replace(" ", "").isdigit():
            continue
        t = topics.setdefault(cid, {"cid": cid, "label": label, "count": 0,
                                    "total": 0, "dist": 0, "last_ts": ""})
        t["total"] += 1
        if r["distilled"]:
            t["dist"] += 1
        if r["kind"] in MEANING_KINDS:
            t["count"] += 1
            # meaning-gated freshness: when a HUMAN-relevant node last joined —
            # lets the Hub sort topics by life, not by machine chatter.
            if (r["timestamp"] or "") > t["last_ts"]:
                t["last_ts"] = r["timestamp"] or ""
    # a topic worth showing has at least one meaning-kind node; order by that
    # count DESC so the richest topics lead, then label for stable ties.
    # THE IMPORT-BLOB GATE (owner's G2 catch: 'meta glasses openclaw' = 602
    # nodes fusing six unrelated projects): a big cluster that is mostly
    # prov:distilled import claims is the backfill stratum wearing a topic
    # label, not a browsable theme — the community pass fused it because
    # distilled claims interlink densely. Gate it off Topics surfaces
    # (DISPLAY only; retrieval, edges and the galaxy are untouched).
    out = [t for t in topics.values() if t["count"] >= 1
           and not (t["total"] >= 20 and t["dist"] / t["total"] > 0.8)]
    out.sort(key=lambda t: (-t["count"], t["label"].lower()))
    return out


def topic_members(vault, cid: str) -> dict:
    """Meaning-kind member nodes of one named community (the topic view, plan C4).
    Reuses the tag-membership listing shape so the client renders it like any tag
    view. hidden_ids respected; meaning-kinds only (the topic's human content)."""
    c = vault.conn
    hidden = vault.hidden_ids()
    label = ""
    rows = c.execute(
        "SELECT id, kind, gist, query, community, timestamp FROM nodes "
        "WHERE status='active' AND community LIKE ? "
        "ORDER BY importance DESC, timestamp DESC LIMIT 120",
        (cid + "|%",)).fetchall()
    nodes = []
    kinds = set(MEANING_KINDS)
    for r in rows:
        if r["id"] in hidden or r["kind"] not in kinds:
            continue
        if not label:
            label = (r["community"] or "").partition("|")[2].strip()
        nodes.append({"id": r["id"], "kind": r["kind"], "gist": _gist(r),
                      "ts": r["timestamp"]})
    return {"cid": cid, "label": label, "nodes": nodes, "count": len(nodes)}


# Machine-tag strata prefixes — retrieval plumbing that must stay OUT of the
# human A–Z Index (plan C4). Mirrors garden._is_machine_tag's prefix list plus
# the Index's own display-only skips (file:/media:/… never were topics). DATA
# untouched; this is a DISPLAY filter for the tag listing only.
_INDEX_TAG_SKIP = ("kw:", "entity:", "prov:", "by:", "stance:", "account:",
                   "turn:", "member:", "due:",
                   "file:", "mtime:", "made:", "lesson:", "from:",
                   "media:", "room:", "ext:")

# Bare, un-prefixed words that are retrieval plumbing or node-kinds — NOT topics —
# plus the ALL-CAPS "CAIRN-..." session/collab markers (the real project topic is
# lowercase "cairn", so case separates them cleanly). These pass the prefix skip
# above (no colon) but still are not human topics, so they crowd the A–Z. We route
# them to a reversible "system tags" list the Index can reveal on demand. This is a
# DISPLAY classification only — DATA untouched, every tag stays clickable. 2026-09-10.
_INDEX_SYSTEM_WORDS = frozenset({
    "conversation", "conversation_turn", "claim", "agent", "mcp", "import",
    "collab", "user", "context_stamp",
    "decision", "insight", "warning", "idea", "question", "resolved",
    "hypothesis", "procedure", "open_item",
})
# NB: bare "note"/"stamp" deliberately NOT reserved — they had zero uses and could be
# real future human topics; only the explicit technical name context_stamp is held.


# Machine-RELATION prefixes that leak into the topic A–Z (they carry a ':' but weren't
# in _INDEX_TAG_SKIP). Routed to the reversible system drawer — NOT deleted, still
# clickable. Deliberately EXCLUDES proj: (real projects) and distills: (owner-kept —
# the prefix earns its place).
_INDEX_SYSTEM_PREFIXES = ("annotates:", "preflight:", "parent:", "schema:")

# Explicit, owner-reviewed agent/collaboration PROCESS tags (bookkeeping, statuses,
# provenance) routed to the reversible drawer — they were too broad to serve as Index
# headings ("used so much it doesn't help searching"). Owner-ruled 2026-09-11 on a
# 12-entry sample. EXACT labels only — NO patterns (a digit-ending like R4/gate could be
# a real subject). Real subjects deliberately EXCLUDED: agent-harness, agent-protocol,
# agent-loop, receipt-contract, blinded-measurement. Reversible; raw tag stays clickable.
_INDEX_PROCESS_TAGS = frozenset({
    "agent-authored", "awaiting-codex", "built-awaiting-review", "a1-residue-sweep",
    "handoff", "receipt", "codex-review", "gate-a",
})


def _is_index_system_tag(t: str) -> bool:
    """True for un-prefixed plumbing/kind words, ALL-CAPS CAIRN-* session markers, and
    the machine-relation prefixes above — kept OUT of the topic A–Z but surfaced in the
    reversible system list. The marker arm requires isupper() so a mixed-case topic like
    'CAIRN-Garden' is NOT redirected. Lowercase 'cairn' (the real topic), proj:, and
    the owner-kept distills: are never caught."""
    return (t.lower() in _INDEX_SYSTEM_WORDS
            or t.lower() in _INDEX_PROCESS_TAGS
            or (t.startswith("CAIRN-") and t.isupper())
            or t.startswith(_INDEX_SYSTEM_PREFIXES))


def _safe_index_key(name: str) -> str:
    """Grouping key for spelling-variant folding: NFC + collapsed inner whitespace +
    lower(). PRESERVES punctuation, word boundaries, accents, digits, suffixes — and
    uses lower() (NOT casefold, which would equate ß/ss and other compatibility forms)
    so ONLY ordinary case differences fold: 'FooBar'/'foobar' group, while
    'Foo Bar'/'foobar.com'/'C++'/'Straße' vs 'Strasse' stay distinct."""
    import unicodedata, re
    s = unicodedata.normalize("NFC", name).strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _index_label(tag: str) -> str:
    """Display label: strip the entity:/proj: prefix for reading. The FULL tag is
    carried separately so the click always reaches the real stored membership."""
    if tag.startswith("entity:"):
        return tag[7:]
    if tag.startswith("proj:"):
        return tag[5:]
    return tag


def _index_namespace(tag: str) -> str:
    """Which tag namespace a display entry lives in. Spelling folding happens ONLY
    within a namespace — topic 'Foo', proj:'Foo' and entity:'Foo' are distinct entries
    (different meanings), never auto-merged just because their labels match."""
    if tag.startswith("entity:"):
        return "entity"
    if tag.startswith("proj:"):
        return "proj"
    return "topic"


def _index_prefs_path(vault=None):
    """Where THIS vault's Index preferences live — co-located with its cairn.db (from
    vault.db_path) so two vaults under the same OS home keep separate choices. Falls
    back to ~/.cairn/index_prefs.json for a vault without a db_path (the default vault)."""
    from pathlib import Path as _P
    try:
        dbp = getattr(vault, "db_path", None)
        if dbp:
            return _P(dbp).parent / "index_prefs.json"
    except Exception:
        pass
    return _P.home() / ".cairn" / "index_prefs.json"


def _index_settings(vault=None):
    """Owner Index preferences from the ACTIVE vault's index_prefs.json (a SEPARATE,
    vault-scoped store — Index writes never contend with settings.json greeting/toured):
      {"overrides": {full_raw_tag: 'keep'|'move'},   # placement that WINS over the classifier
       "aliases":   {source_full_tag: canonical_full_tag}}  # owner 'same as' merges
    Keyed to full raw tags so case/namespace never redirect them. ({}, {}) on any error."""
    import json as _j
    try:
        pf = _index_prefs_path(vault)
        if not pf.exists():
            return {}, {}
        d = _j.loads(pf.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {}, {}
        ov = d.get("overrides") if isinstance(d.get("overrides"), dict) else {}
        al = d.get("aliases") if isinstance(d.get("aliases"), dict) else {}
        return ov, al
    except Exception:
        return {}, {}


def _resolve_alias(tag, aliases):
    """Follow the owner 'same as' chain to its root canonical. Cycle-safe: a cycle
    resolves to its lexically-smallest member so every node in it groups together
    instead of splitting. A->B->C therefore all land under C; A<->B both land under
    the same representative."""
    seen = []
    cur = tag
    while cur in aliases:
        if cur in seen:
            return min(seen)
        seen.append(cur)
        cur = aliases[cur]
    return cur


def _group_index(counter: dict, kind: str, aliases: dict = None, include=None) -> list:
    """Fold n>=2 tags into display entries by _safe_index_key of their DISPLAY LABEL
    (ordinary case/whitespace variants only). Canonical = the real member with the
    most distinct-node support (ties lexical); NO invented casing. Each entry carries
    the FULL stored `tag` for the click AND a stripped `label` for display; every
    spelling likewise keeps its full tag + label + own count. Counts are per-spelling —
    NEVER summed into a group total (a node can bear several spellings). Sorted by label.

    aliases: owner 'same as' map {source_full_tag: canonical_full_tag}, resolved
    transitively (chains) and cycle-safely. include: tags the owner explicitly involved
    (keep override, or a merge source/target) so they are eligible even below n>=2.
    Within an owner-merged group the resolved ROOT wins display; a pure auto spelling
    group still uses most-support. Manual merges only — never inferred."""
    aliases = aliases or {}
    include = include or set()
    targets = set(aliases.values())
    elig = {t: n for t, n in counter.items() if n >= 2 or t in include}
    groups: dict = {}
    for t in elig:
        root = _resolve_alias(t, aliases)
        groups.setdefault((_index_namespace(root), _safe_index_key(_index_label(root))), []).append(t)
    out = []
    for members in groups.values():
        owner_merged = any((m in aliases) or (m in targets) for m in members)
        if owner_merged:
            root = _resolve_alias(members[0], aliases)
            members.sort(key=lambda m: (m != root, -elig.get(m, 0), m))
        else:
            members.sort(key=lambda m: (-elig.get(m, 0), m))
        canon = members[0]
        out.append({
            "tag": canon, "label": _index_label(canon), "count": elig.get(canon, 0),
            "kind": kind, "variants": len(members),
            "spellings": ([{"tag": m, "label": _index_label(m), "count": elig.get(m, 0)}
                           for m in members] if len(members) > 1 else None),
        })
    out.sort(key=lambda e: e["label"].lower())
    return out


def index_data(vault) -> dict:
    """The back-of-book Index: topics, tags, doc cards, defined terms — A to Z."""
    c = vault.conn
    counts: dict = {}
    ent_counts: dict = {}
    for r in c.execute(
            "SELECT tags FROM nodes WHERE status='active' "
            "AND tags IS NOT NULL AND tags != '[]'"):
        try:
            arr = json.loads(r["tags"])
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        # DISTINCT-NODE support: a tag counts ONCE per node even if repeated in the row.
        for t in {x for x in arr if isinstance(x, str)}:
            if t.startswith("entity:"):          # named things -> the Names layer
                if t[7:]:
                    ent_counts[t] = ent_counts.get(t, 0) + 1   # keep the FULL tag
            elif not t.startswith(_INDEX_TAG_SKIP):
                counts[t] = counts.get(t, 0) + 1
    # Machinery/session markers -> reversible drawer (see _is_index_system_tag). Real
    # topics and entity NAMES are each folded on exact spelling variants (_group_index)
    # so a mixed-case tag is ONE entry, not 4 casings. `names` is a separate list the Index
    # interleaves behind a toggle; every original spelling stays clickable via its full tag.
    overrides, aliases = _index_settings(vault)
    move_set = {t for t, v in overrides.items() if v == "move"}
    keep_set = {t for t, v in overrides.items() if v == "keep"}
    involved = set(aliases.keys()) | set(aliases.values())
    def _to_system(tag):
        ov = overrides.get(tag)
        if ov == "move":
            return True          # owner tucked it away
        if ov == "keep":
            return False         # owner pinned it to the A-Z
        return _is_index_system_tag(tag)   # default classifier
    # An explicit keep, or any tag the owner merged, is eligible even below n>=2 — the
    # owner asked for it, so it must actually appear (not vanish under the noise gate).
    include = keep_set | involved
    topic_counts = {t: n for t, n in counts.items() if not _to_system(t)}
    name_counts = {t: n for t, n in ent_counts.items() if not _to_system(t)}
    tags = _group_index(topic_counts, "topic", aliases, include)
    names = _group_index(name_counts, "name", aliases, include)
    # drawer = everything routed to system (machinery + any owner-moved topic/name). Shown
    # at n>=2, OR when the owner explicitly moved it (so an accepted move is never dropped).
    sys_src = {t: n for t, n in counts.items() if _to_system(t)}
    for t, n in ent_counts.items():
        if _to_system(t):
            sys_src[t] = n
    system_tags = [{"tag": t, "count": n}
                   for t, n in sorted(sys_src.items(), key=lambda kv: kv[0].lower())
                   if n >= 2 or t in move_set]

    docs = []
    for r in c.execute(
            "SELECT id, query, tags FROM nodes "
            "WHERE status='active' AND kind='artifact' "
            "ORDER BY query"):
        try:
            t = json.loads(r["tags"] or "[]")
        except Exception:
            t = []
        docs.append({
            "id": r["id"],
            "title": (r["query"] or "")[5:],   # strip 'DOC: '
            "path": next((x[5:] for x in t if x.startswith("file:")), ""),
            "made": next((x[5:] for x in t if x.startswith("made:")), "")})

    terms = [{"id": r["id"], "term": (r["query"] or "").split(":", 1)[0],
              "definition": (r["query"] or "").partition(":")[2].strip()}
             for r in c.execute(
                 "SELECT id, query FROM nodes WHERE status='active' "
                 "AND kind='term' ORDER BY query")]

    # Consolidated knowledge — the neocortex layer folded in from the old Topic-
    # hubs page (plan C4: kill the duplicate, keep its one unique section). These
    # insight/procedure nodes each absorbed several episodes during sleep.
    # The full layer is larger than the old LIMIT 20 (e.g. 92 vs 20): show all of
    # it, and carry the total so the header can say so.
    import re as _re_con
    _con_re = _re_con.compile(r"\[consolidated x(\d+)[^\]]*\]\s*")
    consolidated = []
    hidden = vault.hidden_ids()
    consolidated_total = c.execute(
        "SELECT COUNT(1) FROM nodes WHERE status='active' "
        "AND kind IN ('insight','procedure') AND tags LIKE '%consolidated%'"
    ).fetchone()[0]
    for r in c.execute("""
            SELECT id, kind, gist, query, timestamp FROM nodes
            WHERE status='active' AND kind IN ('insight','procedure')
              AND tags LIKE '%consolidated%'
            ORDER BY timestamp DESC LIMIT 200"""):
        if r["id"] in hidden:
            continue
        g = _gist(r)
        # re-consolidated nodes stack "[consolidated xN]" prefixes — display
        # only the largest merge (the node itself is untouched, append-only).
        merges = _con_re.findall(g)
        if len(merges) > 1:
            g = f"[consolidated x{max(int(m) for m in merges)}] " + _con_re.sub("", g)
        consolidated.append({"id": r["id"], "kind": r["kind"],
                             "gist": g, "ts": r["timestamp"]})

    return {"tags": tags, "names": names, "system_tags": system_tags,
            "index_overrides": overrides, "index_aliases": aliases,
            "docs": docs, "terms": terms,
            "topics": topics_data(vault), "consolidated": consolidated,
            "consolidated_total": consolidated_total}


def page_one(vault, account: "str | None" = None) -> str:
    """The model's orientation head: laws, landscape, warnings, navigation.
    ~30 lines, ~200 tokens. Prepended to orient and MCP orientation.

    account: when set, the per-project 14-day counts are scoped to THAT galaxy —
    so a Codex/GPT session sees ITS own activity, not every account's summed
    together (the old global "307" bug). A live VAULT-totals header always shows
    whole-vault scale so the project numbers never read as a total. account=None
    → global (the nightly shared file)."""
    c = vault.conn
    week = _iso_days_ago(14)
    lines = ["== CAIRN - PAGE ONE ==", f"LAWS: {_LAWS}"]
    # VAULT scale header (live, whole-vault, never account-scoped) — the honest
    # big number, so the per-project 14d counts below never read as a vault total.
    try:
        total = c.execute(
            "SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0]
        sess = c.execute(
            "SELECT COUNT(DISTINCT session) FROM nodes").fetchone()[0]
        lines.append(f"VAULT: {total:,} memories · {sess:,} sessions")
    except Exception:
        pass
    # ACTIVE honesty: a project with zero nodes in the 14d window is NOT active —
    # those collapse to one terse DORMANT: line. Order is deterministic
    # (projects.json insertion order). Counts scope to THIS galaxy when given.
    acct_sql, acct_args = "", []
    if account:
        # case-insensitive: the resolver returns the raw slug (lowercase) but
        # sessions.account stores the canonical Title-case, so match on LOWER().
        acct_sql = " AND session IN (SELECT id FROM sessions WHERE LOWER(account) = LOWER(?))"
        acct_args = [account]
    landscape = ["ACTIVE (this galaxy):" if account else "ACTIVE:"]
    dormant = []
    for tag, v in _projects().items():
        # 3-element (aliased) values must not crash page_one — index, don't unpack.
        name, desc = v[0], v[1]
        n = c.execute(
            "SELECT COUNT(*) FROM nodes WHERE status='active' "
            "AND tags LIKE ? AND timestamp >= ?" + acct_sql,
            [f'%"{tag}"%', week] + acct_args).fetchone()[0]
        if n:
            landscape.append(f"  {name} - {desc} ({n} nodes/14d)")
        else:
            dormant.append(name)
    if not any(line.startswith("  ") for line in landscape):
        landscape.append("  (none active in 14d)")
    if dormant:
        landscape.append("DORMANT: " + ", ".join(dormant))
    warn_lines = []
    warns = c.execute(
        "SELECT id, gist, query FROM nodes WHERE status='active' "
        "AND kind='warning' ORDER BY importance DESC, timestamp DESC "
        "LIMIT 3").fetchall()
    if warns:
        # inline relation suffix — a corrected warning names its correction
        # right in the banner (same line: the 34-line cap never grows).
        try:
            ann = vault.relation_annotations([w["id"] for w in warns])
        except Exception:
            ann = {}
        warn_lines.append("WARNINGS:")
        for w in warns:
            suffix = f"  ({ann[w['id']]})" if w["id"] in ann else ""
            warn_lines.append(f"  - {_gist(w)[:100]}{suffix}")
    tail = [f"NAVIGATE: {_NAVIGATE}", _CLAIM_RULE,
            "== last session's protocol follows =="]
    # cap unchanged at 34 — SECTION-AWARE assembly: laws/vault head, warnings,
    # navigation, claim check, and the protocol marker are REQUIRED and always
    # survive. Only the project landscape may trim, with an honest marker for
    # what was cut. (Timestamp of the old bug: >28 projects silently pushed
    # NAVIGATE — and then even WARNINGS — off the end.)
    budget = 34 - len(lines) - len(warn_lines) - len(tail)
    if len(landscape) > budget:
        keep = max(1, budget - 1)          # room for the "+N more" marker
        cut = len(landscape) - keep
        landscape = landscape[:keep] + [f"  … +{cut} more project line(s) — "
                                        f"full landscape: the Garden"]
    return "\n".join((lines + landscape + warn_lines + tail)[:34])


def write_book(vault, out_dir: Optional[Path] = None) -> dict:
    """Render BOOK.md (<=150 lines) and PAGE_ONE.md to ~/.cairn. Nightly."""
    out = Path(out_dir) if out_dir else Path.home() / ".cairn"
    out.mkdir(parents=True, exist_ok=True)

    b = book_data(vault)
    L = ["# THE BOOK - cairn contents",
         f"_generated {b['generated'][:16]}Z - pointers only, nothing lives here_",
         "", "## This Week"]
    for e in b["this_week"][:10]:
        L.append(f"- [{e['kind']}] {e['gist'][:90]}  `{e['id']}`")
    for p in b["projects"]:
        L.append("")
        L.append(f"## {p['name']} - {p['desc']}  ({p['total']} nodes)")
        for ch in p["chapters"][:5]:
            L.append(f"### {ch['label']}  ({ch['count']})")
            for e in ch["exemplars"][:2]:
                L.append(f"- {e['gist'][:84]}  `{e['id']}`")
    if b["volumes"]:
        L.append("")
        L.append("## Archive Volumes")
        for v in b["volumes"]:
            L.append(f"- {v['account']}: {v['sessions']} convos, "
                     f"{v['nodes']} turns ({v['first']} -> {v['last']})")
    book_path = out / "BOOK.md"
    book_path.write_text("\n".join(L[:150]) + "\n", encoding="utf-8")

    head_path = out / "PAGE_ONE.md"
    head_path.write_text(page_one(vault) + "\n", encoding="utf-8")

    return {"book": str(book_path), "page_one": str(head_path),
            "book_lines": min(len(L), 150)}
