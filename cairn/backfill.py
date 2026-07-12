"""
cairn/backfill.py — agent-driven backfill: turn already-captured conversations
(native OR imported) into distilled CLAIM nodes, connected by edges.

DESIGN (model-agnostic, Cairn law: no bundled LLM): the EXTRACTION step — reading
a conversation and writing its claims — is done by the connected agent (Claude/
GPT/local), exactly like distill.py. This module owns the deterministic scaffolding
the agent drives:

  plan()      — which sessions still need distilling (+ a token-cost estimate so the
                user gets a real warning before spending), idempotent: already-
                distilled sessions are skipped. The basis of the import-time warning.
  reset_*()   — for "get it right" on a vault that was backfilled wrong: void the
                old nodes for a session so a re-run REPLACES instead of duplicating
                (append-only: void, never delete). No dup is possible — session ids
                are deterministic and done-tracking skips finished ones.
  ingest()    — write the agent's claims for a session (wraps distill.write_claims).
  finalize()  — embed the new claims + rebuild edges, then AUDIT the entity bridges
                (the generic-entity instrumentation) so over-bridging is visible.

SOURCE OF TEXT: native sessions are full-text already in the vault. Imports are
truncated stubs in the vault, so for full fidelity the distiller reads the original
export file (claude/chatgpt) when given one; otherwise it falls back to the stub
and the estimate is flagged as a floor.

  EVERYONE maps to one path:
    you   :  cairn backfill claude --reset  (void truncated stubs, distill full text)
    buddy :  same, opt-in, whenever he's ready (his wrong backfill voided + redone)
    fresh :  distill runs at import time behind the cost warning — connected day one
"""
from __future__ import annotations
import json
import zipfile
import re
from pathlib import Path
from typing import Optional

from cairn.vault import Vault
from cairn.distill import write_claims
from cairn.importer import (_conv_claude, _conv_chatgpt, _chatgpt_turns,
                            _stream_array, _slug, _is_noise)

# ── The extraction quality bar — SINGLE SOURCE OF TRUTH ──────────────────────
# "How to turn a conversation into good memory." Kept HERE, in the tool, not in
# a loose doc — so the command hands agents the rules and nothing has to be
# spelled out separately or kept in sync. `cairn backfill` prints EXTRACTION_SPEC
# when it tells the agent to distill; `cairn backfill --prompt` emits the full
# RECONSTRUCT_PROMPT for porting in a conversation Cairn never captured.
EXTRACTION_SPEC = """\
HOW TO EXTRACT — Cairn stores the reasoning PATH, not just conclusions:

- Capture what the user WANTED, the options weighed and REJECTED (and why), the
  problems found, the things learned, and the decisions reached — in order.
- Capture by SALIENCE, not by quota. Skip pleasantries and pure mechanics
  ("ok", "thanks", "yes do it"). A dense decision with rejected alternatives may
  produce several nodes; a quiet stretch may produce none.
- Never invent. If a detail (e.g. a timestamp) is unknown, say so.
- Each node's text is self-contained — the WHAT, the WHY, and any alternatives
  rejected and the reason — readable cold in six months.
- kind is one of: decision · warning · open_item · insight · idea · hypothesis ·
  procedure · resolved · question · context_stamp · conversation_turn.
- importance is 1-10 (10 = a load-bearing decision; 3 = minor context).
- Chain each node to the one it followed from, so the reasoning thread survives."""


# The paste-in prompt for RECONSTRUCTING a chat Cairn never saw (headless / never
# installed / un-exportable). Embeds EXTRACTION_SPEC once so the rules never drift.
RECONSTRUCT_PROMPT = (
    "Backfill a Cairn-less conversation into the vault\n"
    "=================================================\n"
    "Use this when a conversation happened WITHOUT Cairn (headless, never\n"
    "installed, or on a platform you can't export) and you want to port its\n"
    "memory in after the fact. This is RECONSTRUCTION, not distillation — for\n"
    "chats Cairn already captured or imported, use `cairn backfill` instead.\n"
    "\n"
    "STEPS\n"
    "  1. Paste THE PROMPT below into that conversation (or give it the transcript).\n"
    "  2. Save the model's output as backfill.jsonl.\n"
    "  3. Port it in, then build the derived layers:\n"
    "       python -m cairn import-session backfill.jsonl\n"
    "       python -m cairn embed\n"
    "       python -m cairn edges\n"
    "\n"
    "──────── THE PROMPT (paste this) ────────\n"
    "You are extracting THIS conversation into structured episodic-memory nodes\n"
    "for a system called Cairn.\n"
    "\n"
    + EXTRACTION_SPEC + "\n"
    "\n"
    "Output JSONL — one JSON object per line, in chronological order. Fields:\n"
    "  ref        - a stable local id you assign (n1, n2, ...) so later nodes point back\n"
    "  when       - the real timestamp if shown (ISO 8601), else \"turn N\"\n"
    "  kind       - one of the kinds listed above\n"
    "  speaker    - user or agent (who said/decided it)\n"
    "  gist       - <=90-char summary (the headline)\n"
    "  text       - the full self-contained record (what, why, alternatives rejected)\n"
    "  tags       - array: project tag(s), topic(s), any due:YYYY-MM-DD\n"
    "  parent     - the ref of the node this followed from, or null\n"
    "  importance - 1-10\n"
    "\n"
    "Start with one context_stamp node describing the session (date/source, what\n"
    "it was about, and that this is a RECONSTRUCTED backfill — a model summary,\n"
    "accurate to the transcript but not verbatim). Then walk the conversation\n"
    "start->end, emitting nodes and chaining each to its parent. Pair user\n"
    "positions with the decisions they produced. Output ONLY the JSONL.\n"
    "\n"
    "──────── EXAMPLE OUTPUT (shape) ────────\n"
    '{"ref":"n1","when":"turn 1","kind":"context_stamp","speaker":"agent",'
    '"gist":"Backfill: app naming session","text":"Reconstructed backfill of a '
    'ChatGPT session about naming a note app. Model summary, accurate to '
    'transcript, not verbatim.","tags":["appname","backfill"],"parent":null,'
    '"importance":7}\n'
    '{"ref":"n2","when":"turn 4","kind":"decision","speaker":"user","gist":'
    '"NAME LOCKED: Fernwood","text":"Chose Fernwood over the shortlist because '
    'it felt calm and personal; rejected the punchier coinages as too '
    'startup-y.","tags":["appname"],"parent":"n1","importance":9}'
)

# empirical tokens per SOURCE char = input read + claims out + agent overhead.
# NATIVE/dev work is claim-DENSE so it costs ~2x imports:
#   imports (blind Claude full-text, trivia-diluted): 200,819 tok / 672,911 ch ≈ 0.30
#   native  (all-substance dev sessions, measured):                          ≈ 0.55
# Source-aware so estimates never undershoot the way a flat 0.30 did on native.
TOKENS_PER_CHAR = {"native": 0.55, "claude": 0.35, "gpt": 0.35,
                   "chatgpt": 0.35, "all": 0.45}

def _rate(source: str) -> float:
    return TOKENS_PER_CHAR.get(source, 0.45)
_SRC_WHERE = {
    "native":  "session NOT LIKE 'import-%'",
    "claude":  "session LIKE 'import-claude-%'",
    "gpt":     "session LIKE 'import-chatgpt-%'",
    "chatgpt": "session LIKE 'import-chatgpt-%'",
    "all":     "1=1",
}


def distilled_sessions(v: Vault) -> set:
    """Sessions already distilled = those with a live distilled:* claim node.
    Void'd claims don't count, so a reset un-marks a session for re-distilling."""
    return {r["session"] for r in v.conn.execute(
        "SELECT DISTINCT session FROM nodes "
        "WHERE model LIKE 'distilled:%' AND status != 'void'")}


def plan(v: Vault, source: str = "all", reset: bool = False) -> dict:
    """Read-only. The sessions that still need distilling for `source`, with a
    token-cost estimate from the text in the vault. For truncated imports the
    estimate is a FLOOR (real full-text source is larger) — pass an export to
    estimate() for the accurate number."""
    where = _SRC_WHERE.get(source, "1=1")
    rows = v.conn.execute(
        f"SELECT session, COUNT(*) turns, "
        f"COALESCE(SUM(MAX(LENGTH(COALESCE(query,'')),LENGTH(COALESCE(output_preview,'')))),0) ch "
        f"FROM nodes WHERE status!='void' AND kind='conversation_turn' AND ({where}) "
        f"GROUP BY session").fetchall()
    done = set() if reset else distilled_sessions(v)
    pending = [r for r in rows if r["session"] not in done]
    chars = sum(r["ch"] for r in pending)
    truncated = source in ("claude", "gpt", "chatgpt")  # vault holds stubs for imports
    return {
        "source": source, "reset": reset,
        "total_sessions": len(rows),
        "already_done": len(rows) - len(pending),
        "pending": len(pending),
        "sessions": [r["session"] for r in pending],
        "vault_chars": chars,
        "est_tokens": int(chars * _rate(source)),
        "estimate_is_floor": truncated,
    }


def _source_convs(path: Path):
    """Yield (session_id, [turn_texts], chars) from an export file, full text,
    reusing the importer's own extractors (so session ids match the vault)."""
    path = Path(path)
    if str(path).lower().endswith(".zip"):
        z = zipfile.ZipFile(path)
        names = set(z.namelist())
        shards = sorted(n for n in names if re.fullmatch(r"conversations-\d+\.json", Path(n).name))
        if not shards and "conversations.json" in names:
            shards = ["conversations.json"]
        for shard in shards:
            for conv in json.load(z.open(shard)):
                yield from _emit(conv)
    else:
        for conv in _stream_array(path):
            yield from _emit(conv)


def _emit(conv):
    if not isinstance(conv, dict):
        return
    try:
        if "mapping" in conv:
            title, created, turns = _conv_chatgpt(conv); src = "chatgpt"
        elif "chat_messages" in conv or "messages" in conv:
            title, created, turns = _conv_claude(conv); src = "claude"
        else:
            return
        texts = [t[1] for t in turns if not _is_noise(t[1])]
        if texts:
            yield (f"import-{src}-{created[:10]}-{_slug(title)}", texts,
                   sum(len(t) for t in texts))
    except Exception:
        return


def estimate(v: Vault, source: str, source_path: Optional[str] = None,
             reset: bool = False) -> dict:
    """Accurate cost estimate. With an export path, measures FULL-TEXT size for the
    pending sessions; otherwise falls back to plan()'s vault-stub floor."""
    p = plan(v, source, reset)
    if not source_path:
        return p
    pend = set(p["sessions"])
    chars = sum(ch for sid, _t, ch in _source_convs(source_path) if sid in pend)
    p["vault_chars"] = chars
    p["est_tokens"] = int(chars * _rate(source))
    p["estimate_is_floor"] = False
    return p


def warning(p: dict) -> str:
    """Human-readable cost warning for the opt-in prompt."""
    tok = p["est_tokens"]
    mins = max(1, round(p["pending"] * 3.5 / 60))  # ~measured per-conv w/ parallelism
    floor = "  (FLOOR — full-text source is larger)" if p.get("estimate_is_floor") else ""
    done = f"  ·  {p['already_done']} already done (skipped)" if p["already_done"] else ""
    return (f"Backfill [{p['source']}]: {p['pending']} conversations{done}\n"
            f"  est. ~{tok:,} tokens{floor}  ·  ~{mins} min\n"
            f"  Distill now? this is the only big spend; it runs once.")


def reset_session(v: Vault, session: str, drop_turns: bool = False) -> int:
    """Void a session's prior CLAIM nodes so a re-run replaces them (no dup).
    drop_turns also voids the raw turns — used for imports, to replace truncated
    stubs with full-text-derived claims. Native turns are kept (full-text, the
    real captured timeline). Append-only: status->void, never deleted."""
    n = v.conn.execute(
        "UPDATE nodes SET status='void' WHERE session=? AND status!='void' "
        "AND model LIKE 'distilled:%'", (session,)).rowcount
    if drop_turns:
        n += v.conn.execute(
            "UPDATE nodes SET status='void' WHERE session=? AND status!='void' "
            "AND kind='conversation_turn'", (session,)).rowcount
    v.conn.commit()
    return n


def ingest(v: Vault, session: str, claims: list, distiller: str = "claude") -> list:
    """Write the agent's distilled claims for one session (append-only)."""
    return write_claims(v, session, claims, distiller=distiller)


def finalize(v: Vault) -> dict:
    """Embed any new claim nodes, rebuild edges, and audit entity bridges."""
    from cairn.backends.embed import get_embedder
    import os
    rows = v.conn.execute(
        "SELECT id, COALESCE(episodic_text, query, output_preview) t "
        "FROM nodes WHERE embedding IS NULL AND status!='void'").fetchall()
    if rows:
        blobs = get_embedder().encode([r["t"] for r in rows])
        for r, b in zip(rows, blobs):
            v.conn.execute("UPDATE nodes SET embedding=? WHERE id=?", (b, r["id"]))
        v.conn.commit()
    os.environ.setdefault("CAIRN_ENTITY_EDGES", "1")
    from cairn.edges import build_edges
    stats = build_edges(v)
    stats["embedded"] = len(rows)
    stats["entity_audit"] = audit_entity_bridges(v)
    return stats


def audit_entity_bridges(v: Vault, top: int = 25) -> list:
    """Instrumentation for the generic-entity risk: list entities that bridge >1
    conversation, widest first. A generic term over-bridging shows up here as an
    entity spanning many unrelated conversations — caught by eye, fixed with data."""
    import collections
    ent = collections.defaultdict(set)
    for r in v.conn.execute("SELECT session, tags FROM nodes WHERE status!='void' AND tags LIKE '%entity:%'"):
        try:
            for t in json.loads(r["tags"] or "[]"):
                if isinstance(t, str) and t.startswith("entity:"):
                    ent[t[7:]].add(r["session"])
        except Exception:
            pass
    spanning = sorted(((e, len(s)) for e, s in ent.items() if len(s) >= 2),
                      key=lambda kv: -kv[1])
    return spanning[:top]
