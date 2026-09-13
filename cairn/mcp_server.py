"""
cairn/mcp_server.py — the connector. Cairn as an MCP server.

This is what makes Cairn reachable by ANY model: Claude Desktop, Claude Code,
Cursor, a local Ollama frontend — anything that speaks the Model Context
Protocol gets native vault tools, no hooks, no shell glue.

Self-built, zero dependencies: MCP is JSON-RPC 2.0 over stdio. We implement
the slice we need by hand rather than importing an SDK — charter rule, and it
keeps the surface auditable (no external code touches the vault).

Tools exposed:
  cairn_fetch    — THE token-saver. One query -> a compact, budget-fitted
                   context pack. An agent calls this INSTEAD of re-reading
                   files/history. (Karpathy/Graphify pattern, for memory.)
  cairn_search   — ranked hybrid search, ids + gists (cheap; drill with fetch)
  cairn_wander   — weak-tie graph walk: adjacent ideas fetch won't return
  cairn_note     — write a memory (decision/insight/idea/open_item/...).
                   This is ambient capture done right: the model logs as a
                   first-class tool call, not a manual sweep.
  cairn_orient   — inherited context for session start (PROTOCOL digest)
  cairn_recent   — recent decisions/open items, the working set
  cairn_read     — full text of specific nodes by id (the follow-up to the
                   gists recent/search return; works on un-embedded nodes)
  cairn_logs     — the live tail: newest nodes of ANY kind, embedded or not,
                   filterable by kind/session/substring. "What just happened."

Register in Claude Desktop's mcp config (claude_desktop_config.json):
  "cairn": {
    "command": "C:\\\\...\\\\python.exe",
    "args": ["-X","utf8","-m","cairn","mcp"]
  }

Everything stays local. The server reads/writes the same ~/.cairn/cairn.db.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

PROTOCOL_VERSION = "2024-11-05"   # the MCP spec revision we speak (NOT our version)
try:
    from cairn import __version__ as _CAIRN_VERSION
except Exception:
    _CAIRN_VERSION = "0.0.0+source"
# serverInfo.version is THIS server's version — keep it tied to the package so it
# never drifts (was a hard-coded "1.0.0" that outran the real 0.3.x release).
SERVER_INFO = {"name": "cairn", "version": _CAIRN_VERSION}

# ── tool schemas (advertised to the client) ──────────────────────────────────
TOOLS = [
    {
        "name": "cairn_fetch",
        "description": (
            "Token-saving retrieval: ONE query returns a compact context pack "
            "of only the memories/files that matter, fitted to a token budget. "
            "Call this INSTEAD of re-reading files or scrolling history — it "
            "replaces the re-reading tax with a targeted answer. Top hits come "
            "back as capped previews (~1800 chars), the rest as gists — "
            "cairn_read returns any node's complete text (raise max_chars "
            "for very long nodes)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what you need to know"},
                "budget_tokens": {"type": "integer", "default": 1500,
                                  "description": "max tokens in the pack"},
                "account": {"type": "string",
                            "description": "optional: only return memories from "
                                           "this account/galaxy (case-insensitive)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cairn_wander",
        "description": (
            "The creative complement to cairn_fetch. Walks the memory graph "
            "outward from the query's best hits, preferring WEAK ties that "
            "cross topic boundaries — surfaces adjacent ideas and unseen "
            "connections (a past project, an old conversation, an idea from a "
            "different domain) that similarity search will never return. Use "
            "when brainstorming, stuck, or asked for fresh angles. Returns "
            "gists — cairn_read pulls any hit in full (raise max_chars for "
            "very long nodes)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "what you're working on or thinking about"},
                "hops": {"type": "integer", "default": 3,
                         "description": "how far to wander (1-5)"},
                "k": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cairn_search",
        "description": ("Ranked hybrid search across the whole vault. Returns "
                        "ids + gists + scores (cheap) — an index, not the text. "
                        "Pass the ids that matter to cairn_read for their full "
                        "text — raise max_chars for very long nodes "
                        "(cairn_fetch for a budgeted context pack)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cairn_note",
        "description": ("Write a memory to the vault — ambient capture. Use for "
                        "decisions, insights, ideas, open items, warnings, "
                        "procedures discovered while working. The vault remembers "
                        "across sessions and models. Write the complete salient "
                        "fact, decision, warning, or open item WITHOUT truncating "
                        "it — but do not paste transcripts (turns are captured "
                        "separately). For a large artifact, save the file and "
                        "note its path and purpose."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string",
                         "enum": ["decision", "insight", "idea", "open_item",
                                  "warning", "hypothesis", "procedure",
                                  "question", "resolved", "conversation_turn"],
                         "default": "insight"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
    },
    {
        "name": "cairn_orient",
        "description": ("Session-start context: the most recent compiled "
                        "PROTOCOL digest plus open items. Call once at the "
                        "beginning of a session to inherit prior context."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cairn_recent",
        "description": ("The working set: recent decisions, open items, and "
                        "warnings across the vault — what's live right now. "
                        "Returns gists — pass ids to cairn_read for full text "
                        "(raise max_chars for very long nodes)."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 12}},
        },
    },
    {
        "name": "cairn_read",
        "description": ("Read specific nodes IN FULL by id — the follow-up to "
                        "the gists that recent/search/fetch return; this is the "
                        "verbatim layer (long turns carry their complete text "
                        "here). Accepts full ids or unambiguous prefixes, up to "
                        "8 per call; max_chars dials the per-node body budget. "
                        "Unlike fetch/search this needs no embeddings, so it "
                        "reads nodes written moments ago."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "node ids (or prefixes) to read"},
                "max_chars": {"type": "integer",
                              "description": "per-node canonical-body budget in "
                                             "chars (default 24000; total body "
                                             "across ids caps at max(60000, "
                                             "this)) — raise it to pull a very "
                                             "long turn whole, lower it to skim"},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "cairn_logs",
        "description": ("The LIVE tail of the vault — newest nodes first, ALL "
                        "kinds including conversation turns, embedded or not. "
                        "This is how you see what just happened (fetch/search "
                        "can't see un-embedded nodes until the nightly sleep). "
                        "Filter by kind, session prefix, or a contains "
                        "substring; follow up with cairn_read for full text "
                        "(raise max_chars for very long nodes)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit":    {"type": "integer", "default": 20,
                             "description": "rows to return (max 60)"},
                "kind":     {"type": "string",
                             "description": "only this kind (e.g. conversation_turn, decision)"},
                "session":  {"type": "string",
                             "description": "session id prefix (e.g. codex-)"},
                "contains": {"type": "string",
                             "description": "case-insensitive substring to search live text"},
                "unembedded_only": {"type": "boolean", "default": False,
                                    "description": "only nodes the nightly embed hasn't reached"},
            },
        },
    },
]


# ── tool implementations ──────────────────────────────────────────────────────

_VAULT = None


def _vault():
    # Cache the Vault for the process lifetime. The MCP server is strictly serial
    # (serve() handles one stdin line at a time — no threads), so a single shared
    # Vault is safe, and it lets the in-process EmbeddingIndex persist across tool
    # calls instead of rebuilding from scratch on every fetch / search / drift.
    # ensure() re-checks the (count, max-rowid) signature on every query, so writes
    # from other processes (hooks, CLI) are still picked up — reuse when unchanged,
    # rebuild only when it actually changed. No staleness, just no needless work.
    global _VAULT
    if _VAULT is None:
        from cairn.vault import Vault
        _VAULT = Vault()
    return _VAULT


# Who is actually connected? The MCP initialize handshake announces the
# client (clientInfo: name/version); we keep it so writes carry an honest
# label instead of the generic 'mcp-client' that plagued attribution
# (Lane C: 320 anonymous nodes in one week, per the audit organ).
_CLIENT_INFO: dict = {}


def _client_name() -> str:
    return str(_CLIENT_INFO.get("name") or "").strip().lower().replace(" ", "-")[:40]


def _client_label() -> str:
    import os
    env = os.environ.get("CAIRN_MODEL")
    if env:
        return env.strip()[:40]
    return _client_name() or "mcp-client"


def _session() -> str:
    from pathlib import Path
    import os
    sid = os.environ.get("CAIRN_SESSION") or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        return sid
    # A NON-Claude client must never inherit the active Claude session from
    # last_session.txt — that was the eyewitnessed Lane C bug (a rival
    # vendor's writes filed under the Claude session that happened to be
    # open). Unknown or Claude-family clients keep the legacy fallback.
    name = _client_name()
    if name and "claude" not in name:
        return f"mcp-{name}-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    f = Path.home() / ".cairn" / "last_session.txt"
    if f.exists():
        s = f.read_text().strip()
        if s:
            return s
    return datetime.now(timezone.utc).strftime("mcp-%Y-%m-%d")


# ── argument validation ───────────────────────────────────────────────────────
# The tools return their content as a plain string, so a bad argument comes back
# as a readable, self-correcting message (the agent reads it and retries) rather
# than a KeyError surfacing as the cryptic "cairn error: query", or a bare int()
# ValueError. Deliberately NO upper clamp on counts/budgets: a deep, explicit
# read ("everything since this morning") is a real, supported need — we reject
# only garbage and non-positive values, never a large honest one.

class _ErrText(str):
    """A tool return that IS a plain string (so direct callers and tests keep
    working) but is typed as a failure. handle() flags isError:true on these
    without substring-sniffing the text — a successful result that merely quotes
    the words 'cairn error' stays a success."""
    __slots__ = ()


def _req_query(args: dict, key: str = "query"):
    """(query, None) or (None, _ErrText). Required, must be a non-empty string."""
    q = args.get(key)
    if not isinstance(q, str) or not q.strip():
        return None, _ErrText(
            f"cairn error: '{key}' is required and must be a non-empty string — "
            f"pass what you want to find (e.g. {key}=\"auth bug\"). "
            f"Nothing was searched.")
    return q, None


def _pos_int(args: dict, key: str, default: int):
    """(value, None) or (None, _ErrText). Keeps the default when the key is
    absent or null; otherwise requires a WHOLE number >= 1. No ceiling — a large
    explicit count is honored. Rejects bool/fractional/garbage (True and 1.9 are
    not counts), while a positive integer or its numeric-string form is fine."""
    if key not in args or args.get(key) is None:
        return default, None
    raw = args.get(key)

    def _bad():
        return None, _ErrText(
            f"cairn error: '{key}' must be a whole number 1 or more (got "
            f"{raw!r}); omit it for the default of {default}.")

    if isinstance(raw, bool):                      # bool is a subclass of int
        return _bad()
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return _bad()
        n = int(raw)
    elif isinstance(raw, str):
        try:
            n = int(raw.strip())                   # '12' ok, '1.9'/'x' rejected
        except (TypeError, ValueError):
            return _bad()
    else:
        return _bad()
    if n < 1:
        return _bad()
    return n, None


def _tool_fetch(args: dict) -> str:
    from cairn.retrieve import fetch_pack, render_pack
    query, err = _req_query(args)
    if err:
        return err
    budget, err = _pos_int(args, "budget_tokens", 1500)
    if err:
        return err
    pack = fetch_pack(query, vault=_vault(),
                      budget_tokens=budget,
                      account=(args.get("account") or None),
                      channel="mcp_fetch",
                      session=_session())   # honest per-connection receipt (S8)
    return render_pack(pack)


def _tool_wander(args: dict) -> str:
    from cairn.retrieve import drift_pack, render_drift
    query, err = _req_query(args)
    if err:
        return err
    # hops/k stay clamped: a graph walk deeper than 5 hops or wider than 40 is a
    # runaway, not a deep read — the bound is the design, not a stinginess.
    pack = drift_pack(query, vault=_vault(),
                      hops=max(1, min(5, int(args.get("hops", 3) or 3))),
                      k=max(1, min(40, int(args.get("k", 10) or 10))),
                      session=_session())   # honest per-connection receipt (S8)
    return render_drift(pack)


def _tool_search(args: dict) -> str:
    v = _vault()
    query, err = _req_query(args)
    if err:
        return err
    k, err = _pos_int(args, "k", 10)
    if err:
        return err
    rows = v.query_episodic(query, k=k)
    if not rows:
        return "no matches."
    out = [f"{len(rows)} results for {query!r}:"]
    ann = v.relation_annotations([d.get("id") for d in rows])
    for d in rows:
        gist = d.get("gist") or (d.get("query") or "")[:80]
        out.append(f"  [{d.get('id')}] ({d.get('kind')}, {d.get('score',0):.2f}) {gist}")
        if d.get("id") in ann:
            out.append(f"      {ann[d.get('id')]}")
    out.append("\nuse cairn_read for full text of what matters "
               "(raise max_chars for very long nodes).")
    return "\n".join(out)


def _tool_note(args: dict) -> str:
    from cairn.vault import MicroNode
    from cairn.capture import resolve_project_tag
    import os
    v = _vault()
    model = _client_label()
    # cwd-derived project tag — same resolver as turn capture. The MCP server's
    # cwd is often the client's, not the project dir, so CAIRN_PROJECT (env
    # branch (a)) is the reliable channel here; the folder-name fallback still
    # fires when the server does run in the project. Additive, never guesses.
    raw_tags = args.get("tags") or []
    # Tags are STRINGS — schema says so, but the schema is client-side
    # decoration; enforce server-side. A non-string tag would otherwise ride
    # into the JSON column and could crash relation parsers downstream
    # (an exception in an annotation pass = every annotation in that result
    # set silently vanishing — a suppression vector, not just a bug).
    nonstr = [t for t in raw_tags if not isinstance(t, str)]
    if nonstr:
        return _ErrText("cairn_note: REJECTED — tags must be strings (got "
                f"{[type(t).__name__ for t in nonstr]}). Nothing was written.")
    tags = raw_tags + ["mcp"]
    # Relation-integrity gate: authoritative relation tags are CLI-only, where
    # the target is validated (and, for supersedes, atomically voided). A
    # generic MCP tag write has neither validation nor authority — accepting
    # these prefixes would let any connected client forge lineage that
    # supersede-aware surfaces then trust. FAIL CLOSED: reject the whole note,
    # write nothing; forged authority is never silently downgraded.
    from cairn.vault import RESERVED_RELATION_PREFIXES
    _reserved = tuple(f"{p}:" for p in RESERVED_RELATION_PREFIXES)
    bad = [t for t in tags if isinstance(t, str) and t.startswith(_reserved)]
    if bad:
        return _ErrText("cairn_note: REJECTED — reserved relation tag(s) "
                f"[{', '.join(bad)}] cannot be written over MCP. Relations "
                "are authored via the CLI (cairn note --corrects=<id> ...), "
                "which validates the target. Nothing was written.")
    proj = resolve_project_tag()
    if proj and proj not in tags:
        tags.append(proj)
    node = v.write(MicroNode(
        session     = _session(),
        kind        = args.get("kind", "insight"),
        query       = args["text"],
        model       = model,
        agent_role  = "worker",
        memory_tier = 1,
        tags        = tags,
    ))
    return f"written: [{node.id}] {node.kind}"


def _tool_orient(args: dict) -> str:
    from pathlib import Path
    # page one first — the vault's laws, landscape, and warnings. Generated
    # nightly; a model should know the constitution before the gossip.
    head = ""
    # page one — computed LIVE and scoped to THIS galaxy (a Codex/GPT session
    # sees its own activity, not the global total or the nightly-stale file).
    # Falls back to the cached PAGE_ONE.md only if the live render fails.
    try:
        from cairn.book import page_one
        from cairn.vault import _live_account
        # resolve the REAL MCP session, not "" — an empty id skips the per-session
        # channel/Desktop/codex rungs, so page_one's galaxy-scoped counts (and the
        # warning below) would otherwise be computed for no session at all.
        head = page_one(_vault(), account=_live_account(_session())).strip() + "\n\n"
    except Exception:
        p1 = Path.home() / ".cairn" / "PAGE_ONE.md"
        if p1.exists():
            try:
                head = p1.read_text(encoding="utf-8", errors="replace").strip() + "\n\n"
            except Exception:
                head = ""
    # multi-account ambiguity warning — appended to every return path (''=no warn).
    from cairn.vault import orient_account_warning
    warn = orient_account_warning(_session())
    proto_root = Path.home() / ".cairn" / "protocols"
    if not proto_root.exists():
        return head + "no PROTOCOL.md yet — fresh start." + warn
    # newest protocol by mtime
    protos = list(proto_root.glob("*/PROTOCOL.md"))
    if not protos:
        return head + "no PROTOCOL.md yet — fresh start." + warn
    newest = max(protos, key=lambda p: p.stat().st_mtime)
    text = newest.read_text(encoding="utf-8", errors="replace")
    body = text[:4000] + ("\n... (truncated; use cairn_fetch to dig deeper)"
                          if len(text) > 4000 else "")
    return head + body + warn


def _tool_recent(args: dict) -> str:
    v = _vault()
    # No upper clamp: "everything live since this morning" is a real request.
    # But a negative slips through to SQLite as LIMIT -1 = NO limit (the whole
    # working set), and a non-number would crash — so parse safely, default 12.
    limit, err = _pos_int(args, "limit", 12)
    if err:
        return err
    rows = v.conn.execute("""
        SELECT id, kind, query FROM nodes
        WHERE status='active'
          AND kind IN ('decision','open_item','warning','idea','resolved')
        ORDER BY timestamp DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        # The vault may be full of other kinds (turns, procedures); this surface
        # only shows the live working set, so say that, not "empty".
        return ("nothing in the working set (no active decisions, open items, "
                "warnings, ideas, or resolved notes).")
    out = ["working set (most recent):"]
    ann = v.relation_annotations([r["id"] for r in rows])
    for r in rows:
        out.append(f"  [{r['id']}] ({r['kind']}) {(r['query'] or '')[:90]}")
        if r["id"] in ann:
            out.append(f"      {ann[r['id']]}")
    out.append("\nfull text of any of these: cairn_read with the id(s) "
               "— raise max_chars for very long nodes.")
    return "\n".join(out)


def _tool_read(args: dict) -> str:
    # Full-text read by id. Deliberate-reference semantics: unlike the ranked
    # surfaces (fetch/inject filter to active), an explicit id request also
    # returns voided nodes — loudly labeled — because provenance chains
    # (corrections pointing at retired claims) are exactly when you need to
    # see what the correction refers to. Works on un-embedded nodes: this is
    # plain row lookup, no index involved.
    v = _vault()
    raw = args.get("ids") or []
    if isinstance(raw, str):
        raw = [p for p in raw.replace(",", " ").split() if p]
    ids = [str(i).strip() for i in raw if str(i).strip()][:8]
    if not ids:
        return "cairn_read: no ids given."
    # Per-node BODY budget (max_chars) + a total BODY budget across all ids, so
    # eight long turns can't stack into a context bomb. "Body" is exact: the
    # budgets meter canonical body text; small header/tags/inventory lines ride
    # on top. The total scales with an explicit max_chars so one deliberate
    # deep read is never blocked.
    cap = 24000
    try:
        if args.get("max_chars"):
            cap = max(200, int(args["max_chars"]))
    except (TypeError, ValueError):
        cap = 24000
    total_cap = max(60000, cap)
    spent = 0
    skipped = []
    out = []
    _cols = ("SELECT id, kind, status, session, speaker, model, timestamp, tags, "
             "       query, output_preview, episodic_text FROM nodes ")
    for want in ids:
        # Exact id first — a full id that also happens to be the prefix of a
        # longer id must resolve to itself, not report "ambiguous". Only when
        # there's no exact hit do we treat the argument as a prefix, and then
        # LIKE wildcards are escaped so a stray _ or % in the argument matches
        # literally instead of silently fanning out to unrelated nodes.
        rows = v.conn.execute(_cols + "WHERE id = ? LIMIT 1", (want,)).fetchall()
        if not rows:
            esc = want.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = v.conn.execute(
                _cols + "WHERE id LIKE ? || '%' ESCAPE '\\' LIMIT 3",
                (esc,)).fetchall()
        if not rows:
            out.append(f"── [{want}] not found.")
            continue
        if len(rows) > 1:
            matches = ", ".join(r["id"] for r in rows)
            out.append(f"── [{want}] ambiguous prefix — matches: {matches}")
            continue
        r = rows[0]
        head = (f"── [{r['id']}] {r['kind']} · {r['session']} · "
                f"{r['speaker'] or '?'}/{r['model'] or '?'} · {r['timestamp']}")
        out.append(head)
        if r["status"] != "active":
            out.append(f"   ⚠ status={r['status']} — retired from ranked surfaces; "
                       f"historical record, check for correction/resolved notes.")
        # relations, both directions — the back-pointer the void label used to
        # tell readers to go hunt for manually. Absent when none exist.
        _ann = v.relation_annotations([r["id"]])
        if r["id"] in _ann:
            out.append(f"   {_ann[r['id']]}")
        try:
            import json as _json
            from cairn.vault import RESERVED_RELATION_PREFIXES as _rel
            for _t in _json.loads(r["tags"] or "[]"):
                if not isinstance(_t, str):
                    continue
                for _p in _rel:
                    if _t.startswith(_p + ":"):
                        out.append(f"   → {_p} [{_t.split(':', 1)[1]}]")
        except Exception:
            pass
        if r["tags"]:
            out.append(f"   tags: {r['tags']}")

        # ONE canonical body per node — the fullest stored text, not three
        # overlapping fields. query is a prefix of the preview, the preview a
        # prefix of an overflowed turn's episodic text; printing all three
        # tripled the payload with zero new information. Any field that adds
        # real content beyond the body still prints; pure derivations don't.
        q, p, e = r["query"] or "", r["output_preview"] or "", r["episodic_text"] or ""
        body = max((q, p, e), key=len)

        def _core(t):
            # derived episodic text carries a short "actor said/decided: " prefix;
            # strip it so containment checks compare actual content
            head, sep, rest = t.partition(": ")
            return rest if (sep and len(head) <= 40) else t

        extras = [(lbl, t) for lbl, t in (("query", q), ("preview", p), ("episodic", e))
                  if t and t is not body and t not in body and _core(t) not in body]

        room = total_cap - spent
        if room <= 0:
            skipped.append(r["id"])
            # unwind this node's already-appended header/status/tags lines —
            # it gets reported once, in the budget notice instead
            while out and out[-1].startswith("   "):
                out.pop()
            if out and out[-1].startswith("── ["):
                out.pop()
            continue
        eff = min(cap, room)
        t = body if len(body) <= eff else (
            body[:eff] + f" [... truncated at {eff} chars — total "
            f"{len(body)}; pass max_chars={len(body)} (fewer ids) for all of it]")
        spent += len(t)
        out.append(f"   text: {t}")
        for lbl, extra in extras:
            xt = extra if len(extra) <= 500 else extra[:500] + " [...]"
            spent += len(xt)
            out.append(f"   {lbl} (adds content beyond text): {xt}")
        sizes = " · ".join(f"{lbl} {len(t)}c" for lbl, t in
                           (("query", q), ("preview", p), ("episodic", e)) if t)
        out.append(f"   fields: {sizes}")
        out.append("")
    if skipped:
        out.append(f"body budget {total_cap} chars reached — unread: "
                   f"{', '.join(skipped)} (fewer ids per call, or one id with "
                   f"a raised max_chars)")
    return "\n".join(out).rstrip()


def _tool_logs(args: dict) -> str:
    # The live tail. Plain recency-ordered SQL — no embeddings anywhere in the
    # path, so nodes written seconds ago are first-class. This is the answer to
    # "what just happened" that recent (meaning-kinds only) and fetch/search
    # (embedded-only) structurally cannot give.
    v = _vault()
    limit = max(1, min(60, int(args.get("limit", 20))))
    where, params = ["status='active'"], []
    if args.get("kind"):
        where.append("kind = ?")
        params.append(str(args["kind"]))
    if args.get("session"):
        where.append("session LIKE ? || '%'")
        params.append(str(args["session"]))
    if args.get("contains"):
        where.append("(query LIKE '%' || ? || '%' OR episodic_text LIKE '%' || ? || '%')")
        params += [str(args["contains"])] * 2
    if args.get("unembedded_only"):
        where.append("embedding IS NULL")
    rows = v.conn.execute(
        f"SELECT id, kind, session, speaker, timestamp, "
        f"       embedding IS NOT NULL AS emb, "
        f"       substr(REPLACE(COALESCE(query,''), char(10), ' '), 1, 110) AS gist "
        f"FROM nodes WHERE {' AND '.join(where)} "
        f"ORDER BY timestamp DESC LIMIT ?", (*params, limit)).fetchall()
    if not rows:
        return "live log: no matching nodes."
    out = [f"live log — newest first ({len(rows)}):"]
    ann = v.relation_annotations([r["id"] for r in rows])
    for r in rows:
        mark = "·" if r["emb"] else "○"
        t = (r["timestamp"] or "?")[11:16]
        sess = (r["session"] or "")[:26]
        out.append(f"  {mark} [{r['id']}] {t} {r['kind']}/{r['speaker'] or '?'} ({sess}) {r['gist']}")
        if r["id"] in ann:
            out.append(f"      {ann[r['id']]}")
    out.append("\n○ = not yet embedded (invisible to fetch/search until sleep). "
               "Full text: cairn_read with the id(s) — raise max_chars for "
               "very long nodes.")
    return "\n".join(out)


TOOL_FNS = {
    "cairn_fetch":  _tool_fetch,
    "cairn_wander": _tool_wander,
    "cairn_drift":  _tool_wander,   # legacy alias (renamed to wander); NOT advertised in TOOLS, kept so stale callers don't 404
    "cairn_search": _tool_search,
    "cairn_note":   _tool_note,
    "cairn_orient": _tool_orient,
    "cairn_recent": _tool_recent,
    "cairn_read":   _tool_read,
    "cairn_logs":   _tool_logs,
}


# ── JSON-RPC plumbing (stdio) ─────────────────────────────────────────────────

def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for
    notifications (a message with no "id" — these get NO reply, ever).

    Validates the envelope shape before touching it: a malformed message
    (not an object, no string method, wrong-typed params) earns a proper
    JSON-RPC error, never an unhandled exception that would kill serve()."""
    if not isinstance(req, dict):
        return _error(None, -32600,
                      "invalid request: expected a JSON-RPC object.")
    # An id, when present, must be a string, number, or null — not a container.
    if "id" in req and req["id"] is not None and not isinstance(req["id"], (str, int, float)):
        return _error(None, -32600,
                      "invalid request: 'id' must be a string, number, or null.")
    method = req.get("method")
    if not isinstance(method, str):
        rid = req.get("id") if req.get("id") is not None else None
        return _error(rid, -32600, "invalid request: 'method' must be a string.")
    # A JSON-RPC notification has NO "id" member. It must never get a reply,
    # whatever its method — so decide this up front.
    is_notification = "id" not in req
    id_ = req.get("id")

    if is_notification:
        # We take no side effects on any notification today (initialized, etc.);
        # the contract that matters is the silence — a notification NEVER gets a
        # reply, malformed params included.
        return None

    # Requests only: distinguish an OMITTED optional params (fine — default to
    # {}) from one that is PRESENT but the wrong type (reject; never silently
    # run a different, defaulted request than the caller wrote).
    if "params" in req and req["params"] is not None and not isinstance(req["params"], dict):
        return _error(id_, -32602, "invalid params: 'params' must be an object.")
    params = req.get("params") or {}

    if method == "initialize":
        ci = params.get("clientInfo")
        if isinstance(ci, dict):
            _CLIENT_INFO.update(ci)   # remember who's on the line — honest labels
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(id_, -32602,
                          "invalid params: 'name' must be a string.")
        # Same present-but-wrong-typed rejection for arguments: an omitted or
        # null arguments defaults to {}, but a present non-object is refused
        # rather than silently run as a defaulted call.
        if "arguments" in params and params["arguments"] is not None \
                and not isinstance(params["arguments"], dict):
            return _error(id_, -32602,
                          "invalid params: 'arguments' must be an object.")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if not fn:
            # the method (tools/call) exists; it's the tool name that's bad
            return _error(id_, -32602, f"unknown tool: {name}")
        try:
            text = fn(args)
            # A validation/authority rejection returns a typed _ErrText — a
            # failure the client should see flagged, without substring-sniffing
            # (a genuine result may quote the words "cairn error").
            is_error = isinstance(text, _ErrText)
        except Exception as e:
            # Keep a readable message for the agent (it reads it and self-
            # corrects), but log the full detail to stderr instead of leaking
            # internals to the client, and flag it so stricter clients can tell
            # this was a failure, not a result.
            import traceback
            print(f"cairn mcp: tool {name} failed: {e}\n"
                  f"{traceback.format_exc()}", file=sys.stderr, flush=True)
            text = f"cairn error: {e}"
            is_error = True
        return _result(id_, {"content": [{"type": "text", "text": text}],
                             "isError": is_error})

    if method == "ping":
        return _result(id_, {})

    return _error(id_, -32601, f"method not found: {method}")


def serve() -> None:
    """Read JSON-RPC messages line-delimited from stdin, write replies to
    stdout. Logs go to stderr only (stdout is the protocol channel).

    Each message is isolated: a malformed line or an unexpected error yields a
    JSON-RPC error (or is logged) and the loop keeps serving — one bad message
    can never take the whole connection down."""
    # this process IS the harness when nothing else declared one — self-stamp
    # so captures born under the MCP server read 'mcp' rather than 'unknown'.
    os.environ.setdefault("CAIRN_HARNESS", "mcp")
    print("cairn mcp server ready (stdio)", file=sys.stderr, flush=True)

    def _emit(resp):
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            # Malformed JSON: the spec answer is a Parse Error with a null id.
            _emit(_error(None, -32700, "parse error: message was not valid JSON."))
            continue
        try:
            resp = handle(req)
        except Exception as e:
            # Last-resort net: handle() shouldn't raise, but if it ever does,
            # answer with an internal-error (never crash the loop) and log it.
            import traceback
            print(f"cairn mcp: handler error: {e}\n{traceback.format_exc()}",
                  file=sys.stderr, flush=True)
            rid = req.get("id") if isinstance(req, dict) else None
            resp = _error(rid, -32603, "internal error.")
        if resp is not None:
            _emit(resp)


if __name__ == "__main__":
    serve()
