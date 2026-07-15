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

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cairn", "version": "1.0.0"}

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


def _tool_fetch(args: dict) -> str:
    from cairn.retrieve import fetch_pack, render_pack
    pack = fetch_pack(args["query"], vault=_vault(),
                      budget_tokens=int(args.get("budget_tokens", 1500)),
                      account=(args.get("account") or None),
                      channel="mcp_fetch")
    return render_pack(pack)


def _tool_wander(args: dict) -> str:
    from cairn.retrieve import drift_pack, render_drift
    pack = drift_pack(args["query"], vault=_vault(),
                      hops=max(1, min(5, int(args.get("hops", 3)))),
                      k=max(1, min(40, int(args.get("k", 10)))))
    return render_drift(pack)


def _tool_search(args: dict) -> str:
    v = _vault()
    rows = v.query_episodic(args["query"], k=int(args.get("k", 10)))
    if not rows:
        return "no matches."
    out = [f"{len(rows)} results for {args['query']!r}:"]
    for d in rows:
        gist = d.get("gist") or (d.get("query") or "")[:80]
        out.append(f"  [{d.get('id')}] ({d.get('kind')}, {d.get('score',0):.2f}) {gist}")
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
    tags = (args.get("tags") or []) + ["mcp"]
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
        head = page_one(_vault(), account=_live_account("")).strip() + "\n\n"
    except Exception:
        p1 = Path.home() / ".cairn" / "PAGE_ONE.md"
        if p1.exists():
            try:
                head = p1.read_text(encoding="utf-8", errors="replace").strip() + "\n\n"
            except Exception:
                head = ""
    proto_root = Path.home() / ".cairn" / "protocols"
    if not proto_root.exists():
        return head + "no PROTOCOL.md yet — fresh start."
    # newest protocol by mtime
    protos = list(proto_root.glob("*/PROTOCOL.md"))
    if not protos:
        return head + "no PROTOCOL.md yet — fresh start."
    newest = max(protos, key=lambda p: p.stat().st_mtime)
    text = newest.read_text(encoding="utf-8", errors="replace")
    body = text[:4000] + ("\n... (truncated; use cairn_fetch to dig deeper)"
                          if len(text) > 4000 else "")
    return head + body


def _tool_recent(args: dict) -> str:
    v = _vault()
    limit = int(args.get("limit", 12))
    rows = v.conn.execute("""
        SELECT id, kind, query FROM nodes
        WHERE status='active'
          AND kind IN ('decision','open_item','warning','idea','resolved')
        ORDER BY timestamp DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        return "vault is empty."
    out = ["working set (most recent):"]
    for r in rows:
        out.append(f"  [{r['id']}] ({r['kind']}) {(r['query'] or '')[:90]}")
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
    for want in ids:
        rows = v.conn.execute(
            "SELECT id, kind, status, session, speaker, model, timestamp, tags, "
            "       query, output_preview, episodic_text "
            "FROM nodes WHERE id LIKE ? || '%' LIMIT 3", (want,)).fetchall()
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
    for r in rows:
        mark = "·" if r["emb"] else "○"
        t = (r["timestamp"] or "?")[11:16]
        sess = (r["session"] or "")[:26]
        out.append(f"  {mark} [{r['id']}] {t} {r['kind']}/{r['speaker'] or '?'} ({sess}) {r['gist']}")
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
    """Handle one JSON-RPC request. Returns a response dict, or None for
    notifications (which get no reply)."""
    method = req.get("method")
    id_    = req.get("id")
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

    if method == "notifications/initialized":
        return None  # notification, no reply

    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if not fn:
            return _error(id_, -32601, f"unknown tool: {name}")
        try:
            text = fn(args)
        except Exception as e:
            text = f"cairn error: {e}"
        return _result(id_, {"content": [{"type": "text", "text": text}]})

    if method == "ping":
        return _result(id_, {})

    if id_ is not None:
        return _error(id_, -32601, f"method not found: {method}")
    return None


def serve() -> None:
    """Read JSON-RPC messages line-delimited from stdin, write replies to
    stdout. Logs go to stderr only (stdout is the protocol channel)."""
    # this process IS the harness when nothing else declared one — self-stamp
    # so captures born under the MCP server read 'mcp' rather than 'unknown'.
    os.environ.setdefault("CAIRN_HARNESS", "mcp")
    print("cairn mcp server ready (stdio)", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
