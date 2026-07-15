"""
cairn/vault.py
Append-only, immutable, episodic vault.
Every tool call. Every response. Every thought.
Lossless. Never deletes. Captures the path, not just the destination.
"""
from __future__ import annotations
import json, math, os, re, sqlite3, struct, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path.home() / ".cairn"
DB_PATH    = VAULT_ROOT / "cairn.db"

PHI_INV_SQ = 0.3819660112501051  # 1/phi^2 — fractional golden angle

# Live author identity — your handle from ~/.cairn/me.json, stamped on the SESSION
# row so this machine's live work is its own "galaxy" in the Atlas (and mixes with
# the matching import backfill). Read once per process (hooks are short-lived);
# None when no handle is set, which keeps the old unlabeled behavior. Never leaves
# the machine. Imports set their own account explicitly, so this only tags NEW
# live sessions — never overrides an imported archive's label.
_ACCOUNT_MEMO: list = []
def _me_config() -> dict:
    if _ACCOUNT_MEMO:
        return _ACCOUNT_MEMO[0]
    cfg = {}
    try:
        f = VAULT_ROOT / "me.json"
        if f.exists():
            cfg = json.loads(f.read_text(encoding="utf-8")) or {}
            if not isinstance(cfg, dict):
                cfg = {}
    except Exception:
        cfg = {}
    _ACCOUNT_MEMO.append(cfg)
    return cfg


def _channel_match(session_id: str, channels) -> Optional[str]:
    """Longest-prefix channel route. An mcp-<client>-* id (Codex writing via the
    MCP cairn_note tool = 'mcp-codex-mcp-client-...') is re-matched after
    stripping the leading 'mcp-' so its client-name segment ('codex-') anchors
    to its channel — WITHOUT an unanchored 'codex'-in-id test that could
    false-match. Returns the routed account slug, or None."""
    if not (session_id and isinstance(channels, dict)):
        return None
    candidates = [session_id]
    if session_id.startswith("mcp-"):
        candidates.append(session_id[4:])   # mcp-codex-... -> codex-... (anchored)
    for cand in candidates:
        best, hit = "", None
        for prefix, acct in channels.items():
            if (isinstance(prefix, str) and isinstance(acct, str)
                    and cand.startswith(prefix) and len(prefix) > len(best)):
                best, hit = prefix, acct.strip()[:24]
        if best:
            return hit or None
    return None


def _is_registered_slug(slug: str) -> bool:
    """Is this slug a known/registered account? Gates whether an explicit
    CAIRN_ACCOUNT env value is trusted enough to LOCK — an unvalidated typo must
    be able to label but never lock over hard Desktop proof."""
    try:
        from cairn.accounts import _load_accounts
        if (slug or "").lower() in _load_accounts():
            return True
    except Exception:
        pass
    return False


def _multi_account_machine(maker: str = "Claude") -> bool:
    """True when there's evidence of MORE THAN ONE account for this maker on the
    machine — from Claude Desktop account folders and/or registered stable_ids.
    When true, the single-slot CLI login-id and the machine-global me.json handle
    are ambiguous GUESSES: login-id may LABEL but must not LOCK, and handle must
    not fire as truth (this is the guard that disarms the handle-collision bug).
    Fail-closed: a 2nd Desktop account folder alone (even before accounts.json is
    seeded) counts as multi-account."""
    key = (maker or "").lower()
    n = 0
    try:
        from cairn.accounts import _desktop_store_roots
        seen = set()
        for root in _desktop_store_roots():
            try:
                for d in root.iterdir():
                    if d.is_dir():
                        seen.add(d.name)
            except Exception:
                pass
        n = max(n, len(seen))   # Desktop claude-code-sessions folders are Claude accounts
    except Exception:
        pass
    try:
        raw = json.loads((Path.home() / ".cairn" / "accounts.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cnt = sum(1 for v in raw.values()
                      if isinstance(v, dict) and v.get("stable_id")
                      and str(v.get("maker", "")).lower() == key)
            n = max(n, cnt)
    except Exception:
        pass
    return n >= 2


def _canonical_account(slug):
    """Canonical STORAGE form of an account slug — Title-first casing so one
    identity never splits into two galaxies on letter-case alone (acme ->
    Acme, corp -> Corp). Internal caps survive (BigCo stays);
    None stays None. galaxy_label() still lowercases for its registry/display
    lookup, so display is unaffected — this only unifies what gets STORED in
    sessions.account, and the resolver (below) is untouched, so the locked/proof
    ladder and its tests keep their lowercase slugs."""
    if not slug:
        return slug
    s = str(slug).strip()
    return (s[:1].upper() + s[1:]) if s else slug


def _resolve_account(session_id: str = "") -> "tuple[Optional[str], bool]":
    """Resolve (account_slug, locked) for a session — the label plus its
    CONFIDENCE. locked=True means proven/explicit (validated CAIRN_ACCOUNT env,
    channels route, Desktop cliSessionId proof); a locked stamp is never silently
    overwritten by a later guess. locked=False means a fallback/guess
    (multi-account CLI login-id, guarded handle) that later proof or
    fix-session/doctor can correct. Ladder: env -> channels -> Desktop proof ->
    login-id -> handle -> None. Personal names live in config/env/Desktop store,
    never in source."""
    # 1. CAIRN_ACCOUNT env — explicit override. Locks ONLY if it names a known
    #    account (an unvalidated typo labels but must not lock over hard proof).
    env = os.environ.get("CAIRN_ACCOUNT")
    if env and env.strip():
        slug = env.strip()[:24]
        return slug, _is_registered_slug(slug)
    cfg = _me_config()
    # 2. channels — hard route (keeps codex-/mcp-codex -> a Codex account). Locked.
    hit = _channel_match(session_id, cfg.get("channels"))
    if hit:
        return hit, True
    # 3. Desktop per-session PROOF — the definitive receipt when the Desktop app
    #    has filed this session under an account folder. Locked.
    try:
        from cairn.accounts import desktop_account
        d = desktop_account(session_id)
        if d and d.get("slug"):
            return d["slug"], True
    except Exception:
        pass
    # 4. harness login-id — CLI OAuth / Codex auth. Codex (single-surface) or a
    #    single-account machine is authoritative -> LOCK. But a multi-account
    #    Claude machine has a single-slot ~/.claude.json that lags Desktop
    #    account switching, so it may LABEL but must NOT LOCK (a stale label must
    #    stay correctable by later Desktop proof / fix-session). Ids only.
    try:
        from cairn.accounts import claude_identity, codex_identity, slug_register
        sid = str(session_id or "")
        is_codex = sid.startswith("codex-") or sid.startswith("mcp-codex")
        ident = codex_identity() if is_codex else claude_identity()
        slug = slug_register(ident)
        if slug:
            locked = True if is_codex else (not _multi_account_machine("Claude"))
            return slug, locked
    except Exception:
        pass
    # 5. me.json handle — display/fallback ONLY, and ONLY on a single-account
    #    machine (multi-account disarms it: the handle-collision bug). Never locks.
    h = str(cfg.get("handle") or "").strip()[:24]
    if h and not _multi_account_machine("Claude"):
        return h, False
    # 6. nothing resolved
    return None, False


def _live_account(session_id: str = "") -> Optional[str]:
    """The account SLUG stamped on a session (ladder in _resolve_account). Kept
    returning a bare string for back-compat with callers/tests; the confidence
    bit is _live_account_locked()."""
    return _resolve_account(session_id)[0]


def _live_account_locked(session_id: str = "") -> bool:
    """Whether the resolved account label is LOCKED (proven/explicit) vs a guess.
    A locked stamp is never silently overwritten by a later lower-confidence
    write; only fix-session/doctor (human) moves a locked label."""
    return _resolve_account(session_id)[1]


def _live_harness(session_id: str = "") -> Optional[str]:
    """Which HARNESS captured a session — the source tool (distinct from the
    account = who). Provenance decoration only; never gates anything. Resolution:
      1. CAIRN_HARNESS env — any connector may declare itself (e.g. an MCP server);
      2. session-id convention set at capture: 'codex-*' → 'codex',
         'import-<src>-*' → 'import-<src>';
      3. CLAUDE_SESSION_ID present (Claude Code injects it into hook processes)
         → 'claude-code';
      4. else None → displayed 'unknown' (honest; we don't over-claim).
    Old rows predating the column stay NULL (no backfill — Spec A / A3)."""
    env = os.environ.get("CAIRN_HARNESS")
    if env and env.strip():
        return env.strip()[:24]
    sid = str(session_id or "")
    if sid.startswith("codex-"):
        return "codex"
    if sid.startswith("import-"):
        parts = sid.split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else "import"
    if os.environ.get("CLAUDE_SESSION_ID"):
        return "claude-code"
    return None

# Importance scores by node kind — derived from Generative Agents (Park et al. 2023)
# and LUFY psychological memory model (2024). No LLM ratings needed: kind is a
# reliable proxy for cognitive importance. Warnings are 9 because they represent
# danger signals — the brain's amygdala equivalent. Decisions are 8 because they
# represent resolved uncertainty. Open items are 7 because they demand future action.
KIND_IMPORTANCE: dict[str, int] = {
    "warning":          9,
    "decision":         8,
    "compress_summary": 8,  # highest-leverage capture point — session state before collapse
    "procedure":        8,  # crystallized how-to knowledge — session-independent (basal ganglia)
    "artifact":         8,  # registered document card — you bothered to catalog it
    "open_item":        7,
    "insight":          7,
    "idea":             6,  # sparks for later — never urgent, never lost, resurface via rotation
    "resolved":         6,
    "hypothesis":       6,
    "blocker":          6,
    "context_stamp":    5,
    "question":         5,
    "conversation_turn":4,
    "tool_call":        3,
    "file_chunk":       3,  # ingested file content — retrieval fuel, not a thought
    "interrupt":        2,
}

_STATUS_OPENERS = frozenset({
    "done", "ok", "okay", "k", "kk", "on it", "all set", "all banked",
    "banked", "report's in", "reports in", "the report's in", "the reports in",
    "here's the run", "heres the run", "got it", "sure", "will do", "roger",
    "roger that", "ack", "acked", "np", "no problem", "sounds good", "yep",
    "yup", "yes", "yeah", "done deal", "all done", "finished", "complete",
    "on my way", "omw", "10-4", "copy", "copy that", "understood", "noted",
})


def _gist_from_text(text: str) -> str:
    """
    Fuzzy-trace gist (Brainerd & Reyna): a ~12-word distillation stored
    alongside the verbatim episodic text. Gist traces are what survive —
    injection sends gists by default; the verbatim is on demand via
    `cairn read <id>` (CLI) / cairn_read (MCP). (`chain` is the parent-path
    walker, not a text surface.)

    Heuristic, zero-token: cut at the first sentence boundary under 90 chars,
    then cap at 14 words. If that first fragment is a pure status/ack opener
    (a bare "Done." or "On it —"), skip it and take the first SUBSTANTIVE
    sentence instead — otherwise the gist would just read "Done". Compile-time
    refinement can overwrite later.
    """
    src = (text or "").replace("\n", " ").strip()
    if not src:
        return ""

    seps = (". ", " — ", "; ", " | ")

    def _first_fragment(s: str):
        """(head, remainder) split at the first sentence boundary under 90
        chars, using the ORIGINAL separator priority (list order, break on the
        first hit — NOT earliest position), so non-status text cuts exactly as
        before. remainder is '' when no boundary fires."""
        for sep in seps:
            i = s.find(sep)
            if 0 < i < 90:
                return s[:i], s[i + len(sep):].lstrip()
        return s, ""

    # Peel leading status/ack fragments until substantive text — but never
    # blank the node: if the WHOLE text is openers, keep the first fragment.
    working = src
    head, rest = _first_fragment(working)
    while rest and head.strip(" .,!?—-:;|").lower() in _STATUS_OPENERS:
        working = rest
        head, rest = _first_fragment(working)

    src = head
    words = " ".join(src.split()[:14])
    if len(words) <= 110:
        return words
    # never chop mid-word: stop at the last whole word and say so.
    cut = words.rfind(" ", 0, 110)
    return (words[:cut] if cut > 40 else words[:110]) + " …"


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id             TEXT PRIMARY KEY,
    session        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    tool           TEXT,
    query          TEXT,
    output_preview TEXT,
    result_count   INTEGER,
    latency_ms     INTEGER,
    parent         TEXT,
    agent_id       TEXT NOT NULL DEFAULT 'unknown',
    model          TEXT NOT NULL DEFAULT 'unknown',
    agent_role     TEXT NOT NULL DEFAULT 'worker',
    speaker        TEXT NOT NULL DEFAULT 'agent',
    memory_tier    INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active',
    flagged        INTEGER NOT NULL DEFAULT 0,
    trust          INTEGER NOT NULL DEFAULT 1,
    timestamp      TEXT NOT NULL,
    tags           TEXT NOT NULL DEFAULT '[]',
    episodic_text  TEXT,
    embedding      BLOB,
    importance     INTEGER NOT NULL DEFAULT 5,
    stability_days REAL    NOT NULL DEFAULT 1.0,
    last_injected  TEXT,
    gist           TEXT
);

-- Hot-path indexes for the nodes table. The query / inject / index-build paths
-- all filter by status (active vs void), memory_tier, and session — usually
-- ordered by timestamp. Without these a fresh install full-scans nodes on every
-- hook. IF NOT EXISTS is idempotent (a no-op on DBs that already carry them);
-- SCHEMA runs on every open, so this covers fresh and existing DBs alike.
CREATE INDEX IF NOT EXISTS idx_nodes_status_ts   ON nodes(status, timestamp);
CREATE INDEX IF NOT EXISTS idx_nodes_tier_status ON nodes(memory_tier, status);
CREATE INDEX IF NOT EXISTS idx_nodes_session_ts  ON nodes(session, timestamp);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    compiled_at TEXT,
    node_count  INTEGER DEFAULT 0
);

-- Golden-angle feedback loop (schedule.py PositionRecord persistence).
-- Tracks where each node landed in context and whether the model actually
-- used it (appeared in compiled output). middle_hits without compiled_hits
-- = underattended → front-loaded next session. This is what makes the
-- golden angle self-correcting instead of merely fair.
CREATE TABLE IF NOT EXISTS position_records (
    node_id       TEXT PRIMARY KEY,
    middle_hits   INTEGER NOT NULL DEFAULT 0,
    compiled_hits INTEGER NOT NULL DEFAULT 0,
    total_loads   INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);

-- Spark-board dismissals: "stop resurfacing this" is UI state, not memory.
-- The node itself is never touched — it stays searchable, injectable,
-- consolidatable. It just leaves the inspiration rotation.
CREATE TABLE IF NOT EXISTS spark_dismissed (
    node_id      TEXT PRIMARY KEY,
    dismissed_at TEXT
);

-- Set-aside state: archive + snooze are HUMAN attention flags, NOT void. The node
-- stays active (the AI still sees it; a human mark is a fallible signal) — it just
-- leaves the human's attention surfaces. Reversible: "restore" = delete the row.
-- (void is one-way per immutable_nodes, so it cannot back a reversible archive.)
CREATE TABLE IF NOT EXISTS archived (
    node_id     TEXT PRIMARY KEY,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS snoozed (
    node_id    TEXT PRIMARY KEY,
    until      TEXT,
    snoozed_at TEXT
);

-- Typed graph edges, precomputed by cairn/edges.py (cairn edges / cairn sleep).
-- type: chain (parent lineage) | dendrite (consolidation membership) |
--       semantic (embedding kNN). tier applies to semantic only:
--       strong (>=0.78) | medium (>=0.70) | weak (>=0.62).
-- Derived data — safe to rebuild from nodes at any time (not append-only).
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    type       TEXT NOT NULL,
    tier       TEXT,
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TEXT,
    PRIMARY KEY (src, dst, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

-- The attention ledger: a write-through receipt for EVERY memory surfaced to
-- any model through any channel (hook push, MCP/CLI fetch pull, drift).
-- Written at the moment of showing — survives crashed sessions, unlike
-- in-RAM counters. compile marks `cited` when a shown node's text appears in
-- the session's compiled output: shown-but-never-cited at middle positions
-- gets front-loaded next time; cited-from-the-dead-zone proves strength.
CREATE TABLE IF NOT EXISTS attention_ledger (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id  TEXT NOT NULL,
    session  TEXT,
    channel  TEXT NOT NULL,
    position INTEGER,
    trigger  TEXT,
    shown_at TEXT NOT NULL,
    cited    INTEGER NOT NULL DEFAULT 0,
    cited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_node  ON attention_ledger(node_id);
CREATE INDEX IF NOT EXISTS idx_ledger_shown ON attention_ledger(shown_at);

CREATE TRIGGER IF NOT EXISTS immutable_nodes
BEFORE UPDATE ON nodes
WHEN NOT (
    -- allowed: status transition to void (one-way, content unchanged)
    (OLD.status != 'void' AND NEW.status = 'void'
     AND NEW.id = OLD.id AND NEW.kind = OLD.kind
     AND NEW.flagged = OLD.flagged)
    OR
    -- allowed: flag transition 0->1 (one-way, content and status unchanged)
    (NEW.flagged = 1 AND OLD.flagged = 0
     AND NEW.id = OLD.id AND NEW.status = OLD.status
     AND NEW.kind = OLD.kind)
    OR
    -- allowed: embedding update only (NULL -> non-NULL)
    (NEW.id = OLD.id AND NEW.status = OLD.status
     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
     AND NEW.query = OLD.query AND NEW.model = OLD.model
     AND NEW.speaker = OLD.speaker AND NEW.memory_tier = OLD.memory_tier
     AND OLD.embedding IS NULL AND NEW.embedding IS NOT NULL)
    OR
    -- allowed: memory_tier change (promote/demote)
    (NEW.id = OLD.id AND NEW.status = OLD.status
     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
     AND NEW.query = OLD.query AND NEW.model = OLD.model
     AND NEW.speaker = OLD.speaker
     AND NEW.memory_tier != OLD.memory_tier
     AND NEW.embedding IS OLD.embedding)
    OR
    -- allowed: scheduling metadata updates (importance, stability_days, last_injected)
    -- These are query-routing and recall-scheduling signals, not episodic content.
    -- Protects all content fields. Allows any scheduling field to change.
    -- This branch covers: importance backfill, FSRS stability updates, tier-aware
    -- last_injected timestamps — all of which are derived, not authored.
    (NEW.id = OLD.id AND NEW.status = OLD.status
     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
     AND NEW.query IS OLD.query AND NEW.output_preview IS OLD.output_preview
     AND NEW.model = OLD.model AND NEW.speaker = OLD.speaker
     AND NEW.memory_tier = OLD.memory_tier
     AND NEW.embedding IS OLD.embedding
     AND NEW.tags = OLD.tags)
)
BEGIN
    SELECT RAISE(ABORT, 'nodes are immutable — append only');
END;
"""


@dataclass
class MicroNode:
    session:        str
    kind:           str
    id:             str           = field(default_factory=lambda: uuid.uuid4().hex[:12])
    tool:           Optional[str] = None
    query:          Optional[str] = None
    output_preview: Optional[str] = None
    result_count:   Optional[int] = None
    latency_ms:     Optional[int] = None
    parent:         Optional[str] = None
    agent_id:       str           = "unknown"
    model:          str           = "unknown"
    agent_role:     str           = "worker"   # observer|worker|auditor|curator
    speaker:        str           = "agent"    # agent|user — for conversation_turn nodes
    memory_tier:    int           = 1          # 0=hot(in-context) 1=warm(recent) 2=cold(archival)
    status:         str           = "active"
    flagged:        bool          = False
    trust:          int           = 1
    timestamp:      str           = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags:           list          = field(default_factory=list)
    importance:     Optional[int] = None       # 1-10; computed at write if None
    # per-turn token usage — set on agent conversation_turn nodes by turn_hook
    # (read from the transcript); null elsewhere. For spend analysis.
    tokens_in:          Optional[int] = None   # new (uncached) input tokens
    tokens_out:         Optional[int] = None   # output tokens (generation)
    tokens_cache_read:  Optional[int] = None   # cached context read (bloat signal)
    tokens_cache_write: Optional[int] = None   # cache creation tokens
    # 5a: the tool calls that fired DURING this turn, baked into the agent
    # conversation_turn at Stop as a JSON list ({tool,query,preview,latency_ms,
    # result_count,ts}). One exchange carries its own tools instead of minting
    # a node per call. Null on every other kind.
    tool_calls:         Optional[str] = None
    # Full-fidelity turn text for imports whose text overflows output_preview's
    # display cap (TRUNC_PREVIEW). The display fields (query/output_preview) keep
    # their caps for UI/embedding size conventions; the COMPLETE text lands in the
    # derived episodic_text via to_episodic_text() so nothing past the cap is lost
    # (the vault's PAGE ONE data-loss warning). Set ONLY when text exceeds the
    # cap — short turns leave this None and get the normal capped episodic_text.
    # All three turn writers set it the same way (capture.write_turn, codex_hook,
    # the importers): overflow-only. Scrubbed at the write-gate like every other
    # text field, so the frozen episodic_text stays secret-free.
    episodic_full:      Optional[str] = None

    def to_episodic_text(self, parent_hint: str = "") -> str:
        """
        Synthesize a natural-language memory for this node.

        Encodes HOW + WHO + WHAT + WHY into one searchable sentence.
        Chain-aware: parent_hint contextualizes this node within its chain.

        The design principle: embed the MEANING of what happened, not a
        template of the fields. Decisions lead with their conclusion.
        Tool calls lead with what was discovered. Questions stay as questions.

        Model identity stays in the text so semantic search can answer:
          "where did gpt-4o struggle?"     → finds gpt-4o + struggle signals
          "what did the auditor flag?"     → finds auditor role + flagged nodes
          "what changed on the auth flow?" → finds decision nodes near auth files
        """
        TOOL_VERBS = {
            "Grep":      "searched for",
            "Read":      "read",
            "Glob":      "listed files matching",
            "Edit":      "edited",
            "Write":     "wrote",
            "Bash":      "ran",
            "WebSearch": "searched the web for",
            "WebFetch":  "fetched",
        }

        # Tools where rc<=2 is a genuine struggle — they're expected to return many results
        HIGH_VOLUME_TOOLS = {"Grep", "Glob", "WebSearch"}

        actor     = self.model if self.model not in ("unknown", None, "") else "agent"
        role_note = f" [{self.agent_role}]" if self.agent_role not in ("worker", None, "") else ""

        # ── struggle signals (tool-aware thresholds) ──────────────────────────
        slow   = self.latency_ms is not None and self.latency_ms > 2000
        empty  = self.result_count is not None and self.result_count == 0
        sparse = (self.result_count is not None
                  and 0 < self.result_count <= 2
                  and self.tool in HIGH_VOLUME_TOOLS)

        struggle_parts = []
        if slow:   struggle_parts.append(f"{self.latency_ms}ms")
        if empty:  struggle_parts.append("found nothing")
        if sparse: struggle_parts.append(f"only {self.result_count} result(s)")
        struggle_tag = f" [struggled: {', '.join(struggle_parts)}]" if struggle_parts else ""

        chain_tag = f" — following: {parent_hint}" if parent_hint else ""

        # ── per-kind synthesis ────────────────────────────────────────────────

        if self.kind == "context_stamp":
            body = self.output_preview or self.query or ""
            return f"Session context [{actor}]: {body[:500]}{chain_tag}"

        if self.kind in ("decision", "insight"):
            conclusion = self.query or ""
            after      = f" — after: {parent_hint}" if parent_hint else ""
            return f"{actor}{role_note} decided: {conclusion[:400]}{after}"

        if self.kind == "hypothesis":
            return (f"{actor}{role_note} hypothesized: {(self.query or '')[:400]}"
                    f"{chain_tag}")

        if self.kind == "question":
            ctx = (f" — context: {self.output_preview[:200]}"
                   if self.output_preview else "")
            return f"{actor}{role_note} raised question: {(self.query or '')[:400]}{ctx}"

        if self.kind == "open_item":
            detail = f" — {self.output_preview[:200]}" if self.output_preview else ""
            return f"open item [{actor}]: {(self.query or '')[:400]}{detail}"

        if self.kind == "blocker":
            reason = (f" — reason: {self.output_preview[:200]}"
                      if self.output_preview else "")
            return (f"{actor}{role_note} blocked on: {(self.query or '')[:400]}"
                    f"{reason}")

        if self.kind == "resolved":
            return (f"{actor}{role_note} resolved: {(self.query or '')[:400]}"
                    f"{chain_tag}")

        if self.kind == "procedure":
            detail = f" — {self.output_preview[:300]}" if self.output_preview else ""
            return f"procedure [{actor}]: {(self.query or '')[:400]}{detail}"

        if self.kind == "idea":
            detail = f" — {self.output_preview[:200]}" if self.output_preview else ""
            return f"{actor}{role_note} had an idea: {(self.query or '')[:400]}{detail}"

        if self.kind == "file_chunk":
            # query carries "filename [i/n]: head"; preview carries the chunk.
            # Embed file identity + content so "where is X implemented" works.
            return f"file content {self.query or ''} — {(self.output_preview or '')[:400]}"

        if self.kind == "warning":
            return (f"{actor}{role_note} warned: {(self.query or '')[:400]}"
                    f"{chain_tag}")

        if self.kind == "conversation_turn":
            speaker_label = "user" if self.speaker == "user" else actor
            # episodic_full carries the COMPLETE turn text whenever a writer's
            # text overflowed output_preview's display cap — live Claude capture
            # (capture.write_turn), the Codex hook, and the importers all use the
            # same pattern — so the tail past the cap survives verbatim (the
            # full-fidelity trace). When it's None (a short turn) fall back to
            # the capped display body, exactly as before.
            if self.episodic_full:
                return f"{speaker_label} said: {self.episodic_full}"
            body = self.output_preview or self.query or ""
            return f"{speaker_label} said: {body[:500]}"

        if self.kind == "interrupt":
            return f"session event: {(self.query or '')[:200]}"

        if self.kind == "tool_call" and self.tool:
            verb   = TOOL_VERBS.get(self.tool, f"used {self.tool}")
            sought = self.query or ""
            found  = self.output_preview or ""

            if found and sought:
                text = (f"{actor}{role_note} {verb} {sought[:200]}"
                        f" — found: {found[:300]}")
            elif found:
                text = f"{actor}{role_note} {verb} — found: {found[:400]}"
            elif sought:
                text = f"{actor}{role_note} {verb} {sought[:400]}"
            else:
                text = f"{actor}{role_note} {verb}"

            return text + struggle_tag + chain_tag

        # ── fallback ──────────────────────────────────────────────────────────
        content = self.output_preview or self.query or ""
        return (f"{actor}{role_note} {self.kind}: {content[:400]}"
                f"{struggle_tag}{chain_tag}")


class Vault:
    """
    The store. Append-only, immutable, episodic.
    SQLite under the hood — zero infrastructure, runs anywhere.
    """

    def __init__(self, db_path: Path | None = None):
        VAULT_ROOT.mkdir(parents=True, exist_ok=True)
        # Resolve at CALL time, never bind the default in the signature: a default
        # of DB_PATH froze the real-vault path at import, so monkeypatching
        # VAULT_ROOT in tests silently failed and test writes leaked into the live
        # vault. Resolving VAULT_ROOT here makes isolation actually work.
        self.db_path = Path(db_path) if db_path is not None else VAULT_ROOT / "cairn.db"
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # Concurrency: the vault is a multi-writer brain — many models/agents
        # (Claude, Codex, open models) + the dashboard can read/write at once.
        # WAL lets readers and the writer not block each other (default 'delete'
        # mode takes an exclusive lock that freezes the whole DB on every write);
        # busy_timeout makes a contending writer wait-and-retry instead of
        # erroring 'database is locked'; synchronous=NORMAL is WAL's safe, fast
        # companion. These make concurrent multi-model writes actually safe.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass  # never block vault open on PRAGMA (e.g. read-only media)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._embedder = None
        self._index    = None   # lazy EmbeddingIndex — built on first query

    def _migrate(self) -> None:
        """
        Add columns + refresh triggers on every open. Safe and idempotent.

        Column migrations: ALTER TABLE ADD COLUMN with try/except per column.
        Trigger migration: drop + recreate so updated allowed-mutation rules
        take effect without requiring a schema version bump.
        """
        migrations = [
            "ALTER TABLE nodes ADD COLUMN speaker       TEXT    NOT NULL DEFAULT 'agent'",
            "ALTER TABLE nodes ADD COLUMN memory_tier   INTEGER NOT NULL DEFAULT 1",
            # Upgrade 1: importance scoring (LUFY + Generative Agents, 2023-2024)
            # Computed at write time from kind. Enables composite query ranking.
            "ALTER TABLE nodes ADD COLUMN importance     INTEGER NOT NULL DEFAULT 5",
            # Upgrade 2: FSRS scheduling (Jarrett Ye 2022, 700M Anki review benchmark)
            # stability_days: time in days before retrievability drops below 90%.
            # last_injected: ISO timestamp of last injection — used for overdue scoring.
            "ALTER TABLE nodes ADD COLUMN stability_days REAL    NOT NULL DEFAULT 1.0",
            "ALTER TABLE nodes ADD COLUMN last_injected  TEXT",
            # Upgrade 3: gist layer (fuzzy-trace theory, Brainerd & Reyna).
            # Verbatim trace = episodic_text (~100 tokens, full fidelity).
            # Gist trace = ~12-word distillation (survives compression, injected by default).
            "ALTER TABLE nodes ADD COLUMN gist           TEXT",
            # Upgrade 4: community (cairn/edges.py label propagation).
            # 'comm_id|human label' — derived, rewritten on every edge build.
            "ALTER TABLE nodes ADD COLUMN community      TEXT",
            # Upgrade 5: provenance — which account/source a session came from
            # (imports from multiple Claude/GPT accounts). Lives on sessions
            # because node tags are frozen by the append-only trigger.
            "ALTER TABLE sessions ADD COLUMN account     TEXT",
            # Upgrade 5b (attribution v2 / A3): which HARNESS captured a session
            # (claude-code / codex / import-<src>; NULL = unknown for old rows).
            # First-write-wins like account; derived at INSERT by _live_harness.
            "ALTER TABLE sessions ADD COLUMN harness     TEXT",
            # Upgrade 5c (attribution v2 / confidence): is the account label
            # LOCKED (proven/explicit — validated CAIRN_ACCOUNT env, channels,
            # desktop cliSessionId proof, explicit import) vs unlocked (a guess/
            # fallback — active_accounts default, multi-account CLI login-id,
            # guarded handle). A locked stamp is never silently overwritten by a
            # later guess; only fix-session/doctor (human) moves a locked label.
            # 0 = unlocked/guess. Derived at INSERT by _live_account_locked.
            "ALTER TABLE sessions ADD COLUMN account_locked INTEGER NOT NULL DEFAULT 0",
            # Upgrade 6: atlas coordinates — precomputed stable spatial layout
            # (fractal phyllotaxis, cairn/edges.py). Derived, rewritten on
            # every edge build. The full-vault map draws these on canvas.
            "ALTER TABLE nodes ADD COLUMN map_x          REAL",
            "ALTER TABLE nodes ADD COLUMN map_y          REAL",
            # Upgrade 7: per-turn token accounting (turn_hook reads transcript
            # usage). See where spend goes — output = generation cost,
            # cache_read = carried context (the bloat memory exists to cut).
            "ALTER TABLE nodes ADD COLUMN tokens_in          INTEGER",
            "ALTER TABLE nodes ADD COLUMN tokens_out         INTEGER",
            "ALTER TABLE nodes ADD COLUMN tokens_cache_read  INTEGER",
            "ALTER TABLE nodes ADD COLUMN tokens_cache_write INTEGER",
            # Upgrade 8 (5a): tool calls baked into the agent turn as a JSON
            # list — one exchange = one node, ending the node-per-tool-call
            # explosion. Written once at INSERT (Stop hook); never UPDATEd, so
            # the append-only trigger needs no new branch for it.
            "ALTER TABLE nodes ADD COLUMN tool_calls         TEXT",
        ]
        for sql in migrations:
            try:
                self.conn.execute(sql)
            except Exception:
                pass  # column already exists — idempotent

        # Refresh immutable trigger FIRST — the new scheduling-metadata branch
        # (which allows importance/stability_days/last_injected changes) must be
        # installed before we run the importance backfill below. The old trigger
        # would block those UPDATEs since it has no branch for them.
        # The append-only guarantee — enforced in the DB, not just by convention.
        # An UPDATE is allowed ONLY if it matches a known-legitimate mutation
        # below AND leaves IDENTITY + CONTENT + chain frozen: query,
        # output_preview, episodic_text, parent never change (verified: no code
        # path UPDATEs them), and gist only NULL→value (the one-time backfill).
        # This closes the "content rides a void/flag/embed/tier" poisoning hole.
        try:
            self.conn.execute("DROP TRIGGER IF EXISTS immutable_nodes")
            self.conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS immutable_nodes
                BEFORE UPDATE ON nodes
                WHEN NOT (
                    -- (1) active → void (curator retirement)
                    (OLD.status != 'void' AND NEW.status = 'void'
                     AND NEW.id = OLD.id AND NEW.kind = OLD.kind
                     AND NEW.flagged = OLD.flagged
                     AND NEW.query IS OLD.query
                     AND NEW.output_preview IS OLD.output_preview
                     AND NEW.episodic_text IS OLD.episodic_text
                     AND NEW.gist IS OLD.gist AND NEW.parent IS OLD.parent)
                    OR
                    -- (2) flag for review (0 → 1)
                    (NEW.flagged = 1 AND OLD.flagged = 0
                     AND NEW.id = OLD.id AND NEW.status = OLD.status
                     AND NEW.kind = OLD.kind
                     AND NEW.query IS OLD.query
                     AND NEW.output_preview IS OLD.output_preview
                     AND NEW.episodic_text IS OLD.episodic_text
                     AND NEW.gist IS OLD.gist AND NEW.parent IS OLD.parent)
                    OR
                    -- (3) embedding backfill (NULL → vector)
                    (NEW.id = OLD.id AND NEW.status = OLD.status
                     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
                     AND NEW.query IS OLD.query
                     AND NEW.output_preview IS OLD.output_preview
                     AND NEW.episodic_text IS OLD.episodic_text
                     AND NEW.gist IS OLD.gist AND NEW.parent IS OLD.parent
                     AND NEW.model = OLD.model AND NEW.speaker = OLD.speaker
                     AND NEW.memory_tier = OLD.memory_tier
                     AND OLD.embedding IS NULL AND NEW.embedding IS NOT NULL)
                    OR
                    -- (4) memory-tier change
                    (NEW.id = OLD.id AND NEW.status = OLD.status
                     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
                     AND NEW.query IS OLD.query
                     AND NEW.output_preview IS OLD.output_preview
                     AND NEW.episodic_text IS OLD.episodic_text
                     AND NEW.gist IS OLD.gist AND NEW.parent IS OLD.parent
                     AND NEW.model = OLD.model AND NEW.speaker = OLD.speaker
                     AND NEW.memory_tier != OLD.memory_tier
                     AND NEW.embedding IS OLD.embedding)
                    OR
                    -- (5) scheduling metadata + derived graph fields (importance,
                    --     stability_days, last_injected, community, map_x/map_y,
                    --     timestamp) + gist backfill (NULL→value). Content frozen.
                    (NEW.id = OLD.id AND NEW.status = OLD.status
                     AND NEW.flagged = OLD.flagged AND NEW.kind = OLD.kind
                     AND NEW.query IS OLD.query
                     AND NEW.output_preview IS OLD.output_preview
                     AND NEW.episodic_text IS OLD.episodic_text
                     AND NEW.parent IS OLD.parent
                     AND NEW.model = OLD.model AND NEW.speaker = OLD.speaker
                     AND NEW.memory_tier = OLD.memory_tier
                     AND NEW.embedding IS OLD.embedding AND NEW.tags = OLD.tags
                     AND (NEW.gist IS OLD.gist
                          OR (OLD.gist IS NULL AND NEW.gist IS NOT NULL)))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'nodes are immutable — append only');
                END;
            """)
            self.conn.commit()
        except Exception:
            pass

        # Fail LOUD if the guard didn't install. The append-only law IS the
        # product; a missing trigger = a silently mutable vault. Refuse to
        # operate without the guarantee rather than corrupt trust in silence.
        guard = self.conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='trigger' AND name='immutable_nodes'"
        ).fetchone()
        if not guard:
            raise RuntimeError(
                "cairn: FATAL — append-only guard 'immutable_nodes' is missing; "
                "refusing to open a vault without write protection. Restore from "
                "a backup (cairn backup) or recreate the schema.")

        # Backfill importance for existing nodes.
        # Re-derive from kind. Safe now that the new trigger is installed.
        # Only touches nodes at default importance=5 whose kind maps elsewhere.
        try:
            for kind, score in KIND_IMPORTANCE.items():
                if score != 5:
                    self.conn.execute(
                        "UPDATE nodes SET importance=? WHERE kind=? AND importance=5",
                        (score, kind)
                    )
            self.conn.commit()
        except Exception:
            pass

        # Backfill gists for pre-gist-layer nodes (one-time, idempotent).
        try:
            rows = self.conn.execute(
                "SELECT id, kind, query, output_preview, episodic_text "
                "FROM nodes WHERE gist IS NULL"
            ).fetchall()
            for r in rows:
                g = _gist_from_text(r["query"] or r["output_preview"]
                                    or r["episodic_text"] or "")
                if g:
                    self.conn.execute(
                        "UPDATE nodes SET gist=? WHERE id=?", (g, r["id"])
                    )
            if rows:
                self.conn.commit()
        except Exception:
            pass

    def backup(self, dest: Optional[Path] = None) -> Path:
        """Append-only safety net: snapshot the live vault to a timestamped file
        via SQLite's online backup (consistent even with the DB open under WAL),
        then verify the copy with integrity_check. Returns the backup path. The
        vault is irreplaceable — run this before any migration or bulk op."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = Path(dest) if dest else self.db_path.parent / f"cairn.db.bak-{ts}"
        bck = sqlite3.connect(str(dest))
        try:
            self.conn.backup(bck)              # online — atomic + consistent
            row = bck.execute("PRAGMA integrity_check").fetchone()
        finally:
            bck.close()
        if not row or row[0] != "ok":
            raise RuntimeError(f"cairn: backup integrity check FAILED → {row}")
        return dest

    # ── write ──────────────────────────────────────────────────────────────────

    def _compute_importance(self, node: MicroNode) -> int:
        """
        Derive importance 1–10 at write time. No LLM needed.

        Based on three signals, ordered by certainty:
          1. kind  — the strongest signal. A warning IS more important than a tool_call.
                     Derived from KIND_IMPORTANCE table (Generative Agents / LUFY research).
          2. trust — depresses importance for low-confidence nodes (trust < 0 → -2)
          3. flagged — boosts importance by 1 (manually flagged = explicitly noteworthy)

        Caller can override by setting node.importance directly before writing.
        """
        score = KIND_IMPORTANCE.get(node.kind, 5)
        if node.trust is not None and node.trust < 0:
            score = max(1, score - 2)
        if node.flagged:
            score = min(10, score + 1)
        return score

    def _check_near_duplicate(self, node: MicroNode) -> Optional[str]:
        """
        SSGM-inspired pre-write duplicate detection. Warns (never blocks) when
        a semantically-near node of the same kind already exists in the vault.

        Uses keyword Jaccard as a fast proxy (no embedding needed at write time).
        Threshold 0.60 — tuned to catch true duplicates, not topic siblings.

        Returns a warning string if a near-duplicate is found, else None.
        """
        if node.kind not in ('decision', 'open_item', 'warning'):
            return None
        if not node.query or len(node.query) < 20:
            return None

        # Stopwords — common English noise that inflates false-positive Jaccard
        _STOP = {
            "this", "that", "with", "from", "have", "will", "been", "were",
            "they", "them", "when", "than", "then", "what", "also", "into",
            "only", "after", "over", "more", "some", "such", "both", "each",
            "most", "just", "must", "should", "would", "could", "does", "here",
            "there", "where", "which", "their", "about", "above", "using",
            "used", "need", "make", "made", "also", "always", "never",
        }

        def _kw(text: str) -> set[str]:
            return {w.lower().strip(".,;:!?()[]") for w in text.split()
                    if len(w) >= 4 and not w.startswith("-")
                    and w.lower().strip(".,;:!?()[]") not in _STOP}

        # Build keyword set from the new node's query
        words = _kw(node.query)
        if len(words) < 3:
            return None

        existing = self.conn.execute(
            "SELECT id, session, query FROM nodes WHERE kind=? AND status='active'",
            (node.kind,)
        ).fetchall()

        best_match, best_score = None, 0.0
        for e in existing:
            if not e["query"]:
                continue
            ewords = _kw(e["query"])
            if not ewords:
                continue
            union = words | ewords
            jaccard = len(words & ewords) / len(union)
            if jaccard > best_score:
                best_score = jaccard
                best_match = e

        if best_match and best_score >= 0.45:  # tuned: catches semantic duplicates, avoids topic siblings
            sess_short = (best_match["session"] or "").rsplit("-20", 1)[0][-20:]
            return (f"near-duplicate detected (Jaccard={best_score:.2f}): "
                    f"[{best_match['id'][:10]}] in [{sess_short}]: "
                    f"'{(best_match['query'] or '')[:60]}'")
        return None

    def write(self, node: MicroNode, commit: bool = True) -> MicroNode:
        # ── 0. Secret redaction — THE write-gate (append-only safety). EVERY
        # writer funnels through write() — cairn_note, importer, MCP, backfill,
        # distill, consolidate, the hooks — so scrubbing the source text fields
        # HERE is the single chokepoint: the episodic_text + gist derived below
        # inherit the cleaned text, leaving no write path that can leak a secret
        # into the append-only store (where void hides but never deletes).
        # Opt-out: CAIRN_NO_REDACT=1 (warned once, never silent).
        # Fail CLOSED — if the redactor can't load, suppress rather than leak.
        if os.environ.get("CAIRN_NO_REDACT") == "1":
            if not getattr(Vault, "_no_redact_warned", False):
                import sys as _sys
                print("cairn: WARNING — CAIRN_NO_REDACT=1; secrets are NOT "
                      "scrubbed before write", file=_sys.stderr)
                Vault._no_redact_warned = True
        else:
            try:
                from cairn.redact import scrub
                node.query          = scrub(node.query)
                node.output_preview = scrub(node.output_preview)
                node.episodic_full  = scrub(node.episodic_full)
                node.tool_calls     = scrub(node.tool_calls)
                node.tags           = [scrub(t) for t in (node.tags or [])]
            except Exception:
                node.query          = "[capture suppressed — redactor unavailable]"
                node.output_preview = "[capture suppressed — redactor unavailable]"
                node.episodic_full  = None
                node.tool_calls     = None
                node.tags           = []

        # ── 1. Compute importance if not explicitly set ───────────────────────
        if node.importance is None:
            node.importance = self._compute_importance(node)

        # ── 2. Pre-write duplicate detection (SSGM Truth Maintenance) ─────────
        # Warns to stderr — never blocks. The append-only log is the authority;
        # the warning is for the agent to act on, not the vault to enforce.
        dup_warn = self._check_near_duplicate(node)
        if dup_warn:
            import sys as _sys
            print(f"cairn: WARNING — {dup_warn}", file=_sys.stderr)

        # ── 3. Chain-aware episodic text ──────────────────────────────────────
        parent_hint = ""
        if node.parent:
            p = self.get(node.parent)
            if p:
                pkind = p["kind"]
                pq    = (p["query"] or "")[:80]
                parent_hint = f"{pkind}: {pq}"
        etext = node.to_episodic_text(parent_hint=parent_hint)

        # ── 3.5 Gist trace (fuzzy-trace dual representation) ─────────────────
        gist = _gist_from_text(node.query or node.output_preview or etext)

        # ── 4. Insert ─────────────────────────────────────────────────────────
        self.conn.execute("""
            INSERT INTO nodes
              (id, session, kind, tool, query, output_preview,
               result_count, latency_ms, parent,
               agent_id, model, agent_role, speaker, memory_tier,
               status, flagged, trust, timestamp, tags, episodic_text,
               importance, gist,
               tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,
               tool_calls)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            node.id, node.session, node.kind, node.tool, node.query,
            node.output_preview, node.result_count, node.latency_ms,
            node.parent,
            node.agent_id, node.model, node.agent_role,
            node.speaker, node.memory_tier,
            node.status, int(node.flagged), node.trust,
            node.timestamp, json.dumps(node.tags), etext,
            node.importance, gist,
            node.tokens_in, node.tokens_out, node.tokens_cache_read, node.tokens_cache_write,
            node.tool_calls
        ))
        _acct, _locked = _resolve_account(node.session)
        _acct = _canonical_account(_acct)     # unify letter-case → one galaxy per identity
        # First-write stamps (account, account_locked). On conflict: a LOCKED
        # incoming write (proven/explicit) may upgrade an unlocked (guessed)
        # label, but a guess never overwrites a locked stamp and a guess never
        # overwrites another guess — so a stale guess can't refreeze and Desktop
        # proof can heal a provisional label at Stop-time. Only fix-session/
        # doctor (human) moves a locked label.
        self.conn.execute("""
            INSERT INTO sessions (id, started_at, node_count, account, harness, account_locked)
            VALUES (?,?,1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                node_count = node_count + 1,
                account = CASE WHEN account_locked = 0 AND excluded.account_locked = 1
                               THEN excluded.account ELSE account END,
                account_locked = MAX(account_locked, excluded.account_locked)
        """, (node.session, node.timestamp, _acct,
              _live_harness(node.session), 1 if _locked else 0))
        # commit=False lets a bulk caller (the importer) batch a whole
        # conversation into one transaction — atomic + crash-safe resume — and
        # avoid one fsync per turn across a multi-GB backfill.
        if commit:
            self.conn.commit()
        return node

    def void(self, node_id: str) -> None:
        """Only allowed state mutation. Marks node invalidated, never deletes."""
        self.conn.execute(
            "UPDATE nodes SET status='void' WHERE id=? AND status!='void'", (node_id,)
        )
        self.conn.commit()

    def flag(self, node_id: str) -> None:
        self.conn.execute(
            "UPDATE nodes SET flagged=1 WHERE id=?", (node_id,)
        )
        self.conn.commit()

    def set_tier(self, node_id: str, tier: int) -> bool:
        """
        Change memory_tier for a node. Allowed by the immutability trigger.
        Tier semantics: 0=hot (always injected), 1=warm (golden-angle), 2=cold (retrieval-only)
        Returns True if the node was found and its tier actually changed.
        """
        if tier not in (0, 1, 2):
            return False
        row = self.get(node_id)
        if not row:
            return False
        if row["memory_tier"] == tier:
            return False  # nothing to do
        self.conn.execute(
            "UPDATE nodes SET memory_tier=? WHERE id=?", (tier, node_id)
        )
        self.conn.commit()
        return True

    def set_stability(self, node_id: str, stability_days: float,
                      last_injected: Optional[str] = None) -> bool:
        """
        Update FSRS scheduling state for a node.

        stability_days: time (days) before retrievability drops below 90%.
          - Grows after successful recall (node was injected + session used it)
          - Decays after null signal (injected but irrelevant to session work)
          - Resets low after explicit demotion (near-forgotten)

        last_injected: ISO timestamp of most recent injection. Used to compute
          overdue score = days_since_last_injected / stability_days.
          Nodes with overdue_score > 1.0 are behind their schedule.

        Allowed by immutability trigger (scheduling metadata branch).
        Returns True if the update was applied.
        """
        row = self.get(node_id)
        if not row or row["status"] == "void":
            return False
        ts = last_injected or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE nodes SET stability_days=?, last_injected=? WHERE id=?",
            (max(0.1, float(stability_days)), ts, node_id)
        )
        self.conn.commit()
        return True

    # ── attention ledger (write-through receipts for every shown memory) ──────

    def record_shown(self, node_ids: list, channel: str,
                     session: Optional[str] = None,
                     trigger: Optional[str] = None) -> int:
        """
        Receipt every memory the moment it is surfaced to a model — hook push,
        MCP/CLI fetch, drift, anything. position = index within this showing
        (0 = brightest slot). Write-through: survives crashed sessions.
        Also stamps nodes.last_injected so FSRS overdue scoring stays live.
        Returns number of receipts written. Never raises — telemetry must not
        block the path that shows memories.
        """
        if not node_ids:
            return 0
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO attention_ledger "
                    "(node_id, session, channel, position, trigger, shown_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(nid, session or "", channel, i, trigger or "", now)
                     for i, nid in enumerate(node_ids)])
                self.conn.executemany(
                    "UPDATE nodes SET last_injected=? WHERE id=?",
                    [(now, nid) for nid in node_ids])
            return len(node_ids)
        except Exception:
            return 0

    def mark_cited(self, node_ids: list) -> int:
        """
        Close the loop: a shown node's content appeared in compiled output —
        the model actually used it. Marks the most recent unmarked receipts.
        Returns number of ledger rows updated.
        """
        if not node_ids:
            return 0
        try:
            now = datetime.now(timezone.utc).isoformat()
            n = 0
            with self.conn:
                for nid in node_ids:
                    cur = self.conn.execute(
                        "UPDATE attention_ledger SET cited=1, cited_at=? "
                        "WHERE node_id=? AND cited=0", (now, nid))
                    n += cur.rowcount
            return n
        except Exception:
            return 0

    # ── position records (golden-angle feedback loop persistence) ─────────────

    def load_position_records(self) -> dict:
        """
        Load all PositionRecords from the vault. Returns {node_id: PositionRecord}.
        This is the memory of WHERE each node has landed in context and whether
        the model actually used it — the data the feedback loop runs on.
        """
        from cairn.schedule import PositionRecord
        rows = self.conn.execute("SELECT * FROM position_records").fetchall()
        return {
            r["node_id"]: PositionRecord(
                node_id       = r["node_id"],
                middle_hits   = r["middle_hits"],
                compiled_hits = r["compiled_hits"],
                total_loads   = r["total_loads"],
            )
            for r in rows
        }

    def save_position_records(self, records: dict) -> int:
        """Upsert PositionRecords. Called at compile time after update_compiled_hits()."""
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        for rec in records.values():
            self.conn.execute("""
                INSERT INTO position_records
                  (node_id, middle_hits, compiled_hits, total_loads, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                  middle_hits   = excluded.middle_hits,
                  compiled_hits = excluded.compiled_hits,
                  total_loads   = excluded.total_loads,
                  updated_at    = excluded.updated_at
            """, (rec.node_id, rec.middle_hits, rec.compiled_hits,
                  rec.total_loads, now))
            n += 1
        if n:
            self.conn.commit()
        return n

    def dismiss_from_spark(self, node_id: str) -> None:
        """Remove a node from the inspiration rotation. Node untouched."""
        self.conn.execute(
            "INSERT OR REPLACE INTO spark_dismissed (node_id, dismissed_at) VALUES (?,?)",
            (node_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def spark_dismissed_ids(self) -> set:
        return {r["node_id"] for r in
                self.conn.execute("SELECT node_id FROM spark_dismissed").fetchall()}

    # ── set-aside: archive / snooze — human attention flags; node stays ACTIVE ──
    def archive(self, node_id: str) -> None:
        """Hide a node from the human's attention surfaces. Node untouched — still
        active, searchable, AI-visible. Reversible via unarchive()."""
        self.conn.execute(
            "INSERT OR REPLACE INTO archived (node_id, archived_at) VALUES (?,?)",
            (node_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def unarchive(self, node_id: str) -> None:
        """Restore an archived node to the human's surfaces — just drops the flag."""
        self.conn.execute("DELETE FROM archived WHERE node_id=?", (node_id,))
        self.conn.commit()

    def archived_ids(self) -> set:
        return {r["node_id"] for r in
                self.conn.execute("SELECT node_id FROM archived").fetchall()}

    def snooze(self, node_id: str, until: str) -> None:
        """Hide a node until `until` (ISO date/datetime), then it resurfaces.
        Node untouched. Reversible via unsnooze()."""
        self.conn.execute(
            "INSERT OR REPLACE INTO snoozed (node_id, until, snoozed_at) VALUES (?,?,?)",
            (node_id, until, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def unsnooze(self, node_id: str) -> None:
        """Wake a snoozed node now — drops the flag."""
        self.conn.execute("DELETE FROM snoozed WHERE node_id=?", (node_id,))
        self.conn.commit()

    def snoozed_now_ids(self) -> set:
        """Ids STILL snoozed (wake date in the future) — to hide from surfaces."""
        now = datetime.now(timezone.utc).isoformat()
        return {r["node_id"] for r in self.conn.execute(
            "SELECT node_id FROM snoozed WHERE until > ?", (now,)).fetchall()}

    def hidden_ids(self) -> set:
        """Ids the human has set aside — archived OR currently snoozed — to drop
        from the human attention surfaces. The nodes stay ACTIVE; the AI still
        sees them. Snoozes auto-expire (snoozed_now_ids is future-only)."""
        return self.archived_ids() | self.snoozed_now_ids()

    def list_archived(self) -> list:
        """Archived nodes (id + when), newest first — for the Archive view."""
        return [{"node_id": r["node_id"], "archived_at": r["archived_at"]}
                for r in self.conn.execute(
                    "SELECT node_id, archived_at FROM archived "
                    "ORDER BY archived_at DESC").fetchall()]

    def list_snoozed(self) -> list:
        """Snoozed nodes (id + wake date), soonest first — for the Archive view."""
        return [{"node_id": r["node_id"], "until": r["until"]}
                for r in self.conn.execute(
                    "SELECT node_id, until FROM snoozed ORDER BY until ASC").fetchall()]

    def attention_efficiency(self) -> dict:
        """
        Vault-wide attention stats from position records.
        efficiency = compiled_hits / total_loads — how often loaded memory
        actually surfaced in compiled output. The EEG line.
        """
        row = self.conn.execute("""
            SELECT COUNT(*)                                   AS tracked,
                   COALESCE(SUM(total_loads), 0)              AS loads,
                   COALESCE(SUM(compiled_hits), 0)            AS hits,
                   COALESCE(SUM(CASE WHEN middle_hits >= 2
                        AND compiled_hits = 0 THEN 1 END), 0) AS underattended
            FROM position_records
        """).fetchone()
        loads = row["loads"] or 0
        return {
            "tracked":       row["tracked"],
            "total_loads":   loads,
            "compiled_hits": row["hits"],
            "underattended": row["underattended"],
            "efficiency":    (row["hits"] / loads) if loads else 0.0,
        }

    def all_sessions(self) -> list:
        """All sessions ordered by start time, newest first."""
        return self.conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC"
        ).fetchall()

    def session_decision_count(self, session_id: str) -> int:
        """Count decision/resolved/warning nodes in a session (intent signals)."""
        return self.conn.execute(
            """SELECT COUNT(*) FROM nodes
               WHERE session=? AND status='active'
                 AND kind IN ('decision','resolved','warning','context_stamp','open_item')""",
            (session_id,)
        ).fetchone()[0]

    # ── read ───────────────────────────────────────────────────────────────────
    def session_nodes(self, session_id: str, include_void: bool = False) -> list:
        clause = "" if include_void else "AND status != 'void'"
        return self.conn.execute(
            f"SELECT * FROM nodes WHERE session=? {clause} ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()

    def get(self, node_id: str):
        return self.conn.execute(
            "SELECT * FROM nodes WHERE id=?", (node_id,)
        ).fetchone()

    def chain(self, node_id: str) -> list:
        """Walk parent chain backward — the full reasoning path to this node."""
        path, seen = [], set()
        current = node_id
        while current and current not in seen:
            seen.add(current)
            row = self.get(current)
            if not row: break
            path.append(row)
            current = row["parent"]
        return list(reversed(path))

    def struggle_points(self, session_id: str | None = None) -> list:
        """
        Nodes where the agent genuinely struggled.

        Thresholds are tool-aware:
          - Slow (>2000ms):  any tool — something took too long
          - Empty (rc=0):    any tool — found absolutely nothing
          - Sparse (rc<=2):  only high-volume tools (Grep/Glob/WebSearch)
                             A Bash command returning 1 line is often SUCCESS.
                             A Grep returning 1 match is a real struggle signal.
        """
        clause = "AND session=?" if session_id else ""
        params = (session_id,) if session_id else ()
        return self.conn.execute(f"""
            SELECT * FROM nodes
            WHERE status != 'void'
              AND (
                latency_ms > 2000
                OR result_count = 0
                OR (result_count <= 2 AND tool IN ('Grep','Glob','WebSearch'))
              )
              {clause}
            ORDER BY latency_ms DESC NULLS LAST
        """, params).fetchall()

    def stats(self) -> dict:
        row = self.conn.execute("""
            SELECT
                COUNT(*)                                    AS total,
                COUNT(CASE WHEN status='active' THEN 1 END) AS active,
                COUNT(CASE WHEN status='void'   THEN 1 END) AS voided,
                COUNT(CASE WHEN flagged=1       THEN 1 END) AS flagged,
                COUNT(DISTINCT session)                     AS sessions
            FROM nodes
        """).fetchone()
        return dict(row)

    # ── embedder (lazy, hardware-auto-routed) ─────────────────────────────────
    def load_embedder(self) -> "Vault":
        """
        Load the best available embedding backend.
        Auto-routes: CUDA GPU → ONNX NPU → CPU fallback.
        Override with CAIRN_EMBED_BACKEND=cpu|onnx|gpu env var.
        On RTX Spark with ONNX model exported: automatically uses Blackwell NPU.
        """
        from cairn.backends.embed import get_embedder
        self._embedder = get_embedder()
        return self

    def embed_pending(self) -> int:
        """
        Batch-embed all nodes that don't have embeddings yet.

        Two passes:
          1. Nodes with episodic_text already set (normal path).
          2. Pre-rewrite nodes with NULL episodic_text — reconstruct text
             on-the-fly from stored fields, embed, store only the embedding.
             The immutability trigger allows updating NULL→non-NULL embedding
             regardless of whether episodic_text is set.
        """
        if not self._embedder:
            self.load_embedder()

        # Pass 1: nodes with episodic_text set (skip voided)
        rows_with_text = self.conn.execute(
            "SELECT id, episodic_text FROM nodes "
            "WHERE embedding IS NULL AND episodic_text IS NOT NULL AND status != 'void'"
        ).fetchall()

        # Pass 2: old nodes (pre-to_episodic_text() rewrite) with NULL episodic_text
        rows_without_text = self.conn.execute(
            "SELECT * FROM nodes "
            "WHERE embedding IS NULL AND episodic_text IS NULL AND status != 'void'"
        ).fetchall()

        if not rows_with_text and not rows_without_text:
            return 0

        all_items: list[tuple[str, str]] = []  # (id, text)

        for r in rows_with_text:
            all_items.append((r["id"], r["episodic_text"]))

        for r in rows_without_text:
            # Reconstruct MicroNode from DB row to generate episodic text
            n = MicroNode(
                session        = r["session"] or "unknown",
                kind           = r["kind"] or "note",
                id             = r["id"],
                tool           = r["tool"],
                query          = r["query"],
                output_preview = r["output_preview"],
                result_count   = r["result_count"],
                latency_ms     = r["latency_ms"],
                parent         = r["parent"],
                agent_id       = r["agent_id"] or "unknown",
                model          = r["model"] or "unknown",
                agent_role     = r["agent_role"] or "worker",
                speaker        = r["speaker"] or "agent",
                memory_tier    = r["memory_tier"] if r["memory_tier"] is not None else 1,
            )
            etext = n.to_episodic_text()
            all_items.append((r["id"], etext))

        if not all_items:
            return 0

        texts = [t for _, t in all_items]
        blobs = self._embedder.encode(texts)  # backend handles batching + hardware
        for (row_id, _), blob in zip(all_items, blobs):
            self.conn.execute("UPDATE nodes SET embedding=? WHERE id=?", (blob, row_id))
        self.conn.commit()
        return len(all_items)

    def query_episodic(self, question: str, k: int = 8, session_id: str | None = None) -> list:
        """
        Semantic search across episodic text embeddings.
        Returns top-k nodes most similar to the question.

        Supports cross-session search (session_id=None) or within a session.
        Falls back to keyword search if no embeddings exist.

        The key query: "where did gpt-4o struggle?" finds nodes where
        episodic_text contains 'gpt-4o' + 'slow' or 'nothing' — clustered
        by the semantic embedding, not by keyword match alone.
        """
        # Fresh vault / embedder never run → no vectors to compare against.
        # Keyword-fallback (the promise above) instead of loading the model, so
        # fetch/wander/search/dashboard work on a brand-new install rather than
        # raising when the model can't be loaded or downloaded.
        has_emb = self.conn.execute(
            "SELECT 1 FROM nodes WHERE embedding IS NOT NULL AND status != 'void' LIMIT 1"
        ).fetchone()
        if not has_emb:
            return self._keyword_fallback(question, k, session_id)
        if not self._embedder:
            try:
                self.load_embedder()
            except Exception:
                return self._keyword_fallback(question, k, session_id)

        # embed the question using whatever backend is active
        q_blob = self._embedder.encode_one(question)
        dim    = self._embedder.dim

        # cosine similarity — pure python, no numpy required (fallback path)
        def cosine(a_blob: bytes, b_blob: bytes) -> float:
            n = dim
            a = struct.unpack(f"{n}f", a_blob)
            b = struct.unpack(f"{n}f", b_blob)
            dot   = sum(x * y for x, y in zip(a, b))
            mag_a = sum(x * x for x in a) ** 0.5
            mag_b = sum(y * y for y in b) ** 0.5
            if mag_a == 0 or mag_b == 0:
                return 0.0
            return dot / (mag_a * mag_b)

        # Composite scoring — Generative Agents (Park et al. 2023) formula:
        #   score = α·recency + β·relevance + γ·importance
        #
        # Weights tuned for Cairn's use case:
        #   relevance (cosine)  0.50 — semantic match is the primary signal
        #   recency             0.30 — recent decisions beat stale ones at same sim
        #   importance          0.20 — kind-derived importance as tiebreaker
        #
        # Recency: exponential decay with λ=0.07/day → half-life ≈ 10 days.
        # Tuned for engineering memory: a decision from 10 days ago should still
        # be highly relevant, but something from 6 months ago needs to earn its
        # place via high cosine similarity. Adjust RECENCY_LAMBDA to taste.
        #
        # Query-aware weighting (added after live eval 2026-06-10): a factual
        # lookup ("what golden angle constant does cairn use") was losing to a
        # recent-but-wrong node because of the 0.30 recency weight. Timeless
        # facts don't get less true with age. If the query carries no temporal
        # cue, recency drops to a tiebreaker and relevance dominates.
        RECENCY_CUES = ("recent", "latest", "yesterday", "today", "last session",
                        "last time", "what happened", "what did we", "what were we",
                        "status", "currently", "now", "this week", "newest")
        q_lower  = question.lower()
        # Whole-word match only. A plain substring test fired on "now" inside
        # "know"/"shown"/"downtown" and "status" inside "statuses", wrongly
        # tipping timeless factual lookups into recency-weighted mode.
        temporal = re.search(
            r'\b(?:' + '|'.join(re.escape(c) for c in RECENCY_CUES) + r')\b',
            q_lower) is not None

        # Hybrid retrieval: dense (cosine) + sparse (keyword overlap).
        # Live eval 2026-06-10: symbol-heavy facts ("golden angle = 1/phi^2 =
        # 0.3819...") embed weakly — pure cosine ranked them below nodes that
        # merely contained the project name. The sparse component is the
        # white paper's BM25 term, implemented as query-keyword coverage.
        _Q_STOP = {"what", "does", "how", "should", "why", "did", "the",
                   "with", "from", "where", "when", "which", "this", "that"}
        q_keywords = {w.strip(".,;:!?()'\"") for w in q_lower.split()
                      if len(w) >= 4 and w not in _Q_STOP}

        def keyword_score(row) -> float:
            if not q_keywords:
                return 0.0
            text = ((row["query"] or "") + " " + (row["episodic_text"] or "")).lower()
            return sum(1 for w in q_keywords if w in text) / len(q_keywords)

        RECENCY_LAMBDA = 0.07    # decay rate per day
        if temporal:
            W_RELEVANCE, W_RECENCY, W_KEYWORD, W_IMPORTANCE = 0.45, 0.25, 0.15, 0.15
        else:
            W_RELEVANCE, W_RECENCY, W_KEYWORD, W_IMPORTANCE = 0.50, 0.05, 0.25, 0.20
        now_ts = datetime.now(timezone.utc)

        def recency_score(timestamp_str: str) -> float:
            try:
                ts = datetime.fromisoformat(
                    timestamp_str.replace("Z", "+00:00")
                )
                days_old = max(0.0, (now_ts - ts).total_seconds() / 86400.0)
                return math.exp(-RECENCY_LAMBDA * days_old)
            except Exception:
                return 0.5

        # ── vectorized fast path — identical formula, one matmul ─────────────
        # ~1000x fewer interpreter ops than the loop below. Falls through to
        # the pure-Python scan only when numpy is unavailable.
        fast = self._query_fast(
            q_blob, q_keywords, k, session_id,
            (W_RELEVANCE, W_RECENCY, W_KEYWORD, W_IMPORTANCE),
            RECENCY_LAMBDA, now_ts)
        if fast is not None:
            return fast

        clause = "AND session=?" if session_id else ""
        params = (session_id,) if session_id else ()
        rows = self.conn.execute(f"""
            SELECT * FROM nodes
            WHERE embedding IS NOT NULL
              AND status != 'void'
              {clause}
        """, params).fetchall()
        if not rows:
            return []

        scored = []
        for row in rows:
            try:
                cos_sim = cosine(row["embedding"], q_blob)
            except struct.error:
                continue  # skip malformed blobs (dimension mismatch)

            rec  = recency_score(row["timestamp"] or "")
            imp  = int(row["importance"] or 5) / 10.0
            kw   = keyword_score(row)
            comp = (W_RELEVANCE * cos_sim + W_RECENCY * rec
                    + W_KEYWORD * kw + W_IMPORTANCE * imp)
            scored.append((comp, cos_sim, rec, kw, imp, row))

        scored.sort(key=lambda x: -x[0])
        results = []
        for comp, cos_sim, rec, kw, imp, row in scored[:k]:
            d = dict(row)
            d["score"]         = comp       # composite — used for ranking
            d["score_cosine"]  = cos_sim    # raw semantic similarity (dense)
            d["score_recency"] = rec        # recency component
            d["score_keyword"] = kw         # sparse keyword coverage (hybrid term)
            d["score_import"]  = imp        # importance component (0–1)
            results.append(d)
        return results

    def _keyword_fallback(self, question: str, k: int = 8,
                          session_id: str | None = None) -> list:
        """Keyword search for when semantic retrieval isn't available — a fresh
        vault with no embeddings, or an embedder that can't load/download.
        Returns the same dict(row) shape as query_episodic so every caller
        (fetch, wander, search, dashboard) degrades gracefully instead of
        crashing. This is the fallback query_episodic's docstring promises."""
        like = f"%{question}%"
        clause = "AND session = ?" if session_id else ""
        params: list = [like, like, like]
        if session_id:
            params.append(session_id)
        params.append(k)
        rows = self.conn.execute(f"""
            SELECT * FROM nodes
            WHERE status != 'void'
              AND (query LIKE ? OR output_preview LIKE ? OR episodic_text LIKE ?)
              {clause}
            ORDER BY timestamp DESC
            LIMIT ?
        """, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("score", 0.0)   # no semantic score in keyword mode
            out.append(d)
        return out

    def _query_fast(self, q_blob: bytes, q_keywords: set, k: int,
                    session_id, weights: tuple, recency_lambda: float,
                    now_ts) -> list | None:
        """
        Vectorized retrieval via cairn/index.py — exact same composite formula
        as the pure-Python loop in query_episodic. Returns None when numpy is
        unavailable (caller falls back) or [] when nothing is indexed.

        Two stages: dense+recency+importance for ALL nodes in one matmul,
        then the sparse keyword term computed only on the top-M partial-score
        candidates (M=1000 — the keyword term maxes at the W_KEYWORD weight,
        far too small to promote a node from outside the top 1000).
        """
        try:
            from cairn.index import EmbeddingIndex
            if self._index is None:
                self._index = EmbeddingIndex()
            if not self._index.ensure(self.conn):
                return None     # no numpy — use the pure-Python scan
            import numpy as np
        except ImportError:
            return None
        try:
            partial, mask = self._index.partial_scores(
                q_blob, weights, recency_lambda, now_ts, session_id)
        except Exception:
            return None         # any index fault → safe fallback
        if partial is None:
            return []
        if not mask.any():
            return []
        _, _, w_kw, _ = weights
        idx = self._index

        scores = np.where(mask, partial, -np.inf)
        m = min(int(mask.sum()), max(1000, 4 * k))
        cand = np.argpartition(scores, -m)[-m:]
        cand = cand[np.isfinite(scores[cand])]

        # sparse keyword term on candidates only
        kw_scores = {}
        if q_keywords:
            cand_ids = [idx.ids[i] for i in cand]
            qmarks = ",".join("?" * len(cand_ids))
            texts = {r["id"]: ((r["query"] or "") + " "
                               + (r["episodic_text"] or "")).lower()
                     for r in self.conn.execute(
                         f"SELECT id, query, episodic_text FROM nodes "
                         f"WHERE id IN ({qmarks})", cand_ids)}
            nq = len(q_keywords)
            for i in cand:
                text = texts.get(idx.ids[i], "")
                kw_scores[i] = sum(1 for w in q_keywords if w in text) / nq

        ranked = sorted(
            ((float(scores[i]) + w_kw * kw_scores.get(i, 0.0), i) for i in cand),
            key=lambda t: -t[0])[:k]
        if not ranked:
            return []

        top_ids = [idx.ids[i] for _, i in ranked]
        qmarks = ",".join("?" * len(top_ids))
        rows = {r["id"]: r for r in self.conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({qmarks})", top_ids)}

        w_rel, w_rec, _w_kw2, w_imp = weights
        days = np.maximum(0.0, (now_ts.timestamp() - idx.ts) / 86400.0)
        results = []
        for comp, i in ranked:
            row = rows.get(idx.ids[i])
            if row is None:
                continue
            d = dict(row)
            q_arr = np.frombuffer(q_blob, dtype=np.float32)
            qn = float(np.linalg.norm(q_arr)) or 1.0
            d["score"]         = comp
            d["score_cosine"]  = float(idx.mat[i] @ (q_arr / qn))
            d["score_recency"] = float(math.exp(-recency_lambda * days[i]))
            d["score_keyword"] = kw_scores.get(i, 0.0)
            d["score_import"]  = float(idx.imp[i])
            results.append(d)
        return results

    def related_across_sessions(self, question: str, k: int = 5) -> list:
        """
        Cross-session semantic search — find relevant nodes from ANY session.
        The vault remembers across months. This queries all of it.
        """
        return self.query_episodic(question, k=k, session_id=None)

    def session_summary(self, session_id: str) -> dict:
        """
        Returns a structured summary of a session:
        node count, struggle count, decisions made, questions open,
        models that contributed, flags, voids, context stamps.
        Feeds into PROTOCOL.md delta and dashboard.
        """
        nodes     = self.session_nodes(session_id)
        struggles = self.struggle_points(session_id)

        models    = {}
        kinds     = {}
        for r in nodes:
            m = r["model"] or "unknown"
            models[m] = models.get(m, 0) + 1
            k = r["kind"]
            kinds[k] = kinds.get(k, 0) + 1

        stamps    = [r for r in nodes if r["kind"] == "context_stamp"]
        turns     = [r for r in nodes if r["kind"] == "conversation_turn"]
        decisions = [r for r in nodes if r["kind"] == "decision"]
        questions = [r for r in nodes if r["kind"] == "question"]
        blockers  = [r for r in nodes if r["kind"] == "blocker"]
        resolved  = [r for r in nodes if r["kind"] == "resolved"]
        flagged   = [r for r in nodes if r["flagged"]]
        voided    = [r for r in nodes if r["status"] == "void"]

        return {
            "session_id":    session_id,
            "total":         len(nodes),
            "struggles":     len(struggles),
            "models":        models,
            "kinds":         kinds,
            "stamps":        len(stamps),
            "turns":         len(turns),
            "decisions":     len(decisions),
            "questions":     len(questions),
            "blockers":      len(blockers),
            "resolved":      len(resolved),
            "flagged":       len(flagged),
            "voided":        len(voided),
            "stamp_text":    [r["query"] for r in stamps if r["query"]],
            "open_questions":[r["query"] for r in questions if r["query"]],
            "open_blockers": [r["query"] for r in blockers
                              if r["query"] and r["status"] == "active"],
        }
