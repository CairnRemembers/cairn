"""
cairn/retrieve.py — the token-saving layer (Karpathy/Graphify pattern, for memory).

Two halves:

  INGEST — pull project files into the vault as searchable chunks
           (kind='file_chunk', cold tier). Code/docs/configs become
           queryable memory instead of files an AI must re-read.

  FETCH  — the retrieval primitive an AI calls INSTEAD of reading the repo:
           one query → a compact, token-budgeted context pack. Top hits
           verbatim, the rest as gists. Replaces the "re-read 20k tokens
           every session" tax with a targeted ~1k-token answer.

Local, zero external calls. The embeddings do the work.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from cairn.vault import Vault, MicroNode

# What to ingest — source/docs/config. Binaries and build noise skipped.
INGEST_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".md", ".txt", ".json",
    ".html", ".css", ".sql", ".sh", ".yaml", ".yml", ".toml", ".rs",
    ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".vue", ".svelte",
}
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
    "build", ".next", ".cache", "target", ".idea", ".vscode", "media",
}
MAX_FILE_BYTES = 400_000   # skip giant generated files
CHUNK_CHARS    = 1400      # ~350 tokens/chunk — precise retrieval granularity
PREVIEW_CHARS  = 1800


def _chunk(text: str, path: str) -> list[str]:
    """
    Split a file into retrieval chunks at natural boundaries:
    markdown headings, then blank-line blocks, then hard windows.
    """
    text = text.replace("\r\n", "\n")
    if path.endswith(".md"):
        parts = re.split(r"\n(?=#{1,4}\s)", text)
    else:
        parts = re.split(r"\n\s*\n", text)

    chunks, buf = [], ""
    for p in parts:
        if len(buf) + len(p) < CHUNK_CHARS:
            buf += ("\n\n" if buf else "") + p
        else:
            if buf.strip():
                chunks.append(buf)
            while len(p) > CHUNK_CHARS:
                chunks.append(p[:CHUNK_CHARS])
                p = p[CHUNK_CHARS:]
            buf = p
    if buf.strip():
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def ingest_path(
    root: Path,
    vault: Optional[Vault] = None,
    project: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Ingest a file or directory into the vault as file_chunk nodes.

    Idempotent per (path, mtime): unchanged files skip; changed files void
    their old chunks and re-add — the vault always reflects current code,
    and history stays archived (append-only invariant intact).
    """
    v = vault or Vault()
    root = Path(root)
    project = project or (root.name if root.is_dir() else root.parent.name)

    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in INGEST_EXT:
                    files.append(p)

    report = {"files": 0, "chunks": 0, "skipped": 0, "updated": 0,
              "project": project}

    for f in files:
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                report["skipped"] += 1
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            report["skipped"] += 1
            continue

        rel      = str(f)
        mtime    = int(f.stat().st_mtime)
        file_tag = f"file:{rel}"

        existing = v.conn.execute(
            "SELECT id, tags FROM nodes WHERE status='active' AND tags LIKE ?",
            (f'%"{file_tag}"%',)
        ).fetchall()
        if existing:
            if any(f'"mtime:{mtime}"' in (e["tags"] or "") for e in existing):
                report["skipped"] += 1
                continue
            for e in existing:           # file changed → retire stale chunks
                v.void(e["id"])
            report["updated"] += 1

        chunks = _chunk(text, rel)
        report["files"] += 1
        report["chunks"] += len(chunks)
        if dry_run:
            continue

        for i, ch in enumerate(chunks):
            v.write(MicroNode(
                session        = f"files-{project}",
                kind           = "file_chunk",
                query          = f"{f.name} [{i+1}/{len(chunks)}]: " + ch[:300],
                output_preview = ch[:PREVIEW_CHARS],
                model          = "ingest",
                memory_tier    = 2,   # cold — retrieval-only, never floods injection
                tags           = ["file", file_tag, f"mtime:{mtime}",
                                  f"ext:{f.suffix.lower().lstrip('.')}", project],
            ))

    return report


def _strong_neighbors(v: Vault, node_ids: list[str], limit: int = 6) -> list[dict]:
    """
    Graph-RAG hop: the strongest precomputed edges out of the top hits.
    A query lands on a node; what that node is WIRED to is often the answer's
    other half (the decision behind the turn, the warning next to the plan).
    Returns neighbor rows not already in node_ids, best edge first.
    """
    if not node_ids:
        return []
    seen = set(node_ids)
    qmarks = ",".join("?" * len(node_ids))
    rows = v.conn.execute(f"""
        SELECT src, dst, weight FROM edges
        WHERE type = 'semantic' AND tier = 'strong'
          AND (src IN ({qmarks}) OR dst IN ({qmarks}))
        ORDER BY weight DESC LIMIT 40
    """, node_ids + node_ids).fetchall()

    out, picked = [], []
    for r in rows:
        nb = r["dst"] if r["src"] in seen else r["src"]
        if nb in seen:
            continue
        seen.add(nb)
        picked.append((nb, float(r["weight"])))
        if len(picked) >= limit:
            break
    if not picked:
        return []
    qmarks = ",".join("?" * len(picked))
    found = {n["id"]: n for n in (dict(x) for x in v.conn.execute(
        f"SELECT id, kind, session, gist, query, tags FROM nodes "
        f"WHERE id IN ({qmarks}) AND status = 'active'",
        [p[0] for p in picked]))}
    for nid, w in picked:
        if nid in found:
            n = found[nid]
            n["edge_weight"] = w
            out.append(n)
    return out


def _current_session() -> str:
    f = Path.home() / ".cairn" / "last_session.txt"
    try:
        return f.read_text().strip() if f.exists() else ""
    except Exception:
        return ""


def _session_origin(v, session_id, cache):
    """(account, harness) for a session id, cached per pack. (None, None) when
    unknown — provenance is decoration, never a hard dependency."""
    if not session_id:
        return (None, None)
    if session_id in cache:
        return cache[session_id]
    acct = harn = None
    try:
        row = v.conn.execute(
            "SELECT account, harness FROM sessions WHERE id=?",
            (session_id,)).fetchone()
        if row:
            acct, harn = row["account"], row["harness"]
    except Exception:
        pass                      # pre-migration vaults have no such columns
    cache[session_id] = (acct, harn)
    return (acct, harn)


def _origin_label(acct, harn):
    """'account/harness' provenance tag; '' when nothing is known (honest
    'unknown' — we never invent an origin)."""
    return "/".join(p for p in (acct, harn) if p)


def fetch_pack(
    query: str,
    vault: Optional[Vault] = None,
    budget_tokens: int = 1500,
    k: int = 20,
    channel: str = "fetch",
    account: Optional[str] = None,
) -> dict:
    """
    THE token-saving retrieval: one query → only what matters, fitted to a
    budget. Strongest hits get verbatim text; the rest return as gists with
    ids for follow-up. An AI calls this instead of re-reading sources.

    Graph-RAG: after the similarity hits, the pack appends gists of nodes
    strongly WIRED to the top hits (precomputed edges table) — connected
    context similarity alone would miss. Costs a few tokens, no embedding work.
    """
    v = vault or Vault()
    hits = v.query_episodic(query, k=k)

    origin_cache: dict = {}
    if account:                    # optional galaxy filter — case-insensitive
        want = account.strip().lower()
        kept = []
        for d in hits:
            acct, _ = _session_origin(v, d.get("session"), origin_cache)
            if acct and acct.strip().lower() == want:
                kept.append(d)
        hits = kept

    pack, used = [], 0
    for i, d in enumerate(hits):
        gist = d.get("gist") or (d.get("query") or "")[:90]

        src = "memory"
        tags = d.get("tags") or "[]"
        m = re.search(r'"file:([^"]+)"', tags if isinstance(tags, str) else "")
        if m:
            src = m.group(1)
        elif d.get("session"):
            src = d["session"][:40]

        verbatim = ""
        body = d.get("output_preview") or d.get("query") or ""
        est_full = max(1, len(body) // 4)
        if used + est_full <= budget_tokens and i < 8:
            verbatim = body[:PREVIEW_CHARS]
            used += est_full
        else:
            used += max(1, len(gist) // 4)

        acct, harn = _session_origin(v, d.get("session"), origin_cache)
        pack.append({
            "id":     d.get("id"),
            "kind":   d.get("kind"),
            "source": src,
            "gist":   gist,
            "text":   verbatim,     # "" when only the gist fit the budget
            "score":  round(d.get("score", 0), 3),
            "origin": _origin_label(acct, harn),
        })

    # graph-RAG hop — wired context the cosine ranking can't see
    linked = []
    try:
        top_ids = [r["id"] for r in pack[:6] if r["id"]]
        for n in _strong_neighbors(v, top_ids):
            gist = n.get("gist") or (n.get("query") or "")[:90]
            used += max(1, len(gist) // 4)
            linked.append({
                "id":     n["id"],
                "kind":   n.get("kind"),
                "source": (n.get("session") or "")[:40],
                "gist":   gist,
                "weight": round(n.get("edge_weight", 0), 3),
            })
            if used >= budget_tokens:
                break
    except Exception:
        pass   # edges table may not exist yet — fetch works without it

    # attention-ledger receipts: a pulled memory was SHOWN, same as a pushed
    # one — the scheduler needs the complete attention history either way
    shown = [r["id"] for r in pack if r["id"]] + [l["id"] for l in linked]
    v.record_shown(shown, channel=channel, session=_current_session(),
                   trigger=query[:80])

    return {"query": query, "results": pack, "linked": linked,
            "tokens_est": used, "count": len(pack)}


def drift_pack(
    query: str,
    vault: Optional[Vault] = None,
    hops: int = 3,
    k: int = 10,
) -> dict:
    """
    The creative complement to fetch_pack. fetch answers "what's relevant?" —
    strong ties, same topic. drift answers "what's ADJACENT that I'd never
    think to ask for?" — it walks the precomputed edge graph outward from the
    query's best hits, preferring medium/weak semantic ties that CROSS topic
    communities. Granovetter: new ideas arrive through weak ties; the strong
    ones only know what you already know.

    Scoring per hop: edge_weight x tier_mult x community_mult x hop_decay.
      tier_mult      weak 1.0 / medium 0.9 / strong 0.5  (penalize the obvious)
      community_mult 1.5 crossing a topic boundary / 0.7 staying home
      hop_decay      0.8 per hop

    Pure graph walk on the edges table — no model, no embedding call. Runs
    on anything (the NPU/Pi story). Results are receipted in the attention
    ledger as channel='drift'; cited drift hits are confirmed creative leaps.
    """
    TIER_MULT = {"weak": 1.0, "medium": 0.9, "strong": 0.5}
    HOP_DECAY = 0.8
    CROSS, HOME = 1.5, 0.7

    v = vault or Vault()
    seeds = v.query_episodic(query, k=5)
    seed_ids = [s["id"] for s in seeds if s.get("id")]
    if not seed_ids:
        return {"query": query, "results": [], "seeds": []}

    adj: dict = {}
    comm: dict = {}
    for r in v.conn.execute(
            "SELECT src, dst, tier, weight FROM edges WHERE type='semantic'"):
        w = float(r["weight"] or 0.5) * TIER_MULT.get(r["tier"], 0.8)
        adj.setdefault(r["src"], []).append((r["dst"], w))
        adj.setdefault(r["dst"], []).append((r["src"], w))
    for r in v.conn.execute(
            "SELECT id, community FROM nodes WHERE community IS NOT NULL"):
        comm[r["id"]] = (r["community"] or "").partition("|")[0]

    seed_set = set(seed_ids)
    best: dict = {}      # node -> (score, hop, via)
    frontier = {sid: 1.0 for sid in seed_ids}
    for hop in range(1, hops + 1):
        nxt: dict = {}
        for nid, score in frontier.items():
            for nb, w in adj.get(nid, []):
                if nb in seed_set:
                    continue
                boundary = CROSS if comm.get(nb) != comm.get(nid) else HOME
                s = score * w * boundary * HOP_DECAY
                if s > best.get(nb, (0,))[0]:
                    best[nb] = (s, hop, nid)
                    nxt[nb] = max(nxt.get(nb, 0.0), s)
        frontier = nxt
        if not frontier:
            break

    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:k]
    if not ranked:
        return {"query": query, "results": [],
                "seeds": [s.get("gist") or (s.get("query") or "")[:60]
                          for s in seeds[:3]]}

    qmarks = ",".join("?" * len(ranked))
    rows = {r["id"]: r for r in v.conn.execute(
        f"SELECT id, kind, session, gist, query, community FROM nodes "
        f"WHERE id IN ({qmarks}) AND status='active'",
        [nid for nid, _ in ranked])}

    origin_cache: dict = {}
    results = []
    for nid, (score, hop, via) in ranked:
        r = rows.get(nid)
        if not r:
            continue
        acct, harn = _session_origin(v, r["session"], origin_cache)
        results.append({
            "id":      nid,
            "kind":    r["kind"],
            "source":  (r["session"] or "")[:40],
            "gist":    r["gist"] or (r["query"] or "")[:90],
            "topic":   (r["community"] or "").partition("|")[2],
            "score":   round(score, 3),
            "hops":    hop,
            "origin":  _origin_label(acct, harn),
        })

    v.record_shown([r["id"] for r in results], channel="drift",
                   session=_current_session(), trigger=query[:80])

    return {"query": query, "results": results,
            "seeds": [s.get("gist") or (s.get("query") or "")[:60]
                      for s in seeds[:3]]}


def render_drift(pack: dict) -> str:
    """Render a drift pack — adjacent-possible context, clearly labeled."""
    lines = [
        f'# cairn wander: "{pack["query"]}"',
        "# weak-tie walk — adjacent ideas, NOT direct answers. The unseen",
        "# connections live here: different topics, faint edges, old sessions.",
        "",
    ]
    if pack.get("seeds"):
        lines.append("wandering out from: " + " | ".join(pack["seeds"]))
        lines.append("")
    if not pack["results"]:
        lines.append("(nothing to wander to — vault may need `cairn edges` rebuilt)")
        return "\n".join(lines)
    for r in pack["results"]:
        topic = f" [{r['topic']}]" if r["topic"] else ""
        tag = f"  · {r['origin']}" if r.get("origin") else ""
        lines.append(f"~ [{r['kind']}]{topic} {r['gist']}")
        lines.append(f"    {r['hops']} hop(s) out, wander score {r['score']} "
                     f"(id {r['id']}){tag}")
    return "\n".join(lines)


def render_pack(pack: dict) -> str:
    """Render a fetch pack as a drop-in context block (human/AI readable)."""
    lines = [
        f'# cairn fetch: "{pack["query"]}"',
        f"# {pack['count']} results, ~{pack['tokens_est']} tokens "
        f"(instead of re-reading sources wholesale)",
        "",
    ]
    for r in pack["results"]:
        tag = f"  · {r['origin']}" if r.get("origin") else ""
        lines.append(f"## [{r['kind']}] {r['source']}  (score {r['score']}){tag}")
        if r["text"]:
            lines.append(r["text"])
        else:
            lines.append(f"  -> {r['gist']}   (id {r['id']} for full text)")
        lines.append("")
    if pack.get("linked"):
        lines.append("## wired to the top hits (graph edges, not similarity):")
        for r in pack["linked"]:
            lines.append(f"  ~ [{r['kind']}] {r['gist']}   "
                         f"(edge {r['weight']}, id {r['id']})")
        lines.append("")
    return "\n".join(lines)
