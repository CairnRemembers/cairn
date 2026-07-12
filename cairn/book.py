"""
cairn/book.py — the Book. The project's founding idea, finally built.

March 2026, node faa0e964f099: "Cairn = Index + TOC + Glossary = Logbook =
Compass." The engine got built; the book got forgotten. This module is the
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
         "never delete | no external deps | model-agnostic")

_NAVIGATE = ("cairn fetch \"q\" before re-reading files/history | "
             "cairn wander \"topic\" for adjacent ideas | "
             "note decisions/warnings as you work | "
             "full book: ~/.cairn/BOOK.md")


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


def index_data(vault) -> dict:
    """The back-of-book Index: topics, tags, doc cards, defined terms — A to Z."""
    c = vault.conn
    counts: dict = {}
    for r in c.execute(
            "SELECT tags FROM nodes WHERE status='active' "
            "AND tags IS NOT NULL AND tags != '[]'"):
        try:
            for t in json.loads(r["tags"]):
                if isinstance(t, str) and not t.startswith(_INDEX_TAG_SKIP):
                    counts[t] = counts.get(t, 0) + 1
        except Exception:
            continue
    tags = [{"tag": t, "count": n} for t, n in
            sorted(counts.items(), key=lambda kv: kv[0].lower())
            if n >= 2][:1000]

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
    # The owner asked "is there not more than that?" — there was (92 vs the old
    # LIMIT 20): show the full layer, and carry the total so the header can say so.
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

    return {"tags": tags, "docs": docs, "terms": terms,
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
    lines.append("ACTIVE (this galaxy):" if account else "ACTIVE:")
    dormant = []
    for tag, v in _projects().items():
        # 3-element (aliased) values must not crash page_one — index, don't unpack.
        name, desc = v[0], v[1]
        n = c.execute(
            "SELECT COUNT(*) FROM nodes WHERE status='active' "
            "AND tags LIKE ? AND timestamp >= ?" + acct_sql,
            [f'%"{tag}"%', week] + acct_args).fetchone()[0]
        if n:
            lines.append(f"  {name} - {desc} ({n} nodes/14d)")
        else:
            dormant.append(name)
    if not any(line.startswith("  ") for line in lines):
        lines.append("  (none active in 14d)")
    if dormant:
        lines.append("DORMANT: " + ", ".join(dormant))
    warns = c.execute(
        "SELECT gist, query FROM nodes WHERE status='active' "
        "AND kind='warning' ORDER BY importance DESC, timestamp DESC "
        "LIMIT 3").fetchall()
    if warns:
        lines.append("WARNINGS:")
        for w in warns:
            lines.append(f"  - {_gist(w)[:100]}")
    lines.append(f"NAVIGATE: {_NAVIGATE}")
    lines.append("== last session's protocol follows ==")
    return "\n".join(lines[:34])


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
