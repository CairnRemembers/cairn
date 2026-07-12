"""
cairn/garden.py — the Garden: the human face of the vault.

The dashboard (/) is the operating room — live ops, gauges, force graph.
The Garden (/garden) is where a person tends their second brain:

  TODAY    — the temporal spine: what happened this session, auto-written
  REVIEW   — the due-pressure queue: memories the garden owes you a look at.
             Reviewing IS a recall event — it feeds the same FSRS scheduler
             the model's injection uses. One law, two gardeners.
  HUBS     — auto-MOCs: topics from tags + consolidated insight hubs
  SEARCH   — the same hybrid recall pathway the model uses (Ctrl+K)
  CAPTURE  — type a thought, it becomes a node. Human nodes carry
             model='human', speaker='user' — both gardeners attributed.

Maturity states (digital-garden convention, measured not assigned):
  🌱 seedling   stability < 3 days
  🌿 budding    3–30 days
  🌲 evergreen  30+ days, or kind=procedure

100% local: no CDN, no fonts fetched, no JS frameworks. Plain HTML/CSS/JS.
Registered onto the dashboard's FastAPI app — same port, second face.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Module-level so PEP-563 string annotations resolve in __globals__ —
# FastAPI needs to see the real Request class, not the string "Request".
try:
    from fastapi import Request
except ImportError:          # garden only registers when fastapi exists
    Request = object         # keeps `import cairn.garden` safe without it

CAPTURE_KINDS = ["idea", "insight", "decision", "open_item", "question",
                 "warning", "hypothesis", "procedure", "conversation_turn"]

# Declared projects — the human's top-level mental model. Tags map nodes in.
# Any other tag with enough nodes shows up as "emerging" automatically.
# Load from ~/.cairn/projects.json if it exists:
#   {"myapp": ["My App", "What this project is about"], ...}
# Falls back to the built-in default dict.
_PROJECTS_DEFAULT = {
    "cairn": ("Cairn",     "The memory system itself"),
    "meta":  ("The Vault", "This second brain, its charter and care"),
}

def _load_projects() -> dict:
    projects_file = Path.home() / ".cairn" / "projects.json"
    if projects_file.exists():
        try:
            raw = json.loads(projects_file.read_text(encoding="utf-8"))
            # Normalize: values may be 2-element [label, blurb] or 3-element
            # [label, blurb, [alias-tags…]]. tuple() the whole list so readers
            # that use v[0]/v[1] keep working; a 3rd element (aliases) rides along
            # for callers that want it. Both book._projects and this loader stay
            # tolerant of either length (pinned by test_garden_projects.py).
            return {k: tuple(v) for k, v in raw.items()
                    if isinstance(v, (list, tuple)) and len(v) >= 2}
        except Exception:
            pass
    return _PROJECTS_DEFAULT


def _project_aliases(tag: str) -> list:
    """The alias tags declared for a project (3rd element of its projects.json
    value), or []. A project matches its primary tag OR any of these."""
    v = PROJECTS.get(tag)
    if v and len(v) >= 3 and isinstance(v[2], (list, tuple)):
        return [str(a) for a in v[2] if isinstance(a, str)]
    return []


def _project_match_tags(tag: str) -> list:
    """Primary tag + declared aliases — the full set a project's node queries
    should union over. De-duped, primary first."""
    out = [tag]
    for a in _project_aliases(tag):
        if a and a not in out:
            out.append(a)
    return out

PROJECTS = _load_projects()


def _reload_projects() -> dict:
    """Re-read projects.json into the MODULE global so promote takes effect
    without a dashboard restart. Every reader references the module-global name
    `PROJECTS` (never a frozen local copy) — reassigning it here is seen by the
    chat/workroom/book readers on their next call. Returns the new dict."""
    global PROJECTS
    PROJECTS = _load_projects()
    return PROJECTS


# projects.json tag validation: a project tag is a plain label the human types.
# Reject quotes/angle-brackets (they'd break the JSON tag-membership LIKE and any
# HTML interpolation) and anything with control chars; keep it short.
_PROJECT_TAG_RE = re.compile(r'^[^\s"\'<>\\/][^"\'<>\\/]{0,63}$')


def _valid_tag(t: str) -> bool:
    t = (t or "").strip()
    return bool(t) and bool(_PROJECT_TAG_RE.match(t))


# ── Emerging-project noise filter ────────────────────────────────────────────
# The vault carries ~6k machine-tag strata (kw:/entity:/prov:/by:/stance:/claim…)
# that are retrieval plumbing, NOT projects. They must never be offered as
# emerging project candidates (they flooded the view 1,113→~a dozen). This is a
# DISPLAY filter only: the tags stay in the vault, in the index, in retrieval.
_NOT_PROJECT_PREFIXES = ("kw:", "entity:", "prov:", "by:", "stance:",
                         "account:", "turn:", "member:", "due:")


def _is_machine_tag(tag: str) -> bool:
    """True if the tag is machine plumbing (prefixed) — never a project."""
    return isinstance(tag, str) and tag.startswith(_NOT_PROJECT_PREFIXES)


def _family_key(tag: str) -> str:
    """Normalize a tag for GROUPING emerging candidates into one family:
    lowercase, strip a trailing possessive/plural and surrounding punctuation.
    So acme / Acmes / acme's all collapse to 'acme'. Grouping only —
    the most-frequent ORIGINAL spelling is what gets displayed."""
    s = (tag or "").strip().lower()
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s)   # strip leading/trailing punctuation
    s = re.sub(r"'s$", "", s)               # possessive: acme's → acme
    if s.endswith("s") and len(s) > 3:      # light plural: acmes → acme
        s = s[:-1]
    return s


def _gather_norm(s: str) -> str:
    """Normalize for the deep-gather CONTAINS match: lowercase, strip one
    machine prefix, drop every non-alphanumeric — so a human spelling
    ('acme') meets every buried variant ('entity:Acme Corp',
    'kw:acme corp'). Matching only; tags are never altered."""
    s = (s or "").lower()
    for p in _NOT_PROJECT_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    return re.sub(r"[^a-z0-9]+", "", s)


def _dismissed_file() -> Path:
    return Path.home() / ".cairn" / "dismissed.json"


def _load_dismissed() -> dict:
    """Emerging-card dismissals — the human's 'not a project' calls. A LOCAL
    DISPLAY preference (~/.cairn/dismissed.json), NEVER the vault: keyed by
    family key (_family_key) -> {dismissed_at, count, last_ts, name}. A missing
    or corrupt file means no dismissals, so a brand-new user starts clean.
    Append-only law untouched — not one node is archived or deleted."""
    pf = _dismissed_file()
    try:
        if pf.exists():
            data = json.loads(pf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_dismissed(data: dict) -> None:
    """Persist the dismissal map (a config write, not a vault write)."""
    pf = _dismissed_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                  encoding="utf-8")


# ── Identity: your handle. Stamped on every node you author so multi-person
# threads and imports show who-said-what, and so there's a stable author to
# attribute to. Cosmetic + foundational; lives in ~/.cairn/me.json, never
# leaves the machine.
_ME_FILE = Path.home() / ".cairn" / "me.json"
# letters/digits to start, then letters/digits/space/-/_ ; max 24
_HANDLE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 \-_]{0,23}$')

def _my_handle() -> str:
    """Your chosen handle, or '' if never set."""
    try:
        if _ME_FILE.exists():
            raw = json.loads(_ME_FILE.read_text(encoding="utf-8"))
            h = str(raw.get("handle") or "").strip()
            if h:
                return h[:24]
    except Exception:
        pass
    return ""

def _set_my_handle(h: str):
    """Validate + persist the handle, PRESERVING channels and any other me.json
    keys. A bare {"handle": h} write would wipe the `channels` map that routes
    Codex/other harnesses to their accounts (e.g. codex- -> a Codex account), so
    setting a handle in the UI must never silently collapse attribution routing.
    Returns the saved handle or None."""
    h = (h or "").strip()[:24]
    if not _HANDLE_RE.match(h):
        return None
    try:
        _ME_FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        try:
            if _ME_FILE.exists():
                cfg = json.loads(_ME_FILE.read_text(encoding="utf-8"))
                if not isinstance(cfg, dict):
                    cfg = {}
        except Exception:
            cfg = {}
        cfg["handle"] = h
        _ME_FILE.write_text(json.dumps(cfg), encoding="utf-8")
        return h
    except Exception:
        return None

# Facet/plumbing literal tags that are not projects. (Prefix-namespaced machine
# tags — kw:/entity:/prov:/by:/stance:/… — are excluded separately via
# _is_machine_tag; this set is for bare literals.)
_NOT_PROJECTS = {
    "conversation", "user", "agent", "context", "intent", "session-start",
    "garden", "human-capture", "reply", "backfill", "consolidated",
    "compress-event", "pre-compact", "checkpoint", "session-end",
    "agent-authored", "decision", "resolved", "warning", "insight",
    "strategy", "stack", "pipeline", "scraping", "data", "style",
    "white-paper", "hardware", "ip", "verification", "protocol", "dev",
    "origin", "parked", "second-brain",
    # census-driven additions: distill/import plumbing + agent identities that
    # showed up as spurious emerging cards.
    "claim", "import", "mcp", "codex", "claude", "human", "distilled",
    "test", "codex-test",
    # agent work-exhaust (2026-07-03): build/audit session vocabulary that
    # flooded the owner's project surfaces as fake emerging families. Process
    # words, never projects.
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

MEANING_KINDS_SQL = ("'decision','warning','open_item','resolved','insight',"
                     "'hypothesis','procedure','question','context_stamp','idea'")

# ── the REGISTER: life vs the machine's work (two-reader architecture) ────────
# The vault has two readers — the human and the machine — and two voices in it:
# the owner's life, and agents documenting their own engineering. Both persist
# forever (receipts are the product); the human's attention surfaces show LIFE
# by default, with a "show the machine's work" toggle. Display-only: search,
# recall, project views, and the AI see everything regardless.
PROCESS_TAGS = frozenset({
    "verified", "shipped", "handoff", "registry", "backlog", "sprint",
    "hygiene", "audit", "codex-audit", "audit-brief", "owner-ruling",
    "owner-rulings", "owner-approved", "g2", "g2-early", "phase-0",
    "lane-a", "lane-b", "lane-c", "lane-d", "lane-e", "for-codex",
    "for-all-agents", "spec", "correction", "desk-cleanup",
    "resolved-by-receipts", "twin-reseeded", "wayline", "age-gate",
    "live-gate", "family-hygiene", "codex-audit-summary", "launch-checklist",
    # the second-census work vocabulary (same words the family denylist
    # carries): notes ABOUT the garden/dashboard/capture machinery are the
    # machine's work even when they also mention a life project by name.
    "ia", "garden", "ux", "docs", "doc", "dashboard", "brain", "naming",
    "feedback", "capture", "compact-event", "launch", "security",
    "federation", "tools", "tooling", "icons",
})


def _is_process(tags, model: str = "") -> bool:
    """True when a node is the machine talking about its own work.

    A human-planted note is NEVER the machine's work — the Plant stamps
    'human-capture', and that trumps every machine word (the Plant also
    stamps 'garden' for provenance, which is in PROCESS_TAGS; without this
    guard the register hid the owner's own planted notes — G2 finding)."""
    tags = tags or []
    if any(isinstance(t, str) and t == "human-capture" for t in tags):
        return False
    return any(isinstance(t, str) and t in PROCESS_TAGS for t in tags)


def _maturity(stability: float, kind: str) -> str:
    if kind == "procedure" or (stability or 1.0) >= 30:
        return "evergreen"
    if (stability or 1.0) >= 3:
        return "budding"
    return "seedling"


def _node_dict(r) -> dict:
    """Row → JSON-safe dict for garden cards."""
    def g(key, default=None):
        try:
            v = r[key]
            return v if v is not None else default
        except (KeyError, IndexError):
            return default
    stability = float(g("stability_days", 1.0) or 1.0)
    kind      = g("kind", "note")
    return {
        "id":           g("id"),
        "kind":         kind,
        "gist":         g("gist") or (g("query") or "")[:90],
        "query":        g("query") or "",
        "preview":      g("output_preview") or "",
        "session":      g("session", ""),
        "model":        g("model", "unknown"),
        "speaker":      g("speaker", "agent"),
        "timestamp":    g("timestamp", ""),
        "tier":         int(g("memory_tier", 1) or 1),
        "importance":   int(g("importance", 5) or 5),
        "stability":    round(stability, 1),
        "flagged":      bool(g("flagged", 0)),
        "status":       g("status", "active"),
        "last_injected": g("last_injected"),
        "tags":         (tags := json.loads(g("tags") or "[]")),
        "process":      _is_process(tags, g("model", "")),
        "maturity":     _maturity(stability, kind),
    }


def _spawn_embed() -> None:
    """Best-effort background embed — never blocks a request."""
    try:
        cairn_root = Path(__file__).parent.parent
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "cairn", "embed"],
            cwd=str(cairn_root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def register_garden(app, vault, current_session_fn) -> None:
    """Mount all /garden routes onto the dashboard's FastAPI app."""
    from fastapi.responses import HTMLResponse, JSONResponse

    # ── data endpoints ────────────────────────────────────────────────────────

    @app.get("/api/garden/today")
    def garden_today(sess: str | None = None, days_ago: int = 0):
        if sess:
            # explicit per-session drill-down (back-compat)
            rows = vault.conn.execute(
                "SELECT * FROM nodes WHERE session = ? AND status != 'void' "
                "ORDER BY timestamp ASC", (sess,)).fetchall()
            return {"session": sess, "nodes": [_node_dict(r) for r in rows]}
        # default: a cross-session DATE view — everything captured that local
        # day, across all sessions. A human thinks in days, not sessions.
        # ?days_ago=N travels back one day at a time (owner: travel-back),
        # keeping the one-day-one-job shape instead of an endless append.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        days_ago = max(0, min(int(days_ago or 0), 3650))
        day0 = _dt.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0) - _td(days=days_ago)
        start = day0.astimezone(_tz.utc).isoformat()
        end = (day0 + _td(days=1)).astimezone(_tz.utc).isoformat()
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE status != 'void' AND timestamp >= ? "
            "AND timestamp < ? AND session NOT LIKE 'import-%' "
            "ORDER BY timestamp ASC", (start, end)).fetchall()
        return {"session": "today", "day": day0.strftime("%Y-%m-%d"),
                "days_ago": days_ago, "nodes": [_node_dict(r) for r in rows]}

    @app.get("/api/garden/justlanded")
    def garden_just_landed(before: str = ""):
        """The 'just landed' strip (plan C8): the newest ~10 active nodes of ANY
        kind — the cairn_logs live tail, humanized. Each row carries its
        embedded-state (embedded=False => the ○ freshness marker: captured, weaves
        in at tonight's sleep). Process markers (tool_call/interrupt/context_stamp)
        are skipped and hidden_ids respected — the same signal-vs-noise cut every
        recency surface uses. Rows are click-through to the node deep-view, which
        works pre-embedding."""
        hidden = vault.hidden_ids()
        # ?before=<iso> pages the tail older (owner: "the option to go back
        # further") — same shape, cursor on timestamp.
        cur = " AND timestamp < ?" if before else ""
        params = (before[:40],) if before else ()
        rows = vault.conn.execute(
            "SELECT id, kind, gist, query, speaker, timestamp, embedding "
            "FROM nodes WHERE status='active' "
            "AND kind NOT IN ('tool_call','interrupt','context_stamp') "
            f"AND session NOT LIKE 'import-%'{cur} "
            "ORDER BY timestamp DESC LIMIT 40", params).fetchall()
        out = []
        for r in rows:
            if r["id"] in hidden:
                continue
            gist = (r["gist"] or (r["query"] or "")[:90] or "").replace("\n", " ")
            out.append({
                "id":       r["id"],
                "kind":     r["kind"],
                "gist":     gist,
                "speaker":  r["speaker"] or "agent",
                "ts":       r["timestamp"] or "",
                "embedded": r["embedding"] is not None,
            })
            # first page stays 10 (the hub's original use); cursor pages come
            # in 30s so "go back further" has real depth.
            if len(out) >= (30 if before else 10):
                break
        return {"nodes": out, "count": len(out)}

    @app.get("/api/garden/session/{session_id}/turns")
    def garden_session_turns(session_id: str, offset: int = 0):
        """The conversation reader (plan C3): a session's turns IN ORDER, full
        fidelity. Active only, ASC by time, paged LIMIT 300 OFFSET ?, hidden
        respected. Returns each turn's best available text — episodic_text
        (verbatim, uncapped) if present, else output_preview, else query — so
        the reader shows the real conversation, not the display gist.
        speaker/model/time ride along so the client can render user/agent
        chips. `total` is the full active-turn count for this session so the
        client can offer a "load more" page once offset+300 < total; callers
        that pass no offset keep the original first-300-turns behavior."""
        hidden = vault.hidden_ids()
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE session = ? AND status='active' "
            "ORDER BY timestamp ASC LIMIT 300 OFFSET ?", (session_id, offset)).fetchall()
        total = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE session = ? AND status='active'",
            (session_id,)).fetchone()[0]
        turns = []
        for r in rows:
            if r["id"] in hidden:
                continue
            # full fidelity: episodic_text is the verbatim, uncapped turn text
            # (imports store it when the turn overflows the display cap); fall
            # back to output_preview then query so short/live turns still read.
            def _col(key):
                try:
                    return r[key]
                except (KeyError, IndexError):
                    return None
            text = _col("episodic_text") or _col("output_preview") or r["query"] or ""
            turns.append({
                "id":      r["id"],
                "kind":    r["kind"],
                "speaker": r["speaker"] or "agent",
                "model":   r["model"] or "unknown",
                "ts":      r["timestamp"] or "",
                "text":    text,
            })
        return {"session": session_id, "turns": turns, "count": len(turns),
                "offset": offset, "total": total}

    @app.get("/api/garden/review")
    def garden_review():
        # Same due-pressure law the model's heartbeat uses. The human review
        # queue and the model's injection feed are one scheduler, two readers.
        rows = vault.conn.execute(f"""
            SELECT *,
              ( (julianday('now') - julianday(COALESCE(last_injected,'2020-01-01')))
                / MAX(COALESCE(stability_days,1.0), 0.1) )
              * COALESCE(importance,5) AS due_pressure
            FROM nodes
            WHERE status='active' AND memory_tier <= 1
              AND kind IN ({MEANING_KINDS_SQL})
            ORDER BY due_pressure DESC
            LIMIT 12
        """).fetchall()
        return {"nodes": [_node_dict(r) for r in rows]}

    @app.get("/api/garden/projects")
    def garden_projects():
        """The human home view: projects with status rollups."""
        # gather nodes per tag in one pass
        by_tag: dict[str, list] = {}
        for r in vault.conn.execute(
                "SELECT id, session, tags, kind, stability_days, timestamp, gist, query FROM nodes WHERE status='active'").fetchall():
            try:
                for t in json.loads(r["tags"] or "[]"):
                    if isinstance(t, str) and not t.startswith("member:"):
                        by_tag.setdefault(t, []).append(r)
            except Exception:
                continue

        def rollup(tag: str, name: str, desc: str, emerging: bool,
                   union_over: list | None = None) -> dict:
            # union_over: the full tag set to count over (primary + aliases /
            # family spellings), deduped by node id so a node tagged both
            # primary + alias counts once. Defaults to [tag] — the single-tag
            # case. This is the P1 leftover fix: the LIST rollup now unions the
            # same family the DETAIL view already does, so a promoted project's
            # counts include its alias tags (Acme's silver/hallmark nodes).
            tags = union_over if union_over is not None else [tag]
            seen_ids, rows = set(), []
            for t in tags:
                for r in by_tag.get(t, []):
                    rid = r["id"] if "id" in r.keys() else id(r)
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    rows.append(r)
            kinds: dict[str, int] = {}
            mats  = {"seedling": 0, "budding": 0, "evergreen": 0}
            last_ts, last_gist = "", ""
            for r in rows:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                mats[_maturity(r["stability_days"] or 1.0, r["kind"])] += 1
                if (r["timestamp"] or "") > last_ts and r["kind"] != "tool_call":
                    last_ts = r["timestamp"] or ""
                    try:
                        last_gist = r["gist"] or (r["query"] or "")[:80]
                    except (KeyError, IndexError):
                        last_gist = (r["query"] or "")[:80]
            return {
                "tag": tag, "name": name, "desc": desc, "emerging": emerging,
                "total": len(rows),
                "open":       kinds.get("open_item", 0),
                "decisions":  kinds.get("decision", 0),
                "procedures": kinds.get("procedure", 0),
                "warnings":   kinds.get("warning", 0),
                "maturity":   mats,
                "last_ts":    last_ts, "last_gist": last_gist,
            }

        projects = [rollup(t, v[0], v[1], False, _project_match_tags(t))
                    for t, v in PROJECTS.items()
                    if any(by_tag.get(mt) for mt in _project_match_tags(t))]
        # declared-project tags (primary + aliases) never re-appear as emerging.
        declared = set()
        for t in PROJECTS:
            declared.update(_project_match_tags(t))

        # emerging: untracked tags with real mass, AFTER stripping the machine-tag
        # strata (kw:/entity:/prov:/… + literal plumbing) that flooded the view.
        # Survivors are grouped into FAMILIES by normalized key so spelling
        # variants (acme/Acmes/acme's) count as one; the family is
        # displayed under its most-frequent original spelling, with all its
        # variant tags carried so the card and its downstream query see the union.
        families: dict[str, dict] = {}
        for tag, rows in by_tag.items():
            if tag in declared or tag in _NOT_PROJECTS or _is_machine_tag(tag):
                continue
            key = _family_key(tag)
            if not key:
                continue
            fam = families.setdefault(key, {"spellings": {}, "tags": []})
            fam["spellings"][tag] = fam["spellings"].get(tag, 0) + len(rows)
            fam["tags"].append(tag)

        # 'not a project' dismissals — a local display preference, not the vault.
        dismissed = _load_dismissed()
        for key, fam in families.items():
            # union the family's nodes (a node tagged both acme + acmes
            # counts once); apply the >=6 threshold to the whole family.
            seen_ids, union_rows = set(), []
            for t in fam["tags"]:
                for r in by_tag.get(t, []):
                    rid = r["id"] if "id" in r.keys() else id(r)
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    union_rows.append(r)
            # structural hygiene (owner, 2026-07-03): a real project spans TIME
            # and CONVERSATIONS. One session's work-exhaust tags — however many
            # nodes they collect in a night — never qualify as a project family.
            sessions = {r["session"] for r in union_rows if r["session"]}
            days = {(r["timestamp"] or "")[:10] for r in union_rows}
            if len(union_rows) < 6 or len(sessions) < 2 or len(days) < 2:
                continue
            # display spelling = the variant carrying the most nodes
            display = max(fam["spellings"].items(), key=lambda kv: kv[1])[0]
            # rollup now unions the whole family (same path the declared cards
            # use), so counts/maturity/last already reflect the union — no
            # manual recompute needed.
            card = rollup(display, display, "emerging topic", True, fam["tags"])
            card["aliases"] = sorted(t for t in fam["tags"] if t != display)
            snap = dismissed.get(key)
            if snap and not (
                    card["total"] > int(snap.get("count") or 0)
                    or str(card["last_ts"] or "") > str(snap.get("last_ts") or "")):
                # Hidden by a 'not a project' click, and no genuinely new
                # evidence since (the family neither grew nor gained a newer
                # node). Display-only: every memory stays active in the vault.
                continue
            projects.append(card)
        projects.sort(key=lambda p: p["last_ts"], reverse=True)
        # 'show hidden' payload: dismissed families that have NOT re-surfaced
        # (a re-surfaced family already sits in `projects`, so drop it here).
        emerging_keys = {_family_key(p["tag"]) for p in projects if p.get("emerging")}
        hidden = [{"key": k, "name": (v.get("name") or k),
                   "dismissed_at": (v.get("dismissed_at") or ""),
                   "count": (v.get("count") or 0)}
                  for k, v in dismissed.items() if k not in emerging_keys]
        return {"projects": projects, "dismissed": hidden}

    def _like(term: str) -> str:
        """Escape LIKE wildcards in user-supplied input. Pair with ESCAPE '\\'.
        Without this, a tag of '%' matches every node — pattern injection."""
        return (term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_"))

    # Per-IP sliding-window rate limit for write endpoints - backstop against
    # runaway or abusive clients.
    _rate_hits: dict = {}
    RATE_MAX, RATE_WINDOW_S = 30, 60.0
    MAX_TEXT_LEN = 20_000

    def _rate_ok(request) -> bool:
        import time as _time
        ip = request.client.host if request and request.client else "?"
        now = _time.monotonic()
        hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_WINDOW_S]
        if len(hits) >= RATE_MAX:
            _rate_hits[ip] = hits
            return False
        hits.append(now)
        _rate_hits[ip] = hits
        if len(_rate_hits) > 200:    # bound the table itself
            _rate_hits.clear()
        return True

    @app.post("/api/garden/promote")
    async def garden_promote_project(payload: dict, request: Request):
        """Promote an emerging family to a DECLARED project — the one write this
        surface makes to ~/.cairn/projects.json (config, not the vault). It:
          • reads projects.json with create-if-missing semantics (same shape as
            _load_projects), REFUSES if the tag key already exists (never
            overwrites a declared project),
          • appends [name, blurb, [aliases…]] (aliases optional),
          • writes the file and hot-reloads the module PROJECTS global so the
            chat/workroom/book readers see it without a restart.
        Same-origin CSRF guard (dashboard middleware) already covers this POST."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        tag   = str(payload.get("tag") or "").strip()
        name  = str(payload.get("name") or "").strip()[:80] or tag
        blurb = str(payload.get("blurb") or "").strip()[:200]
        raw_aliases = payload.get("aliases") or []
        if not _valid_tag(tag):
            return JSONResponse(
                {"error": "invalid tag (no quotes/angle brackets/slashes)"},
                status_code=400)
        # validate every alias too; silently drop ones that collide with the tag
        aliases = []
        if isinstance(raw_aliases, (list, tuple)):
            for a in raw_aliases:
                a = str(a).strip()
                if a and a != tag and _valid_tag(a) and a not in aliases:
                    aliases.append(a[:64])
        aliases = aliases[:40]

        pf = Path.home() / ".cairn" / "projects.json"
        try:
            data = {}
            if pf.exists():
                try:
                    data = json.loads(pf.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            if tag in data:
                return JSONResponse(
                    {"error": f"'{tag}' is already a declared project"},
                    status_code=409)
            value = [name, blurb]
            if aliases:
                value.append(aliases)
            data[tag] = value
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"},
                                status_code=500)
        _reload_projects()   # module global — readers pick it up on next call
        return {"tag": tag, "name": name, "blurb": blurb,
                "aliases": aliases, "promoted": True}

    @app.post("/api/garden/greeting")
    async def garden_set_greeting(payload: dict, request: Request):
        """Set (or clear) the Hub's custom welcome greeting. Config, NOT the vault
        (~/.cairn/settings.json) — a mutable UI setting, so it never touches the
        append-only memory. Empty/blank text resets to the default line. Settable
        from the click-to-edit headline or `cairn hello "…"`. Same-origin CSRF
        guard (dashboard middleware) already covers this POST."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        text = str(payload.get("text") or "").strip()[:200]
        sf = Path.home() / ".cairn" / "settings.json"
        try:
            data = {}
            if sf.exists():
                try:
                    data = json.loads(sf.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            if text:
                data["greeting"] = text
            else:
                data.pop("greeting", None)
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)
        return {"greeting": text, "ok": True}

    @app.post("/api/garden/file-under")
    async def garden_file_under(payload: dict, request: Request):
        """File an emerging family under an EXISTING declared project — the
        alias write. G2 finding: 'Promote' was the only door, but some
        emergents BELONG to a project already (the trademark research was
        Cairn work wearing its own tag). Appends the tag to the project's
        alias list in projects.json; the family folds into that project on
        the next render. Config write, not the vault — append-only holds."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        tag  = str(payload.get("tag") or "").strip()
        proj = str(payload.get("project") or "").strip()
        if not _valid_tag(tag):
            return JSONResponse(
                {"error": "invalid tag (no quotes/angle brackets/slashes)"},
                status_code=400)
        pf = Path.home() / ".cairn" / "projects.json"
        try:
            data = {}
            if pf.exists():
                try:
                    data = json.loads(pf.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            if proj not in data:
                return JSONResponse(
                    {"error": f"'{proj}' is not a declared project"},
                    status_code=404)
            if tag in data:
                return JSONResponse(
                    {"error": f"'{tag}' is itself a declared project"},
                    status_code=409)
            value = data[proj]
            if not isinstance(value, list) or len(value) < 2:
                return JSONResponse(
                    {"error": f"'{proj}' has an unexpected shape"},
                    status_code=500)
            aliases = value[2] if len(value) > 2 and isinstance(value[2], list) else []
            if tag not in aliases:
                aliases.append(tag[:64])
            if len(value) > 2:
                value[2] = aliases
            else:
                value.append(aliases)
            pf.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"},
                                status_code=500)
        _reload_projects()
        return {"tag": tag, "project": proj, "filed": True}

    @app.get("/api/garden/syslog")
    def garden_syslog():
        """The self-correction log (owner ask, worded his way: 'if you hid
        something... if you're unsure it goes to like a error log'). One
        quiet place listing what the system did ON ITS OWN — audit reports,
        clusters gated off Topics as import blobs, flagged memories —
        reviewable and fixable, never an alarm."""
        out = {"audits": [], "gated_topics": [], "flagged": []}
        try:
            for r in vault.conn.execute("""
                    SELECT id, output_preview, timestamp FROM nodes
                    WHERE status='active' AND tags LIKE '%"cairn-audit"%'
                    ORDER BY timestamp DESC LIMIT 10"""):
                body = (r["output_preview"] or "").split(" (as of", 1)[0]
                body = body.replace("CAIRN AUDIT — ", "", 1)
                out["audits"].append({
                    "id": r["id"], "ts": r["timestamp"],
                    "findings": [f.strip() for f in body.split("; ") if f.strip()]})
        except Exception:
            pass
        try:
            # recompute the import-blob gate's victims so the human can
            # inspect (and one day overrule) what topics_data filtered out
            blobs: dict = {}
            for r in vault.conn.execute(
                    "SELECT community, "
                    "       (tags LIKE '%\"prov:distilled\"%') AS d FROM nodes "
                    "WHERE status='active' AND community IS NOT NULL "
                    "AND community != ''"):
                cid, _, label = (r["community"] or "").partition("|")
                if not label.strip():
                    continue
                b = blobs.setdefault(cid, {"cid": cid, "label": label.strip(),
                                           "total": 0, "dist": 0})
                b["total"] += 1
                b["dist"] += 1 if r["d"] else 0
            out["gated_topics"] = [
                {"cid": b["cid"], "label": b["label"], "total": b["total"]}
                for b in blobs.values()
                if b["total"] >= 20 and b["dist"] / b["total"] > 0.8]
        except Exception:
            pass
        try:
            for r in vault.conn.execute("""
                    SELECT id, kind, substr(COALESCE(gist, query),1,90) g,
                           timestamp FROM nodes
                    WHERE status='active' AND flagged=1
                    ORDER BY timestamp DESC LIMIT 20"""):
                out["flagged"].append({"id": r["id"], "kind": r["kind"],
                                       "gist": r["g"], "ts": r["timestamp"]})
        except Exception:
            pass
        return out

    @app.post("/api/garden/demote-project")
    async def garden_demote_project(payload: dict, request: Request):
        """The exit for an accidental promote: removes the projects.json
        entry so the family returns to emerging. Config-only — not one vault
        node is touched (append-only untouched); if the slug has a registry
        row, an 'archived' action is APPENDED so the ledger remembers the
        demotion instead of pretending it never happened."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        tag = str(payload.get("tag") or "").strip()
        pf = Path.home() / ".cairn" / "projects.json"
        try:
            data = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
            if not isinstance(data, dict) or tag not in data:
                return JSONResponse({"error": f"'{tag}' is not a declared project"},
                                    status_code=404)
            removed = data.pop(tag)
            pf.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"},
                                status_code=500)
        _reload_projects()
        try:
            from cairn.registry import act as _reg_act
            _reg_act(vault, tag, "archive",
                     reason="demoted from Garden (accidental promote exit)",
                     by="human")
        except Exception:
            pass
        return {"tag": tag, "demoted": True,
                "name": removed[0] if isinstance(removed, list) and removed else tag}

    @app.post("/api/garden/dismiss-project")
    async def garden_dismiss_project(payload: dict, request: Request):
        """'Not a project' on an EMERGING card. Emerging topics are detected
        tag-families (not registry rows), so there is no status to flip —
        instead we record a LOCAL display preference in ~/.cairn/dismissed.json
        and the projects view filters against it. NOTHING in the vault is
        touched: no node archived, no tag removed, the append-only law intact.
        The snapshot (node count + latest timestamp AT dismissal) lets the card
        re-surface only when genuinely new evidence lands — never a permanent
        grave. Same-origin CSRF guard (dashboard middleware) covers this POST."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        tag = str(payload.get("tag") or "").strip()
        if not _valid_tag(tag):
            return JSONResponse({"error": "invalid tag"}, status_code=400)
        key = _family_key(tag)
        if not key:
            return JSONResponse({"error": "empty family key"}, status_code=400)
        # Authoritative snapshot from the LIVE emerging computation — never trust
        # a client-supplied count. Find the family this tag belongs to right now.
        snap_count, snap_last = 0, ""
        for p in garden_projects()["projects"]:
            if p.get("emerging") and _family_key(p["tag"]) == key:
                snap_count = int(p.get("total") or 0)
                snap_last  = str(p.get("last_ts") or "")
                break
        data = _load_dismissed()
        data[key] = {"dismissed_at": datetime.now(timezone.utc).isoformat(),
                     "count": snap_count, "last_ts": snap_last, "name": tag}
        try:
            _save_dismissed(data)
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"},
                                status_code=500)
        return {"tag": tag, "key": key, "dismissed": True}

    @app.post("/api/garden/undismiss-project")
    async def garden_undismiss_project(payload: dict, request: Request):
        """Undo a 'not a project' — the family returns to emerging on the next
        render. Config-only, the mirror of dismiss. Accepts the family key (from
        the dismiss response / hidden list) or a raw tag, which we normalize."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        raw  = str(payload.get("key") or payload.get("tag") or "").strip()
        data = _load_dismissed()
        key  = raw if raw in data else _family_key(raw)
        if key not in data:
            return JSONResponse({"error": "not dismissed"}, status_code=404)
        data.pop(key, None)
        try:
            _save_dismissed(data)
        except Exception as exc:
            return JSONResponse({"error": f"write failed: {exc}"},
                                status_code=500)
        return {"key": key, "restored": True}

    @app.get("/api/garden/registry")
    def garden_registry():
        """The project registry ledger, folded — every project ever proposed,
        blessed, passed or archived (Lane B: no project ever lost)."""
        try:
            from cairn.registry import rows
            return {"rows": sorted(rows(vault).values(),
                                   key=lambda s: -(s.get("evidence") or 0))}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/garden/registry/act")
    async def garden_registry_act(payload: dict, request: Request):
        """Human verdict on a registry row: bless / pass / archive / revive.
        Appends a ledger node (never edits). BLESS also declares the project
        in projects.json with the row's alias set, so its scattered strata
        regroup as one project on the next render — the owner's ruling:
        'when i approve it regroups as a project'."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        slug   = str(payload.get("slug") or "").strip()
        action = str(payload.get("action") or "").strip()
        reason = str(payload.get("reason") or "").strip()[:200]
        try:
            from cairn.registry import act
            state = act(vault, slug, action, reason=reason, by="human")
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        if state is None:
            return JSONResponse({"error": f"unknown slug or action"},
                                status_code=400)
        if action == "bless":
            try:
                pf = Path.home() / ".cairn" / "projects.json"
                data = {}
                if pf.exists():
                    try:
                        data = json.loads(pf.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            data = {}
                    except Exception:
                        data = {}
                if slug not in data:
                    aliases = [a for a in (state.get("aliases") or [])
                               if _valid_tag(a)][:40]
                    value = [state.get("name") or slug,
                             (state.get("why") or "")[:200]]
                    if aliases:
                        value.append(aliases)
                    data[slug] = value
                    pf.parent.mkdir(parents=True, exist_ok=True)
                    pf.write_text(json.dumps(data, indent=2,
                                             ensure_ascii=False),
                                  encoding="utf-8")
                    _reload_projects()
            except Exception as exc:
                return {"state": state,
                        "warning": f"blessed in ledger, projects.json write "
                                   f"failed: {exc}"}
        return {"state": state}

    @app.get("/api/garden/gather")
    def garden_gather(q: str = ""):
        """Deep-gather (plan P3.5): find a project BURIED in machine tags.
        Backfilled history carries project identity almost entirely in
        kw:/entity: distill tags (e.g. ~250 nodes for a backfilled project with no bare human
        tag), which the emerging view rightly denies as candidates. This is
        the deliberate path: a normalized contains-search over EVERY active
        tag — machine strata INCLUDED, because the denylist gates discovery,
        not reunification — returning variant candidates with counts, the
        distinct-node total, and sample gists, ready to feed the promote form
        as aliases. Read-only; the only write stays promote's."""
        needle = _gather_norm(q)
        if len(needle) < 3:
            return {"q": q, "candidates": [], "total": 0, "samples": []}
        counts: dict = {}
        ids: set = set()
        samples: list = []
        for r in vault.conn.execute(
                "SELECT id, tags, gist, query FROM nodes WHERE status='active'"):
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                continue
            hit = False
            for t in tags:
                if isinstance(t, str) and needle in _gather_norm(t):
                    counts[t] = counts.get(t, 0) + 1
                    hit = True
            if hit and r["id"] not in ids:
                ids.add(r["id"])
                if len(samples) < 3:
                    g = (r["gist"] or (r["query"] or "")[:90] or "")
                    samples.append(g.replace("\n", " "))
        cands = [{"tag": t, "count": n} for t, n in
                 sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:24]
        return {"q": q, "candidates": cands, "total": len(ids),
                "samples": samples}

    @app.get("/api/garden/flagged")
    def garden_flagged():
        """The flagged view — active flagged=1 nodes, newest first, hidden_ids
        respected. Flags used to vanish into luck; this is their one home."""
        hidden = vault.hidden_ids()
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE status='active' AND flagged=1 "
            "ORDER BY timestamp DESC LIMIT 100").fetchall()
        nodes = [_node_dict(r) for r in rows if r["id"] not in hidden]
        return {"nodes": nodes, "count": len(nodes)}

    @app.get("/api/garden/project/{tag}")
    def garden_project(tag: str):
        """Deep view, human-ordered: attention first, then knowledge, then story.
        A declared project matches its primary tag OR any of its alias tags, so a
        promoted family (e.g. acme + acmes + kw:silver) reads as one."""
        match_tags = _project_match_tags(tag)   # primary + declared aliases
        clause = " OR ".join("tags LIKE ? ESCAPE '\\'" for _ in match_tags)
        params = [f'%"{_like(t)}"%' for t in match_tags]
        seen, rows = set(), []
        for r in vault.conn.execute(
                f"SELECT * FROM nodes WHERE status='active' AND ({clause}) "
                f"ORDER BY timestamp DESC LIMIT 200", params).fetchall():
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            rows.append(r)
        nodes = [_node_dict(r) for r in rows]
        v = PROJECTS.get(tag, (tag, "emerging topic"))
        name, desc = v[0], v[1]
        # librarian stats — the readable header's raw material (one aggregate).
        st = vault.conn.execute(
            f"SELECT COUNT(1), MIN(timestamp), MAX(timestamp), "
            f"COUNT(DISTINCT session) FROM nodes "
            f"WHERE status='active' AND ({clause})", params).fetchone()
        stats = {"total": st[0], "first_ts": st[1] or "", "last_ts": st[2] or "",
                 "sessions": st[3]}
        # DROPS v1 (owner asked three times): the Exchange folder is the ingest
        # surface — files dropped into Exchange\<tag>\ list on the project page,
        # live from disk (no auto-written nodes; note-taking stays human/agent
        # deliberate). Karpathy raw/ + GBrain source-registration, Cairn idiom.
        files = []
        try:
            drop_dir = Path.home() / "Exchange" / tag
            if drop_dir.is_dir():
                for f in sorted(drop_dir.iterdir(),
                                key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                    if f.is_file():
                        st = f.stat()
                        files.append({"name": f.name, "path": str(f),
                                      "kb": round(st.st_size / 1024, 1),
                                      "mtime": datetime.fromtimestamp(
                                          st.st_mtime, tz=timezone.utc).isoformat()})
        except Exception:
            files = []
        return {
            "tag": tag, "name": name, "desc": desc, "files": files, "stats": stats,
            "declared": tag in PROJECTS,
            "attention": [n for n in nodes
                          if n["kind"] in ("open_item", "warning", "blocker")
                          or n["flagged"]][:12],
            "decisions":  [n for n in nodes if n["kind"] == "decision"][:10],
            "procedures": [n for n in nodes if n["kind"] == "procedure"][:10],
            "knowledge":  [n for n in nodes
                           if n["kind"] in ("insight", "hypothesis")][:10],
            "recent":     [n for n in nodes
                           if n["kind"] not in ("tool_call", "interrupt")][:10],
        }

    @app.get("/api/garden/desk")
    def garden_desk():
        """
        The assistant surface: everything that needs attention, across all
        projects, in one place. Open items, blockers, warnings, flags —
        sorted by due date (if a 'due:YYYY-MM-DD' tag exists), then
        importance, then age. The to-do list that wrote itself.
        """
        # LIVE gate (owner ruling 2026-07-03): imported/backfilled history is
        # reference, not chores — it never surfaces as Desk work. Display-only:
        # those nodes stay fully active for the AI's recall.
        _LIVE = ("AND session NOT LIKE 'import-%' "
                 "AND COALESCE(tags,'') NOT LIKE '%\"prov:distilled\"%'")
        _LIVE_N = ("AND n.session NOT LIKE 'import-%' "
                   "AND COALESCE(n.tags,'') NOT LIKE '%\"prov:distilled\"%'")
        # A warning that a later `resolved` node references (by id, anywhere in
        # its text) has been answered — it leaves the Desk without needing a
        # void. Append-only: nothing changes on the warning itself.
        import re as _rex
        resolved_refs: set = set()
        for rr in vault.conn.execute(
                "SELECT query, output_preview FROM nodes "
                "WHERE status='active' AND kind='resolved'"):
            for fld in (rr["query"], rr["output_preview"]):
                resolved_refs.update(_rex.findall(r"\b[0-9a-f]{12}\b", fld or ""))

        # Open loops and Watch are queried SEPARATELY so a pile of high-
        # importance warnings can never crowd real open items out of the
        # shared LIMIT (the bug that made the Desk show "0 open" while the
        # vault held real loops).
        rows = vault.conn.execute(f"""
            SELECT * FROM nodes
            WHERE status='active'
              AND kind IN ('open_item','blocker','question') {_LIVE}
            ORDER BY importance DESC, timestamp ASC
            LIMIT 60
        """).fetchall() + vault.conn.execute(f"""
            SELECT * FROM nodes
            WHERE status='active'
              AND (kind='warning' OR flagged=1) {_LIVE}
            ORDER BY timestamp DESC
            LIMIT 30
        """).fetchall()

        hidden = vault.hidden_ids()   # archived/snoozed leave the Desk (stay active for the AI)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dated, undated, watch = [], [], []
        _seen_desk: set = set()
        for r in rows:
            if r["id"] in hidden or r["id"] in resolved_refs or r["id"] in _seen_desk:
                continue
            _seen_desk.add(r["id"])
            n = _node_dict(r)
            due = next((t.split(":", 1)[1] for t in n["tags"]
                        if isinstance(t, str) and t.startswith("due:")), None)
            n["due"] = due
            n["overdue"] = bool(due and due < today)
            # OPEN LOOPS = open_items/blockers (plan C2). A DATED item of any of
            # those kinds — or a question that was explicitly dated — is an
            # Overdue/Dated row. An UNdated question is NOT an open loop: it flows
            # to Tend as a "loose thread" (its home per the plan). warnings/flags
            # → Watch.
            if n["kind"] in ("open_item", "blocker"):
                (dated if due else undated).append(n)
            elif n["kind"] == "question":
                if due:
                    dated.append(n)          # a dated question is a real todo
                # undated question falls through to Tend (loose thread)
            else:
                watch.append(n)   # warnings + flags — keep an eye on
        dated.sort(key=lambda n: n["due"])
        watch.sort(key=lambda n: n["timestamp"] or "", reverse=True)  # warnings/flags newest-first
        watch = watch[:8]                                             # Desk is triage, not a dump

        # 📥 Inbox — phone captures awaiting filing (any kind, tag 'inbox')
        inbox_rows = vault.conn.execute("""
            SELECT * FROM nodes WHERE status='active' AND tags LIKE '%"inbox"%'
            ORDER BY timestamp DESC LIMIT 30
        """).fetchall()
        inbox = [_node_dict(r) for r in inbox_rows if r["id"] not in hidden]

        # ── TEND — the one "what am I forgetting?" section (plan C2). Merges four
        # forgetting engines into ONE deduped list, each row carrying WHY it
        # surfaced: the FSRS review queue ("overdue review"), the neglect-ranked
        # fading list ("fading"), Spark's loose threads ("loose thread") and its
        # from-the-deep scan ("from the deep"). A node can qualify on several
        # engines — we keep every reason (`reasons`) and show the highest-priority
        # one first (`reason`). Nodes already on the Desk (open loops / dated /
        # watch / inbox) are excluded so nothing double-shows; hidden respected.
        already = {n["id"] for n in dated} | {n["id"] for n in undated} \
                  | {n["id"] for n in watch} | {n["id"] for n in inbox} | hidden
        tend_by_id: dict = {}   # id → node dict (with reasons[])

        def _tend_add(r, reason: str):
            nid = r["id"]
            if nid in already or nid in resolved_refs:
                return
            n = tend_by_id.get(nid)
            if n is None:
                n = _node_dict(r)
                n["reasons"] = []
                tend_by_id[nid] = n
            if reason not in n["reasons"]:
                n["reasons"].append(reason)

        # 1. overdue review — same due-pressure law the model's heartbeat uses
        for r in vault.conn.execute(f"""
            SELECT *,
              ( (julianday('now') - julianday(COALESCE(last_injected,'2020-01-01')))
                / MAX(COALESCE(stability_days,1.0), 0.1) )
              * COALESCE(importance,5) AS due_pressure
            FROM nodes
            WHERE status='active' AND memory_tier <= 1
              AND kind IN ({MEANING_KINDS_SQL}) {_LIVE}
              AND julianday('now') - julianday(timestamp) >= 3
            ORDER BY due_pressure DESC LIMIT 12
        """).fetchall():
            _tend_add(r, "overdue review")

        # 2. fading — neglect-ranked (importance-led, FSRS-overdue tie-broken):
        #    valuable memories shown but never cited, slipping past schedule.
        for r in vault.conn.execute(f"""
            SELECT n.*, COUNT(l.id) AS shown_cnt,
                   ( (julianday('now')
                        - julianday(COALESCE(n.last_injected, '2020-01-01')))
                     / MAX(COALESCE(n.stability_days, 1.0), 0.1) ) AS neglect
            FROM nodes n JOIN attention_ledger l ON l.node_id = n.id
            WHERE n.status='active' AND l.cited = 0 {_LIVE_N}
            GROUP BY n.id HAVING shown_cnt >= 2
            ORDER BY COALESCE(n.importance, 5) DESC, neglect DESC, shown_cnt DESC
            LIMIT 8
        """).fetchall():
            _tend_add(r, "fading")

        # 3. loose threads — questions/hypotheses opened, never pulled (no active
        #    children). Conversation topics that got away (Spark's math, moved here).
        for r in vault.conn.execute(f"""
            SELECT n.* FROM nodes n
            WHERE n.status='active' AND n.kind IN ('question','hypothesis')
              {_LIVE_N}
              AND NOT EXISTS (SELECT 1 FROM nodes c
                              WHERE c.parent = n.id AND c.status='active')
            ORDER BY n.timestamp ASC LIMIT 8
        """).fetchall():
            _tend_add(r, "loose thread")

        # 4. from the deep — old high-importance insights/decisions/hypotheses
        #    you haven't seen in the longest (overdue-pressure ordering).
        for r in vault.conn.execute(f"""
            SELECT *,
              ( (julianday('now') - julianday(COALESCE(last_injected,'2020-01-01')))
                / MAX(COALESCE(stability_days,1.0), 0.1) )
              * COALESCE(importance,5) AS dp
            FROM nodes
            WHERE status='active' AND importance >= 6
              AND kind IN ('insight','decision','hypothesis') {_LIVE}
              AND julianday('now') - julianday(timestamp) >= 3
            ORDER BY dp DESC LIMIT 6
        """).fetchall():
            _tend_add(r, "from the deep")

        # primary reason = first (priority order above); cap the section — Tend is
        # a nudge, not a dump. Order: multi-reason first, then by importance.
        tend = list(tend_by_id.values())
        for n in tend:
            n["reason"] = n["reasons"][0]
        tend.sort(key=lambda n: (-len(n["reasons"]), -(n.get("importance") or 5)))
        tend = tend[:12]

        # the PROBLEMS strip (owner ask: "somewhere easy to check in on and
        # fix"): the newest audit-organ report, split back into its findings,
        # plus the flagged count — system trouble gets one corner, on top.
        problems = {"findings": [], "as_of": "", "node": "", "flagged": 0}
        try:
            row = vault.conn.execute("""
                SELECT id, output_preview, timestamp FROM nodes
                WHERE status='active' AND tags LIKE '%"cairn-audit"%'
                ORDER BY timestamp DESC LIMIT 1""").fetchone()
            if row:
                body = (row["output_preview"] or "").split(" (as of", 1)[0]
                body = body.replace("CAIRN AUDIT — ", "", 1)
                problems = {"findings": [f.strip() for f in body.split("; ")
                                         if f.strip()],
                            "as_of": row["timestamp"], "node": row["id"],
                            "flagged": 0}
            problems["flagged"] = vault.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE status='active' "
                "AND flagged=1").fetchone()[0]
        except Exception:
            pass

        return {"dated": dated, "open": undated, "tend": tend,
                "watch": watch, "inbox": inbox, "problems": problems,
                "counts": {"todo": len(dated) + len(undated),
                           "tend": len(tend),
                           "watch": len(watch), "inbox": len(inbox)}}

    @app.post("/api/garden/node/{node_id}/done")
    async def garden_done(node_id: str, payload: dict | None = None):
        """
        Mark an open item done — the append-only way: write a resolved node
        chained to it, THEN void the open item. History preserved, desk cleared.
        """
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        note = (payload or {}).get("note") or ""
        from cairn.vault import MicroNode
        resolved = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = "resolved",
            query       = (note or f"done: {(row['query'] or '')[:200]}"),
            parent      = node_id,
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            tags        = ["garden", "desk-done"],
        ))
        vault.void(node_id)
        _spawn_embed()
        return {"resolved": resolved.id, "voided": node_id}

    @app.post("/api/garden/node/{resolved_id}/undo-done")
    async def garden_undo_done(resolved_id: str):
        """Undo a mis-clicked done — the append-only way (the DB trigger
        rightly refuses to un-void anything): re-raise the original item as
        a NEW open_item chained to it, and void the accidental done-receipt.
        History keeps the click AND the regret; the Desk gets the item back.
        Born the day the owner done'd the provisional patent by accident."""
        receipt = vault.get(resolved_id)
        if not receipt or receipt["kind"] != "resolved" or not receipt["parent"]:
            return JSONResponse({"error": "not an undoable done-receipt"},
                                status_code=400)
        orig = vault.get(receipt["parent"])
        if not orig:
            return JSONResponse({"error": "original item missing"},
                                status_code=404)
        try:
            tags = [t for t in json.loads(orig["tags"] or "[]")
                    if isinstance(t, str) and t != "restored-after-misclick"]
        except Exception:
            tags = []
        from cairn.vault import MicroNode
        fresh = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = orig["kind"] or "open_item",
            query       = orig["query"],
            output_preview = (orig["output_preview"] or orig["query"] or ""),
            parent      = orig["id"],
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            tags        = tags + ["restored-after-misclick"],
        ))
        vault.void(resolved_id)
        _spawn_embed()
        return {"restored": fresh.id, "receipt_voided": resolved_id}

    # ── set-aside actions: archive / snooze are reversible HIDE flags. The node
    # stays active (the AI still sees it); restore just drops the flag. No void,
    # no confirm popups — one click, with an undo toast on the client.
    @app.post("/api/garden/node/{node_id}/archive")
    async def garden_archive(node_id: str):
        if not vault.get(node_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        vault.archive(node_id)
        return {"archived": node_id}

    @app.post("/api/garden/node/{node_id}/unarchive")
    async def garden_unarchive(node_id: str):
        vault.unarchive(node_id)
        return {"unarchived": node_id}

    @app.post("/api/garden/node/{node_id}/snooze")
    async def garden_snooze(node_id: str, payload: dict | None = None):
        """Hide until a date (body {'until': ISO}); defaults to 7 days out."""
        if not vault.get(node_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        until = (payload or {}).get("until") or ""
        if not until:
            from datetime import timedelta
            until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        vault.snooze(node_id, until)
        return {"snoozed": node_id, "until": until}

    @app.post("/api/garden/node/{node_id}/unsnooze")
    async def garden_unsnooze(node_id: str):
        vault.unsnooze(node_id)
        return {"unsnoozed": node_id}

    @app.get("/api/garden/archived")
    def garden_archived():
        """The set-aside drawer (powers the Archive view): Archived + Snoozed are
        one-click restorable; Done is the completed record. Everything here is still
        in the vault — nothing was deleted, only set aside."""
        now = datetime.now(timezone.utc).isoformat()

        def _hydrate(items, extra):
            out = []
            for it in items:
                r = vault.get(it["node_id"])
                if not r:
                    continue
                nd = _node_dict(r)
                nd.update(extra(it))
                out.append(nd)
            return out

        archived = _hydrate(vault.list_archived(),
                            lambda it: {"set_aside_at": it["archived_at"]})
        snoozed = _hydrate([it for it in vault.list_snoozed()
                            if (it["until"] or "") > now],
                           lambda it: {"wake": it["until"]})
        done_rows = vault.conn.execute("""
            SELECT * FROM nodes WHERE kind='resolved' AND tags LIKE '%"desk-done"%'
            ORDER BY timestamp DESC LIMIT 30
        """).fetchall()
        done = [_node_dict(r) for r in done_rows]
        return {"archived": archived, "snoozed": snoozed, "done": done,
                "counts": {"archived": len(archived),
                           "snoozed": len(snoozed), "done": len(done)}}

    @app.get("/api/garden/ideas")
    def garden_ideas():
        """
        The ideas bank — sparks kept for later. Three shelves:
          fresh   — planted in the last 14 days
          bank    — older, still alive (the reference shelf)
          parked  — explicitly parked projects/ideas (tag 'parked')
        Revisiting an idea is a recall event: stability grows, it earns
        its place. Ideas never nag — they wait, and occasionally resurface
        through the heartbeat rotation.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        idea_rows = vault.conn.execute("""
            SELECT * FROM nodes WHERE status='active' AND kind='idea'
            ORDER BY timestamp DESC LIMIT 100
        """).fetchall()
        parked_rows = vault.conn.execute("""
            SELECT * FROM nodes WHERE status='active' AND kind != 'idea'
              AND tags LIKE '%"parked"%'
            ORDER BY timestamp DESC LIMIT 20
        """).fetchall()

        # findings: children chained onto each idea (research results, replies)
        child_counts = dict(vault.conn.execute(
            "SELECT parent, COUNT(*) FROM nodes "
            "WHERE parent IS NOT NULL AND status='active' GROUP BY parent"
        ).fetchall())

        # the research queue: open requests awaiting an AI (or human) session
        queue_rows = vault.conn.execute("""
            SELECT * FROM nodes WHERE kind='open_item' AND status='active'
              AND tags LIKE '%"research-queue"%'
            ORDER BY timestamp DESC LIMIT 20
        """).fetchall()

        hidden = vault.hidden_ids()   # archived/snoozed ideas leave the bank (stay active for the AI)
        fresh, bank = [], []
        for r in idea_rows:
            if r["id"] in hidden:
                continue
            n = _node_dict(r)
            n["findings"] = child_counts.get(n["id"], 0)
            # ripe: revisited enough to be stable AND research came back
            n["ripe"] = n["stability"] >= 2 and n["findings"] >= 1
            (fresh if (n["timestamp"] or "") >= cutoff else bank).append(n)
        return {"fresh": fresh, "bank": bank,
                "parked": [_node_dict(r) for r in parked_rows if r["id"] not in hidden],
                "queue":  [_node_dict(r) for r in queue_rows if r["id"] not in hidden],
                "total": len(fresh) + len(bank)}

    @app.post("/api/garden/node/{node_id}/research")
    async def garden_research(node_id: str, payload: dict | None = None):
        """
        Queue an idea for research — the human→AI handoff. Writes an
        open_item chained to the idea, tagged research-queue. Any session
        that orients sees the queue; findings get written as children of
        the idea, and the request is resolved via the normal done flow.
        """
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        note = (payload or {}).get("note") or ""
        from cairn.vault import MicroNode
        req = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = "open_item",
            query       = f"RESEARCH: {(row['query'] or '')[:300]}",
            output_preview = note or "requested from the Ideas tab — findings go on the idea node",
            parent      = node_id,
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            memory_tier = 1,
            tags        = ["research-queue", "garden"],
        ))
        _spawn_embed()
        return {"id": req.id, "idea": node_id}

    @app.get("/api/garden/spark")
    def garden_spark():
        """
        Inspiration on demand — zero LLM. Pick the most-overdue idea
        (due-pressure law) and surface its semantic neighbors from OTHER
        topics: cross-domain collision is where new ideas come from
        (Koestler's bisociation). The embedding space already knows
        which of your thoughts rhyme — this just introduces them.
        """
        import struct as _struct
        hidden = vault.hidden_ids()   # archived/snoozed stay out of inspiration too
        idea = next((r for r in vault.conn.execute("""
            SELECT *,
              ( (julianday('now') - julianday(COALESCE(last_injected,'2020-01-01')))
                / MAX(COALESCE(stability_days,1.0), 0.1) ) AS overdue
            FROM nodes
            WHERE status='active' AND kind='idea' AND embedding IS NOT NULL
            ORDER BY overdue DESC LIMIT 8
        """).fetchall() if r["id"] not in hidden), None)
        if not idea:
            return {"idea": None, "collisions": []}

        idea_tags = set(json.loads(idea["tags"] or "[]"))
        dim = len(idea["embedding"]) // 4
        a = _struct.unpack(f"{dim}f", idea["embedding"])
        mag_a = sum(x * x for x in a) ** 0.5

        # Collision pool: idea-shaped thoughts only. Decisions/procedures/
        # resolveds are operational record — true, useful, not inspiring.
        cands = vault.conn.execute("""
            SELECT * FROM nodes WHERE embedding IS NOT NULL AND status='active'
              AND id != ? AND kind IN ('idea','insight','hypothesis','question')
        """, (idea["id"],)).fetchall()

        scored = []
        for c in cands:
            try:
                b = _struct.unpack(f"{dim}f", c["embedding"])
            except Exception:
                continue
            dot = sum(x * y for x, y in zip(a, b))
            mag_b = sum(y * y for y in b) ** 0.5
            if not (mag_a and mag_b):
                continue
            sim = dot / (mag_a * mag_b)
            c_tags = set(json.loads(c["tags"] or "[]"))
            cross = not (idea_tags & c_tags - {"backfill", "garden", "cairn"})
            # sweet spot: related enough to rhyme, distant enough to surprise
            if 0.35 <= sim <= 0.85:
                scored.append((sim + (0.15 if cross else 0), sim, cross, c))
        scored.sort(key=lambda x: -x[0])

        collisions = []
        for _, sim, cross, c in scored[:4]:
            nd = _node_dict(c)
            nd["similarity"] = round(sim, 3)
            nd["cross_domain"] = cross
            collisions.append(nd)

        dismissed = vault.spark_dismissed_ids() | hidden   # set-aside nodes drop out of Spark too
        collisions = [c for c in collisions if c["id"] not in dismissed]

        # Loose threads: questions/hypotheses opened and never pulled —
        # no children, still active. Conversation topics that got away.
        threads = [n for n in (_node_dict(r) for r in vault.conn.execute("""
            SELECT * FROM nodes n
            WHERE n.status='active' AND n.kind IN ('question','hypothesis')
              AND NOT EXISTS (SELECT 1 FROM nodes c
                              WHERE c.parent = n.id AND c.status='active')
            ORDER BY n.timestamp ASC LIMIT 8
        """).fetchall()) if n["id"] not in dismissed][:5]

        # 🔭 From the deep: the live scan. Old high-importance memories
        # (insights, key decisions, hypotheses) you haven't seen in the
        # longest — overdue-pressure ordering, dismissals excluded.
        # No cron needed: every Spark press IS the scan.
        deep = [n for n in (_node_dict(r) for r in vault.conn.execute("""
            SELECT *,
              ( (julianday('now') - julianday(COALESCE(last_injected,'2020-01-01')))
                / MAX(COALESCE(stability_days,1.0), 0.1) )
              * COALESCE(importance,5) AS dp
            FROM nodes
            WHERE status='active' AND importance >= 6
              AND kind IN ('insight','decision','hypothesis')
            ORDER BY dp DESC LIMIT 10
        """).fetchall()) if n["id"] not in dismissed][:3]

        # Dormant projects: declared projects with no activity in 7+ days
        from datetime import timedelta
        dormant_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        dormant = []
        for tag, v in PROJECTS.items():
            name = v[0]
            # a project is only dormant if NONE of its tags (primary + aliases)
            # have recent activity — index over the whole family.
            mt = _project_match_tags(tag)
            clause = " OR ".join("tags LIKE ?" for _ in mt)
            last = vault.conn.execute(
                f"SELECT MAX(timestamp) FROM nodes WHERE status='active' AND ({clause})",
                [f'%"{t}"%' for t in mt]).fetchone()[0]
            if last and last < dormant_cutoff:
                days = max(1, int((datetime.now(timezone.utc)
                    - datetime.fromisoformat(last.replace('Z','+00:00'))).days))
                dormant.append({"tag": tag, "name": name, "days": days})

        # surfacing IS an exposure — reset the idea's clock so spark rotates
        try:
            vault.set_stability(idea["id"], idea["stability_days"] or 1.0)
        except Exception:
            pass
        return {"idea": _node_dict(idea), "collisions": collisions,
                "threads": threads, "deep": deep, "dormant": dormant}

    @app.post("/api/garden/node/{node_id}/dismiss-spark")
    async def garden_dismiss_spark(node_id: str):
        """Off the board, not out of memory — node stays fully alive."""
        vault.dismiss_from_spark(node_id)
        return {"id": node_id, "dismissed": True}

    @app.get("/api/garden/hubs")
    def garden_hubs():
        # topic hubs from tags
        tag_counts: dict[str, int] = {}
        for r in vault.conn.execute(
                "SELECT tags FROM nodes WHERE status='active'").fetchall():
            try:
                for t in json.loads(r["tags"] or "[]"):
                    if isinstance(t, str) and not t.startswith("member:") \
                       and t not in ("backfill", "consolidated"):
                        tag_counts[t] = tag_counts.get(t, 0) + 1
            except Exception:
                continue
        tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:24]

        # consolidation hubs — the neocortex layer
        hubs = vault.conn.execute("""
            SELECT * FROM nodes
            WHERE status='active' AND kind IN ('insight','procedure')
              AND tags LIKE '%consolidated%'
            ORDER BY timestamp DESC LIMIT 20
        """).fetchall()
        return {
            "tags": [{"tag": t, "count": c} for t, c in tags],
            "consolidated": [_node_dict(r) for r in hubs],
        }

    @app.get("/api/garden/hub/{tag}")
    def garden_hub(tag: str):
        rows = vault.conn.execute("""
            SELECT * FROM nodes
            WHERE status='active' AND tags LIKE ? ESCAPE '\\'
            ORDER BY importance DESC, timestamp DESC LIMIT 60
        """, (f'%"{_like(tag)}"%',)).fetchall()
        return {"tag": tag, "nodes": [_node_dict(r) for r in rows]}

    @app.get("/api/garden/search")
    def garden_search(q: str):
        if not q or len(q.strip()) < 2:
            return {"results": [], "mode": "none"}
        # hybrid semantic — same pathway the model uses
        try:
            results = vault.query_episodic(q, k=12)
            out = []
            for d in results:
                nd = _node_dict_from_plain(d)
                nd["score"] = round(d.get("score", 0), 3)
                out.append(nd)
            return {"results": out, "mode": "hybrid"}
        except Exception:
            # keyword fallback when sentence-transformers is unavailable
            like = f"%{_like(q)}%"
            rows = vault.conn.execute("""
                SELECT * FROM nodes
                WHERE status='active'
                  AND (query LIKE ? ESCAPE '\\' OR episodic_text LIKE ? ESCAPE '\\'
                       OR gist LIKE ? ESCAPE '\\')
                ORDER BY importance DESC, timestamp DESC LIMIT 12
            """, (like, like, like)).fetchall()
            return {"results": [_node_dict(r) for r in rows], "mode": "keyword"}

    def _node_dict_from_plain(d: dict) -> dict:
        stability = float(d.get("stability_days") or 1.0)
        kind      = d.get("kind", "note")
        return {
            "id": d.get("id"), "kind": kind,
            "gist": d.get("gist") or (d.get("query") or "")[:90],
            "query": d.get("query") or "", "preview": d.get("output_preview") or "",
            "session": d.get("session", ""), "model": d.get("model", "unknown"),
            "speaker": d.get("speaker", "agent"), "timestamp": d.get("timestamp", ""),
            "tier": int(d.get("memory_tier") or 1),
            "importance": int(d.get("importance") or 5),
            "stability": round(stability, 1),
            "flagged": bool(d.get("flagged")), "status": d.get("status", "active"),
            "last_injected": d.get("last_injected"),
            "tags": (tags := json.loads(d.get("tags") or "[]")),
            "process": _is_process(tags, d.get("model", "")),
            "maturity": _maturity(stability, kind),
        }

    @app.get("/api/garden/drift")
    def garden_drift(q: str = "", k: int = 8):
        """Drift from a query — graph walk that surfaces adjacent weak-tie nodes."""
        q = q.strip()[:500]
        if not q:
            from fastapi.responses import JSONResponse as _JR
            return _JR({"error": "q is required"}, status_code=400)
        k = max(1, min(25, k))
        try:
            from cairn.retrieve import drift_pack
            pack = drift_pack(q, vault=vault, k=k)
            results = []
            for r in pack.get("results", []):
                results.append({
                    "id":    r.get("id"),
                    "kind":  r.get("kind", "note"),
                    "gist":  r.get("gist") or "",
                    "topic": r.get("topic") or "",
                    "score": round(float(r.get("score", 0)), 4),
                    "hops":  r.get("hops", 0),
                    "source": r.get("source", ""),
                })
            return {
                "query":   pack.get("query", q),
                "seeds":   pack.get("seeds", []),
                "results": results,
            }
        except Exception as exc:
            from fastapi.responses import JSONResponse as _JR
            return _JR({"error": str(exc)}, status_code=500)

    @app.get("/api/garden/ledger")
    def garden_ledger():
        """Per-channel attention stats: how much memory was shown vs cited."""
        try:
            rows = vault.conn.execute(
                "SELECT channel, COUNT(*) as shown, SUM(COALESCE(cited,0)) as cited "
                "FROM attention_ledger GROUP BY channel ORDER BY shown DESC"
            ).fetchall()
            channels = [{"channel": r["channel"], "shown": r["shown"],
                         "cited": int(r["cited"] or 0)} for r in rows]
            total_shown  = sum(c["shown"]  for c in channels)
            total_cited  = sum(c["cited"]  for c in channels)
            return {"channels": channels, "total_shown": total_shown,
                    "total_cited": total_cited}
        except Exception as exc:
            from fastapi.responses import JSONResponse as _JR
            return _JR({"error": str(exc)}, status_code=500)

    # ── Garden Hub / Book / Index (the human face) ─────────────────────────────
    @app.get("/api/garden/hub")
    def garden_hub_page():
        from cairn.book import hub_data, _gist, RECENCY_KINDS
        try:
            data = hub_data(vault)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        # custom Hub greeting (config, NOT the vault) — set via click-to-edit on
        # the headline or `cairn hello "…"`; ~/.cairn/settings.json. Unset → the
        # client falls back to the default line. See POST /api/garden/greeting.
        try:
            import json as _gj
            from pathlib import Path as _GP
            _sf = _GP.home() / ".cairn" / "settings.json"
            if _sf.exists():
                _s = _gj.loads(_sf.read_text(encoding="utf-8"))
                if isinstance(_s, dict) and str(_s.get("greeting") or "").strip():
                    data["greeting"] = str(_s["greeting"]).strip()[:200]
        except Exception:
            pass
        # "Since your last visit" — a small, high-signal delta. Two markers in
        # ~/.cairn (robust across tabs/ports, unlike localStorage): `marker` is
        # the stable delta baseline; `last_seen` updates every load. A >30min gap
        # since last_seen = a NEW visit, which moves the baseline to where you
        # left off — so the delta holds steady while you work (polling won't
        # reset it) and only refreshes on your next real visit.
        try:
            import json as _json
            from datetime import datetime, timezone, timedelta
            from pathlib import Path as _P
            vf = _P.home() / ".cairn" / "garden_visit.json"
            st = {}
            if vf.exists():
                try: st = _json.loads(vf.read_text(encoding="utf-8"))
                except Exception: st = {}
            marker, last_seen = st.get("marker"), st.get("last_seen")
            now = datetime.now(timezone.utc); now_iso = now.isoformat()
            new_visit = True
            if last_seen:
                try: new_visit = (now - datetime.fromisoformat(last_seen)) > timedelta(minutes=30)
                except Exception: new_visit = True
            if new_visit:
                marker = last_seen if last_seen else (marker or now_iso)
            if marker:
                # "N new" should mean meaningful captures (decisions/ideas/notes/…),
                # NOT raw conversation_turn chatter (which dominated the count). Gate
                # the delta to real kinds — that's what "since your last visit" means.
                _dk = tuple(k for k in RECENCY_KINDS if k not in ("conversation_turn", "artifact"))
                _rk = ",".join("?" * len(_dk))
                rows = vault.conn.execute(
                    f"SELECT id, kind, gist, query, speaker, timestamp, tags, model FROM nodes "
                    f"WHERE status='active' AND kind IN ({_rk}) "
                    f"AND session NOT LIKE 'import-%' AND timestamp > ? "
                    f"ORDER BY timestamp DESC LIMIT 12", (*_dk, marker)).fetchall()
                cnt = vault.conn.execute(
                    f"SELECT COUNT(*) FROM nodes WHERE status='active' "
                    f"AND kind IN ({_rk}) AND session NOT LIKE 'import-%' "
                    f"AND timestamp > ?", (*_dk, marker)).fetchone()[0]
                # SEPARATE conversation-turn count: a conversation-heavy day where
                # nothing crossed into a meaning-kind used to read "0 new" because
                # conversation_turn is (correctly) excluded from the delta above.
                # Surface it as its OWN number so the copy can say "N notes + M
                # turns" without polluting the meaning-kind count. Capped at 99 so
                # the label stays terse (a busy day can mint hundreds of turns).
                turns = vault.conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE status='active' "
                    "AND kind='conversation_turn' AND session NOT LIKE 'import-%' "
                    "AND timestamp > ?", (marker,)).fetchone()[0]
                data["since_last_visit"] = {
                    "since": marker, "count": cnt, "turns": min(turns, 99),
                    "items": [{"id": r["id"], "kind": r["kind"], "gist": _gist(r),
                               "speaker": r["speaker"], "ts": r["timestamp"],
                               "process": _is_process(_json.loads(r["tags"] or "[]"), r["model"] or "")}
                              for r in rows]}
            else:
                data["since_last_visit"] = {"since": None, "count": 0, "turns": 0, "items": []}
            vf.parent.mkdir(parents=True, exist_ok=True)
            vf.write_text(_json.dumps({"marker": marker, "last_seen": now_iso}), encoding="utf-8")
        except Exception:
            data.setdefault("since_last_visit", {"since": None, "count": 0, "turns": 0, "items": []})

        # ── Router cards + project-family activity (de-UUID the hub) ──────────
        # Replaces the raw session-UUID strip: this-week active-node counts
        # aggregated by PROJECT (declared+aliases first, then surviving emerging
        # families), rendered as "Name — N nodes". Machine-tag strata are stripped
        # exactly like the projects view; an 'untagged' residual line is fine.
        try:
            from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
            wk = (_dt2.now(_tz2.utc) - _td2(days=7)).isoformat()
            rows = vault.conn.execute(
                "SELECT id, tags, session FROM nodes WHERE status='active' "
                "AND timestamp >= ? AND session NOT LIKE 'import-%'",
                (wk,)).fetchall()
            # session → this-week node count, for the honest "incl. conversation"
            # approximation: a session holding a project's notes is that project's
            # working session (interim heuristic until affinity attributes turns).
            sess_week: dict[str, int] = {s: n for s, n in vault.conn.execute(
                "SELECT session, COUNT(1) FROM nodes WHERE status='active' "
                "AND timestamp >= ? AND session NOT LIKE 'import-%' "
                "GROUP BY session", (wk,))}

            # declared families: primary tag → its full match-tag set
            declared_of: dict[str, str] = {}
            declared_name: dict[str, str] = {}
            for ptag, pv in PROJECTS.items():
                declared_name[ptag] = pv[0]
                for mt in _project_match_tags(ptag):
                    declared_of[mt] = ptag

            fam_counts: dict[str, set] = {}     # key → set of node ids (dedupe)
            fam_name: dict[str, str] = {}
            fam_spell: dict[str, dict] = {}     # emerging: key → {spelling: n}
            fam_sess: dict[str, set] = {}       # key → sessions holding its notes
            untagged = 0
            for r in rows:
                try:
                    tags = _json.loads(r["tags"] or "[]")
                except Exception:
                    tags = []
                hit = False
                for t in tags:
                    if not isinstance(t, str):
                        continue
                    if t in declared_of:
                        ptag = declared_of[t]
                        fam_counts.setdefault("p:" + ptag, set()).add(r["id"])
                        fam_name["p:" + ptag] = declared_name[ptag]
                        if r["session"]:
                            fam_sess.setdefault("p:" + ptag, set()).add(r["session"])
                        hit = True
                    elif not _is_machine_tag(t) and t not in _NOT_PROJECTS:
                        fk = _family_key(t)
                        if not fk:
                            continue
                        fam_counts.setdefault("e:" + fk, set()).add(r["id"])
                        sp = fam_spell.setdefault("e:" + fk, {})
                        sp[t] = sp.get(t, 0) + 1
                        if r["session"]:
                            fam_sess.setdefault("e:" + fk, set()).add(r["session"])
                        hit = True
                if not hit:
                    untagged += 1
            # emerging display name = most-frequent spelling
            for k, sp in fam_spell.items():
                fam_name[k] = max(sp.items(), key=lambda kv: kv[1])[0]

            fam_list = []
            for k, ids in fam_counts.items():
                # weekly-strip hygiene: an emerging blip needs real mass this
                # week (>=3 nodes) to earn a Hub line; declared always show.
                if k.startswith("e:") and len(ids) < 3:
                    continue
                sess = fam_sess.get(k, set())
                fam_list.append({
                    "tag": k[2:] if k.startswith("p:") else fam_name[k],
                    "name": fam_name.get(k, k[2:]),
                    "nodes": len(ids),
                    "sessions": len(sess),
                    "approx": sum(sess_week.get(s, 0) for s in sess),
                    "declared": k.startswith("p:")})
            # declared first, then by node count
            fam_list.sort(key=lambda f: (not f["declared"], -f["nodes"]))
            data["project_activity"] = {"families": fam_list, "untagged": untagged}

            # router card counts
            desk = data.get("open_items") or []
            due = data.get("due") or {}
            overdue_n = len(due.get("overdue") or [])
            flagged_n = vault.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE status='active' AND flagged=1"
            ).fetchone()[0]
            today_start = _dt2.now().astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0).astimezone(_tz2.utc).isoformat()
            today_meaning = vault.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE status='active' "
                "AND kind NOT IN ('conversation_turn','tool_call','interrupt','context_stamp') "
                "AND session NOT LIKE 'import-%' AND timestamp >= ?",
                (today_start,)).fetchone()[0]
            today_turns = vault.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE status='active' "
                "AND kind='conversation_turn' AND session NOT LIKE 'import-%' "
                "AND timestamp >= ?", (today_start,)).fetchone()[0]
            movers = [f["name"] for f in fam_list][:2]
            # tend pressure — the FSRS review queue count. The old Review hub
            # BANNER is gone (P2 absorption): its "N memories need tending"
            # number now rides in the single Desk router-card count, since Desk
            # is the one attention home and Tend is a section inside it.
            tend_n = vault.conn.execute(f"""
                SELECT COUNT(*) FROM nodes
                WHERE status='active' AND memory_tier <= 1
                  AND kind IN ({MEANING_KINDS_SQL})
                  AND ( (julianday('now')
                          - julianday(COALESCE(last_injected,'2020-01-01')))
                        / MAX(COALESCE(stability_days,1.0), 0.1) )
                      * COALESCE(importance,5) >= 6
            """).fetchone()[0]
            data["router"] = {
                "desk": len(desk) + overdue_n + flagged_n + tend_n,
                "today_meaning": today_meaning, "today_turns": today_turns,
                "movers": movers, "flagged": flagged_n, "tend": tend_n}
        except Exception:
            data.setdefault("project_activity", {"families": [], "untagged": 0})
            data.setdefault("router", {"desk": 0, "today_meaning": 0,
                                       "today_turns": 0, "movers": [],
                                       "flagged": 0, "tend": 0})

        # ── Hub Topics strip (P3.5 both-stacked, part 2) ───────────────────
        # Named community clusters under the projects strip — freshest life
        # first, capped. Kept an INDEPENDENT block so flipping the stack order
        # (or dropping one) at G2 is a template move, not a rebuild (ruling
        # 079cbf03d2f3). The REGISTER reaches topics here: a cluster whose
        # recent meaning-members are mostly the machine's own work-notes is
        # marked process (display-only, same toggle — the community column and
        # retrieval are untouched). book.py stays a leaf — annotate here.
        try:
            from cairn.book import topics_data
            # hub-strip hygiene (same rule as emerging projects above): a
            # cluster needs real meaning-mass (>=3 notes) to earn Hub space.
            # The Index/Knowledge shelf still shows every named topic —
            # topics_total lets the strip SAY it's a slice (G2 finding: the
            # hub and the Library must not look like two different truths).
            _all_topics = topics_data(vault)
            tps = sorted((t for t in _all_topics if t["count"] >= 3),
                         key=lambda t: t.get("last_ts") or "", reverse=True)[:12]
            data["topics_total"] = len(_all_topics)
            # honesty clock (owner's catch: "it says freshest... its not the
            # freshest"): a topic's last_ts can only be as new as the nightly
            # graph pass that stamps communities — notes planted since the
            # last sleep belong to NO cluster yet. Tell the human when the
            # graph last looked instead of overpromising "freshest".
            data["topics_asof"] = max(
                (t.get("last_ts") or "" for t in _all_topics), default="")
            for t in tps:
                mem = vault.conn.execute(
                    "SELECT tags, model FROM nodes WHERE status='active' "
                    f"AND community LIKE ? AND kind IN ({MEANING_KINDS_SQL}) "
                    "ORDER BY timestamp DESC LIMIT 24",
                    (t["cid"] + "|%",)).fetchall()
                proc = sum(1 for r in mem
                           if _is_process(json.loads(r["tags"] or "[]"),
                                          r["model"] or ""))
                t["process"] = bool(mem) and proc * 2 >= len(mem)
            data["topics"] = tps
        except Exception:
            data.setdefault("topics", [])
        return data

    @app.get("/api/garden/book")
    def garden_book_page():
        from cairn.book import book_data
        try:
            d = book_data(vault)
            # register annotation: mark this-week entries that are the machine
            # talking about its own work (book.py stays a leaf — annotate here).
            week = d.get("this_week") or []
            ids = [n["id"] for n in week if n.get("id")]
            if ids:
                ph = ",".join("?" * len(ids))
                meta = {r["id"]: (r["tags"], r["model"]) for r in vault.conn.execute(
                    f"SELECT id, tags, model FROM nodes WHERE id IN ({ph})", ids)}
                for n in week:
                    t, m = meta.get(n.get("id"), ("[]", ""))
                    n["process"] = _is_process(json.loads(t or "[]"), m or "")
            return d
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/garden/bookindex")
    def garden_bookindex_page():
        from cairn.book import index_data
        try:
            return index_data(vault)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/garden/topic/{cid}")
    def garden_topic(cid: str):
        """A named-community topic view (plan C4): its meaning-kind member nodes,
        listed like a tag-membership view. Reads the nightly community output —
        DISPLAY only, the community column + retrieval are untouched."""
        from cairn.book import topic_members
        try:
            return topic_members(vault, cid)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/garden/volume/{account}/sessions")
    def garden_volume_sessions(account: str):
        """One archive volume's sessions (plan C4 Archive drill-in). Each row
        opens the P2 conversation reader (session/{id}/turns)."""
        from cairn.book import volume_sessions
        try:
            return volume_sessions(vault, account)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/garden/node/{node_id}")
    def garden_node(node_id: str):
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        node  = _node_dict(row)
        chain = [_node_dict(r) for r in vault.chain(node_id)]

        children = [_node_dict(r) for r in vault.conn.execute(
            "SELECT * FROM nodes WHERE parent=? AND status!='void' "
            "ORDER BY timestamp ASC LIMIT 20", (node_id,)).fetchall()]

        # consolidation lineage, both directions
        members = []
        for t in node["tags"]:
            if isinstance(t, str) and t.startswith("member:"):
                mr = vault.get(t.split(":", 1)[1])
                if mr:
                    members.append(_node_dict(mr))
        member_of = [_node_dict(r) for r in vault.conn.execute(
            "SELECT * FROM nodes WHERE status='active' "
            "AND tags LIKE ? ESCAPE '\\' LIMIT 5",
            (f'%member:{_like(node_id)}%',)).fetchall()]

        # semantic neighbors — top 5 by cosine (skip if no embeddings)
        neighbors = []
        try:
            if row["embedding"]:
                import struct as _struct
                emb  = row["embedding"]
                dim  = len(emb) // 4
                a    = _struct.unpack(f"{dim}f", emb)
                mag_a = sum(x * x for x in a) ** 0.5
                cands = vault.conn.execute(
                    "SELECT * FROM nodes WHERE embedding IS NOT NULL "
                    "AND status='active' AND id != ?", (node_id,)).fetchall()
                scored = []
                for c in cands:
                    try:
                        b = _struct.unpack(f"{dim}f", c["embedding"])
                    except Exception:
                        continue
                    dot   = sum(x * y for x, y in zip(a, b))
                    mag_b = sum(y * y for y in b) ** 0.5
                    if mag_a and mag_b:
                        scored.append((dot / (mag_a * mag_b), c))
                scored.sort(key=lambda x: -x[0])
                for s, c in scored[:5]:
                    nd = _node_dict(c)
                    nd["similarity"] = round(s, 3)
                    neighbors.append(nd)
        except Exception:
            pass

        # entity-bridge neighbors — other active nodes that share an
        # `entity:<name>` tag (the STAR bridges from edges.py). Empty until a
        # vault is distilled (raw turns carry no entity tags) — lights up then.
        entity_neighbors = []
        ent_tags = [t for t in node["tags"]
                    if isinstance(t, str) and t.startswith("entity:")]
        if ent_tags:
            seen = set()
            for et in ent_tags[:6]:
                for r2 in vault.conn.execute(
                        "SELECT * FROM nodes WHERE status='active' AND id!=? "
                        "AND tags LIKE ? ESCAPE '\\' "
                        "ORDER BY importance DESC LIMIT 6",
                        (node_id, f'%{_like(et)}%')).fetchall():
                    if r2["id"] in seen:
                        continue
                    seen.add(r2["id"])
                    nd = _node_dict(r2)
                    nd["entity"] = et.split(":", 1)[1]
                    entity_neighbors.append(nd)
                if len(entity_neighbors) >= 8:
                    break

        return {"node": node, "chain": chain, "children": children,
                "members": members, "member_of": member_of,
                "neighbors": neighbors, "entity_neighbors": entity_neighbors[:8]}

    # ── action endpoints ──────────────────────────────────────────────────────

    @app.post("/api/garden/capture")
    async def garden_capture(payload: dict, request: Request):
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        text = str(payload.get("text") or "").strip()[:MAX_TEXT_LEN]
        kind = payload.get("kind") or "insight"
        if not text and not payload.get("image_b64"):
            return JSONResponse({"error": "empty"}, status_code=400)
        if kind not in CAPTURE_KINDS:
            kind = "insight"

        tags = ["garden", "human-capture"]
        # remote capture (phone) → inbox: file it later, human or AI
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1"):
            tags.append("inbox")
        # 'due:YYYY-MM-DD' anywhere in text becomes a desk date tag
        import re as _re
        m = _re.search(r"due:(\d{4}-\d{2}-\d{2})", text)
        if m:
            tags.append(f"due:{m.group(1)}")

        # optional photo — base64 (no python-multipart dependency)
        if payload.get("image_b64"):
            import base64, uuid as _uuid
            ext = "jpg"
            name = (payload.get("image_name") or "").lower()
            for e in ("png", "webp", "gif", "jpeg", "jpg"):
                if name.endswith(e):
                    ext = "jpg" if e == "jpeg" else e
                    break
            try:
                raw = base64.b64decode(payload["image_b64"], validate=False)
                if len(raw) > 15_000_000:
                    return JSONResponse({"error": "image too large"}, status_code=413)
                media_dir = Path.home() / ".cairn" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                fname = f"{_uuid.uuid4().hex[:12]}.{ext}"
                (media_dir / fname).write_bytes(raw)
                tags.append(f"media:{fname}")
                if not text:
                    text = f"[photo capture] {payload.get('image_name') or fname}"
            except Exception as e:
                return JSONResponse({"error": f"bad image: {e}"}, status_code=400)

        from cairn.vault import MicroNode
        node = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = kind,
            query       = text,
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            memory_tier = 1,
            tags        = tags,
        ))
        _spawn_embed()
        return {"id": node.id, "kind": kind, "inbox": "inbox" in tags}

    @app.post("/api/garden/node/{node_id}/reply")
    async def garden_reply(node_id: str, payload: dict, request: Request):
        """Attach a NOTE to a node — an annotation. Append-only-native: the
        original is never touched; your note hangs off it as a child.
          • memory=False (default) → a QUIET margin-note: not embedded, stays
            out of search/inject (tier 2). Visible only on the node it annotates.
          • memory=True → a kept memory: embedded + retrievable like any node."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        text = str(payload.get("text") or "").strip()[:MAX_TEXT_LEN]
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        memory = bool(payload.get("memory"))
        from cairn.vault import MicroNode
        node = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = "note",
            query       = text,
            output_preview = text,
            parent      = node_id,
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            memory_tier = 1 if memory else 2,
            tags        = ["annotation", "garden", "memory" if memory else "quiet"],
        ))
        if memory:
            _spawn_embed()          # only kept notes join semantic recall
        return {"id": node.id, "memory": memory}

    @app.post("/api/garden/node/{node_id}/review")
    async def garden_review_node(node_id: str):
        """Human reviewed this memory = a successful recall event.
        Stability grows, clock resets — same FSRS law as model citation."""
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        new_stab = min(365.0, (row["stability_days"] or 1.0) * 1.2)
        vault.set_stability(node_id, new_stab)
        return {"id": node_id, "stability": round(new_stab, 1)}

    @app.post("/api/garden/node/{node_id}/flag")
    async def garden_flag(node_id: str):
        vault.flag(node_id)
        return {"id": node_id, "flagged": True}

    @app.post("/api/garden/node/{node_id}/void")
    async def garden_void(node_id: str):
        vault.void(node_id)
        return {"id": node_id, "status": "void"}

    @app.post("/api/garden/node/{node_id}/promote")
    async def garden_promote(node_id: str):
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        tier = max(0, (row["memory_tier"] or 1) - 1)
        vault.set_tier(node_id, tier)
        return {"id": node_id, "tier": tier}

    # Which project-family tags does a source node belong to? A tag counts if it
    # is a declared project's primary/alias tag, else a bare literal topic tag
    # (machine strata + plumbing literals are dropped — they're not the family).
    # The promoted node inherits these so it lands on the same shelves the source
    # would (feeds Ideas/Desk/Projects naturally).
    def _family_tags_of(source_row) -> list:
        try:
            src_tags = json.loads(source_row["tags"] or "[]")
        except Exception:
            src_tags = []
        declared = set()
        for pt in PROJECTS:
            declared.update(_project_match_tags(pt))
        out = []
        for t in src_tags:
            if not isinstance(t, str):
                continue
            keep = (t in declared) or (
                not _is_machine_tag(t) and t not in _NOT_PROJECTS
                and not t.startswith(("garden", "human-capture", "inbox",
                                      "annotation", "chat", "prov:", "from:")))
            if keep and t not in out:
                out.append(t)
        return out[:12]

    _PROMOTE_KINDS = {"idea", "open_item"}

    @app.post("/api/garden/promote-node")
    async def garden_promote_node(payload: dict, request: Request):
        """Turn→shelf promotion (plan C7): take a conversation_turn / insight (or
        any node) and mint a NEW curated node from its gist — kind=idea|open_item
        — chained to the source (parent=source id). Append-only clean: the source
        is NEVER touched; the new node feeds the Ideas / Desk shelves naturally.
        Inherits the source's project-family tags + 'promoted' so it files itself.
        Same-origin CSRF guard (dashboard middleware) already covers this POST."""
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        node_id = str(payload.get("id") or "").strip()[:64]
        kind = str(payload.get("kind") or "").strip()
        if kind not in _PROMOTE_KINDS:
            return JSONResponse(
                {"error": "kind must be idea or open_item"}, status_code=400)
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        # source gist/text, trimmed — prefer the distilled gist, then query, then
        # the verbatim preview; strip any raw due: token so it doesn't re-fire.
        def _col(key):
            try:
                return row[key]
            except (KeyError, IndexError):
                return None
        text = (_col("gist") or row["query"] or _col("output_preview") or "").strip()
        text = re.sub(r"\s*\bdue:\S+", "", text).strip()[:300]
        if not text:
            return JSONResponse({"error": "source has no text"}, status_code=400)
        tags = _family_tags_of(row)
        tags.append("promoted")
        from cairn.vault import MicroNode
        node = vault.write(MicroNode(
            session     = current_session_fn(),
            kind        = kind,
            query       = text,
            parent      = node_id,
            model       = "human",
            speaker     = "user",
            agent_role  = "curator",
            memory_tier = 1,
            tags        = tags,
        ))
        _spawn_embed()
        return {"id": node.id, "kind": kind, "parent": node_id, "tags": tags}

    @app.get("/api/garden/me")
    def garden_me():
        """Your identity handle. set=False means first run — UI prompts."""
        h = _my_handle()
        return {"handle": h or "you", "set": bool(h)}

    @app.post("/api/garden/me")
    async def garden_me_set(payload: dict, request: Request):
        if not _rate_ok(request):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad payload"}, status_code=400)
        saved = _set_my_handle(str(payload.get("handle") or ""))
        if not saved:
            return JSONResponse(
                {"error": "handle must start alphanumeric; letters, digits, "
                          "space, - and _ only; max 24"}, status_code=400)
        return {"handle": saved, "set": True}

    # ── the page ──────────────────────────────────────────────────────────────

    @app.get("/garden", response_class=HTMLResponse)
    def garden_page():
        # no-store: stale cached pages have caused "nothing works" reports —
        # the browser must always fetch the current HTML+JS from the server
        return HTMLResponse(GARDEN_HTML,
                            headers={"Cache-Control": "no-store, max-age=0"})


GARDEN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cairn Garden</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#16140E">
<link rel="icon" type="image/png" sizes="192x192" href="/assets/brand/app-icon-192.png">
<link rel="icon" type="image/png" sizes="48x48" href="/assets/brand/favicon-48.png">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/assets/brand/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/app-icon-180.png">
<link rel="manifest" href="/assets/brand/manifest.webmanifest">
<style>
  /* ── Facelift palette (field-manual). dusk = "stone" (default), :root = "dawn"/paper. ── */
  :root {
    --paper:   #C4B493;  --card:  rgba(248,241,224,.50);  --card2: rgba(248,241,224,.42);
    --ink:     #352A18;  --muted: #6E5E43;  --line:  rgba(74,60,38,.30);
    --moss:    #4C7256;  --terra: #A85A34;  --amber: #937415;
    --gold:    #937415;  --slate: #5E6B78;  --ever:  #3C5A48;
    --idea:    #7C5CBF;
    --faint:   #897755;  --faint2: #9C8A6B;  --raised: rgba(248,241,224,.55);
    --gutter:  rgba(70,56,34,.10);  --topbar: rgba(228,218,194,.82);
    --border-strong: rgba(74,60,38,.55);  --card-hover: rgba(250,244,229,.68);
    --shadow:  0 1px 3px rgba(60,50,30,.08), 0 4px 16px rgba(60,50,30,.06);
  }
  [data-theme="dusk"] {
    --paper:   #16140E;  --card:  #1A1811;  --card2: #1C1A12;
    --ink:     #E8E2D2;  --muted: #968F7D;  --line:  #2C2A21;
    --moss:    #7CA38C;  --terra: #C06B3E;  --amber: #C9A227;  --gold: #C9A227;
    --faint:   #6E6757;  --faint2: #5A5446;  --raised: #191711;
    --gutter:  #17150F;  --topbar: rgba(22,20,14,.86);
    --border-strong: #3A3528;  --card-hover: #1F1C14;
    --shadow:  0 1px 3px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.25);
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--paper); color: var(--ink);
    font: 15px/1.55 "Segoe UI", system-ui, sans-serif;
    transition: background .3s, color .3s;
  }
  a { color: var(--moss); text-decoration: none; }

  /* ── header ── */
  header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 26px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: var(--topbar); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); z-index: 50;
  }
  .mark { font-size: 19px; letter-spacing: 3px; font-weight: 600; color: var(--moss); }
  .mark .stones { letter-spacing: 0; margin-right: 8px; }
  .logo-mark { width: 34px; height: 34px; opacity: .96; }
  .logo-word { height: 32px; width: auto; opacity: .96; margin-left: -3px; }
  .gardenlbl { font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 13px; letter-spacing: .22em; color: var(--moss); margin-left: 4px; }
  /* theme logo swap — default-hide via STYLESHEET (light=ink, dusk=bone), never inline */
  .boneOnly { display: none; }
  .inkOnly  { display: inline-block; }
  [data-theme="dusk"] .boneOnly { display: inline-block; }
  [data-theme="dusk"] .inkOnly  { display: none; }
  .sub  { color: var(--muted); font-size: 12px; }
  header .spacer { flex: 1; }
  .hbtn {
    background: none; border: 1px solid var(--line); color: var(--muted);
    border-radius: 7px; padding: 5px 12px; cursor: pointer; font-size: 12px;
  }
  .hbtn:hover { color: var(--ink); border-color: var(--muted); }

  /* ── capture ── */
  /* ── the Plant bar, built from the owner's element sheet (Plant Bar
     Elements, 2026-07-03): swatches by hex, cut-corner octagon frames
     matching the brain/garden buttons. --octo is the shared cut. ──── */
  :root {
    --pb-ivory: #E8E2D6; --pb-gray: #7F8878; --pb-mint: #8FBFAF;
    --pb-brass: #C79A43; --pb-green: #5D7D5A; --pb-red: #8C4A3D;
    --pb-black: #12110E;
  }
  /* THE OWNER'S ART IS THE UI (his call: 'chop and use elements of image 4
     and lay text boxes or dropdowns or buttons on top'). Every frame below
     is a real slice of his element sheet via border-image — corners pinned
     pixel-true, only the straight runs stretch, texture fully his. */
  #capture-bar {
    position: relative; display: flex; gap: 8px; align-items: center;
    max-width: 940px; margin: 8px auto 2px; padding: 3px 6px;
    border: 16px solid transparent;
    border-image: url('/assets/pb/frame.png') 20 fill / 16px stretch;
  }
  #capture-bar > * { position: relative; z-index: 1; }
  #capture-text {
    flex: 1; background: none; color: var(--pb-ivory);
    border: 12px solid transparent;
    border-image: url('/assets/pb/input-default.png') 11 fill / 11px stretch;
    padding: 2px 8px; font: inherit; min-height: 20px;
  }
  #capture-text:focus { outline: none;
    border-image-source: url('/assets/pb/input-focus.png'); }
  #capture-text:disabled { opacity: .5; }
  /* the TYPE dropdown from the sheet — brass frame, mint when open, and the
     owner's glyphs (native <select> can't render them, so it's hand-built) */
  #kind-dd { position: relative; align-self: stretch; display: flex; }
  #kind-btn {
    display: flex; align-items: center; gap: 6px; cursor: pointer;
    background: none; color: var(--pb-ivory); font: inherit; font-size: 13px;
    border: 12px solid transparent;
    border-image: url('/assets/pb/util-default.png') 12 fill / 11px stretch;
    padding: 0 4px;
  }
  #kind-btn:hover { border-image-source: url('/assets/pb/util-hover.png'); }
  #kind-dd.open #kind-btn { border-image-source: url('/assets/pb/util-active.png'); }
  #kind-btn img, .kind-item img { width: 14px; height: 14px; object-fit: contain; }
  .kind-chev { color: var(--pb-brass); font-size: 12px; margin-left: 2px; }
  #kind-menu {
    position: absolute; top: calc(100% + 10px); right: 0; z-index: 120; min-width: 175px;
    background: var(--pb-black);
    border: 18px solid transparent;
    border-image: url('/assets/pb/menu-panel.png') 17 / 16px stretch;
    filter: drop-shadow(0 12px 28px rgba(0,0,0,0.55));
  }
  #kind-menu-inner { padding: 2px 0; margin: -6px; }
  .kind-item {
    display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer;
    color: var(--pb-ivory); font-size: 12.5px; border-radius: 2px; border: 1px solid transparent;
  }
  /* item states in plain paint — squishing the baked row art left side-border
     artifacts (owner: 'the drop down window needs work still') */
  .kind-item:hover { background: rgba(232,226,214,0.08); }
  .kind-item.sel { border: none; background: rgba(127,136,120,0.18);
    box-shadow: inset 2px 0 0 var(--pb-mint); }

  /* ── DAWN: the owner's light-theme element sheet — same components,
     paper-native fills, swapped per theme so the bar is never a dark
     slab on the light page (his call after seeing dusk art on dawn). */
  body:not([data-theme='dusk']) #capture-bar {
    border-image: url('/assets/pb-dawn/frame.png') 20 fill / 16px stretch; }
  body:not([data-theme='dusk']) #capture-text {
    border-image: url('/assets/pb-dawn/input-default.png') 10 fill / 11px stretch;
    color: #2a2519; }
  body:not([data-theme='dusk']) #capture-text:focus {
    border-image-source: url('/assets/pb-dawn/input-focus.png'); }
  body:not([data-theme='dusk']) #kind-btn,
  body:not([data-theme='dusk']) #photo-btn,
  body:not([data-theme='dusk']) #due-btn {
    border-image: url('/assets/pb-dawn/util-default.png') 11 fill / 11px stretch;
    color: #2a2519; }
  body:not([data-theme='dusk']) #kind-btn:hover,
  body:not([data-theme='dusk']) #photo-btn:hover,
  body:not([data-theme='dusk']) #due-btn:hover {
    border-image-source: url('/assets/pb-dawn/util-hover.png'); }
  body:not([data-theme='dusk']) #kind-dd.open #kind-btn,
  body:not([data-theme='dusk']) #photo-btn:active,
  body:not([data-theme='dusk']) #due-btn:active {
    border-image-source: url('/assets/pb-dawn/util-active.png'); }
  /* (no dawn Plant override — both themes share the one whole-image rule) */
  body:not([data-theme='dusk']) #kind-menu {
    border-image: url('/assets/pb-dawn/menu-panel.png') 19 / 16px stretch;
    background: #D4C5AE; }  /* exact interior tone probed from the art */
  body:not([data-theme='dusk']) .kind-item { color: #2a2519; }
  body:not([data-theme='dusk']) .kind-item:hover { background: rgba(42,37,25,0.08); }
  body:not([data-theme='dusk']) .kind-item.sel { background: rgba(93,125,90,0.16);
    box-shadow: inset 2px 0 0 #5D7D5A; }
  #photo-btn, #due-btn {
    background: none; cursor: pointer;
    border: 12px solid transparent;
    border-image: url('/assets/pb/util-default.png') 12 fill / 11px stretch;
    padding: 0 2px;
  }
  #photo-btn:hover, #due-btn:hover { border-image-source: url('/assets/pb/util-hover.png'); }
  #photo-btn:active, #due-btn:active { border-image-source: url('/assets/pb/util-active.png'); }
  /* Plant wears the DAWN sheet's filled-green art on BOTH themes (owner:
     'make dusk plant button match the dawn' — the dark sheet's fill came
     out muddy; the dawn one reads like his original mock everywhere). */
  /* Plant: the DAWN sheet's star-pointed button on BOTH themes. Drawn as a
     whole un-stretched image — border-image was smearing the pointed tips
     into a rounded blob (they live in the stretch zones); a fixed-size
     button needs no nine-slice, just the art at its own aspect. */
  /* Plant: the owner's STAR BUTTON ELEMENTS card (2026-07-03) — the pointed
     banner badge, blank fills, one per theme colorway. Whole un-stretched
     image at natural aspect; label is real DOM text on top. */
  #capture-go {
    border: none; cursor: pointer; color: var(--pb-ivory);
    font: inherit; font-weight: 700; letter-spacing: .05em;
    width: 116px; height: 38px; padding: 0 4px;
    background: url('/assets/pb/star2-dusk.png') center / 100% 100% no-repeat;
  }
  #capture-go:hover { filter: brightness(1.15); }
  #capture-go:active { filter: brightness(0.85); }
  #capture-go:disabled { filter: grayscale(0.6) opacity(0.6); }
  body:not([data-theme='dusk']) #capture-go {
    background-image: url('/assets/pb/star2-dawn.png'); color: #24301f; }
  /* dual-ink glyphs — the owner's art recolored as FILES, not filters:
     bright set on dusk, dark-umber set on dawn. One img each, CSS picks. */
  .gi { width: 16px; height: 16px; object-fit: contain; }
  .gi-dawn { display: none; }
  body:not([data-theme='dusk']) .gi-dusk { display: none; }
  body:not([data-theme='dusk']) .gi-dawn { display: inline-block; }
  /* NOTE: a stale duplicate #capture-go rule used to live here and re-declared
     the button as a plain moss-outline pill — because it came later in the
     cascade it silently clobbered the star art above (lines ~2721-2731) on
     every render, which is why the Plant button never matched the owner's
     card no matter how often the art was recut. Removed 2026-07-04. The star
     art rule is now the single source of truth for #capture-go. */

  /* ── nav tabs ── */
  nav {
    display: block; padding: 18px 26px 0;
    max-width: 980px; margin: 0 auto; border-bottom: 1px solid var(--line);
    position: relative;
  }
  /* tabs center over the 760px hub column (which is left-aligned inside main),
     NOT the full page — owner: 'center to the boxes under it' (2026-07-04). */
  .way-tabs { display: flex; justify-content: center; gap: 26px; max-width: 760px; }
  nav button {
    background: none; border: none; color: var(--muted);
    font-family: 'Anton', sans-serif; font-size: 17px; letter-spacing: .05em; text-transform: uppercase;
    padding: 0 2px 12px; margin-bottom: -1px; cursor: pointer;
    border-bottom: 2px solid transparent;
  }
  nav button.active { color: var(--ink); border-bottom-color: var(--moss); font-weight: 600; }
  nav button:hover  { color: var(--ink); }

  main { max-width: 980px; margin: 0 auto; padding: 18px 26px 80px; }
  .hint { color: var(--muted); font-size: 12px; margin: 6px 0 14px; }

  /* ── cards ── */
  .card {
    display: flex; background: var(--card); border: 1px solid var(--line);
    border-radius: 3px; margin-bottom: 10px; overflow: hidden;
    cursor: pointer; transition: border-color .12s, background .12s;
  }
  .card:hover { border-color: var(--moss); background: var(--card-hover); }
  .card-main { flex: 1; min-width: 0; padding: 14px 16px; }
  .card.k-decision  { border-left-color: var(--moss); }
  .card.k-warning   { border-left-color: var(--terra); }
  .card.k-open_item { border-left-color: var(--amber); }
  .card.k-insight   { border-left-color: var(--gold); }
  .card.k-procedure { border-left-color: var(--ever); }
  .card.k-question, .card.k-hypothesis { border-left-color: var(--slate); }
  .card.k-idea      { border-left-color: var(--idea); }
  .card.k-conversation_turn { border-left-color: var(--slate); }
  .card .top { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .kind-chip {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--muted); font-weight: 700;
  }
  .who { font-size: 11px; color: var(--muted); }
  .who.human { color: var(--terra); font-weight: 600; }
  .mat { font-size: 13px; }
  .gist {
    font-size: 14.5px; margin: 6px 0 2px; line-height: 1.45;
  }
  .meta { font-size: 11.5px; color: var(--muted); margin-top: 6px; display: flex; gap: 14px; flex-wrap: wrap; }
  .card .actions { margin-top: 10px; display: none; gap: 8px; }
  .card.expanded .actions { display: flex; }
  .card .verbatim {
    display: none; margin-top: 10px; padding-top: 10px;
    border-top: 1px dashed var(--line);
    font-size: 13.5px; color: var(--ink); white-space: pre-wrap;
  }
  .card.expanded .verbatim { display: block; }
  .abtn {
    background: var(--card2); border: 1px solid var(--line); color: var(--muted);
    border-radius: 4px; padding: 5px 11px; cursor: pointer;
    font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 10.5px; letter-spacing: .04em;
  }
  .abtn:hover { border-color: var(--moss); color: var(--moss); }
  .abtn.danger:hover { border-color: var(--terra); color: var(--terra); }

  /* ── timeline (Today) ── */
  .tl { position: relative; padding-left: 26px; }
  .tl::before {
    content: ""; position: absolute; left: 8px; top: 6px; bottom: 6px;
    width: 2px; background: var(--line);
  }
  .tl .card { position: relative; }
  .tl .card::before {
    content: ""; position: absolute; left: -22px; top: 20px;
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--moss); border: 2px solid var(--paper);
  }
  .tl .card.k-warning::before  { background: var(--terra); }
  .tl .card.k-open_item::before{ background: var(--amber); }
  .tl .card.k-insight::before  { background: var(--gold); }

  /* ── Hub / Book / Index (human face) ── */
  .hub-wrap, .book-wrap { max-width: 760px; }
  .hub-wrap { position: relative; }
  .hub-hero { display: flex; align-items: flex-start; gap: 16px; }
  /* collapsed-welcome medallion: pinned just past the RIGHT edge of the 760px hub
     column (anchored to hub-wrap), so it sits in the gap immediately right of the
     Library card / the welcome text — the same spot whether the welcome is shown
     or hidden (owner circled this exact spot, 2026-07-04). */
  .hero-stone-corner { position: absolute; top: 14px; right: -50px; margin: 0; z-index: 3; }
  .hub-hello { font-family: 'VT323', monospace; font-size: 44px; line-height: 1.05; color: var(--ink); margin: 8px 0 20px; font-weight: 400; }
  /* click-to-edit greeting: invisible at rest. No underline, no button — the
     only hover cue is a text caret (owner: the dotted underline was too loud).
     Click opens the edit; a small transient hint shows only WHILE editing. */
  .hub-hello:hover { cursor: text; }
  .hub-hello.editing { outline: none; cursor: text; text-decoration: none; }
  .hub-hello.editing::after { content: '↵ save · esc cancel'; display: block;
    font-family: -apple-system, system-ui, sans-serif; font-size: 11px; font-weight: 400;
    color: var(--muted); letter-spacing: .04em; margin-top: 8px; }
  .hero-stone { display: inline-block; flex: none; width: 38px; height: 38px; border-radius: 50%; cursor: pointer;
    background: url('/assets/stone-button.jpg') center/cover; border: 1px solid var(--border-strong);
    box-shadow: inset 0 2px 6px rgba(0,0,0,.5); margin-top: 6px; }
  .hero-stone:hover { box-shadow: inset 0 2px 6px rgba(0,0,0,.5), 0 0 0 3px rgba(124,163,140,.35); }
  .hub-actions { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .hub-bigbtn {
    background: var(--moss); color: #FDFBF4; border: none; border-radius: 10px;
    padding: 10px 18px; font: inherit; font-weight: 600; font-size: 14px; cursor: pointer;
  }
  .hub-bigbtn.ghost { background: var(--card); color: var(--moss); border: 1px solid var(--line); }
  .hub-bigbtn:hover { filter: brightness(1.06); }
  .hub-grid { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; counter-reset: reg; }
  .hub-card {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 11px 14px; cursor: pointer; transition: border-color .12s;
  }
  .hub-card:hover { border-color: var(--moss); }
  .hub-line { font-size: 14px; color: var(--ink); line-height: 1.4; }
  .hub-foot { margin-top: 5px; display: flex; gap: 10px; align-items: center; }
  .hub-sub { font-size: 11px; color: var(--muted); }
  .hub-ts  { font-size: 11px; color: var(--muted); margin-left: auto; }
  /* ── register card (field-manual): numbered gutter + glyph + status dot + mono label ── */
  .reg-card { position: relative; display: flex; border: 1px solid var(--line); border-radius: 3px;
    background: var(--card); cursor: pointer; overflow: hidden; transition: border-color .12s, background .12s; }
  .reg-card:hover { border-color: var(--moss); background: var(--card-hover); }
  .reg-gutter { flex: none; width: 48px; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 6px; background: var(--gutter); border-right: 1px solid var(--line); }
  .hub-grid .reg-gutter::before { counter-increment: reg; content: counter(reg, decimal-leading-zero);
    font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 11px; letter-spacing: .04em; color: var(--faint); }
  .reg-glyph { color: var(--moss); font-size: 8px; opacity: .65; }
  .reg-main { flex: 1; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 14px 16px 13px; }
  .reg-title { font-size: 14.5px; line-height: 1.45; color: var(--ink); }
  .reg-status { display: inline-flex; align-items: center; gap: 7px; margin-top: 9px; }
  .reg-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--faint2); flex: none; }
  .reg-sub { font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 9.5px; letter-spacing: .14em; color: var(--faint); text-transform: uppercase; }
  .reg-ts  { font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 9.5px; letter-spacing: .04em; color: var(--faint2); white-space: nowrap; padding-top: 3px; }
  /* ── NEEDS ATTENTION banner: moss-tint + corner registration ticks ── */
  .needs-banner { position: relative; display: flex; align-items: center; justify-content: space-between;
    padding: 17px 20px; border: 1px solid rgba(124,163,140,.4); border-radius: 3px; cursor: pointer;
    background: linear-gradient(180deg, rgba(124,163,140,.10), rgba(124,163,140,.035)); }
  .needs-banner .tick { position: absolute; width: 9px; height: 9px; }
  .needs-banner .tl { top: 5px; left: 5px;  border-top: 1px solid rgba(124,163,140,.7); border-left: 1px solid rgba(124,163,140,.7); }
  .needs-banner .tr { top: 5px; right: 5px; border-top: 1px solid rgba(124,163,140,.7); border-right: 1px solid rgba(124,163,140,.7); }
  .needs-banner .bl { bottom: 5px; left: 5px;  border-bottom: 1px solid rgba(124,163,140,.7); border-left: 1px solid rgba(124,163,140,.7); }
  .needs-banner .br { bottom: 5px; right: 5px; border-bottom: 1px solid rgba(124,163,140,.7); border-right: 1px solid rgba(124,163,140,.7); }
  .nb-left { display: flex; align-items: center; gap: 15px; }
  .nb-tile { flex: none; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
    border: 1px solid rgba(124,163,140,.4); border-radius: 2px; background: rgba(124,163,140,.1); color: var(--moss); font-size: 15px; }
  .nb-title { font-size: 15px; color: var(--ink); }
  .nb-sub { font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 10px; letter-spacing: .14em; color: var(--moss); margin-top: 5px; text-transform: uppercase; }
  .nb-cta { font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size: 10px; letter-spacing: .12em; color: var(--moss); white-space: nowrap; }
  .hub-strip {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 14px; display: flex; flex-direction: column; gap: 5px;
  }
  .hub-strip-line { font-size: 13px; color: var(--ink); }
  /* Router cards — the four homes, promoted to the top of the Hub. */
  .router-row {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
    margin: 6px 0 14px;
  }
  @media (max-width: 640px){ .router-row { grid-template-columns: 1fr 1fr; } }
  .router-card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 14px; cursor: pointer; transition: border-color .12s, transform .12s;
  }
  .router-card:hover { border-color: var(--moss); transform: translateY(-1px); }
  .router-card .rc-top { display: flex; align-items: center; gap: 8px; }
  .router-card .rc-name { font-weight: 700; font-size: 14px; }
  .router-card .rc-badge {
    margin-left: auto; min-width: 22px; text-align: center;
    background: var(--moss); color: #fff; border-radius: 11px;
    font-size: 12px; font-weight: 700; padding: 1px 8px;
  }
  .router-card .rc-sub { font-size: 12px; color: var(--muted); margin-top: 5px; }

  /* ── Today: session blocks + conversation reader (C3) ── */
  .sess-block { border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 14px 14px; margin: 0 0 16px; background: var(--card2); }
  .sess-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
    padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--line); }
  .sess-title { font-family: Georgia, serif; font-size: 17px; color: var(--ink); font-weight: 600; }
  .sess-range { font-size: 12px; color: var(--muted); }
  .sess-model { font-size: 10.5px; letter-spacing: .4px; color: var(--slate);
    border: 1px solid var(--line); border-radius: 10px; padding: 1px 7px; }
  .sess-count { margin-left: auto; font-size: 11.5px; color: var(--muted); }
  .sess-read { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    margin-top: 8px; font-size: 12.5px; font-weight: 600; color: var(--moss);
    border: 1px dashed var(--line); border-radius: 8px; padding: 6px 11px; user-select: none; }
  .sess-read:hover { border-color: var(--moss); background: var(--card-hover); }
  .sess-read-caret { font-size: 11px; }
  .sess-turns { margin-top: 10px; }
  .reader { border-left: 2px solid var(--line); padding-left: 14px; margin-left: 4px; }
  .reader .turn { margin: 0 0 14px; }
  .reader .turn-meta { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
  .reader .turn-who { font-weight: 700; font-size: 12px; letter-spacing: .3px; }
  .reader .turn-you .turn-who { color: var(--moss); }
  .reader .turn-ai  .turn-who { color: var(--slate); }
  .reader .turn-time { font-size: 11px; color: var(--muted); }
  .reader .turn-text { font-family: Georgia, serif; font-size: 14.5px; line-height: 1.62; color: var(--ink); }
  .reader .turn-text p { margin: 0 0 8px; white-space: pre-wrap; overflow-wrap: anywhere; }

  .book-title { font-size: 24px; font-weight: 600; color: var(--ink); margin: 4px 0 2px; letter-spacing: 0.5px; }
  .book-section-h {
    font-size: 12px; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--moss); margin: 28px 0 12px; font-weight: 700;
    border-bottom: 1px solid var(--line); padding-bottom: 5px;
  }
  .book-line {
    font-size: 14px; color: var(--ink); padding: 7px 2px; cursor: pointer;
    line-height: 1.45; border-bottom: 1px solid transparent;
  }
  .book-line:hover { color: var(--moss); }
  .book-project { margin-bottom: 26px; }
  .book-proj-h { font-size: 18px; font-weight: 600; color: var(--ink); }
  .book-count { color: var(--muted); font-size: 13px; font-weight: 400; }
  .book-desc { font-size: 13px; color: var(--muted); margin: 3px 0 10px; line-height: 1.5; }
  .book-chapter { margin: 12px 0 12px 14px; }
  .book-chap-h { font-size: 14px; font-weight: 600; color: var(--ink); margin-bottom: 5px; }
  .book-ex { font-size: 13px; color: var(--muted); padding: 4px 0 4px 14px; cursor: pointer; line-height: 1.4; }
  .book-ex:hover { color: var(--moss); }
  .book-vol { font-size: 13px; color: var(--ink); padding: 6px 2px; }
  .book-vol-head { cursor: pointer; display: flex; align-items: baseline; gap: 7px; user-select: none; }
  .book-vol-head:hover { color: var(--moss); }
  .vol-caret { font-size: 11px; color: var(--muted); }
  .vol-sessions { margin: 6px 0 10px 18px; }
  .vol-sess-row { font-size: 12.5px; color: var(--ink); padding: 5px 2px; cursor: pointer;
    display: flex; align-items: baseline; gap: 8px; user-select: none; }
  .vol-sess-row:hover { color: var(--moss); }
  .vol-sess-turns { margin: 4px 0 10px 16px; }

  /* ── Book: older projects (collapsed) ── */
  .book-older summary { cursor: pointer; font-size: 13px; color: var(--muted);
    font-weight: 600; padding: 4px 0; user-select: none; list-style: none; }
  .book-older summary:hover { color: var(--moss); }
  .book-older summary::before { content: '▸ '; font-size: 11px; }
  .book-older[open] summary::before { content: '▾ '; }
  .book-older-row { font-size: 13px; color: var(--ink); padding: 5px 2px 5px 16px;
    cursor: pointer; display: flex; align-items: baseline; gap: 8px; }
  .book-older-row:hover { color: var(--moss); }

  /* ── Today: the just-landed strip ── */
  .landed-strip { border: 1px solid var(--line); border-radius: 10px;
    background: var(--card); padding: 9px 13px; margin-bottom: 12px; }
  .landed-h { font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--moss); font-weight: 700; margin-bottom: 5px; }
  .landed-row { display: flex; align-items: baseline; gap: 8px; padding: 3.5px 0;
    cursor: pointer; font-size: 13px; min-width: 0; }
  .landed-row:hover .landed-gist { color: var(--moss); }
  .landed-mark { font-size: 13px; color: var(--muted); flex-shrink: 0; width: 12px; text-align: center; }
  .landed-gist { color: var(--ink); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; flex: 1; min-width: 0; }
  .landed-ts { font-size: 11px; color: var(--muted); flex-shrink: 0; }

  /* ── node tags: human chips + folded machine strata (display only) ── */
  .tag-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 10px 0 4px; }
  .tag-chip { background: var(--card); border: 1px solid var(--line); color: var(--ink);
    border-radius: 999px; padding: 2px 11px; font-size: 12px; cursor: pointer; }
  .tag-chip:hover { border-color: var(--moss); color: var(--moss); }
  .tag-chip.machine { color: var(--muted); font-family: ui-monospace, monospace;
    font-size: 11px; cursor: default; }
  .tag-chip.machine:hover { border-color: var(--line); color: var(--muted); }
  .prov-toggle { font-size: 11.5px; color: var(--muted); cursor: pointer;
    user-select: none; white-space: nowrap; }
  .prov-toggle:hover { color: var(--moss); }
  .prov-tags { display: inline-flex; flex-wrap: wrap; gap: 6px; }

  /* ── hubs ── */
  .tag-cloud { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 22px; }
  .tag-pill {
    background: var(--card); border: 1px solid var(--line); color: var(--ink);
    border-radius: 999px; padding: 6px 16px; cursor: pointer; font-size: 13px;
  }
  .tag-pill:hover { border-color: var(--moss); color: var(--moss); }
  .tag-pill .n { color: var(--muted); font-size: 11px; margin-left: 5px; }
  /* declared-project marker, moved to the END so the pill sorts/reads by the
     project NAME (demoapp under D), not the 'proj:' prefix (owner ask). */
  .tag-pill .pj { color: var(--muted); font-size: 9.5px; margin-left: 6px;
    text-transform: uppercase; letter-spacing: .08em; opacity: .75; }
  .az-bar { display: flex; flex-wrap: wrap; gap: 2px; margin: 0 0 14px; padding: 4px 0; border-bottom: 1px solid var(--line); }
  .az-jump { cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; padding: 2px 7px; border-radius: 5px; }
  .az-jump:hover { color: var(--moss); background: var(--card); }
  .az-h { font-size: 13px; font-weight: 700; color: var(--moss); border-bottom: 1px solid var(--line); margin: 8px 0 8px; padding-bottom: 3px; scroll-margin-top: 70px; }
  #index-topics { scroll-margin-top: 70px; }
  .hub-head {
    font-family: Georgia, serif; font-size: 19px; margin: 18px 0 4px;
    display: flex; align-items: center; gap: 10px;
  }
  .gold-dot { width: 11px; height: 11px; border-radius: 50%; background: var(--gold);
              box-shadow: 0 0 0 3px color-mix(in srgb, var(--gold) 25%, transparent); }

  /* ── node page ── */
  .breadcrumb { font-size: 12px; color: var(--muted); margin-bottom: 14px; display: flex; align-items: center; gap: 12px; }
  .breadcrumb a { cursor: pointer; }
  .backbtn { background: var(--card); border: 1px solid var(--line); color: var(--ink);
    border-radius: 8px; padding: 7px 16px; cursor: pointer; font-size: 14px; font-weight: 600;
    display: inline-flex; align-items: center; gap: 6px; }
  .backbtn:hover { border-color: var(--moss); color: var(--moss); background: var(--card2); }
  .section-h {
    font-size: 11px; letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--muted); margin: 22px 0 8px; font-weight: 700;
  }
  /* distressed tier divider — a single line torn from the plant-box element
     sheet, the tier's name sitting on it (owner: separate the 3 project tiers) */
  .proj-sec {
    display: flex; align-items: center; justify-content: center; text-align: center; gap: 8px;
    margin: 30px 0 16px; min-height: 12px; padding: 1px 20px;
    font-size: 12px; letter-spacing: 2.5px; text-transform: uppercase;
    color: #d8cfb8; font-weight: 700;
    border: 16px solid transparent;                                    /* same frame as the Plant bar */
    border-image: url('/assets/pb/frame.png') 20 fill / 16px stretch;  /* fill = frame's own interior, no seam, blacks match */
  }
  .proj-sec:first-of-type { margin-top: 8px; }
  body:not([data-theme='dusk']) .proj-sec {
    border-image: url('/assets/pb-dawn/frame.png') 20 fill / 16px stretch;
  }
  .reply-row { display: flex; gap: 8px; margin-top: 14px; }
  .reply-row input {
    flex: 1; background: var(--card); color: var(--ink);
    border: 1px solid var(--line); border-radius: 8px; padding: 9px 14px; font: inherit;
  }

  /* ── project cards ── */
  .proj {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px 22px; margin-bottom: 14px;
    box-shadow: var(--shadow); cursor: pointer; transition: transform .08s;
  }
  .proj:hover { transform: translateY(-2px); border-color: var(--moss); }
  .proj .name { font-family: Georgia, serif; font-size: 20px; display: flex; align-items: baseline; gap: 10px; }
  .proj .desc { color: var(--muted); font-size: 13px; }
  .proj .badges { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; font-size: 12px; }
  .pbadge { border-radius: 6px; padding: 3px 10px; background: var(--card2); color: var(--muted); }
  .pbadge.open    { color: var(--amber); font-weight: 700; }
  .pbadge.warn    { color: var(--terra); font-weight: 700; }
  .matbar { display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 12px; background: var(--card2); }
  .matbar div { height: 100%; }
  .m-seed { background: #9CC09A; } .m-bud { background: var(--moss); } .m-ever { background: var(--ever); }
  .proj .lastline { margin-top: 10px; font-size: 12.5px; color: var(--muted); font-style: italic; }
  .emerging-chip { font-size: 10px; color: var(--amber); border: 1px solid var(--amber);
                   border-radius: 5px; padding: 1px 7px; letter-spacing: 1px; }
  .proj-actions { margin-top: 12px; }
  .abtn:disabled { opacity: .45; cursor: default; }
  .promote-form { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 10px; }
  .promote-grid { display: grid; grid-template-columns: auto 1fr; gap: 8px 12px; align-items: center; }
  .promote-lbl { font-size: 12px; color: var(--muted); font-weight: 600; }
  .promote-in { padding: 6px 10px; font-size: 13px; background: var(--card2); color: var(--ink);
                border: 1px solid var(--line); border-radius: 7px; width: 100%; }
  .promote-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
  .promote-alias { font-size: 12.5px; color: var(--ink); display: inline-flex; align-items: center; gap: 4px; }
  .promote-btns { margin-top: 10px; display: flex; gap: 8px; }

  /* the tour — rebuilt from the owner's element sheet: the MENU panel's
     cut-corner sage frame, sized to its text (his call: the first card was
     'wrong size for text and sloppy'). Same octagon language as the bar. */
  #tour-scrim { position: fixed; inset: 0; z-index: 400; background: rgba(10,12,10,0.62); }
  #tour-card { position: fixed; left: 50%; bottom: 9vh; transform: translateX(-50%);
    width: fit-content; max-width: min(400px, calc(100vw - 40px));
    background: var(--pb-gray, #7F8878);
    --cut: 14px;
    clip-path: polygon(var(--cut) 0, calc(100% - var(--cut)) 0, 100% var(--cut),
      100% calc(100% - var(--cut)), calc(100% - var(--cut)) 100%, var(--cut) 100%,
      0 calc(100% - var(--cut)), 0 var(--cut));
    padding: 1.5px; filter: drop-shadow(0 16px 40px rgba(0,0,0,0.6)); }
  #tour-inner { background-color: var(--pb-black, #12110E); padding: 20px 22px 16px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 0.55 0 0 0 0 0.53 0 0 0 0 0.45 0 0 0 0.16 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
    background-size: 160px;
    clip-path: polygon(13px 0, calc(100% - 13px) 0, 100% 13px, 100% calc(100% - 13px),
      calc(100% - 13px) 100%, 13px 100%, 0 calc(100% - 13px), 0 13px);
    position: relative; }
  #tour-inner::after { content: ''; position: absolute; top: 14px; right: 14px;
    width: 38px; height: 38px; border: 1.5px solid rgba(127,136,120,0.45); border-radius: 50%;
    background: url('/assets/icons/cairn-mark.png') center/22px no-repeat; opacity: .4; }
  .tour-step { font: 700 12px/1 monospace; letter-spacing: .14em; color: var(--pb-brass, #C79A43); margin-bottom: 10px; }
  .tour-title { font-family: 'Courier New', Georgia, serif; font-weight: 700; font-size: 21px;
    line-height: 1.1; color: var(--pb-ivory, #E8E2D6); padding-right: 46px; }
  .tour-title::after { content: ''; display: block; width: 30px; height: 2px;
    background: var(--pb-mint, #8FBFAF); margin-top: 9px; }
  .tour-text { margin-top: 10px; font: 400 13px/1.65 monospace; color: #b9b3a4; }
  .tour-btns { margin-top: 16px; display: flex; gap: 10px; justify-content: flex-end; }
  .tour-btns .abtn { --cut: 7px; border: 1px solid var(--pb-gray, #7F8878); border-radius: 2px;
    padding: 8px 16px; font-family: monospace; color: var(--pb-ivory, #E8E2D6); background: transparent;
    clip-path: polygon(var(--cut) 0, calc(100% - var(--cut)) 0, 100% var(--cut),
      100% calc(100% - var(--cut)), calc(100% - var(--cut)) 100%, var(--cut) 100%,
      0 calc(100% - var(--cut)), 0 var(--cut)); }
  .tour-next { border-color: var(--pb-mint, #8FBFAF) !important; color: var(--pb-mint, #8FBFAF) !important; font-weight: 700; }
  /* glow = outline ONLY — painting a background here made a wrong-colored
     box on the owner's real vault theme (his 'weird blue box' catch) */
  .tour-glow { position: relative; z-index: 401; outline: 2px solid var(--pb-mint, #8FBFAF);
    outline-offset: 4px; border-radius: 10px; }
  /* blends in until the tour is actually running (owner: 'shouldnt be lit
     up unless its on tour') */
  #tour-btn { border: 1px solid transparent; border-radius: 2px;
    color: var(--muted); font-weight: 700; min-width: 30px; }
  #tour-btn:hover { border-color: rgba(127,136,120,0.5); color: var(--ink); }
  body.touring #tour-btn { border-color: var(--pb-mint, #8FBFAF); color: var(--pb-mint, #8FBFAF); }

  .empty { color: var(--muted); font-style: italic; padding: 30px 0; text-align: center; }

  /* phone */
  @media (max-width: 640px) {
    header { padding: 10px 14px; } .sub { display: none; }
    #capture-bar { padding: 12px 14px 2px; flex-wrap: wrap; }
    #capture-text { min-width: 100%; }
    nav { padding: 10px 14px 0; overflow-x: auto; }
    main { padding: 14px 14px 70px; }
    .gist { font-size: 15.5px; }
  }

  .toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--ink); color: var(--paper); border-radius: 8px;
    padding: 9px 20px; font-size: 13px; opacity: 0; transition: opacity .25s;
    pointer-events: none; z-index: 100;
  }
  .toast.show { opacity: 1; }
  .score-chip { font-size: 11px; color: var(--moss); font-weight: 700; }

  /* ── facelift: self-hosted fonts + page texture (served from /assets) ── */
  @font-face{font-family:'Anton';font-weight:400;font-display:swap;src:url('/assets/fonts/anton-latin-400.woff2') format('woff2');}
  @font-face{font-family:'VT323';font-weight:400;font-display:swap;src:url('/assets/fonts/vt323-latin-400.woff2') format('woff2');}
  @keyframes cairnGrain{0%{transform:translate(0,0)}20%{transform:translate(-6%,3%)}40%{transform:translate(-3%,-5%)}60%{transform:translate(5%,4%)}80%{transform:translate(6%,-2%)}100%{transform:translate(0,0)}}
  .bgStone,.bgPaper{position:fixed;inset:0;pointer-events:none;z-index:-2;background-size:cover;background-position:center;}
  .bgStone{background-image:url('/assets/garden-bg.png');opacity:.95;display:none;}
  .bgPaper{background-image:url('/assets/paper-bg.png');}
  [data-theme="dusk"] .bgStone{display:block;}
  [data-theme="dusk"] .bgPaper{display:none;}
  .grain{position:fixed;inset:-50%;width:200%;height:200%;pointer-events:none;z-index:-1;mix-blend-mode:overlay;opacity:.05;animation:cairnGrain 7s steps(8) infinite;background-image:url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22200%22 height=%22200%22><filter id=%22n%22><feTurbulence type=%22fractalNoise%22 baseFrequency=%220.9%22 numOctaves=%222%22 stitchTiles=%22stitch%22/></filter><rect width=%22100%25%22 height=%22100%25%22 filter=%22url(%23n)%22/></svg>');}
  body:not([data-theme="dusk"]) .grain{mix-blend-mode:multiply;opacity:.5;}

  /* ── Cairn icon set — the bone-glyph PNGs used as luminance masks so a single
       currentColor-tinted glyph inherits the surrounding text color and theme.
       Icons sit BESIDE their text labels (locked design rule) — never replace
       them. Per-icon --u carries the mask url; size class sets the box. ── */
  .cicon { display:inline-block; width:18px; height:18px;
    mask-image:var(--u); -webkit-mask-image:var(--u); mask-mode:luminance;
    mask-size:contain; -webkit-mask-size:contain; mask-repeat:no-repeat;
    -webkit-mask-repeat:no-repeat; mask-position:center; -webkit-mask-position:center;
    background-color:currentColor; vertical-align:-2px; flex:none; }
  .cicon.s16 { width:16px; height:16px; }
  .cicon.s18 { width:18px; height:18px; }
  .cicon.s20 { width:20px; height:20px; }
  /* ── KEY legend (popover + Book plate) ── */
  #key-pop { position:fixed; top:58px; right:22px; z-index:80; width:320px; max-height:78vh;
    overflow-y:auto; display:none; background:var(--card); border:1px solid var(--border-strong);
    border-radius:10px; box-shadow:var(--shadow); padding:14px 16px; }
  #key-pop.open { display:block; }
  #key-pop .key-h { font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size:10px;
    letter-spacing:.18em; text-transform:uppercase; color:var(--moss); margin:12px 0 7px; font-weight:700;
    border-bottom:1px solid var(--line); padding-bottom:4px; }
  #key-pop .key-h:first-child { margin-top:0; }
  .key-row { display:flex; align-items:center; gap:9px; padding:4px 0; }
  .key-row .cicon { color:var(--ink); }
  .key-lbl { font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size:10px;
    letter-spacing:.1em; text-transform:uppercase; color:var(--ink); font-weight:700; flex:none; width:78px; }
  .key-def { font-size:12px; color:var(--muted); line-height:1.35; }
  .fresh-glyph { flex:none; width:18px; text-align:center; font-size:15px; color:var(--moss); font-weight:700; }
  .key-actions { display:flex; flex-wrap:wrap; gap:10px 14px; }
  .key-act { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--muted); }
  .key-act .cicon { color:var(--moss); }
  /* Book/Index legend plate — field-manual framing */
  .legend-plate { border:1px solid var(--line); border-radius:3px; background:var(--card);
    padding:14px 16px; margin:8px 0 4px; }
  .legend-plate .lp-grid { display:grid; grid-template-columns:1fr 1fr; gap:3px 22px; }
  @media (max-width:640px){ .legend-plate .lp-grid{ grid-template-columns:1fr; } }
</style>
</head>
<body>

<div class="bgStone" aria-hidden="true"></div>
<div class="bgPaper" aria-hidden="true"></div>
<div class="grain" aria-hidden="true"></div>

<header>
  <img class="logo-mark boneOnly" src="/assets/mark-bone.png" alt="">
  <img class="logo-mark inkOnly"  src="/assets/mark-ink.png"  alt="">
  <img class="logo-word boneOnly" src="/assets/wordmark.png"     alt="Cairn">
  <img class="logo-word inkOnly"  src="/assets/wordmark-ink.png" alt="Cairn">
  <span class="gardenlbl">Remembers</span>
  <span class="sub">what your memory is holding</span>
  <span class="spacer"></span>
  <button class="hbtn" id="tour-btn" onclick="tourStart(0)" title="the walkthrough — how to move around">?</button>
  <button class="hbtn" id="key-btn" onclick="toggleKey()" title="what the icons mean"><span class="cicon s16" style="--u:url('/assets/icons/book.png');vertical-align:-3px" aria-hidden="true"></span> KEY</button>
  <button class="hbtn" id="theme-btn" onclick="toggleTheme()">dusk</button>
  <a href="/" id="tour-brain-link" title="the node space — every memory as a star" style="display:inline-flex;align-items:center">
    <img src="/assets/btn-brain.png" alt="the brain →" style="height:30px;width:auto;display:block"></a>
</header>

<!-- KEY legend popover — static; opens from the header KEY button -->
<div id="key-pop" aria-label="icon key">
  <div class="key-h">Kinds</div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/idea.png')"></span><span class="key-lbl">Idea</span><span class="key-def">a spark kept for later</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/todo.png')"></span><span class="key-lbl">To-do</span><span class="key-def">an open item that needs doing</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/question.png')"></span><span class="key-lbl">Question</span><span class="key-def">a thread left open</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/note.png')"></span><span class="key-lbl">Note</span><span class="key-def">an insight or observation</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/warning.png')"></span><span class="key-lbl">Warning</span><span class="key-def">something to watch out for</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/done.png')"></span><span class="key-lbl">Done</span><span class="key-def">resolved — kept as record</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/photo.png')"></span><span class="key-lbl">Photo</span><span class="key-def">a captured image</span></div>
  <div class="key-h">Maturity</div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/seedling.png')"></span><span class="key-lbl">Seedling</span><span class="key-def">young — stability under 3 days</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/budding.png')"></span><span class="key-lbl">Budding</span><span class="key-def">taking root — 3 to 30 days</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/evergreen.png')"></span><span class="key-lbl">Evergreen</span><span class="key-def">stable — 30+ days, or a procedure</span></div>
  <div class="key-h">Status &amp; shelves</div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/due.png')"></span><span class="key-lbl">Due</span><span class="key-def">dated — on the desk for a day</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/overdue.png')"></span><span class="key-lbl">Overdue</span><span class="key-def">past its date</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/inbox.png')"></span><span class="key-lbl">Inbox</span><span class="key-def">captured on the go, awaiting filing</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/fresh.png')"></span><span class="key-lbl">Fresh</span><span class="key-def">planted in the last 14 days</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/bank.png')"></span><span class="key-lbl">Bank</span><span class="key-def">the older reference shelf</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/parked.png')"></span><span class="key-lbl">Parked</span><span class="key-def">deliberately shelved — waiting</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/ripe.png')"></span><span class="key-lbl">Ripe</span><span class="key-def">researched &amp; revisited — ready</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/findings.png')"></span><span class="key-lbl">Findings</span><span class="key-def">research chained onto an idea</span></div>
  <div class="key-row"><span class="cicon s18" style="--u:url('/assets/icons/fog.png')"></span><span class="key-lbl">Fading</span><span class="key-def">surfaced but never used</span></div>
  <div class="key-h">Actions</div>
  <div class="key-actions">
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/done.png')"></span>done / still-true</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/archive.png')"></span>archive</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/snooze.png')"></span>snooze</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/pin.png')"></span>pin</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/research.png')"></span>research</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/revisit.png')"></span>revisit</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/restore.png')"></span>restore</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/flag.png')"></span>flag</span>
    <span class="key-act"><span class="cicon s16" style="--u:url('/assets/icons/dismiss.png')"></span>dismiss</span>
  </div>
  <div class="key-h">Freshness</div>
  <div class="key-row"><span class="fresh-glyph">·</span><span class="key-lbl">Live</span><span class="key-def">Hub, Today, Desk, Projects — read the vault as-is, every load</span></div>
  <div class="key-row"><span class="fresh-glyph">○</span><span class="key-lbl">Sleep-refreshed</span><span class="key-def">Search, Spark, Wander, Topics — refreshed by the nightly sleep pass</span></div>
</div>

<div id="capture-bar">
  <input id="capture-text" placeholder="Plant a thought… add due:2026-07-01 to date it"
         onkeydown="if(event.key==='Enter')capture()">
  <input type="hidden" id="capture-kind" value="idea">
  <div id="kind-dd">
    <button id="kind-btn" type="button" onclick="kindToggle(event)" title="what kind of thought">
      <img id="kind-glyph" class="gi gi-dusk" src="/assets/icons/pb-idea.png" alt="">
      <img id="kind-glyph-d" class="gi gi-dawn" src="/assets/icons/pb-idea-dark.png" alt="">
      <span id="kind-label">idea</span><span class="kind-chev">⌄</span>
    </button>
    <div id="kind-menu" style="display:none"><div id="kind-menu-inner">
      <div class="kind-item sel" data-k="idea" onclick="kindPick('idea')"><img class="gi gi-dusk" src="/assets/icons/pb-idea.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-idea-dark.png" alt="">idea</div>
      <div class="kind-item" data-k="open_item" onclick="kindPick('open_item')"><img class="gi gi-dusk" src="/assets/icons/pb-todo.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-todo-dark.png" alt="">to-do</div>
      <div class="kind-item" data-k="question" onclick="kindPick('question')"><img class="gi gi-dusk" src="/assets/icons/pb-question.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-question-dark.png" alt="">question</div>
      <div class="kind-item" data-k="insight" onclick="kindPick('insight')"><img class="gi gi-dusk" src="/assets/icons/pb-note.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-note-dark.png" alt="">note</div>
    </div></div>
  </div>
  <input type="file" id="capture-photo" accept="image/*" capture="environment" style="display:none"
         onchange="photoChosen()">
  <button class="hbtn" id="photo-btn" style="padding:0 12px" title="attach a photo"
          onclick="document.getElementById('capture-photo').click()"><img class="gi gi-dusk" src="/assets/icons/pb-camera-bright.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-camera-dark.png" alt=""></button>
  <input type="date" id="capture-date" style="display:none" onchange="dueChosen()">
  <button class="hbtn" id="due-btn" style="padding:0 12px" title="add a due date — appends due:YYYY-MM-DD"
          onclick="document.getElementById('capture-date').showPicker ? document.getElementById('capture-date').showPicker() : document.getElementById('capture-date').click()"><img class="gi gi-dusk" src="/assets/icons/pb-calendar-bright.png" alt=""><img class="gi gi-dawn" src="/assets/icons/pb-calendar-dark.png" alt=""></button>
  <button id="capture-go" onclick="capture()">Plant</button>
</div>

<!-- One navigation, the human one: the Hub is the hall, every room links back
     through it. ⌂ home · where-you-are label · search. (Owner ruling 562e4e710584:
     the six-tab bar duplicated the Hub's router cards — it's gone.) -->
<nav id="wayline">
  <div class="way-tabs">
    <button id="way-home" class="active" onclick="show('hub')">Hub</button>
    <button id="way-index" onclick="show('index')">Index</button>
    <button id="way-search" title="search (Ctrl+K)" onclick="show('search')">⌕ search</button>
  </div>
  <span id="way-here" style="position:absolute;right:26px;top:18px;font-size:12px;color:var(--muted);letter-spacing:.08em"></span>
</nav>

<main id="main"></main>
<div class="toast" id="toast"></div>

<script>
// error reporter — any JS failure shows itself instead of dying silently
window.onerror = function(msg, src, line) {
  let b = document.getElementById('errbar');
  if (!b) {
    b = document.createElement('div');
    b.id = 'errbar';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#B65C32;color:#fff;'
      + 'padding:8px 16px;font:13px monospace;z-index:999;white-space:pre-wrap';
    document.body.appendChild(b);
  }
  b.textContent = 'garden error: ' + msg + ' (line ' + line + ')';
  return false;
};
window.onunhandledrejection = function(e) {
  window.onerror('async: ' + (e.reason && e.reason.message || e.reason), '', 0);
};
const $ = id => document.getElementById(id);
let view = 'hub';

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}
// ── Cairn icon set ───────────────────────────────────────────────────────────
// DECLARED FIRST: applyThemeLabel() runs at script boot a few lines down, and
// it calls icon() — a `const` declared after that call is a temporal-dead-zone
// ReferenceError that kills the whole script (blank Hub). Keep this block above
// every top-level caller.
// 37 finalized bone-glyph PNGs live in /assets/icons; each is used as a CSS
// luminance mask (see .cicon) so one currentColor-tinted glyph inherits its
// context. ICONS lists the names that have a real PNG — anything NOT here has
// no icon by design (e.g. from-the-deep) and falls back to its emoji/text.
// icon(name, emoji, cls) returns a <span class="cicon"> when the PNG exists,
// else the emoji string — so fallback is automatic, never hand-tracked.
// Only STATIC name strings ever reach the --u url (never user input); labels
// beside icons stay in the calling template and still go through esc().
const ICONS = new Set(['cairn-mark','idea','todo','question','note','done',
  'archive','snooze','pin','research','revisit','restore','flag','dismiss',
  'book','search','spark','drift','fresh','bank','parked','settings','due',
  'overdue','inbox','warning','findings','ripe','fog','speaker-you','photo',
  'seedling','budding','evergreen','dawn','dusk','skip',
  // the owner's element-sheet glyphs (Plant Bar Elements, 2026-07-03)
  'pb-idea','pb-todo','pb-question','pb-note','pb-camera','pb-calendar',
  'pb-due','pb-archive','pb-mark',
  // recolored inks: -dark set for dawn, -bright pair for the dusk chips
  'pb-idea-dark','pb-todo-dark','pb-question-dark','pb-note-dark',
  'pb-camera-dark','pb-calendar-dark','pb-due-dark','pb-archive-dark',
  'pb-camera-bright','pb-calendar-bright']);
function icon(name, emoji, cls) {
  if (!ICONS.has(name)) return emoji || '';          // no PNG → emoji fallback
  const sz = cls ? (' ' + cls) : ' s18';
  // name is a static literal from our own call sites — safe in the url()
  return `<span class="cicon${sz}" style="--u:url('/assets/icons/${name}.png')" aria-hidden="true"></span>`;
}

function toggleTheme() {
  const b = document.body;
  b.dataset.theme = b.dataset.theme === 'dusk' ? '' : 'dusk';
  localStorage.setItem('garden-theme', b.dataset.theme);
  applyThemeLabel();
}
function applyThemeLabel() {
  const btn = document.getElementById('theme-btn');
  if (!btn) return;
  // icon BESIDE the word (design rule): dawn icon when going to dawn, dusk when dusk.
  const dusk = document.body.dataset.theme === 'dusk';
  const name = dusk ? 'dawn' : 'dusk';   // the label = where the click takes you
  btn.innerHTML = icon(name, dusk ? '☀' : '☾', 's16') +
    ' ' + (dusk ? 'dawn' : 'dusk');
  const b = btn.querySelector('.cicon'); if (b) b.style.verticalAlign = '-3px';
}
// KEY legend popover — pure show/hide of static markup; closes on outside click.
function toggleKey() {
  const p = document.getElementById('key-pop');
  if (!p) return;
  const open = p.classList.toggle('open');
  if (open) setTimeout(() => document.addEventListener('click', _keyOutside), 0);
}
function _keyOutside(e) {
  const p = document.getElementById('key-pop'), b = document.getElementById('key-btn');
  if (p && !p.contains(e.target) && b && !b.contains(e.target)) {
    p.classList.remove('open');
    document.removeEventListener('click', _keyOutside);
  }
}
// dusk is the default now; only an explicit dawn choice (stored '') opts out
const _gt = localStorage.getItem('garden-theme');
if (_gt === 'dusk' || _gt === null) document.body.dataset.theme = 'dusk';
applyThemeLabel();

const MAT = { seedling: '🌱', budding: '🌿', evergreen: '🌲' };

function esc(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
// Card title: never mid-word. Old stored gists may end mid-letter (the old
// writer hard-chopped at 110); when the query is the same-but-fuller text,
// rebuild the title from it, and always cut on a word boundary with an "…".
function titleOf(n) {
  const g = (n.gist || '').trim(), q = (n.query || '').replace(/\n/g, ' ').trim();
  const clipped = g && q.length > g.length && q.startsWith(g.slice(0, Math.min(60, g.length)));
  let src = clipped ? q : (g || q);
  if (src.length <= 110) return src;
  const c = src.lastIndexOf(' ', 110);
  return (c > 40 ? src.slice(0, c) : src.slice(0, 110)) + ' …';
}
// JS-string-context escape — for a value interpolated INTO an onclick="..."
// handler (a single-quoted JS string inside a double-quoted HTML attribute).
// HEX-escape the quotes/backslash/'<' so NO raw quote ever reaches the HTML
// stream: a literal " would close the attribute and a literal ' the JS string,
// and HTML ignores backslash escapes (so \" would still break out). \x22/\x27/
// \x3c decode to the right chars only at JS-eval. (HTML entities are wrong here
// too: &#39; would decode back to ' and break the string.) Use jesc in onclick.
function jesc(s) {
  return (s || '').replace(/\\/g,'\\\\').replace(/'/g,'\\x27').replace(/"/g,'\\x22')
                  .replace(/</g,'\\x3c').replace(/\r?\n/g,' ');
}

function when(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})
                 : d.toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
  } catch(e) { return ''; }
}

// ── Node tags: human chips + folded machine strata (DISPLAY only) ───────────
// Mirrors the server's _is_machine_tag prefix denylist (garden.py). Human tags
// render as chips; machine-strata tags (kw:/entity:/prov:/…) fold behind a
// small "provenance N ▸" expander — the owner's brain-tags feedback. The data
// is untouched: every tag stays in the vault and in retrieval.
const MACHINE_TAG_PREFIXES = ['kw:','entity:','prov:','by:','stance:',
  'account:','turn:','member:','due:'];
function isMachineTag(t) {
  return typeof t === 'string' && MACHINE_TAG_PREFIXES.some(p => t.startsWith(p));
}
function tagChipsHTML(tags) {
  const list = Array.isArray(tags) ? tags.filter(t => typeof t === 'string' && t) : [];
  if (!list.length) return '';
  const human   = list.filter(t => !isMachineTag(t));
  const machine = list.filter(isMachineTag);
  const chips = human.map(t =>
    `<span class="tag-chip" onclick="renderHub('${jesc(t)}')" title="see everything tagged ${esc(t)}">${esc(t)}</span>`).join('');
  const fold = machine.length ? `<span class="prov-toggle" onclick="toggleProv(event,this)" ` +
    `title="machine strata — retrieval plumbing the AI uses; folded away, never deleted">` +
    `provenance ${machine.length} <span class="prov-caret">▸</span></span>` +
    `<span class="prov-tags" style="display:none">` +
    machine.map(t => `<span class="tag-chip machine">${esc(t)}</span>`).join('') +
    `</span>` : '';
  if (!chips && !fold) return '';
  return `<div class="tag-row">${chips}${fold}</div>`;
}
function toggleProv(e, el) {
  e.stopPropagation();
  const zone = el.nextElementSibling;
  const caret = el.querySelector('.prov-caret');
  if (!zone) return;
  const open = zone.style.display === 'none';
  zone.style.display = open ? 'inline-flex' : 'none';
  if (caret) caret.textContent = open ? '▾' : '▸';
}

function cardHTML(n, opts = {}) {
  const who = n.model === 'human'
    ? `<span class="who human">${icon('speaker-you','✍','s16')} you</span>`
    : `<span class="who">⚙ ${esc(n.model)}</span>`;
  const score = opts.score !== undefined
    ? `<span class="score-chip">${opts.score}</span>` : '';
  return `
  <div class="card" data-id="${esc(n.id)}" onclick="toggleCard(event, this)">
    <div class="reg-gutter"><span class="reg-glyph">▲</span></div>
    <div class="card-main">
    <div class="top">
      <span class="reg-sub">${esc(n.kind).replace('_',' ')}</span>
      ${who} ${score}
      <span class="who" style="margin-left:auto">${when(n.timestamp)} · ${esc((n.session||'').slice(0,26))}</span>
    </div>
    <div class="gist">${esc(titleOf(n))}</div>
    ${(n.tier === 0 || n.flagged) ? `<div class="meta">
      ${n.tier === 0 ? '<span style="color:var(--gold)" title="pinned — kept front-and-center & surfaced to the AI more">' + icon('pin','📌','s16') + ' pinned</span>' : ''}
      ${n.flagged ? '<span style="color:var(--amber)">' + icon('flag','⚑','s16') + ' flagged</span>' : ''}
    </div>` : ''}
    ${mediaThumb(n)}
    <div class="verbatim">${(() => { const q = n.query || '', p = n.preview || '';
      // query is the capped headline (500); when preview is the SAME text but
      // fuller, show the full text — never a mid-sentence cutoff.
      if (p && _sameStart(p, q) && p.length > q.length) return esc(p);
      return esc(q) + (p && !_sameStart(p, q) ? '\n\n' + esc(p) : ''); })()}</div>
    <div class="actions">
      <button class="abtn" onclick="openNode(event,'${jesc(n.id)}')">open</button>
      ${opts.done ? `<button class="abtn" style="border-color:var(--moss);color:var(--moss)" title="mark done — kept as record" onclick="markDone(event,'${jesc(n.id)}')">${icon('done','✓','s16')} done</button>` : ''}
      ${opts.review ? `<button class="abtn" title="still true — grows its stability" onclick="act(event,'${jesc(n.id)}','review','tended — stability grew')">${icon('done','✓','s16')} still true</button>` : ''}
      ${opts.revisit ? `<button class="abtn" style="border-color:var(--idea);color:var(--idea)" title="still interesting — keeps the spark alive" onclick="act(event,'${jesc(n.id)}','review','spark tended — it lives on')">${icon('revisit','↺','s16')} still interesting</button>` : ''}
      ${opts.research ? `<button class="abtn" title="queue for research — findings chain back to this idea" onclick="requestResearch(event,'${jesc(n.id)}')">${icon('research','🔍','s16')} research</button>` : ''}
      ${(opts.shelf !== false && (n.kind === 'conversation_turn' || n.kind === 'insight')) ? `<button class="abtn" style="border-color:var(--idea);color:var(--idea)" title="promote to the Ideas shelf — writes a NEW idea node linked back to this (this one is untouched)" onclick="promoteNode(event,'${jesc(n.id)}','idea')">${icon('idea','💡','s16')} → idea</button><button class="abtn" style="border-color:var(--moss);color:var(--moss)" title="promote to the Desk — writes a NEW open item linked back to this (this one is untouched)" onclick="promoteNode(event,'${jesc(n.id)}','open_item')">${icon('todo','✅','s16')} → open item</button>` : ''}
      <button class="abtn" title="hide from your views — stays searchable & the AI still sees it" onclick="setAside(event,'${jesc(n.id)}','archive')">${icon('archive','🗄','s16')} archive</button>
      <button class="abtn" title="hide until later (7 days), then it comes back" onclick="setAside(event,'${jesc(n.id)}','snooze')">${icon('snooze','💤','s16')} snooze</button>
      <button class="abtn" title="pin — keep it front-and-center (and surface it to the AI more)" onclick="act(event,'${jesc(n.id)}','promote','pinned — surfaced more')">${icon('pin','📌','s16')} pin</button>
    </div>
    </div>
  </div>`;
}

function _sameStart(a, b) {   // true if a & b share a long prefix — one is a truncation of the other (render-once)
  a = (a || '').trim(); b = (b || '').trim();
  var m = Math.min(a.length, b.length, 80);
  return m >= 20 && a.slice(0, m) === b.slice(0, m);
}
function toggleCard(e, el) {
  if (e.target.tagName === 'BUTTON') return;
  el.classList.toggle('expanded');
}

function mediaThumb(n) {
  const m = (n.tags || []).find(t => typeof t === 'string' && t.startsWith('media:'));
  if (!m) return '';
  return `<img src="/media/${esc(m.slice(6))}" loading="lazy"
    style="max-width:100%;max-height:220px;border-radius:8px;margin-top:8px;display:block">`;
}

async function act(e, id, action, msg) {
  e.stopPropagation();
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
  toast(msg);
  if (action === 'void') {
    const el = document.querySelector(`.card[data-id="${id}"]`);
    if (el) { el.style.opacity = .3; el.style.pointerEvents = 'none'; }
  }
}

// Set aside = hide it from your views (NOT delete). The node stays active in the
// vault — searchable, and the AI still sees it (a human mark is a trusted-but-
// fallible signal). One click, undoable; restore lives in the Archive drawer.
async function setAside(e, id, kind) {
  e.stopPropagation();
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/${kind}`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}',
  });
  const el = document.querySelector(`.card[data-id="${id}"]`);
  if (el) { el.style.opacity = .3; el.style.pointerEvents = 'none'; }
  const label = kind === 'archive' ? 'archived' : 'snoozed 7 days';
  toastUndo(`${label} — still active for the AI`, async () => {
    await fetch(`/api/garden/node/${encodeURIComponent(id)}/un${kind}`, { method: 'POST' });
    if (el) { el.style.opacity = ''; el.style.pointerEvents = ''; }
    toast('restored');
  });
}

// A toast with an Undo affordance — stays ~6s so a misclick is recoverable
// without a confirm popup (clean one-click + undo, per the set-aside model).
function toastUndo(msg, undoFn) {
  const t = $('toast');
  t.innerHTML = esc(msg) +
    ' · <a href="#" id="toast-undo" style="color:var(--idea);font-weight:600">undo</a>';
  t.classList.add('show');
  let used = false;
  const close = () => { t.classList.remove('show'); t.textContent = ''; };
  const timer = setTimeout(() => { if (!used) close(); }, 6000);
  const u = document.getElementById('toast-undo');
  if (u) u.onclick = async (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    used = true; clearTimeout(timer); close(); await undoFn();
  };
}

let pendingPhoto = null;  // {b64, name}

function photoChosen() {
  const f = $('capture-photo').files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    pendingPhoto = { b64: rd.result.split(',')[1], name: f.name };
    $('photo-btn').innerHTML = icon('photo', '📷', 's18') + ' ✓';
    toast('photo attached — add a note and Plant');
  };
  rd.readAsDataURL(f);
}

// Date chip: append a due:YYYY-MM-DD token to the capture text. The server-side
// capture pipeline already parses due: out of the text (garden_capture regex) —
// so this stays compatible, no new field. Replaces any existing due: token so a
// re-pick doesn't stack two dates. To-do is the natural kind for a dated item.
function dueChosen() {
  const dv = $('capture-date').value;   // 'YYYY-MM-DD' or ''
  if (!dv) return;
  const box = $('capture-text');
  let t = (box.value || '').replace(/\s*\bdue:\d{4}-\d{2}-\d{2}/g, '').trim();
  box.value = (t ? t + ' ' : '') + 'due:' + dv;
  const k = $('capture-kind'); if (k && k.value === 'insight') kindPick('open_item', true);
  box.focus();
  toast('📅 due ' + dv + ' — press Plant to save');
}

async function capture() {
  const text = $('capture-text').value.trim();
  if (!text && !pendingPhoto) return;
  const kind = $('capture-kind').value;
  const body = { text, kind };
  if (pendingPhoto) {
    body.image_b64 = pendingPhoto.b64;
    body.image_name = pendingPhoto.name;
  }
  let r;
  try {
    r = await fetch('/api/garden/capture', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    }).then(r => r.json());
  } catch (e) {
    toast('could not plant — network error; your text is untouched');
    return;
  }
  if (!r || r.error || !r.id) {
    // NEVER clear the box on failure — the user's words survive the error.
    toast('could not plant: ' + esc((r && r.error) || 'server error') + ' — your text is kept');
    return;
  }
  $('capture-text').value = '';
  pendingPhoto = null;
  $('photo-btn').innerHTML = icon('photo', '📷', 's18');
  $('capture-photo').value = '';
  const _cd = $('capture-date'); if (_cd) _cd.value = '';
  toast(`planted ${kind} [${r.id}]${r.inbox ? ' → inbox' : ''}`);
  if (view === 'today') show('today'); else if (view === 'desk') show('desk');
}

// ── Hub / Book / Index — the human face ──────────────────────────────────────
// `sub` is TRUSTED HTML (may carry a static icon() span prefix). Every caller
// escapes its own dynamic bits (esc(n.due) etc.) or passes a static/integer
// value — nothing user-authored reaches `sub` raw. `gist` is always escaped.
function sentenceCard(gist, ts, sub, onclick) {
  const t = ts ? `<span class="reg-ts">${when(ts)}</span>` : '';
  const s = sub ? `<div class="reg-status"><span class="reg-dot"></span><span class="reg-sub">${sub}</span></div>` : '';
  return `<div class="reg-card"${onclick ? ` onclick="${onclick}"` : ''}>` +
    `<div class="reg-gutter"><span class="reg-glyph">▲</span></div>` +
    `<div class="reg-main"><div class="reg-body"><div class="reg-title">${esc(gist || '')}</div>${s}</div>${t}</div>` +
    `</div>`;
}

// The field-manual legend plate — a static reference of the icon vocabulary,
// shown on the Index tab. Icons sit BESIDE their mono-caps labels (design rule).
// Entirely static markup (no dynamic bits) — safe to inline.
function legendPlate() {
  const row = (name, lbl, def) =>
    `<div class="key-row">${icon(name, '', 's18')}<span class="key-lbl">${lbl}</span><span class="key-def">${def}</span></div>`;
  return `
  <div class="legend-plate">
    <div class="key-h">Kinds</div>
    <div class="lp-grid">
      ${row('idea','Idea','a spark kept for later')}
      ${row('todo','To-do','an open item to do')}
      ${row('question','Question','a thread left open')}
      ${row('note','Note','an insight or observation')}
      ${row('warning','Warning','something to watch')}
      ${row('done','Done','resolved — kept as record')}
      ${row('photo','Photo','a captured image')}
      ${row('book','Book','the table of contents')}
    </div>
    <div class="key-h">Maturity</div>
    <div class="lp-grid">
      ${row('seedling','Seedling','young — under 3 days')}
      ${row('budding','Budding','taking root — 3 to 30 days')}
      ${row('evergreen','Evergreen','stable — 30+ days / procedure')}
    </div>
    <div class="key-h">Status &amp; shelves</div>
    <div class="lp-grid">
      ${row('due','Due','dated — on the desk')}
      ${row('overdue','Overdue','past its date')}
      ${row('inbox','Inbox','captured on the go')}
      ${row('fresh','Fresh','planted in last 14 days')}
      ${row('bank','Bank','the older reference shelf')}
      ${row('parked','Parked','deliberately shelved')}
      ${row('ripe','Ripe','researched &amp; revisited')}
      ${row('findings','Findings','research chained on')}
      ${row('fog','Fading','surfaced, never used')}
    </div>
    <div class="key-h">Actions</div>
    <div class="key-actions">
      <span class="key-act">${icon('done','','s16')}done / still-true</span>
      <span class="key-act">${icon('archive','','s16')}archive</span>
      <span class="key-act">${icon('snooze','','s16')}snooze</span>
      <span class="key-act">${icon('pin','','s16')}pin</span>
      <span class="key-act">${icon('research','','s16')}research</span>
      <span class="key-act">${icon('revisit','','s16')}revisit</span>
      <span class="key-act">${icon('restore','','s16')}restore</span>
      <span class="key-act">${icon('flag','','s16')}flag</span>
      <span class="key-act">${icon('dismiss','','s16')}dismiss</span>
    </div>
    <div class="key-h">Freshness</div>
    <div class="lp-grid">
      <div class="key-row"><span class="fresh-glyph">·</span><span class="key-lbl">Live</span><span class="key-def">Hub, Today, Desk, Projects — read the vault as-is</span></div>
      <div class="key-row"><span class="fresh-glyph">○</span><span class="key-lbl">Sleep-refreshed</span><span class="key-def">Search, Spark, Wander, Topics — refreshed by the nightly sleep</span></div>
    </div>
  </div>`;
}

async function renderHub_() {
  $('main').innerHTML = '<div class="empty">gathering your garden…</div>';
  const d = await fetch('/api/garden/hub').then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">hub unavailable: ${esc(d.error)}</div>`; return; }
  // P2 absorption: the Hub no longer duplicates Desk. "Needs attention"
  // (open items + review banner + fading) and "Open things" (ideas + upcoming)
  // are gone — the router cards + Desk's own sections carry them now. The Hub
  // keeps: router, a slim DUE strip, since-last-visit / just-captured, project
  // activity. No separate /review fetch — the count rides in the Desk card.
  const due   = d.due || {};
  const fresh = d.just_captured || [];
  const slv   = d.since_last_visit || {};
  // show the high-signal DELTA since your last visit; fall back to the latest
  // captures on a first visit / when nothing's new — never both (avoid noise).
  // `count` = meaning-kind captures (notes); `turns` = a SEPARATE capped count
  // of conversation_turns, so a talk-heavy day no longer reads "0 new" just
  // because nothing crossed into a meaning kind. Both are integers from JSON —
  // safe to interpolate directly; strings still go through esc()/jesc().
  const slvTurns = slv.turns || 0;
  const hasDelta = (slv.count || slvTurns);
  const recTitle = hasDelta ? 'Since your last visit' : 'Just captured';
  // the REGISTER: hide the machine's own work-notes unless the toggle is on
  const recAll = slv.count ? (slv.items || []) : fresh;
  const recItems = showMachine ? recAll : recAll.filter(n => !n.process);
  const recHidden = recAll.length - recItems.length;
  // "N new · M turns" — either half shown only when non-zero; nothing when both are 0.
  const recSub   = hasDelta
    ? [ slv.count ? `${slv.count} new` : '',
        slvTurns  ? `${slvTurns}${slvTurns >= 99 ? '+' : ''} ${slvTurns === 1 ? 'turn' : 'turns'}` : '' ]
        .filter(Boolean).join(' · ')
    : '';

  // Slim DUE strip — dated items only, a glance not a list. Overdue (red) and
  // due-today lead; a couple of upcoming trail. The full open-item / fading /
  // review triage lives on the Desk now (one click via its router card).
  const dueRows = [];
  (due.overdue || []).forEach(n => dueRows.push(sentenceCard(
    n.gist, null, `${icon('overdue','⚠','s16')} overdue — was due ${esc(n.due)}`, `openNode(event,'${jesc(n.id)}')`)));
  (due.today || []).forEach(n => dueRows.push(sentenceCard(
    n.gist, null, `${icon('due','📅','s16')} due today`, `openNode(event,'${jesc(n.id)}')`)));
  (due.upcoming || []).slice(0, 3).forEach(n => dueRows.push(sentenceCard(
    n.gist, null, `due ${esc(n.due)}`, `openNode(event,'${jesc(n.id)}')`)));

  // De-UUID'd project activity: this week's active-node counts by PROJECT
  // family (declared+aliases first, then surviving emerging families), never
  // session UUIDs. "Name — N nodes", each a link to the project view.
  const pa = d.project_activity || { families: [], untagged: 0 };
  // Hub shows a PREVIEW, not the field: top 3 by activity, one line for the
  // rest. The full list (and the emerging pile) lives in Projects where it
  // belongs (owner ruling 562e4e710584 item 6 — "really messy on this home screen").
  const fams = pa.families || [];
  // "notes", not "nodes": this strip counts TAGGED meaning-notes only.
  // Conversation turns / untagged captures aren't project-attributed until the
  // affinity work lands — they're the "untagged" line below, not missing data.
  const famLines = fams.slice(0, 3).map(f =>
    `<div class="hub-strip-line" onclick="renderProject('${jesc(f.tag)}')" style="cursor:pointer">` +
    `${esc(f.name)} — ${f.nodes} ${f.nodes === 1 ? 'note' : 'notes'}` +
    `${f.approx > f.nodes ? ` · ~${f.approx} nodes with conversation, ${f.sessions} ${f.sessions === 1 ? 'session' : 'sessions'}` : ''} this week` +
    `${f.declared ? '' : ' <span class="hub-sub">emerging</span>'}</div>`);
  const restN = Math.max(0, fams.length - 3);
  if (restN || pa.untagged) famLines.push(
    `<div class="hub-strip-line" onclick="show('projects')" style="cursor:pointer;color:var(--moss)">` +
    `${restN ? `and ${restN} more project${restN === 1 ? '' : 's'}` : ''}` +
    `${restN && pa.untagged ? ' · ' : ''}${pa.untagged ? `${pa.untagged} untagged captures (mostly conversation)` : ''} →</div>`);

  // ○ Topics — the second stack (P3.5 both-stacked, reversible at G2): what
  // the threads keep circling, freshest first. The register holds here too —
  // machine-heavy clusters hide behind the same toggle instead of leading.
  const topAll = d.topics || [];
  const tops = showMachine ? topAll : topAll.filter(t => !t.process);
  const topHidden = topAll.length - tops.length;
  const topicsHTML = tops.map(t =>
    `<span class="tag-pill" onclick="renderTopic('${jesc(t.cid)}')">` +
    `${esc(t.label)}<span class="n">${t.count}</span></span>`).join('');

  // Router cards — four homes with live badges, promoted from the demoted
  // views (Desk / Today / Projects / Library). Links reuse existing endpoints.
  const rt = d.router || { desk: 0, today_meaning: 0, today_turns: 0, movers: [], flagged: 0, tend: 0 };
  const routerCard = (name, badge, sub, onclick) =>
    `<div class="router-card" onclick="${onclick}">` +
    `<div class="rc-top"><span class="rc-name">${name}</span>` +
    `${badge !== '' ? `<span class="rc-badge">${badge}</span>` : ''}</div>` +
    `<div class="rc-sub">${sub}</div></div>`;
  const moversTxt = (rt.movers && rt.movers.length)
    ? rt.movers.map(m => esc(m)).join(', ') : 'no movers yet';
  const todaySub = `${rt.today_meaning} new · ${rt.today_turns}${rt.today_turns >= 99 ? '+' : ''} ${rt.today_turns === 1 ? 'turn' : 'turns'}`;
  const routerHTML =
    routerCard(`${icon('todo','✅','s16')} Desk`, rt.desk || '', 'still open · tend · watch', "show('desk')") +
    routerCard('Today', '', todaySub, "show('today')") +
    routerCard('Projects', '', moversTxt, "show('projects')") +
    routerCard(`${icon('book','📖','s16')} Library`, '', 'Book + Index', "show('book')");

  $('main').innerHTML = `
    <div class="hub-wrap">
      <span id="hero-stone" class="hero-stone hero-stone-corner" title="hide welcome" onclick="setHero(!heroOpen)"></span>
      <div id="hub-hero" class="hub-hero">
        <div class="hub-hello" onclick="startHelloEdit(this)">${esc(d.greeting || DEFAULT_HELLO)}</div>
      </div>

      <div class="router-row">${routerHTML}</div>

      ${dueRows.length ? `<div class="section-h">${icon('due','📅','s16')} Due</div>
      <div class="hub-grid">${dueRows.join('')}</div>` : ''}

      <div class="section-h">${recTitle}${recSub ? ` <span class="hub-sub">${recSub}</span>` : ''}
        <a style="cursor:pointer;color:var(--moss);font-size:12px;margin-left:8px" onclick="show('fresh')">○ live tail →</a></div>
      ${recItems.length ? `<div class="hub-grid">${recItems.map(n => sentenceCard(
          n.gist, n.ts, n.speaker === 'user' ? 'you' : esc((n.kind || 'note').replace('_',' ')),
          `openNode(event,'${jesc(n.id)}')`)).join('')}</div>`
        : '<div class="empty">Nothing new — the garden is calm.</div>'}
      ${recHidden > 0 ? `<div class="hub-sub" style="margin:4px 0 0;cursor:pointer" onclick="toggleMachine()">⚙ ${recHidden} build ${recHidden === 1 ? 'note' : 'notes'} from the machine — show</div>` : ''}

      <div class="section-h">Across your projects</div>
      <div class="hub-strip">
        ${famLines.length ? famLines.join('') : '<div class="hub-strip-line">No project activity yet this week.</div>'}
      </div>

      ${topAll.length ? `<div class="section-h">Topics <span class="fresh-glyph" title="refreshed by the nightly sleep">○</span>
        <a style="cursor:pointer;color:var(--moss);font-size:12px;margin-left:8px" onclick="showIndexTopics()">all ${d.topics_total || ''} topics →</a></div>
      <div class="hint">Themes the graph found on its own — ${tops.length} of ${d.topics_total || topAll.length}, most recently touched as of the last sleep${d.topics_asof ? ` (${when(d.topics_asof)})` : ''}; today's notes join tonight. The Library's Knowledge shelf holds them all.</div>
      ${tops.length ? `<div class="tag-cloud">${topicsHTML}</div>` : ''}
      ${topHidden > 0 ? `<div class="hub-sub" style="margin:4px 0 0;cursor:pointer" onclick="toggleMachine()">⚙ ${topHidden} machine ${topHidden === 1 ? 'cluster' : 'clusters'} — show</div>` : ''}` : ''}

      <div class="hub-sub" style="margin-top:14px;cursor:pointer" onclick="toggleMachine()">
        ⚙ the machine's work-notes are ${showMachine ? 'SHOWN on your surfaces — click to hide them' : 'hidden from your surfaces — click to show them'}</div>
    </div>`;
  // collapsible welcome — default-open on first visit ever, then persisted however left
  try { const _ho = localStorage.getItem('cairn-heroOpen'); setHero(_ho === null ? true : _ho === '1'); } catch(e) {}
  // first visit ever → the navigation walkthrough, once (the ? replays it)
  try { if (!localStorage.getItem('cairn-toured') && tourAt < 0) tourStart(0); } catch(e) {}
}

// hide/show the welcome hero; collapsed -> the stone medallion sits at the right of
// the action row. Persisted in localStorage (default-open only on the first visit).
let heroOpen = true;
function setHero(open) {
  heroOpen = open;
  const h = document.getElementById('hub-hero');
  const s = document.getElementById('hero-stone');
  if (h) h.style.display = open ? 'flex' : 'none';
  if (s) s.title = open ? 'hide welcome' : 'show welcome';
  try { localStorage.setItem('cairn-heroOpen', open ? '1' : '0'); } catch(e) {}
}

// ── click-to-edit greeting. The headline IS the control — no buttons. Click it
// to edit in place; Enter or click-away saves, Esc cancels, clearing it resets
// to default. Persists to ~/.cairn/settings.json via POST /api/garden/greeting
// (also settable from the terminal with `cairn hello "…"`).
const DEFAULT_HELLO = "Welcome back. Here's what your second brain is holding.";
function startHelloEdit(el) {
  if (el.getAttribute('contenteditable') === 'true') return;
  el.dataset.orig = el.textContent;
  el.setAttribute('contenteditable', 'true');
  el.classList.add('editing');
  el.focus();
  const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
  el.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    else if (e.key === 'Escape') { e.preventDefault(); el.textContent = el.dataset.orig || DEFAULT_HELLO; el.blur(); }
  };
  el.onblur = () => saveHello(el);
}
async function saveHello(el) {
  if (el.getAttribute('contenteditable') !== 'true') return;
  el.setAttribute('contenteditable', 'false');
  el.classList.remove('editing');
  const txt = (el.textContent || '').trim();
  const orig = (el.dataset.orig || '').trim();
  if (txt === orig) { el.textContent = orig || DEFAULT_HELLO; return; }
  try {
    const res = await fetch('/api/garden/greeting', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: txt }) });
    const j = await res.json();
    if (!res.ok || (j && j.error)) throw new Error((j && j.error) || 'save failed');
    el.textContent = (j && j.greeting) ? j.greeting : DEFAULT_HELLO;
    toast(txt ? 'greeting saved' : 'greeting reset to default');
  } catch (e) { el.textContent = orig || DEFAULT_HELLO; toast('could not save greeting — text kept'); }
}

// ── THE TOUR — first-run navigation walkthrough (owner spec: teach moving
// around as effectively as possible; REAL empty UI, zero fake data, zero
// demo content; a small ? in the header replays it anytime). v1, honest
// mini-jank: scrim + card + a glow on the real element it's talking about.
const TOUR = [
  { view: 'hub', q: null, title: 'Welcome to your Garden',
    text: 'It starts empty on purpose. Everything you and your AI actually work through will grow here as real memory. This walk just shows you the paths.' },
  { view: 'hub', q: null, title: 'It remembers on its own',
    text: 'Once Cairn is connected to your AI — any model, Claude, GPT, whatever you use — it quietly captures the decisions, reasons, and dead ends as you work. You don’t save anything. It just remembers.' },
  { view: 'hub', q: '#capture-bar', title: 'The Plant bar',
    text: 'Want to drop something in by hand — an idea, a to-do, a question? Type it and Plant. Nothing you plant is ever deleted; the vault only grows.' },
  { view: 'hub', q: '#wayline', title: 'Getting around',
    text: 'Hub is home. Search digs the whole vault by meaning — ask it anything. And Back always retraces your exact steps, wherever you wander.' },
  { view: 'hub', q: '.router-row', title: 'The four rooms',
    text: 'Desk — anything that needs your hands. Today — your day as a story. Projects — your work, grouped the way you think. Library — the Book, the Index, and distilled Knowledge.' },
  { view: 'desk', q: null, title: 'The Desk',
    text: 'Where anything needing you lands: open items, things to tend, warnings to heed, the Log. Empty now — it fills itself as you work.' },
  { view: 'book', q: null, title: 'The Library',
    text: 'The Book reads your history back like a story. The Index is your A–Z — every topic, alphabetical. And Knowledge is the distilled version: each night Cairn turns the day’s raw chat into sharp, connected notes, so tomorrow you — or any model — start from what mattered.' },
  { view: 'hub', q: '#tour-brain-link', title: 'The brain',
    text: 'Flip to the other side: every memory as a star, your whole vault as a galaxy, fresh work glowing at the edges. The Garden reads it; the brain shows it mapped.' },
  { view: 'hub', q: null, title: 'That’s the walk',
    text: 'The ? up top replays this anytime. Now go do real work with your AI — the garden grows from there.' },
];
// ── the TYPE dropdown (owner's sheet #3/#4): hand-built so his glyphs render.
// #capture-kind stays a hidden input so every existing reader still works.
const KIND_META = { idea: ['idea', 'pb-idea'], open_item: ['to-do', 'pb-todo'],
                    question: ['question', 'pb-question'], insight: ['note', 'pb-note'] };
function kindToggle(e) {
  if (e) e.stopPropagation();
  const dd = document.getElementById('kind-dd'), m = document.getElementById('kind-menu');
  const open = m.style.display !== 'none';
  m.style.display = open ? 'none' : 'block';
  dd.classList.toggle('open', !open);
}
function kindPick(k, quiet) {
  const meta = KIND_META[k] || [k, 'pb-note'];
  document.getElementById('capture-kind').value = k;
  document.getElementById('kind-label').textContent = meta[0];
  document.getElementById('kind-glyph').src = '/assets/icons/' + meta[1] + '.png';
  document.getElementById('kind-glyph-d').src = '/assets/icons/' + meta[1] + '-dark.png';
  document.querySelectorAll('.kind-item').forEach(el =>
    el.classList.toggle('sel', el.dataset.k === k));
  if (!quiet) kindToggle();
}
document.addEventListener('click', ev => {
  const m = document.getElementById('kind-menu');
  if (m && m.style.display !== 'none' && !ev.target.closest('#kind-dd')) {
    m.style.display = 'none';
    document.getElementById('kind-dd').classList.remove('open');
  }
});

let tourAt = -1;
function tourGlowOff() {
  document.querySelectorAll('.tour-glow').forEach(el => el.classList.remove('tour-glow'));
}
function tourEnd() {
  tourAt = -1; tourGlowOff();
  document.body.classList.remove('touring');
  const s = document.getElementById('tour-scrim'); if (s) s.remove();
  try { localStorage.setItem('cairn-toured', '1'); } catch (e) {}
}
// place the card NEXT TO what it describes (owner: 'put the pop up next to
// the window') — below the glowed element when there's room, above when
// not, centered-low when the step has no target.
function tourPlace(card, el) {
  // ALWAYS pin exactly one vertical anchor and release the other with
  // 'auto' — clearing to '' let the stylesheet's default bottom re-apply,
  // stretching the card between both anchors (the owner's giant green box
  // on steps 2/3/7).
  card.style.left = ''; card.style.right = ''; card.style.transform = '';
  if (!el) { card.style.top = 'auto'; card.style.bottom = '9vh';
             card.style.left = '50%'; card.style.transform = 'translateX(-50%)'; return; }
  const r = el.getBoundingClientRect(), cw = Math.min(420, window.innerWidth - 40);
  const left = Math.max(20, Math.min(r.left, window.innerWidth - cw - 20));
  card.style.left = left + 'px';
  if (r.bottom + 300 < window.innerHeight) {
    card.style.bottom = 'auto'; card.style.top = (r.bottom + 16) + 'px';
  } else if (r.top > 320) {
    card.style.top = 'auto'; card.style.bottom = (window.innerHeight - r.top + 16) + 'px';
  } else {
    card.style.top = 'auto'; card.style.bottom = '9vh';
    card.style.left = '50%'; card.style.transform = 'translateX(-50%)';
  }
}
function tourStart(i) {
  tourAt = i; tourGlowOff();
  document.body.classList.add('touring');
  const step = TOUR[i];
  // the walk changes tabs (owner ask) — each step declares its room
  let moved = false;
  if (step.view && view !== step.view) { try { show(step.view); moved = true; } catch (e) {} }
  let s = document.getElementById('tour-scrim');
  if (!s) {
    s = document.createElement('div'); s.id = 'tour-scrim';
    s.innerHTML = '<div id="tour-card"><div id="tour-inner"></div></div>';
    document.body.appendChild(s);
  }
  setTimeout(() => {
    const el = step.q ? document.querySelector(step.q) : null;
    if (el) { el.classList.add('tour-glow'); el.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    const card = document.getElementById('tour-card');
    document.getElementById('tour-inner').innerHTML =
      `<div class="tour-step">${i + 1} / ${TOUR.length}</div>` +
      `<div class="tour-title">${step.title}</div>` +
      `<div class="tour-text">${step.text}</div>` +
      `<div class="tour-btns">` +
      (i > 0 ? `<button class="abtn" onclick="tourStart(${i - 1})">&lt;- back</button>` : '') +
      `<button class="abtn" onclick="tourEnd()">skip</button>` +
      (i + 1 < TOUR.length
        ? `<button class="abtn tour-next" onclick="tourStart(${i + 1})">next -&gt;</button>`
        : `<button class="abtn tour-next" onclick="tourEnd()">start planting</button>`) +
      `</div>`;
    tourPlace(card, el);
  }, moved || i === 0 ? 380 : 80);
}

// The Library is one room with two shelves — the Book (narrative) and the
// Index (A–Z). This bar sits atop both so the vocabulary can't fork again.
function libraryShelfBar(active) {
  const shelf = (v, label) =>
    `<button class="abtn" style="${active === v ? 'border-color:var(--moss);color:var(--moss);font-weight:700' : ''}" onclick="show('${v}')">${label}</button>`;
  return `<div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
    <button class="backbtn" onclick="goBack()">← Back</button>
    <span>${icon('book','📖','s16')} Library</span>
    ${shelf('index', 'the Index')} ${shelf('book', 'the Book')} ${shelf('knowledge', 'Knowledge')}
  </div>`;
}

async function renderBook() {
  $('main').innerHTML = '<div class="empty">opening the book…</div>';
  const d = await fetch('/api/garden/book').then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">book unavailable: ${esc(d.error)}</div>`; return; }
  const week = d.this_week || [];
  const projs = d.projects || [];
  const older = d.older_projects || [];
  const vols = d.volumes || [];

  // the REGISTER: this-week reads as YOUR week by default; the machine's
  // build notes collapse to one line with a toggle.
  const weekLife = showMachine ? week : week.filter(n => !n.process);
  const weekHidden = week.length - weekLife.length;
  const weekHTML = weekLife.map(n =>
    `<div class="book-line" onclick="openNode(event,'${jesc(n.id)}')">` +
    `<span class="kind-chip">${esc((n.kind||'').replace('_',' '))}</span> ${esc(n.gist || '')}</div>`).join('') +
    (weekHidden > 0 ? `<div class="hint" style="cursor:pointer" onclick="toggleMachine()">⚙ ${weekHidden} build ${weekHidden === 1 ? 'note' : 'notes'} from the machine — show</div>` : '');

  // project chapters arrive LAST-TOUCH DESC from the server (plan C8) — the
  // most recently active project sits at top; each header shows when.
  const projHTML = projs.map(p => {
    const chapters = (p.chapters || []).map(c => {
      const ex = (c.exemplars || []).map(e =>
        `<div class="book-ex" onclick="openNode(event,'${jesc(e.id)}')">` +
        `<span class="kind-chip">${esc((e.kind||'').replace('_',' '))}</span> ${esc(e.gist || '')}</div>`).join('');
      return `<div class="book-chapter"><div class="book-chap-h">${esc(c.label)} <span class="book-count">(${c.count})</span></div>${ex}</div>`;
    }).join('');
    return `<div class="book-project">
      <div class="book-proj-h">${esc(p.name || p.tag)} <span class="book-count">(${p.total})</span>
        ${p.last_ts ? `<span class="hub-sub" title="last activity across this project's family">· ${when(p.last_ts)}</span>` : ''}</div>
      ${p.desc ? `<div class="book-desc">${esc(p.desc)}</div>` : ''}
      ${chapters}
    </div>`;
  }).join('');

  // Older / other projects (plan C8): real-but-undeclared tag families, cleaned
  // + last-activity ordered, collapsed by default. Every real project is
  // reachable from the Book — promoted or not.
  const olderHTML = older.length ? `
    <div class="book-section-h">Older projects</div>
    <details class="book-older">
      <summary>${older.length} undeclared ${older.length === 1 ? 'project' : 'projects'} with real mass — open the drawer</summary>
      ${older.map(p => `
        <div class="book-older-row" onclick="renderProject('${jesc(p.tag)}')">
          <span>${esc(p.name)}</span>
          <span class="book-count">(${p.total})</span>
          ${p.last_ts ? `<span class="hub-sub" style="margin-left:auto">${when(p.last_ts)}</span>` : ''}
        </div>`).join('')}
    </details>` : '';

  // Archive Volumes with drill-in (plan C4): a volume opens its session list;
  // a session row opens the P2 conversation reader (session/{id}/turns).
  const volHTML = vols.map(v => `
    <div class="book-vol">
      <div class="book-vol-head" onclick="toggleVolume(event, this, '${jesc(v.account)}')">
        <span class="vol-caret">▸</span>
        <span>${esc(v.account)} — ${v.sessions} sessions, ${v.nodes} nodes</span>
        <span class="hub-sub">${esc((v.first||'').slice(0,10))} → ${esc((v.last||'').slice(0,10))}</span>
      </div>
      <div class="vol-sessions" data-loaded="0" style="display:none"></div>
    </div>`).join('');

  $('main').innerHTML = `
    <div class="book-wrap">
      ${libraryShelfBar('book')}
      <div class="book-title">${icon('book','📖','s20')} The Book</div>
      <div class="hint">A table of contents for your memory — generated from the vault.</div>

      <div class="book-section-h">This Week</div>
      ${week.length ? weekHTML : '<div class="empty">Nothing recorded this week yet.</div>'}

      ${projs.length ? '<div class="book-section-h">Projects</div><div class="hint">most recently touched first</div>' + projHTML : ''}

      ${olderHTML}

      ${vols.length ? '<div class="book-section-h">Archive Volumes</div><div class="hint">imported conversations — open a volume, then read any session</div>' + volHTML : ''}
    </div>`;
}

// Expand/collapse an archive volume's session list; lazy-load once. Each
// session row then expands into the conversation reader (renderTurns — the
// same component Today uses; plan C4 "Archive drill-in reuses C3's reader").
async function toggleVolume(e, head, account) {
  e.stopPropagation();
  const zone = head.nextElementSibling;
  const caret = head.querySelector('.vol-caret');
  if (!zone) return;
  const open = zone.style.display === 'none';
  zone.style.display = open ? 'block' : 'none';
  if (caret) caret.textContent = open ? '▾' : '▸';
  if (open && zone.getAttribute('data-loaded') === '0') {
    zone.setAttribute('data-loaded', '1');
    zone.innerHTML = '<div class="empty" style="padding:6px">opening the volume…</div>';
    try {
      const d = await fetch('/api/garden/volume/' + encodeURIComponent(account) + '/sessions').then(r => r.json());
      const sess = d.sessions || [];
      zone.innerHTML = sess.length ? sess.map(s => `
        <div class="vol-sess-row" onclick="toggleVolSession(event, this, '${jesc(s.id)}')">
          <span class="vol-caret">▸</span>
          <span>${esc(s.first || '')}${s.last && s.last !== s.first ? ' → ' + esc(s.last) : ''}</span>
          <span class="hub-sub">${s.nodes} ${s.nodes === 1 ? 'turn' : 'turns'}</span>
        </div>
        <div class="vol-sess-turns" data-loaded="0" style="display:none"></div>`).join('')
        : '<div class="empty" style="padding:6px">no sessions in this volume</div>';
    } catch(err) {
      zone.innerHTML = '<div class="empty" style="padding:6px">could not open this volume</div>';
      zone.setAttribute('data-loaded', '0');
    }
  }
}

// A session row inside a volume → the conversation reader, lazy-loaded once.
async function toggleVolSession(e, row, sessionId) {
  e.stopPropagation();
  const zone = row.nextElementSibling;
  const caret = row.querySelector('.vol-caret');
  if (!zone) return;
  const open = zone.style.display === 'none';
  zone.style.display = open ? 'block' : 'none';
  if (caret) caret.textContent = open ? '▾' : '▸';
  if (open && zone.getAttribute('data-loaded') === '0') {
    zone.setAttribute('data-loaded', '1');
    zone.innerHTML = '<div class="empty" style="padding:6px">reading…</div>';
    try {
      const d = await fetch('/api/garden/session/' + encodeURIComponent(sessionId) + '/turns').then(r => r.json());
      zone.innerHTML = renderTurns(d.turns || [], sessionId, d.offset || 0, d.total || 0);
    } catch(err) {
      zone.innerHTML = '<div class="empty" style="padding:6px">could not read this session</div>';
      zone.setAttribute('data-loaded', '0');
    }
  }
}

async function renderIndexPage() {
  $('main').innerHTML = '<div class="empty">turning to the index…</div>';
  const d = await fetch('/api/garden/bookindex').then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">index unavailable: ${esc(d.error)}</div>`; return; }

  // Real A-Z index: sort + group by the DISPLAYED name so entries land under
  // their true letter. A `proj:<name>` tag is a declared project — bucket it by
  // <name> (owner: "demoapp belongs in D, not P") and move the 'proj' marker to
  // the END so it's still flagged as a project but findable in A-Z. entity: is
  // stripped the same way (legacy). Click → EXACT tag membership (renderHub uses
  // the full, unmodified tag), not a fuzzy word search.
  const labeled = (d.tags || []).map(t => {
    const raw = t.tag || '';
    const isProj = raw.startsWith('proj:');
    const name = isProj ? raw.slice(5) : raw.replace(/^entity:/, '');
    return { tag: raw, count: t.count, name, isProj };
  });
  labeled.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  const groups = {};
  labeled.forEach(t => {
    const c0 = (t.name[0] || '#').toUpperCase();
    const key = (c0 >= 'A' && c0 <= 'Z') ? c0 : '#';
    (groups[key] = groups[key] || []).push(t);
  });
  const letters = Object.keys(groups).sort();
  const jumpHTML = letters.map(L =>
    `<span class="az-jump" onclick="document.getElementById('az-${L}').scrollIntoView({behavior:'smooth',block:'start'})">${L}</span>`).join('');
  const tagsHTML = letters.map(L =>
    `<div class="az-h" id="az-${L}">${L}</div><div class="tag-cloud">` +
    groups[L].map(t =>
      `<span class="tag-pill" onclick="renderHub('${jesc(t.tag)}')">` +
      `${esc(t.name)}${t.isProj ? '<span class="pj">proj</span>' : ''}` +
      `<span class="n">${t.count}</span></span>`).join('') +
    `</div>`).join('');

  // The Index is PURE lookup (owner: "the fastest large knowledge tags
  // navigation" — and it got junked when browsing content moved in). A–Z only;
  // Topics/Consolidated/Documents/Terms/Legend live on the Knowledge shelf.
  $('main').innerHTML = `
    <div class="book-wrap">
      ${libraryShelfBar('index')}
      <div class="book-title">Index</div>
      <input class="promote-in" placeholder="narrow the index… type to filter, stay right here"
        oninput="libFilter(this.value)" style="width:100%;margin:4px 0 10px">
      ${labeled.length ? `<div class="az-bar">${jumpHTML}</div>${tagsHTML}` : '<div class="empty">No tags yet.</div>'}
    </div>`;
}

// In-place Library filter (owner: "search just that… stay on the view").
// Pure show/hide — no navigation, no server call; clearing restores everything.
function libFilter(q) {
  q = (q || '').trim().toLowerCase();
  document.querySelectorAll('main .tag-pill, main .book-line').forEach(el => {
    el.style.display = !q || el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// The Library's third shelf — KNOWLEDGE: what the vault has distilled and
// collected. Topics (graph-named clusters), consolidated insights (episodic →
// semantic), documents, terms, and the icon Legend as the back-page key.
// Browsing, not lookup — the future librarian pages live here too.
async function renderKnowledge() {
  $('main').innerHTML = '<div class="empty">opening the knowledge shelf…</div>';
  const d = await fetch('/api/garden/bookindex').then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">knowledge unavailable: ${esc(d.error)}</div>`; return; }
  const docs = d.docs || [], terms = d.terms || [];
  // Library Topics ordering (owner ask): A-Z by default so a specific topic is
  // easy to find in a long list; 'by size' toggle keeps the richest-first view.
  const topics = (d.topics || []).slice().sort(topicSort === 'az'
    ? (a, b) => (a.label || '').toLowerCase().localeCompare((b.label || '').toLowerCase())
    : (a, b) => (b.count || 0) - (a.count || 0) || (a.label || '').toLowerCase().localeCompare((b.label || '').toLowerCase()));
  const consolidated = d.consolidated || [];
  const topicsHTML = topics.length
    ? `<div class="tag-cloud">${topics.map(t =>
        `<span class="tag-pill" onclick="renderTopic('${jesc(t.cid)}')">` +
        `${esc(t.label)}<span class="n">${t.count}</span></span>`).join('')}</div>`
    : '<div class="empty">No named topics yet — they grow from the nightly sleep\'s community pass.</div>';
  const consHTML = consolidated.map(n =>
    `<div class="book-line" onclick="openNode(event,'${jesc(n.id)}')">` +
    `<span class="kind-chip">${esc((n.kind||'').replace('_',' '))}</span> ${esc(n.gist || '')}</div>`).join('');
  const docsHTML = docs.map(o =>
    `<div class="book-line" onclick="openNode(event,'${jesc(o.id)}')">` +
    `<b>${esc(o.title || '(untitled)')}</b> — <span class="hub-sub">${esc(o.path || '')}</span>` +
    `${o.made ? ` · <span class="hub-ts">${esc((o.made||'').slice(0,10))}</span>` : ''}</div>`).join('');
  const termsHTML = terms.map(t =>
    `<div class="book-line"><b>${esc(t.term)}</b> — ${esc(t.definition || '')}</div>`).join('');
  $('main').innerHTML = `
    <div class="book-wrap">
      ${libraryShelfBar('knowledge')}
      <div class="book-title">Knowledge</div>
      <div class="hint">What the vault has distilled and collected — for browsing; the Index is for lookup.</div>
      <input class="promote-in" placeholder="narrow the shelf… type to filter topics, insights, docs, terms"
        oninput="libFilter(this.value)" style="width:100%;margin:4px 0 10px">

      <div class="book-section-h" id="index-topics">Topics <span class="fresh-glyph" title="refreshed by the nightly sleep">○</span><a style="cursor:pointer;color:var(--moss);font-size:12px;margin-left:10px;font-weight:400" title="toggle topic sort" onclick="toggleTopicSort()">${topicSort === 'az' ? 'by size' : 'A-Z'}</a></div>
      <div class="hint">Themes the graph found on its own — cross-session clusters, named while you slept.</div>
      ${topicsHTML}

      ${consolidated.length ? `<div class="hub-head"><span class="gold-dot"></span>Consolidated knowledge <span class="hub-sub">${d.consolidated_total || consolidated.length} distilled</span></div>
        <div class="hint">episodic → semantic: each of these absorbed several episodes</div>${consHTML}` : ''}

      <div class="book-section-h">Documents</div>
      ${docs.length ? docsHTML : '<div class="empty">No documents catalogued. Add one with: cairn doc add &lt;path&gt;</div>'}

      ${terms.length ? `<div class="book-section-h">Terms</div>
        <div class="hint">define terms with: cairn note --kind=term "term: definition"</div>${termsHTML}` : ''}

      <div class="book-section-h">Legend</div>
      <div class="hint">The field-manual key — what each mark means. Icons sit beside their labels; the mark never replaces the word.</div>
      ${legendPlate()}
    </div>`;
}

// A topic view: one named community's meaning-kind members, listed like a tag-
// membership page (breadcrumb back to the Index's Topics section).
async function renderTopic(cid) {
  pushView();   // same contract as renderProject: Back restores where you were
  $('main').innerHTML = '<div class="empty">gathering the topic…</div>';
  const d = await fetch('/api/garden/topic/' + encodeURIComponent(cid)).then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">topic unavailable: ${esc(d.error)}</div>`; return; }
  const nodes = d.nodes || [];
  $('main').innerHTML = `
    <div class="breadcrumb"><a onclick="showIndexTopics()">topics</a> / ${esc(d.label || cid)}</div>
    <div class="hub-head">${esc(d.label || cid)}</div>
    <div class="hint">${nodes.length} memories in this cluster <span class="fresh-glyph" title="refreshed by the nightly sleep">○</span> — grouped by the graph, named while you slept</div>
    ${nodes.length ? nodes.map(n =>
      `<div class="book-line" onclick="openNode(event,'${jesc(n.id)}')">` +
      `<span class="kind-chip">${esc((n.kind||'').replace('_',' '))}</span> ${esc(n.gist || '')}` +
      `${n.ts ? ` <span class="hub-sub">${when(n.ts)}</span>` : ''}</div>`).join('')
      : '<div class="empty">No meaning-kind members yet.</div>'}`;
  window.scrollTo(0, 0);
}

// Jump to the Index's Topics section from anywhere (the Hub's more:-row link).
// renderIndexPage is async — poll briefly for the anchor, then scroll.
function showIndexTopics() {
  show('knowledge');
  let tries = 0;
  const t = setInterval(() => {
    const el = document.getElementById('index-topics');
    if (el || ++tries > 40) {
      clearInterval(t);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, 100);
}

// the REGISTER toggle — the human surfaces show LIFE by default; ⚙ reveals
// the machine's work-notes. Persisted per-browser; display-only everywhere.
let showMachine = localStorage.getItem('cairn-machine') === '1';
let projSort = (() => { try { return localStorage.getItem('cairn-projsort') || 'recent'; } catch (e) { return 'recent'; } })();
let topicSort = (() => { try { return localStorage.getItem('cairn-topicsort') || 'az'; } catch (e) { return 'az'; } })();
function toggleMachine() {
  showMachine = !showMachine;
  localStorage.setItem('cairn-machine', showMachine ? '1' : '0');
  show(view);
}
function toggleTopicSort() {
  topicSort = topicSort === 'az' ? 'size' : 'az';
  try { localStorage.setItem('cairn-topicsort', topicSort); } catch (e) {}
  renderKnowledge();
}

// The wayline: ⌂ lights up at home; the label says where you are. One
// vocabulary — Book and Index are shelves of the Library.
function setWayline(v) {
  const here = { hub:'', today:'Today', book:'Library · the Book', index:'Library · the Index',
    knowledge:'Library · Knowledge', fresh:'Fresh ○ — the live tail',
    ideas:'Desk · Ideas', search:'Search', projects:'Projects', desk:'Desk',
    review:'Desk · Review', hubs:'Topics', flagged:'Desk · Flagged', archive:'Desk · Archive' }[v] || '';
  const wh = $('way-home'); if (wh) wh.classList.toggle('active', v === 'hub');
  const wi = $('way-index'); if (wi) wi.classList.toggle('active', v === 'index');
  const wl = $('way-here'); if (wl) wl.textContent = here;
}

function show(v) {
  if (v !== view) pushView();
  view = v;
  setWayline(v);
  ({ hub: renderHub_, book: renderBook, index: renderIndexPage,
     knowledge: renderKnowledge, fresh: renderFresh,
     projects: renderProjects, desk: renderDesk, ideas: renderIdeas,
     today: renderToday, review: renderReview, hubs: renderHubs,
     archive: renderArchive, flagged: renderFlagged, search: renderSearch,
     syslog: renderSyslog })[v]();
}

// The set-aside drawer — where archived / snoozed / done items live and come
// back. Nothing here is deleted; archived & snoozed are still active in the vault
// (the AI still sees them), they've just left your attention surfaces.
async function renderArchive() {
  const d = await fetch('/api/garden/archived').then(r => r.json());
  const row = (n, restoreKind, meta) => `
    <div class="card" data-id="${esc(n.id)}" onclick="toggleCard(event,this)">
      <div class="top">
        <span class="kind-chip">${esc((n.kind || '').replace('_',' '))}</span>
        <span class="who" style="margin-left:auto">${meta || ''}</span>
      </div>
      <div class="gist">${esc(titleOf(n))}</div>
      <div class="verbatim">${esc(((n.preview || '').length > (n.query || '').length && _sameStart(n.preview, n.query)) ? n.preview : (n.query || ''))}</div>
      <div class="actions">
        <button class="abtn" onclick="openNode(event,'${jesc(n.id)}')">open</button>
        ${restoreKind ? `<button class="abtn" style="border-color:var(--moss);color:var(--moss)" onclick="restore(event,'${jesc(n.id)}','${jesc(restoreKind)}')">${icon('restore','↩','s16')} restore</button>` : ''}
      </div>
    </div>`;
  $('main').innerHTML = `
    <div class="hint">${icon('archive','🗄','s16')} The set-aside drawer — <b>nothing here was deleted.</b> Archived &amp; snoozed items
    are still active in the vault (the AI still sees them); <b>${icon('restore','↩','s16')} restore</b> brings them back to your views.
    Done items are kept as your record.</div>
    <div class="section-h">${icon('archive','🗄','s16')} Archived (${d.counts.archived})</div>
    ${d.archived.length ? d.archived.map(n => row(n, 'unarchive', 'set aside ' + when(n.set_aside_at))).join('') : '<div class="empty">Nothing archived.</div>'}
    <div class="section-h">${icon('snooze','💤','s16')} Snoozed (${d.counts.snoozed})</div>
    ${d.snoozed.length ? d.snoozed.map(n => row(n, 'unsnooze', 'wakes ' + (n.wake || '').slice(0,10))).join('') : '<div class="empty">Nothing snoozed.</div>'}
    <div class="section-h">${icon('done','✓','s16')} Done (${d.counts.done})</div>
    ${d.done.length ? d.done.map(n => row(n, null, when(n.timestamp))).join('') : '<div class="empty">Nothing marked done yet.</div>'}`;
}

async function restore(e, id, kind) {
  e.stopPropagation();
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/${kind}`, { method: 'POST' });
  toast('↩ restored to your views');
  renderArchive();
}

// Flagged view — active flagged=1 nodes, newest first (hidden_ids respected
// server-side). The flag set used to vanish into luck; this is its one home.
// Mirrors the Archive drawer's structure (its closest sibling).
async function renderFlagged() {
  $('main').innerHTML = '<div class="empty">gathering flagged memories…</div>';
  const d = await fetch('/api/garden/flagged').then(r => r.json());
  if (d.error) { $('main').innerHTML = `<div class="empty">flagged view unavailable: ${esc(d.error)}</div>`; return; }
  const nodes = d.nodes || [];
  $('main').innerHTML = `
    <div class="hint">${icon('flag','⚑','s16')} Flagged — memories you (or the AI) marked to keep an eye on.
    Newest first; archived &amp; snoozed items stay out of this view (they're still active for the AI).</div>
    <div class="section-h">${icon('flag','⚑','s16')} Flagged (${d.count || 0})</div>
    ${nodes.length ? nodes.map(n => cardHTML(n)).join('')
      : '<div class="empty">Nothing flagged. Flag a memory from its card to keep it here.</div>'}`;
}

async function renderIdeas() {
  const d = await fetch('/api/garden/ideas').then(r => r.json());
  const ideaCard = n => `
    <div style="position:relative">
      ${n.findings || n.ripe ? `<div style="font-size:11px;font-weight:700;margin:2px 0 6px 64px;color:${n.ripe ? 'var(--gold)' : 'var(--muted)'}">
        ${n.ripe ? icon('ripe','🍃','s16') + ' RIPE — researched & revisited, ready to become a project · ' : ''}${n.findings ? icon('findings','📎','s16') + ' ' + n.findings + ' finding' + (n.findings > 1 ? 's' : '') : ''}</div>` : ''}
      ${cardHTML(n, {revisit:true, research:true})}
    </div>`;
  $('main').innerHTML = `
    <div class="hint" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span>${icon('idea','💡','s16')} The ideas bank — sparks kept for later. <b>${icon('research','🔍','s16')} research</b> queues it for the AI;
      findings come back chained to the idea. <b>${icon('revisit','↺','s16')}</b> keeps one alive.</span>
      <button class="abtn" style="border-color:var(--idea);color:var(--idea);font-weight:600"
        onclick="spark()">${icon('spark','✦','s16')} Spark me</button>
      <span style="display:flex;align-items:center;gap:6px">
        <input id="drift-q" class="hbtn" style="width:160px;padding:6px 10px;font-size:13px;background:var(--card);color:var(--ink)"
          placeholder="wander from what?" onkeydown="if(event.key==='Enter')doDrift()">
        <button class="abtn" style="border-color:var(--muted);color:var(--muted)"
          onclick="doDrift()">${icon('drift','⟳','s16')} Wander</button>
      </span>
    </div>
    <div id="drift-zone"></div>
    <div id="spark-zone"></div>
    ${d.queue.length ? '<div class="section-h">' + icon('research','🔍','s16') + ' Research queue</div><div class="hint">waiting for an AI session — findings will attach to each idea</div>' + d.queue.map(n => cardHTML(n, {done:true})).join('') : ''}
    ${d.fresh.length ? '<div class="section-h">' + icon('fresh','✨','s16') + ' Fresh (last 14 days)</div>' + d.fresh.map(ideaCard).join('') : ''}
    ${d.bank.length  ? '<div class="section-h">' + icon('bank','🏦','s16') + ' The bank</div>' + d.bank.map(ideaCard).join('') : ''}
    ${d.parked.length? '<div class="section-h">' + icon('parked','🅿','s16') + ' Parked projects</div><div class="hint">deliberately shelved — not dead, waiting</div>' + d.parked.map(n => cardHTML(n)).join('') : ''}
    ${!d.total && !d.parked.length ? '<div class="empty">No sparks banked yet — plant one above with kind ' + icon('idea','💡','s16') + ' idea.</div>' : ''}`;
}

async function requestResearch(e, id) {
  e.stopPropagation();
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/research`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}',
  });
  toast('🔍 queued — the next AI session will pick it up');
  renderIdeas();
}

async function spark() {
  const z = $('spark-zone');
  z.innerHTML = '<div class="empty">colliding thoughts…</div>';
  const d = await fetch('/api/garden/spark').then(r => r.json());
  if (!d.idea) { z.innerHTML = '<div class="empty">need a few embedded ideas first</div>'; return; }
  z.innerHTML = `
    <div style="border:1.5px solid var(--idea);border-radius:12px;padding:16px 18px;margin:6px 0 18px;background:var(--card)">
      <div class="section-h" style="margin-top:0;color:var(--idea)">${icon('spark','✦','s16')} Spark — an old thought, reintroduced</div>
      <div class="gist" style="cursor:pointer" onclick="openNode(null,'${jesc(d.idea.id)}')">${esc(d.idea.gist)}</div>
      ${d.collisions.length ? `<div class="section-h" style="margin-top:14px">collides with…</div>` +
        d.collisions.map(c => `
          <div style="padding:7px 0;border-top:1px dashed var(--line);cursor:pointer" onclick="openNode(null,'${jesc(c.id)}')">
            <span style="color:${c.cross_domain ? 'var(--terra)' : 'var(--muted)'};font-size:10.5px;font-weight:700;letter-spacing:1px">
              ${c.cross_domain ? '⚡ CROSS-DOMAIN' : 'related'} · ${esc((c.kind || '').replace('_',' '))}</span>
            <div style="font-family:Georgia,serif;font-size:14.5px">${esc(c.gist)}</div>
          </div>`).join('')
        : '<div class="empty">no rhymes found yet — the bank is young</div>'}
      ${d.threads && d.threads.length ? `<div class="section-h" style="margin-top:14px">🧵 loose threads — opened, never pulled</div>` +
        d.threads.map(t => sparkRow(t, t.kind)).join('') : ''}
      ${d.deep && d.deep.length ? `<div class="section-h" style="margin-top:14px">🔭 from the deep — important, long unseen</div>` +
        d.deep.map(t => sparkRow(t, t.kind)).join('') : ''}
      ${d.dormant && d.dormant.length ? `<div class="section-h" style="margin-top:14px">${icon('snooze','💤','s16')} dormant projects</div>` +
        d.dormant.map(p => `<div style="padding:6px 0;border-top:1px dashed var(--line);cursor:pointer" onclick="renderProject('${jesc(p.tag)}')">
          <span style="font-family:Georgia,serif;font-size:14.5px">${esc(p.name)}</span>
          <span style="color:var(--muted);font-size:12px"> — quiet for ${p.days} days</span></div>`).join('') : ''}
      <div class="hint" style="margin:10px 0 0">what would the combination be? plant it 💡 if it sparks. ✕ takes it off the board — the memory itself stays.</div>
    </div>`;
  window.scrollTo(0, 0);
}

function sparkRow(t, label) {
  return `
    <div style="padding:7px 0;border-top:1px dashed var(--line);display:flex;align-items:flex-start;gap:8px">
      <div style="flex:1;cursor:pointer" onclick="openNode(null,'${jesc(t.id)}')">
        <span style="color:var(--muted);font-size:10.5px;font-weight:700;letter-spacing:1px">${esc(label)}</span>
        <div style="font-family:Georgia,serif;font-size:14.5px">${esc(t.gist)}</div>
      </div>
      <button class="abtn" title="off the board — memory stays" style="padding:2px 9px"
        onclick="dismissSpark(event,'${jesc(t.id)}',this)">✕</button>
    </div>`;
}

async function dismissSpark(e, id, btn) {
  e.stopPropagation();
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/dismiss-spark`, { method: 'POST' });
  const row = btn.closest('div[style*="flex"]') || btn.parentElement;
  row.style.opacity = .25; row.style.pointerEvents = 'none';
  toast('off the board — still in memory, find it anytime via search');
}

async function doDrift() {
  const input = $('drift-q');
  const zone  = $('drift-zone');
  if (!input || !zone) return;
  const q = (input.value || '').trim();
  if (!q) { toast('type something to wander from'); return; }
  zone.innerHTML = '<div class="empty">wandering…</div>';
  try {
    const r = await fetch('/api/garden/drift?q=' + encodeURIComponent(q) + '&k=8');
    const d = await r.json();
    if (!r.ok || d.error) { zone.innerHTML = `<div class="empty">${esc(d.error || 'wander failed')}</div>`; return; }
    if (!d.results || !d.results.length) { zone.innerHTML = '<div class="empty">nothing to wander to — vault may need more edges</div>'; return; }
    const seedsLine = d.seeds && d.seeds.length
      ? `<div class="hint" style="margin:4px 0 6px">wandering out from: <b>${d.seeds.map(s => esc(String(s))).join(', ')}</b></div>`
      : '';
    zone.innerHTML = `
      <div style="border:1.5px solid var(--muted);border-radius:12px;padding:14px 16px;margin:6px 0 16px;background:var(--card)">
        <div class="section-h" style="margin-top:0;color:var(--muted)">${icon('drift','⟳','s16')} Wander — adjacent weak ties</div>
        ${seedsLine}
        ${d.results.map(n => `
          <div style="padding:8px 0;border-top:1px dashed var(--line);cursor:pointer"
               onclick="openNode(null,'${jesc(n.id)}')">
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span class="kind-chip">${esc((n.kind||'note').replace('_',' '))}</span>
              ${n.topic ? `<span style="color:var(--muted);font-size:11px">${esc(n.topic)}</span>` : ''}
              <span style="color:var(--muted);font-size:11px;margin-left:auto">hops ${n.hops} · ${n.score}</span>
            </div>
            <div style="font-family:Georgia,serif;font-size:14.5px;margin-top:3px">${esc(n.gist || n.source || '')}</div>
          </div>`).join('')}
      </div>`;
  } catch(err) {
    zone.innerHTML = `<div class="empty">wander error — ${esc(String(err))}</div>`;
  }
}

// Desk = THE one attention home (plan C2). Sections, in order:
// OVERDUE/DATED · OPEN LOOPS · TEND · WATCH · INBOX. Tend merges the review
// queue, fading, loose threads and from-the-deep into one deduped list, each row
// labelled with WHY it surfaced.
const TEND_REASON = {
  'overdue review': ['var(--terra)', 'overdue'],
  'fading':         ['var(--muted)', 'fog'],
  'loose thread':   ['var(--idea)',  'question'],
  'from the deep':  ['var(--moss)',  'spark'],
};
async function renderDesk() {
  const d = await fetch('/api/garden/desk').then(r => r.json());
  const tend = d.tend || [];
  const cnt = d.counts || {};
  const doneable = n => `
    <div style="position:relative">
      ${n.due ? `<div style="font-size:11px;font-weight:700;margin:2px 0 6px 64px;color:${n.overdue ? 'var(--terra)' : 'var(--muted)'}">
        ${n.overdue ? icon('overdue','⏰','s16') + ' OVERDUE ' : icon('due','📅','s16') + ' due '}${esc(n.due)}</div>` : ''}
      ${cardHTML(n, {done:true})}
    </div>`;
  // a Tend row: reason badge (why it surfaced) atop the card. reason is one of a
  // fixed set (server-supplied) — look it up in TEND_REASON; unknown falls back
  // to muted text. Both esc()'d. Tend cards offer "still true" (grows stability).
  const tendCard = n => {
    const rs = (n.reasons || [n.reason]).map(r => {
      const meta = TEND_REASON[r] || ['var(--muted)', null];
      return `<span style="color:${meta[0]}">${meta[1] ? icon(meta[1],'','s16') : ''}${esc(r)}</span>`;
    }).join(' <span style="color:var(--line)">·</span> ');
    return `<div style="position:relative">
      <div style="font-size:11px;font-weight:700;letter-spacing:.5px;margin:2px 0 6px 64px">${rs}</div>
      ${cardHTML(n, {review:true})}
    </div>`;
  };
  $('main').innerHTML = `
    <div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
      <button class="backbtn" onclick="goBack()">← Back</button>
      <span>${icon('todo','✅','s16')} Desk</span>
      <button class="abtn" onclick="show('ideas')">${icon('idea','💡','s16')} Ideas</button>
      <button class="abtn" onclick="show('review')">Review</button>
      <button class="abtn" onclick="show('flagged')">${icon('flag','⚑','s16')} Flagged</button>
      <button class="abtn" onclick="show('syslog')">⚙ Log</button>
      <button class="abtn" onclick="show('archive')">${icon('archive','🗄','s16')} Archive</button>
    </div>
    <div class="hint">Everything that needs a human, across every project — the one attention home.
    ${cnt.todo || 0} open · ${cnt.tend || 0} to tend · ${cnt.watch || 0} to watch · ${cnt.inbox || 0} in inbox.
    Write <b>due:2026-07-01</b> in a capture to date it.</div>
    ${d.dated.length ? '<div class="section-h">' + icon('due','📅','s16') + ' Overdue &amp; dated</div>' + d.dated.map(doneable).join('') : ''}
    ${d.open.length  ? '<div class="section-h">Still open</div>' + d.open.map(doneable).join('') : ''}
    ${tend.length ? '<div class="section-h">' + icon('spark','✦','s16') + ' Tend <span class="hub-sub">what you might be forgetting</span></div>' +
      '<div class="hint">the review queue, fading memories, loose threads and long-unseen deep cuts — each labelled with why it surfaced. <b>✓ still true</b> tends it.</div>' +
      tend.map(tendCard).join('') : ''}
    ${(() => { const w = (d.watch || []).filter(n => showMachine || !n.process); const hid = (d.watch || []).length - w.length;
      return (w.length ? '<div class="section-h">' + icon('warning','⚠','s16') + ' Watch (warnings &amp; flags)</div>' + w.map(n => cardHTML(n)).join('') : '')
        + (hid > 0 ? `<div class="hint" style="cursor:pointer" onclick="toggleMachine()">⚙ ${hid} machine warning${hid === 1 ? '' : 's'} — show</div>` : ''); })()}
    ${d.inbox.length ? '<div class="section-h">' + icon('inbox','📥','s16') + ' Inbox (from your pocket)</div><div class="hint">captured away from the desk — file, flag, or archive</div>' + d.inbox.map(n => cardHTML(n)).join('') : ''}
    ${!cnt.todo && !cnt.tend && !cnt.watch && !d.inbox.length ? '<div class="empty">Desk is clear. Rare and beautiful.</div>' : ''}`;
}

// The self-correction log — one quiet place for what the system did on its
// own: audit reports, topics gated as import blobs, flagged memories.
// A log to browse and fix, not an alarm (owner's correction of the first cut).
async function renderSyslog() {
  $('main').innerHTML = '<div class="empty">reading the log…</div>';
  const d = await fetch('/api/garden/syslog').then(r => r.json()).catch(() => ({}));
  const audits = d.audits || [], gated = d.gated_topics || [], flagged = d.flagged || [];
  $('main').innerHTML = `
    <div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
      <button class="backbtn" onclick="goBack()">← Back</button>
      <span>⚙ System log</span>
    </div>
    <div class="hint">What the machine did on its own — hid, gated, or wasn't sure about. Browse and overrule at your pace; nothing here is an emergency.</div>

    <div class="section-h">Nightly audit reports</div>
    ${audits.length ? audits.map(a => `
      <div class="proj" style="cursor:pointer" onclick="openNode(event,'${jesc(a.id)}')">
        <div class="name">${when(a.ts)} <span class="who" style="margin-left:auto">${a.findings.length} finding${a.findings.length === 1 ? '' : 's'}</span></div>
        ${a.findings.map(f => `<div class="lastline">· ${esc(f)}</div>`).join('')}
      </div>`).join('')
      : '<div class="empty">No audit reports yet — the organ runs with every sleep.</div>'}

    <div class="section-h">Topics hidden as import blobs</div>
    <div class="hint">big clusters that are almost entirely distilled backfill — kept out of Topics so they can't wear a costume; click to inspect the members.</div>
    ${gated.length ? `<div class="tag-cloud">${gated.map(t =>
        `<span class="tag-pill" onclick="renderTopic('${jesc(t.cid)}')">${esc(t.label)}<span class="n">${t.total}</span></span>`).join('')}</div>`
      : '<div class="empty">Nothing gated.</div>'}

    <div class="section-h">Flagged memories</div>
    ${flagged.length ? flagged.map(n => `
      <div class="book-line" onclick="openNode(event,'${jesc(n.id)}')">
        <span class="kind-chip">${esc((n.kind || '').replace('_',' '))}</span> ${esc(n.gist || '')}
        ${n.ts ? ` <span class="hub-sub">${when(n.ts)}</span>` : ''}</div>`).join('')
      : '<div class="empty">Nothing flagged.</div>'}`;
  window.scrollTo(0, 0);
}

// Promote a firehose node (a turn / insight) onto a curated shelf — writes a
// NEW linked idea|open_item, source untouched (append-only). CSRF-covered POST.
async function promoteNode(e, id, kind) {
  e.stopPropagation();
  const r = await fetch('/api/garden/promote-node', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ id, kind }),
  }).then(r => r.json()).catch(() => ({ error: 'network' }));
  if (r.error) { toast('promote failed: ' + r.error); return; }
  toast(kind === 'idea' ? '💡 added to Ideas — linked to this' : '✅ added to the Desk — linked to this');
}

async function markDone(e, id) {
  e.stopPropagation();
  const r = await fetch(`/api/garden/node/${encodeURIComponent(id)}/done`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}',
  }).then(r => r.json()).catch(() => ({}));
  const el = document.querySelector(`.card[data-id="${id}"]`);
  if (el) { el.style.opacity = .3; el.style.pointerEvents = 'none'; }
  // undo lives in the toast — the patent-misclick lesson: one wrong tap
  // shouldn't need a rescue mission. Undo re-raises the item append-only.
  if (r.resolved) {
    toastUndo('✓ resolved — history kept, desk cleared', async () => {
      const u = await fetch(`/api/garden/node/${encodeURIComponent(r.resolved)}/undo-done`, {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}',
      }).then(x => x.json()).catch(() => ({}));
      if (u.restored) { toast('restored — back on the desk'); renderDesk(); }
      else toast('undo failed: ' + (u.error || 'unknown'));
    });
  } else {
    toast('✓ resolved — history kept, desk cleared');
  }
}

async function renderProjects() {
  const d = await fetch('/api/garden/projects').then(r => r.json());
  // owner's ordering: most-recently-worked first (the server's order),
  // flippable to A–Z; the choice persists like the theme does.
  if (projSort === 'az')
    (d.projects || []).sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  // owner's rule (2026-07-08): section order is Approved → Proposed → Emerging.
  // Each group keeps the chosen order (recent or A–Z) within itself.
  const approved = (d.projects || []).filter(p => !p.emerging);
  const emerging = (d.projects || []).filter(p => p.emerging);
  // the registry ledger (Lane B): agent-proposed projects awaiting the
  // owner's verdict, worked at his own pace — bless regroups, pass keeps.
  let reg = { rows: [] };
  try { reg = await fetch('/api/garden/registry').then(r => r.json()); } catch (e) {}
  const proposed = (reg.rows || []).filter(r => r.status === 'proposed' || r.status === 'revived');
  const passed   = (reg.rows || []).filter(r => r.status === 'passed' || r.status === 'archived');
  const propHTML = proposed.map(r => `
    <div class="proj" style="border-style:dashed">
      <div class="name">${esc(r.name)} <span class="emerging-chip">PROPOSED</span>
        <span class="who" style="margin-left:auto">${r.evidence} notes${r.span ? ' · ' + esc(r.span) : ''}</span></div>
      ${r.why ? `<div class="desc">${esc(r.why)}</div>` : ''}
      ${r.code ? `<div class="lastline">code: ${esc(r.code)}</div>` : ''}
      <div class="proj-actions">
        <button class="abtn" style="border-color:var(--moss);color:var(--moss);font-weight:600"
          onclick="registryAct(event,'${jesc(r.slug)}','bless')">✓ make it a project</button>
        <button class="abtn" onclick="registryAct(event,'${jesc(r.slug)}','pass')">– not a project</button>
      </div>
    </div>`).join('');
  $('main').innerHTML = `
    <div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
      <button class="backbtn" onclick="goBack()">← Back</button>
      <span>Projects</span>
      <select class="abtn" style="margin-left:auto;cursor:pointer" title="how to order the list"
        onchange="projSort = this.value; try { localStorage.setItem('cairn-projsort', projSort); } catch(e) {}; renderProjects()">
        <option value="recent" ${projSort === 'az' ? '' : 'selected'}>Recently worked</option>
        <option value="az" ${projSort === 'az' ? 'selected' : ''}>A–Z</option>
      </select>
    </div>
    <div class="hint">Your work, organized the way you think about it. The same lens scopes the AI's recall — a project is a sub-context.
    <b>Emerging</b> topics have real mass but aren't declared yet — <b>promote</b> one to make it a project (uniting its spelling variants).</div>
    <div style="display:flex;gap:8px;margin:12px 0;align-items:center">
      <input class="promote-in" id="gather-q" style="flex:1"
        placeholder="Find an old project… (backfilled work hides in machine tags — try the project's name)"
        onkeydown="if(event.key==='Enter')doGather()">
      <button class="abtn" onclick="doGather()">${icon('research','🔎','s16')} gather</button>
    </div>
    <div id="gather-out"></div>
    ${(() => {
      // G2: emerging families need TWO doors — promote (it IS a project) or
      // file under an existing one (it BELONGS to a project: the trademark
      // research was Cairn work wearing its own tag). Options built once.
      const declared = (d.projects || []).filter(x => !x.emerging);
      window._fileUnderOpts = '<option value="">file under…</option>' +
        declared.map(x => `<option value="${esc(x.tag)}">${esc(x.name)}</option>`).join('');
      return '';
    })()}
    ${(() => { const pcard = p => {
      const m = p.maturity, tot = Math.max(1, m.seedling + m.budding + m.evergreen);
      const aliases = p.aliases || [];
      return `
      <div class="proj">
        <div class="name" onclick="renderProject('${jesc(p.tag)}')" style="cursor:pointer">${esc(p.name)}
          ${p.emerging ? '<span class="emerging-chip">EMERGING</span>' : ''}
          <span class="who" style="margin-left:auto">${when(p.last_ts)}</span>
        </div>
        <div class="desc" onclick="renderProject('${jesc(p.tag)}')" style="cursor:pointer">${esc(p.desc)}</div>
        <div class="badges">
          ${p.open ? `<span class="pbadge open">● ${p.open} open</span>` : ''}
          ${p.warnings ? `<span class="pbadge warn">${icon('warning','⚠','s16')} ${p.warnings}</span>` : ''}
          <span class="pbadge">${p.decisions} decisions</span>
          <span class="pbadge">${p.procedures} how-tos</span>
          <span class="pbadge">${p.total} memories</span>
          ${aliases.length ? `<span class="pbadge" title="spelling variants folded in">+${aliases.length} ${aliases.length===1?'variant':'variants'}</span>` : ''}
        </div>
        <div class="matbar">
          <div class="m-seed" style="width:${m.seedling/tot*100}%"></div>
          <div class="m-bud"  style="width:${m.budding/tot*100}%"></div>
          <div class="m-ever" style="width:${m.evergreen/tot*100}%"></div>
        </div>
        ${p.last_gist ? `<div class="lastline">latest: ${esc(p.last_gist)}</div>` : ''}
        ${p.emerging ? `<div class="proj-actions" style="display:flex;flex-direction:column;align-items:stretch;gap:8px">
          <div style="display:flex;gap:8px;align-items:center">
            <select id="fu-${esc(p.tag)}" class="promote-in" style="font-size:12.5px;padding:4px 8px;width:auto;max-width:240px;flex:0 1 240px"
              title="this belongs to an existing project — pick it, then click file"
              onclick="event.stopPropagation()" onchange="fileUnderPick(event,'${jesc(p.tag)}')">${window._fileUnderOpts || ''}</select>
            <button id="fu-go-${esc(p.tag)}" class="abtn" disabled
              title="file this emerging topic under the selected project"
              onclick="fileUnderGo(event,'${jesc(p.tag)}')">🗂 file</button>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="abtn" style="border-color:var(--moss);color:var(--moss);font-weight:600"
              onclick="openPromote(event,'${jesc(p.tag)}',${JSON.stringify(aliases).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')})">${icon('pin','📌','s16')} promote to project</button>
            <button class="abtn"
              title="not a project — hide this emerging card. No memories are touched, and it can return if new work lands on it."
              onclick="dismissProject(event,'${jesc(p.tag)}','${jesc(p.name)}')">${icon('dismiss','✕','s16')} not a project</button>
          </div>
          <div id="promote-${esc(p.tag)}" class="promote-form" style="display:none"></div>
        </div>` : ''}
      </div>`;
      };
      const prop = proposed.length ? `<div class="proj-sec">Proposed <span class="hub-sub" style="text-transform:none;letter-spacing:0;font-weight:400;margin-left:6px">${proposed.length} found in your history — your call, your pace</span></div>${propHTML}` : '';
      const empty = (!approved.length && !emerging.length && !proposed.length) ? '<div class="empty">No projects yet — plant thoughts with project tags.</div>' : '';
      const hA = approved.length ? `<div class="proj-sec">Approved <span class="hub-sub" style="text-transform:none;letter-spacing:0;font-weight:400">${approved.length} declared ${approved.length === 1 ? 'project' : 'projects'}</span></div>` : '';
      const hE = emerging.length ? `<div class="proj-sec">Emerging <span class="hub-sub" style="text-transform:none;letter-spacing:0;font-weight:400">${emerging.length} ${emerging.length === 1 ? 'topic' : 'topics'} with real mass — promote to declare</span></div>` : '';
      return hA + approved.map(pcard).join('') + prop + hE + emerging.map(pcard).join('') + empty;
    })()}
    ${passed.length ? `<div class="hub-sub" style="cursor:pointer;margin:4px 0 10px" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? '' : 'none'">▸ ${passed.length} passed/archived proposal${passed.length === 1 ? '' : 's'} — kept, revivable</div>
    <div style="display:none">${passed.map(r => `
      <div class="proj" style="opacity:.65">
        <div class="name">${esc(r.name)} <span class="who" style="margin-left:auto">${esc(r.status)}</span></div>
        <div class="proj-actions"><button class="abtn" onclick="registryAct(event,'${jesc(r.slug)}','revive')">revive</button></div>
      </div>`).join('')}</div>` : ''}
    ${(d.dismissed && d.dismissed.length) ? `<div class="hub-sub" style="cursor:pointer;margin:4px 0 10px" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? '' : 'none'">▸ ${d.dismissed.length} hidden — marked “not a project” (restorable)</div>
    <div style="display:none">${d.dismissed.map(x => `
      <div class="proj" style="opacity:.6">
        <div class="name">${esc(x.name)} <span class="who" style="margin-left:auto">hidden${x.dismissed_at ? ' ' + when(x.dismissed_at) : ''}</span></div>
        <div class="proj-actions"><button class="abtn" onclick="undismissProject(event,'${jesc(x.key)}')">restore to emerging</button></div>
      </div>`).join('')}</div>` : ''}`;
}

// Registry verdicts (Lane B): bless / pass / revive append to the ledger —
// nothing is edited, nothing deleted. Bless also declares the project so
// its scattered strata regroup ("when i approve it regroups as a project").
async function registryAct(e, slug, action) {
  if (e) e.stopPropagation();
  const r = await fetch('/api/garden/registry/act', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, action })
  }).then(r => r.json()).catch(() => ({ error: 'network — is the server up?' }));
  if (r.error) { toast('registry: ' + r.error); return; }
  if (r.warning) { toast(r.warning); }
  else toast(action === 'bless' ? `✓ '${slug}' is a project — its memories regroup now`
             : action === 'pass' ? `'${slug}' set aside — kept, revivable`
             : `'${slug}' revived`);
  renderProjects();
}

// 'Not a project': hide an emerging card. No vault write — just a local display
// preference (~/.cairn/dismissed.json) the projects view filters against. The
// card can return if new work lands on that family. Undo lives in the toast.
async function dismissProject(e, tag, name) {
  if (e) e.stopPropagation();
  const r = await fetch('/api/garden/dismiss-project', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag })
  }).then(r => r.json()).catch(() => ({ error: 'network — is the server up?' }));
  if (r.error) { toast('dismiss failed: ' + r.error); return; }
  toastUndo(`“${name || tag}” hidden — not a project`, async () => {
    await fetch('/api/garden/undismiss-project', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: r.key })
    }).catch(() => {});
    renderProjects();
  });
  renderProjects();
}

// Restore a dismissed family from the "hidden" list — the inverse of above.
async function undismissProject(e, key) {
  if (e) e.stopPropagation();
  const r = await fetch('/api/garden/undismiss-project', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key })
  }).then(r => r.json()).catch(() => ({ error: 'network — is the server up?' }));
  if (r.error) { toast('restore failed: ' + r.error); return; }
  toast('restored — back in emerging');
  renderProjects();
}

// G2: file an emerging family under an existing project — one alias write via
// POST /file-under; the family folds into that project on the next render.
// TWO steps on purpose (owner, 2026-07-09): picking the dropdown only ARMS the
// choice — it no longer commits on select (a stray click used to file it and
// vanish the card). The explicit 'file' button is what actually writes.
function fileUnderPick(e, tag) {
  if (e) e.stopPropagation();
  const sel = document.getElementById('fu-' + tag);
  const go  = document.getElementById('fu-go-' + tag);
  if (!sel || !go) return;
  const armed = !!sel.value;
  go.disabled = !armed;
  // moss-green invites the click once a project is chosen (matches promote)
  go.style.borderColor = armed ? 'var(--moss)' : '';
  go.style.color       = armed ? 'var(--moss)' : '';
  go.style.fontWeight  = armed ? '600' : '';
}

async function fileUnderGo(e, tag) {
  if (e) e.stopPropagation();
  const sel = document.getElementById('fu-' + tag);
  const go  = document.getElementById('fu-go-' + tag);
  const proj = sel ? sel.value : '';
  if (!proj) { toast('pick a project first'); return; }
  if (sel) sel.disabled = true;
  if (go)  go.disabled  = true;
  const r = await fetch('/api/garden/file-under', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, project: proj })
  }).then(r => r.json()).catch(() => ({ error: 'network — is the server up?' }));
  if (r.error) {
    toast('file-under failed: ' + r.error);
    if (sel) sel.disabled = false;
    if (go)  go.disabled  = false;
    return;
  }
  toast(`✓ '${tag}' filed under '${r.project}' — its memories now count there`);
  renderProjects();
}

// Deep-gather (plan P3.5): the discovery half of promote. Backfilled projects
// (old apps, side projects, imported work…) carry their identity ONLY in
// kw:/entity: machine tags, so they can never surface as emerging candidates.
// Type a name -> the server sweeps every active tag (machine strata included,
// normalized so "acme" meets "entity:Acme Corp") -> candidates
// render as pre-checked alias boxes -> one click declares the project through
// the same POST /promote path. Reunification without touching a single tag.
async function doGather() {
  const q = (document.getElementById('gather-q').value || '').trim();
  const out = document.getElementById('gather-out');
  if (!out) return;
  if (q.length < 3) {
    out.innerHTML = '<div class="empty">type at least 3 characters of the project name</div>';
    return;
  }
  out.innerHTML = '<div class="empty">sweeping the vault…</div>';
  let d;
  try {
    d = await fetch('/api/garden/gather?q=' + encodeURIComponent(q)).then(r => r.json());
  } catch (e) { out.innerHTML = '<div class="empty">gather failed</div>'; return; }
  if (!d.candidates || !d.candidates.length) {
    out.innerHTML = `<div class="empty">nothing found for “${esc(q)}” — try another spelling</div>`;
    return;
  }
  const slug = q.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  const checks = d.candidates.map(c =>
    `<label class="promote-alias"><input type="checkbox" checked data-alias="${esc(c.tag)}"> ${esc(c.tag)} <span class="who">${c.count}</span></label>`).join('');
  out.innerHTML = `
    <div class="proj" style="border-color:var(--moss)">
      <div class="name">${icon('bank','🏦','s16')} ${esc(q)} — ${d.total} memories found across ${d.candidates.length} tag variants</div>
      ${(d.samples || []).map(s => `<div class="lastline">· ${esc(s)}</div>`).join('')}
      <div class="promote-grid" style="margin-top:8px">
        <label class="promote-lbl">Name</label>
        <input class="promote-in" id="ga-name" value="${esc(q)}" maxlength="80">
        <label class="promote-lbl">Blurb</label>
        <input class="promote-in" id="ga-blurb" placeholder="what this project is about" maxlength="200">
        <label class="promote-lbl">Fold in</label>
        <div class="promote-checks">${checks}</div>
      </div>
      <div class="promote-btns">
        <button class="abtn" style="border-color:var(--moss);color:var(--moss);font-weight:600"
          onclick="declareGathered('${jesc(slug)}')">declare project — unite ${d.total} memories</button>
        <button class="abtn" onclick="document.getElementById('gather-out').innerHTML=''">cancel</button>
      </div>
    </div>`;
}

async function declareGathered(slug) {
  const name  = (document.getElementById('ga-name').value || slug).trim();
  const blurb = (document.getElementById('ga-blurb').value || '').trim();
  const aliases = [...document.querySelectorAll('#gather-out input[data-alias]:checked')]
    .map(i => i.getAttribute('data-alias'));
  const res = await fetch('/api/garden/promote', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tag: slug, name: name, blurb: blurb, aliases: aliases})});
  const j = await res.json().catch(() => ({}));
  if (!res.ok) { toast(j.error || 'declare failed'); return; }
  toast('project declared — ' + name);
  renderProjects();
}

// Promote flow: reveal an inline form on an emerging card. The alias picker is
// pre-filled with the family's same-spelling variants (checkboxes) and a free
// text field lets the user add extra alias tags (the Acme case — kw:silver,
// kw:hallmark, entity:Acme's). All inputs are esc/jesc'd on render and sent
// as JSON; the server re-validates every tag (no quotes/angle brackets).
function openPromote(e, tag, aliases) {
  e.stopPropagation();
  const box = document.getElementById('promote-' + tag);
  if (!box) return;
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  const al = Array.isArray(aliases) ? aliases : [];
  const checks = al.map((a, i) =>
    `<label class="promote-alias"><input type="checkbox" checked data-alias="${esc(a)}"> ${esc(a)}</label>`).join('');
  box.innerHTML = `
    <div class="promote-grid">
      <label class="promote-lbl">Name</label>
      <input class="promote-in" id="pm-name-${esc(tag)}" value="${esc(tag)}" maxlength="80">
      <label class="promote-lbl">Blurb</label>
      <input class="promote-in" id="pm-blurb-${esc(tag)}" placeholder="what this project is about" maxlength="200">
      ${checks ? `<label class="promote-lbl">Fold in variants</label><div class="promote-checks">${checks}</div>` : ''}
      <label class="promote-lbl">More alias tags</label>
      <input class="promote-in" id="pm-extra-${esc(tag)}" placeholder="comma-separated, e.g. kw:silver, kw:hallmark">
    </div>
    <div class="promote-btns">
      <button class="abtn" style="border-color:var(--moss);color:var(--moss);font-weight:600"
        onclick="submitPromote(event,'${jesc(tag)}')">confirm promote</button>
      <button class="abtn" onclick="document.getElementById('promote-${jesc(tag)}').style.display='none'">cancel</button>
    </div>`;
  box.style.display = 'block';
}

async function submitPromote(e, tag) {
  e.stopPropagation();
  const box = document.getElementById('promote-' + tag);
  const name = (document.getElementById('pm-name-' + tag).value || tag).trim();
  const blurb = (document.getElementById('pm-blurb-' + tag).value || '').trim();
  const aliases = [];
  box.querySelectorAll('input[data-alias]').forEach(c => {
    if (c.checked) aliases.push(c.getAttribute('data-alias'));
  });
  const extra = (document.getElementById('pm-extra-' + tag).value || '');
  extra.split(',').map(s => s.trim()).filter(Boolean).forEach(a => {
    if (!aliases.includes(a)) aliases.push(a);
  });
  const r = await fetch('/api/garden/promote', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ tag, name, blurb, aliases }),
  }).then(r => r.json());
  if (r.error) { toast('promote failed: ' + r.error); return; }
  toast(`✓ '${r.name}' is now a project${r.aliases && r.aliases.length ? ' (+' + r.aliases.length + ' aliases)' : ''}`);
  renderProjects();
}

async function renderProject(tag) {
  pushView();
  const d = await fetch('/api/garden/project/' + encodeURIComponent(tag)).then(r => r.json());
  // the REGISTER applies here too: a machine work-note that mentions this
  // project (and got tagged with it) shouldn't read as the project's own life.
  let projHidden = 0;
  const life = items => {
    const keep = showMachine ? (items || []) : (items || []).filter(n => !n.process);
    projHidden += (items || []).length - keep.length;
    return keep;
  };
  const sect = (title, items, hint) => (items = life(items)).length
    ? `<div class="section-h">${title}</div>` +
      (hint ? `<div class="hint">${hint}</div>` : '') +
      items.map(n => cardHTML(n)).join('') : '';
  // DROPS: files from ~\\Exchange\\<tag>\\ — live listing; click copies the path.
  const filesHTML = (d.files || []).length ? `
    <div class="section-h">${icon('photo','📎','s16')} Files <span class="hub-sub">from Exchange\\${esc(d.tag)}</span></div>
    <div class="hint">drop files into that folder and they appear here — click a row to copy its path for any agent</div>
    ${d.files.map(f => `
      <div class="book-line" style="cursor:copy" onclick="navigator.clipboard.writeText('${jesc(f.path)}').then(()=>toast('path copied — paste it to any agent'))">
        <b>${esc(f.name)}</b> <span class="hub-sub">${f.kb} KB · ${when(f.mtime)}</span>
      </div>`).join('')}` : '';
  $('main').innerHTML = `
    <div class="breadcrumb"><button class="backbtn" onclick="goBack()">← Back</button><a onclick="show('projects')">projects</a> / ${esc(d.name)}</div>
    <div class="hub-head">${esc(d.name)}</div>
    ${(() => { // the librarian header: what this is · where it stands · what's open
      const s = d.stats || {};
      if (!s.total) return `<div class="hint">${esc(d.desc)}</div>`;
      const day = t => (t || '').slice(0, 10);
      const openN = (d.attention || []).filter(n => showMachine || !n.process).length;
      return `<div class="hint" style="font-family:Georgia,serif;font-size:14.5px;line-height:1.55">
        ${esc(d.desc)} — <b>${s.total}</b> memories across <b>${s.sessions}</b> ${s.sessions === 1 ? 'session' : 'sessions'},
        ${day(s.first_ts)} → ${day(s.last_ts)}.
        ${d.decisions.length ? `<b>${d.decisions.length}${d.decisions.length >= 10 ? '+' : ''}</b> decisions on record.` : ''}
        ${openN ? ` <b>${openN}</b> still open.` : ' Nothing waiting on you.'}
      </div>`; })()}
    ${filesHTML}
    ${sect(icon('flag','⚑','s16') + ' Needs attention', d.attention, 'open items, warnings, and flags — the to-do surface')}
    ${sect('Decisions', d.decisions)}
    ${sect(icon('evergreen','🌲','s16') + ' How-to (procedures)', d.procedures, 'evergreen — these never fade with time')}
    ${sect('Knowledge', d.knowledge)}
    ${sect('Recent activity', d.recent)}
    ${projHidden > 0 ? `<div class="hint" style="cursor:pointer" onclick="toggleMachine()">⚙ ${projHidden} machine work-note${projHidden === 1 ? '' : 's'} mention this project — show</div>` : ''}
    ${!d.attention.length && !d.decisions.length && !d.recent.length ?
      '<div class="empty">Nothing here yet.</div>' : ''}
    ${d.declared ? `<div style="margin-top:28px;text-align:right">
      <span class="hub-sub" style="cursor:pointer;font-size:10.5px;opacity:.55"
        title="undo an accidental promote — the memories stay, only the project label is removed"
        onclick="demoteProject(event,'${jesc(d.tag)}','${jesc(d.name)}')">demote from projects…</span></div>` : ''}`;
  window.scrollTo(0, 0);
}

// The exit for an accidental promote — deliberately small and quiet, with a
// hard are-you-sure. Config-only: every memory stays exactly where it is;
// only the project LABEL is removed (family returns to emerging), and the
// registry ledger appends the demotion so history keeps it.
async function demoteProject(e, tag, name) {
  if (e) e.stopPropagation();
  if (!confirm(`Remove "${name}" from your projects?\n\nNo memories are deleted — everything stays in the vault; the grouping just returns to 'emerging'. The registry keeps a record and you can promote it again anytime.`)) return;
  const r = await fetch('/api/garden/demote-project', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag })
  }).then(r => r.json()).catch(() => ({ error: 'network — is the server up?' }));
  if (r.error) { toast('demote failed: ' + r.error); return; }
  toast(`'${r.name}' demoted — memories untouched, revivable anytime`);
  show('projects');
}

// ── FRESH — the live tail, cleaner (owner ask: "where's the most recent
// non-embedded stuff... almost like live log but cleaner"). One line per
// capture, newest first: ○ = captured, weaves in at tonight's sleep; · = woven.
// No cards, no grouping — a log you can trust at a glance. Reached from the
// Hub's since-you-left header and Today's hint; deliberately not in the wayline.
let freshOldest = '';
function freshRowsHTML(rows) {
  return rows.map(n => `
      <div class="book-line" onclick="openNode(event,'${jesc(n.id)}')">
        <span class="fresh-glyph" title="${n.embedded ? 'woven in' : 'weaves in at tonight&#39;s sleep'}">${n.embedded ? '·' : '○'}</span>
        <span class="kind-chip">${esc((n.kind || 'note').replace('_',' '))}</span>
        ${esc(n.gist || '')} <span class="hub-sub">${n.speaker === 'user' ? 'you · ' : ''}${when(n.ts)}</span>
      </div>`).join('');
}
async function renderFresh() {
  $('main').innerHTML = '<div class="empty">reading the live tail…</div>';
  const d = await fetch('/api/garden/justlanded').then(r => r.json());
  const rows = (d.nodes || []);
  freshOldest = rows.length ? rows[rows.length - 1].ts : '';
  $('main').innerHTML = `
    <div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
      <button class="backbtn" onclick="goBack()">← Back</button>
      <span>○ Fresh — the live tail</span>
    </div>
    <div class="hint">newest captures, any kind · <b>○</b> captured, weaves in at tonight's sleep · <b>·</b> woven in and findable by meaning</div>
    <div id="fresh-rows">${rows.length ? freshRowsHTML(rows) : '<div class="empty">Nothing captured yet.</div>'}</div>
    ${rows.length >= 10 ? `<div class="hint" id="fresh-older" style="cursor:pointer" onclick="freshOlder()">▾ go back further</div>` : ''}`;
}
async function freshOlder() {
  if (!freshOldest) return;
  const d = await fetch('/api/garden/justlanded?before=' + encodeURIComponent(freshOldest)).then(r => r.json());
  const rows = (d.nodes || []);
  if (!rows.length) { const b = $('fresh-older'); if (b) b.textContent = '— the tail ends here —'; return; }
  freshOldest = rows[rows.length - 1].ts;
  const z = $('fresh-rows'); if (z) z.insertAdjacentHTML('beforeend', freshRowsHTML(rows));
  if (rows.length < 30) { const b = $('fresh-older'); if (b) b.textContent = '— the tail ends here —'; }
}

// ── Today = the Stream, grouped by SESSION, with a conversation reader (C3) ──
// DECLARED-FIRST consts (JS decl-order rule): renderToday reads these at call.
const TODAY_MEANING = new Set(['decision','warning','insight','idea','open_item',
  'procedure','resolved','hypothesis','question','blocker','artifact']);
// machine/plumbing tags never make a good session label — mirror the server's
// _is_machine_tag prefixes + a few bare plumbing literals.
const TODAY_TAG_SKIP_PREFIX = ['kw:','entity:','prov:','by:','stance:','account:',
  'turn:','member:','due:','room:','from:','media:','file:','mtime:','made:'];
const TODAY_TAG_SKIP = new Set(['garden','human-capture','inbox','annotation',
  'chat','conversation','user','agent','backfill','consolidated','quiet',
  'memory','desk-done','research-queue','promoted','chat-pin','prov:own',
  // distill/import plumbing literals (mirror the server's _NOT_PROJECTS) so a
  // session never labels itself with a machine stratum like 'claim'/'import'.
  'claim','import','mcp','codex','claude','human','distilled','test','codex-test']);
function todayTagOk(t) {
  if (typeof t !== 'string' || !t) return false;
  if (TODAY_TAG_SKIP.has(t)) return false;
  return !TODAY_TAG_SKIP_PREFIX.some(p => t.startsWith(p));
}
// A humanized session header: dominant topic tag (if any) · start–end time ·
// distinct-model chips · node count. NEVER the raw session id as the label —
// fall back to "session" + the time range (plan C3).
function sessionLabel(nodes) {
  const tagCount = {};
  nodes.forEach(n => (n.tags || []).forEach(t => {
    if (todayTagOk(t)) tagCount[t] = (tagCount[t] || 0) + 1; }));
  const top = Object.keys(tagCount).sort((a, b) => tagCount[b] - tagCount[a])[0];
  const times = nodes.map(n => n.timestamp).filter(Boolean).sort();
  const t0 = times[0], t1 = times[times.length - 1];
  const range = t0 ? (when(t0) + (t1 && t1 !== t0 ? ' – ' + when(t1) : '')) : '';
  return { title: top || 'session', range,
           models: [...new Set(nodes.map(n => n.model).filter(m => m && m !== 'unknown'))] };
}

let todayFilter = 'all';
let todayBrain = false;   // model-telemetry ledger strip — hidden by default
let todayDaysAgo = 0;      // travel-back: 0 = today, 1 = yesterday, …

async function renderToday() {
  const d = await fetch('/api/garden/today' + (todayDaysAgo ? '?days_ago=' + todayDaysAgo : '')).then(r => r.json());
  const meaning = d.nodes.filter(n => n.kind !== 'tool_call');
  const dayLabel = todayDaysAgo === 0 ? 'Today'
    : todayDaysAgo === 1 ? 'Yesterday' : (d.day || todayDaysAgo + ' days ago');

  // ONE JOB (owner ruling 562e4e710584): Today answers "what happened since I
  // left" — the session-grouped stream IS the content. The just-landed strip
  // lived here too and duplicated the stream's newest rows; the Hub's
  // "since your last visit" already covers arrivals. Cut.
  // group by session, preserve first-seen order (nodes arrive ASC by time)
  const order = [], bySession = {};
  meaning.forEach(n => {
    const s = n.session || 'unknown';
    if (!bySession[s]) { bySession[s] = []; order.push(s); }
    bySession[s].push(n);
  });

  // ledger strip — now behind a "brain" toggle, hidden by default. Fetched only
  // when the toggle is on (fire-and-forget, never blocks render).
  let ledgerStrip = '';
  if (todayBrain) {
    try {
      const ld = await fetch('/api/garden/ledger').then(r => r.json());
      if (ld && ld.total_shown >= 0) {
        const parts = (ld.channels || []).map(c => `${esc(c.channel)} ${c.shown}`).join(' / ');
        ledgerStrip = `<div style="font-size:11px;color:var(--muted);margin:2px 0 8px;letter-spacing:0.3px">` +
          `memory shown ${ld.total_shown}x · used ${ld.total_cited}x` +
          (parts ? ` · ${parts}` : '') + `</div>`;
      }
    } catch(e) { /* ledger unavailable — skip strip */ }
  }
  // The digest box is gone (owner: Today was noisy) — due/fading live on the
  // Desk; the two counts worth keeping fold into the hint line below.
  const learned = meaning.filter(n => n.kind === 'decision' || n.kind === 'insight').length;
  const emerged = meaning.filter(n => n.kind === 'idea' || n.kind === 'question' || n.kind === 'hypothesis').length;

  // one block per session: humanized header · highlight (meaning) cards · a
  // collapsed "N turns — read" expander that lazy-loads the conversation reader.
  const blocks = order.map((s, i) => {
    const nodes = bySession[s];
    const lbl = sessionLabel(nodes);
    // the REGISTER: highlights show your life; the machine's work-notes wait
    // behind the ⚙ toggle (the conversation reader below is untouched).
    const highlights = nodes.filter(n => TODAY_MEANING.has(n.kind))
                            .filter(n => showMachine || !n.process);
    const turns = nodes.filter(n => n.kind === 'conversation_turn');
    if (!highlights.length && !turns.length) return null;
    // tiny threads (1-2 node process chatter, e.g. title-generation calls)
    // collapse below the stream instead of standing as full session blocks.
    const tiny = nodes.length <= 2 && !highlights.length;
    const modelChips = lbl.models.map(m =>
      `<span class="sess-model">${esc(m)}</span>`).join('');
    const readBar = turns.length ? `
      <div class="sess-read" onclick="toggleTurns(event, this, '${jesc(s)}')">
        <span class="sess-read-caret">▸</span>
        <span>${turns.length} ${turns.length === 1 ? 'turn' : 'turns'} — read</span>
      </div>
      <div class="sess-turns" data-loaded="0" style="display:none"></div>` : '';
    return { tiny, html: `
      <div class="sess-block">
        <div class="sess-head">
          <span class="sess-title">${esc(lbl.title)}</span>
          ${lbl.range ? `<span class="sess-range">${lbl.range}</span>` : ''}
          ${modelChips}
          <span class="sess-count">${nodes.length} ${nodes.length === 1 ? 'node' : 'nodes'}</span>
        </div>
        ${highlights.map(n => cardHTML(n)).join('')}
        ${readBar}
      </div>` };
  }).filter(Boolean);
  const mainHTML = blocks.filter(b => !b.tiny).map(b => b.html).join('');
  const tinyList = blocks.filter(b => b.tiny);

  $('main').innerHTML = `
    <div class="breadcrumb" style="display:flex;gap:8px;align-items:center">
      <button class="backbtn" onclick="goBack()">← Back</button>
      <span>${esc(dayLabel)}${todayDaysAgo ? ` <span class="hub-sub">${esc(d.day || '')}</span>` : ''}</span>
      <button class="abtn" title="one day earlier" onclick="todayDaysAgo++;renderToday()">◂ earlier</button>
      ${todayDaysAgo ? `<button class="abtn" title="one day later" onclick="todayDaysAgo--;renderToday()">later ▸</button>
      <button class="abtn" onclick="todayDaysAgo=0;renderToday()">today</button>` : ''}
    </div>
    ${ledgerStrip}
    <div class="hint" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>${todayDaysAgo ? `What happened that day` : `What happened since you left`} — every session, grouped by conversation.
      <b>${meaning.length}</b> memories · <b>${order.length}</b> ${order.length === 1 ? 'session' : 'sessions'}${learned ? ` · <b>${learned}</b> learned` : ''}${emerged ? ` · <b>${emerged}</b> emerged` : ''}.</span>
      <button class="abtn" style="border-color:var(--muted);color:var(--muted)"
        onclick="todayBrain=!todayBrain;renderToday()">${icon('spark','🧠','s16')} brain${todayBrain ? ' ▾' : ''}</button>
    </div>
    <div class="tl">${mainHTML || '<div class="empty">Nothing captured today yet — plant the first thought above.</div>'}
    ${tinyList.length ? `<div class="hint" style="cursor:pointer" onclick="const z=document.getElementById('tiny-threads');z.style.display=z.style.display==='none'?'block':'none'">▸ ${tinyList.length} tiny thread${tinyList.length === 1 ? '' : 's'} collapsed — quick calls &amp; titles, no meaning captured</div><div id="tiny-threads" style="display:none">${tinyList.map(b => b.html).join('')}</div>` : ''}</div>`;
}

// Expand/collapse a session's turns; lazy-load the conversation reader once.
async function toggleTurns(e, bar, session) {
  e.stopPropagation();
  const zone = bar.nextElementSibling;
  const caret = bar.querySelector('.sess-read-caret');
  if (!zone) return;
  const open = zone.style.display === 'none';
  zone.style.display = open ? 'block' : 'none';
  if (caret) caret.textContent = open ? '▾' : '▸';
  if (open && zone.getAttribute('data-loaded') === '0') {
    zone.setAttribute('data-loaded', '1');
    zone.innerHTML = '<div class="empty" style="padding:8px">reading…</div>';
    try {
      const d = await fetch('/api/garden/session/' + encodeURIComponent(session) + '/turns').then(r => r.json());
      zone.innerHTML = renderTurns(d.turns || [], session, d.offset || 0, d.total || 0);
    } catch(err) {
      zone.innerHTML = `<div class="empty" style="padding:8px">could not read this session</div>`;
      zone.setAttribute('data-loaded', '0');
    }
  }
}

// The conversation reader (app's first): each turn as speaker label + time +
// esc'd text paragraphs. user lines lean right/warm, agent lines left/plain.
// session/offset/total (optional, from the paged turns endpoint) drive a
// trailing "load more turns" row when more turns remain past this page.
function renderTurns(turns, session, offset, total) {
  if (!turns.length) return '<div class="empty" style="padding:8px">no turns in this session</div>';
  const shown = (offset || 0) + turns.length;
  const more = (session && total && shown < total)
    ? `<div class="hub-strip-line" style="cursor:pointer" onclick="loadMoreTurns(event,this,'${jesc(session)}',${shown})">load more turns (shown ${shown} of ${total}) ▸</div>`
    : '';
  return '<div class="reader">' + turns.map(t => {
    const mine = t.speaker === 'user';
    const who = mine ? 'you' : esc(t.model || 'agent');
    const paras = esc(t.text || '').split(/\n{2,}/).map(p =>
      `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
    return `<div class="turn ${mine ? 'turn-you' : 'turn-ai'}">
      <div class="turn-meta"><span class="turn-who">${who}</span><span class="turn-time">${when(t.ts)}</span></div>
      <div class="turn-text">${paras}</div>
    </div>`;
  }).join('') + more + '</div>';
}

// "load more turns" row click: fetch the next page and stack its turns (+ a
// fresh load-more row if still more remain) into the reader in place of this
// row, so pages accumulate in order rather than replacing what's shown.
async function loadMoreTurns(e, row, session, offset) {
  e.stopPropagation();
  const d = await fetch('/api/garden/session/' + encodeURIComponent(session) + '/turns?offset=' + offset).then(r => r.json());
  const html = renderTurns(d.turns || [], session, d.offset || 0, d.total || 0);
  // renderTurns wraps in a .reader div; unwrap it so the new turns (+ next
  // load-more row, if any) insert as siblings before this row, in order.
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  const inner = wrap.firstElementChild ? wrap.firstElementChild.innerHTML : html;
  row.insertAdjacentHTML('beforebegin', inner);
  row.remove();
}

async function renderReview() {
  const d = await fetch('/api/garden/review').then(r => r.json());
  $('main').innerHTML = `
    <div class="hint">The due-pressure queue — the same scheduler that feeds the model's memory feeds yours.
    Tending a memory (<b>✓ still true</b>) grows its stability; it returns less often. Archive what's stale.</div>
    ${d.nodes.length ? d.nodes.map(n => cardHTML(n, {review:true})).join('') :
      '<div class="empty">Garden is fully tended. Come back tomorrow.</div>'}`;
}

async function renderHubs() {
  const d = await fetch('/api/garden/hubs').then(r => r.json());
  $('main').innerHTML = `
    <div class="hint">Topic hubs grow from tags; gold hubs were synthesized by the consolidation pass while you slept.</div>
    <div class="tag-cloud">${d.tags.map(t =>
      `<span class="tag-pill" onclick="renderHub('${jesc(t.tag)}')">${esc(t.tag.replace(/^entity:/,''))}<span class="n">${t.count}</span></span>`).join('')}</div>
    ${d.consolidated.length ? `<div class="hub-head"><span class="gold-dot"></span>Consolidated knowledge</div>
      <div class="hint">episodic → semantic: each of these absorbed several episodes</div>` : ''}
    ${d.consolidated.map(n => cardHTML(n)).join('')}`;
}

async function renderHub(tag) {
  const d = await fetch('/api/garden/hub/' + encodeURIComponent(tag)).then(r => r.json());
  $('main').innerHTML = `
    <div class="breadcrumb"><a onclick="show('hubs')">hubs</a> / ${esc(tag)}</div>
    <div class="hub-head">${esc(tag)}</div>
    <div class="hint">${d.nodes.length} memories</div>
    ${d.nodes.map(n => cardHTML(n)).join('')}`;
}

function renderSearch() {
  $('main').innerHTML = `
    <div class="hint">Hybrid recall — dense semantics + sparse keywords + importance. The exact pathway the model uses. (Ctrl+K from anywhere)</div>
    <input id="search-box" class="hbtn" style="width:100%;padding:12px 16px;font-size:15px;text-align:left;background:var(--card);color:var(--ink)"
      placeholder="Ask your second brain anything…" onkeydown="if(event.key==='Enter')doSearch()">
    <div id="search-results" style="margin-top:16px"></div>`;
  $('search-box').focus();
}

async function doSearch() {
  const q = $('search-box').value.trim();
  if (!q) return;
  $('search-results').innerHTML = '<div class="empty">recalling…</div>';
  const d = await fetch('/api/garden/search?q=' + encodeURIComponent(q)).then(r => r.json());
  $('search-results').innerHTML = d.results.length
    ? `<div class="hint">${d.results.length} memories · ${d.mode} recall</div>` +
      d.results.map(n => cardHTML(n, {score: n.score})).join('')
    : '<div class="empty">No memory of that — yet.</div>';
}

// ── State-preserving back (owner ruling fbf7f4350ea9) ───────────────────────
// Every drill-down snapshots the EXACT view it leaves — HTML, scroll position,
// and view name — so Back restores your place without retraveling. The browser
// back button is wired to the same stack via pushState/popstate.
let navStack = [];
let _restoring = false;
function pushView() {
  if (_restoring) return;
  const m = $('main');
  if (!m || !m.innerHTML) return;
  navStack.push({ html: m.innerHTML, y: window.scrollY, v: view });
  if (navStack.length > 30) navStack.shift();
  try { history.pushState({ garden: navStack.length }, ''); } catch (e) {}
}
function goBack() {
  // Route through browser history so the URL/back-button state stays in sync;
  // popstate does the actual restore. Fallback restores directly.
  if (navStack.length && history.state && history.state.garden) { history.back(); return; }
  restoreView();
}
function restoreView() {
  if (!navStack.length) { _restoring = true; try { show('hub'); } finally { _restoring = false; } return; }
  const s = navStack.pop();
  _restoring = true;
  try {
    view = s.v;
    setWayline(view);
    $('main').innerHTML = s.html;
    requestAnimationFrame(() => window.scrollTo(0, s.y));
  } finally { _restoring = false; }
}
window.addEventListener('popstate', () => { if (navStack.length) restoreView(); });

async function openNode(e, id, opts) {
  if (e) e.stopPropagation();
  opts = opts || {};
  // Remember the exact view we're leaving (HTML + scroll + view name) so Back
  // restores your place. Skip on in-place refresh (e.g. reply).
  if (!opts.replace) pushView();
  let d;
  try {
    d = await fetch('/api/garden/node/' + encodeURIComponent(id)).then(r => r.json());
  } catch (err) {
    toast('could not open node (network) — is the server up?');
    return;
  }
  if (!d || !d.node) { toast('node not found: ' + id); return; }
  const n = d.node;
  try {
  const sect = (title, items, opts) => items && items.length
    ? `<div class="section-h">${title}</div>` + items.map(x => cardHTML(x, opts||{})).join('') : '';
  const _isNote   = x => (x.tags || []).includes('annotation');
  const notes     = (d.children || []).filter(_isNote);
  const followers = (d.children || []).filter(x => !_isNote(x));
  $('main').innerHTML = `
    <div class="breadcrumb"><button class="backbtn" onclick="goBack()">← Back</button><span>node ${esc(n.id)}</span></div>
    ${cardHTML(n)}
    ${tagChipsHTML(n.tags)}
    <div class="reply-row">
      <input id="reply-text" placeholder="Add a note — attaches to this node forever"
        onkeydown="if(event.key==='Enter')reply('${jesc(n.id)}')">
      <label class="reply-mem" title="Keep this note as searchable memory — off = quiet margin-note"
        style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);white-space:nowrap">
        <input type="checkbox" id="reply-mem"> remember</label>
      <button class="abtn" onclick="reply('${jesc(n.id)}')">add note</button>
    </div>
    ${sect(icon('note','📝','s16') + ' Notes on this node', notes)}
    ${sect('Reasoning chain (how we got here)', d.chain.slice(0,-1))}
    ${sect('Follows from this', followers)}
    ${sect('Absorbed episodes (dendrites)', d.members)}
    ${sect('Part of consolidated knowledge', d.member_of)}
    ${sect('Semantic neighbors', d.neighbors, {})}
    ${sect('Shared-entity neighbors', d.entity_neighbors, {})}
  `;
  const first = document.querySelector('.card');
  if (first) first.classList.add('expanded');
  window.scrollTo(0, 0);
  } catch (err) {
    toast('could not render node: ' + (err && err.message ? err.message : err));
  }
}

async function reply(id) {
  const el = $('reply-text');
  const text = el ? el.value.trim() : '';
  if (!text) return;
  const memEl = document.getElementById('reply-mem');
  const memory = !!(memEl && memEl.checked);
  await fetch(`/api/garden/node/${encodeURIComponent(id)}/reply`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ text, memory }),
  });
  toast(memory ? '📝 note kept as memory' : '📝 quiet note attached');
  openNode(null, id, {replace:true});
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault(); show('search');
  }
});

show('hub');

// Keep the Hub fresh — the human's curated "live feed". Re-render only when the
// data actually changed (cheap signature) so it never resets your scroll/hover
// for no reason. Hub only (Today is scroll-heavy, left manual); curated, so no
// bash/tool-call noise. The poll also keeps `last_seen` warm so the
// since-last-visit baseline stays stable while you're here.
let _hubSig = '';
setInterval(async () => {
  if (view !== 'hub') return;
  try {
    const d = await fetch('/api/garden/hub').then(r => r.json());
    const slv = d.since_last_visit || {};
    const sig = ((d.just_captured && d.just_captured[0] && d.just_captured[0].id) || '')
              + '|' + (slv.count || 0)
              + '|' + (slv.turns || 0)
              + '|' + ((d.open_items || []).length)
              + '|' + ((d.due && (d.due.overdue || []).length) || 0);
    if (_hubSig && sig !== _hubSig) renderHub_();
    _hubSig = sig;
  } catch (e) {}
}, 15000);
</script>
</body>
</html>"""
