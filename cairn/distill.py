"""
cairn/distill.py — turn raw conversation turns into sharp, connectable CLAIM nodes.

The problem: ~97% of a captured vault is raw `conversation_turn`s ("user said: so
I was thinking…"), whose embeddings are bland and weakly connected. The fix is to
distill each conversation into the sharp CLAIMS it actually contains.

MODEL-AGNOSTIC by design: the EXTRACTION (reading a conversation, writing the
claims) is done by whatever model is already in the loop — the connected agent
(Claude/GPT/Gemini), or a local model. This module does NOT call a model itself
(charter: local-first, no bundled LLM, no API key). It owns the two parts that
must be correct and consistent regardless of distiller:
  1. EXTRACT_PROMPT — the standardized instruction every distiller fills.
  2. write_claims() — validates + writes the claims as append-only nodes, linked
     back to their source turns, so they flow through the EXISTING embed / edge /
     atlas machinery natively (a claim is just a well-formed meaning-kind node —
     no schema change, no new index).

Why this beats fine-tuning the embedder: the metric is already good (raw MiniLM
tested ~99.7% precise); the win is in WHAT we embed. A sharp claim embeds far
better than the raw turn it came from — proven: distillation roughly halved the
orphan rate in the sandbox. Keep the one fixed embedder; feed it better text.

INVOCATION (agent-driven, the model-agnostic flow):
  1. Agent reads a conversation's turns (e.g. `cairn chain <session>` / the vault).
  2. Agent fills EXTRACT_PROMPT and returns a JSON list of claims.
  3. Caller passes that list to write_claims(vault, session, claims, distiller=<model>).
The raw turns are never mutated or deleted — claims are NEW nodes that point back.
"""
from __future__ import annotations
from typing import Optional

# Cairn's real meaning-kinds; distiller kinds outside this map fall back sensibly.
CLAIM_KINDS = {"decision", "insight", "idea", "question", "warning",
               "open_item", "procedure", "resolved", "hypothesis"}
_KIND_MAP = {"fact": "insight", "preference": "insight", "task": "open_item",
             "blocker": "warning", "todo": "open_item", "note": "insight"}

EXTRACT_PROMPT = """\
You are distilling a raw AI conversation into its durable CLAIMS for an episodic
memory. Read the turns and extract the sharp, standalone assertions worth keeping
— decisions, insights, ideas, questions, warnings, open items. A jumpy chat holds
several distinct claims; extract each separately. Skip chit-chat, acknowledgements,
and process noise.

Return a JSON array; each item:
{
  "claim":    "<=160 chars, ONE sharp assertion, leads with the conclusion, no pronouns",
  "keywords": ["3-8 salient lowercase terms"],
  "entities": ["named things: people, projects, tools, files, places"],
  "kind":     "decision|insight|idea|question|warning|open_item|procedure|resolved|hypothesis",
  "stance":   "asserted|rejected|open|superseded",
  "evidence": ["the turn/node ids this claim was drawn from"]
}
Be faithful: never invent claims that aren't supported by the turns. Sharp > many.
"""


def claim_embed_text(claim: str, keywords=None, entities=None) -> str:
    """Enriched text for embedding (A-MEM style: augment, never truncate). Used by
    callers that want keywords/entities folded into the vector; the default node
    path embeds the sharp claim alone, which is already most of the win."""
    parts = [claim.strip()]
    if keywords: parts.append("keywords: " + ", ".join(keywords))
    if entities: parts.append("entities: " + ", ".join(entities))
    return " | ".join(p for p in parts if p)


def _norm_kind(k: Optional[str]) -> str:
    k = (k or "insight").lower().strip()
    if k in CLAIM_KINDS:
        return k
    return _KIND_MAP.get(k, "insight")


def write_claims(vault, source_session: str, extractions: list,
                 distiller: str = "agent", tier: int = 1,
                 commit: bool = True, timestamp: str = None) -> list:
    """Write distilled claims as append-only meaning-kind nodes.

    extractions: list of dicts shaped by EXTRACT_PROMPT. Each becomes one node:
      kind   = the claim's kind (mapped to a real Cairn kind)
      query  = the sharp claim (clean — drives display/gist + the embed text)
      parent = first evidence turn id (so the claim hangs off its source)
      tags   = ['claim','prov:distilled', by:<distiller>, kw:*, entity:*, distills:<turn>]
    timestamp: the real source date for the claims. If None, defaults to the
      source SESSION's started_at (the true conversation date) so backfilled /
      imported claims are dated WHEN THEY HAPPENED, not today. Without this,
      MicroNode defaults to now() and every import reads as 'today'.
    Returns the new node ids. Raw turns are untouched (append-only).
    """
    from cairn.vault import MicroNode
    if timestamp is None:                 # inherit the source session's real date
        row = vault.conn.execute(
            "SELECT started_at FROM sessions WHERE id = ?", (source_session,)
        ).fetchone()
        timestamp = row[0] if row and row[0] else None
    written = []
    for e in extractions:
        if not isinstance(e, dict):
            continue
        claim = str(e.get("claim") or "").strip()
        if not claim:
            continue
        kws = [str(k).lower().strip() for k in (e.get("keywords") or []) if str(k).strip()][:8]
        ents = [str(x).strip() for x in (e.get("entities") or []) if str(x).strip()][:8]
        ev = [str(t).strip() for t in (e.get("evidence") or []) if str(t).strip()]
        stance = str(e.get("stance") or "asserted").lower().strip()
        tags = ["claim", "prov:distilled", f"by:{distiller}", f"stance:{stance}"]
        tags += [f"kw:{k}" for k in kws]
        tags += [f"entity:{x}" for x in ents]
        tags += [f"distills:{t}" for t in ev[:20]]
        node = MicroNode(
            session        = source_session,
            kind           = _norm_kind(e.get("kind")),
            query          = claim[:500],
            output_preview = (claim + ("  ·  " + ", ".join(kws) if kws else ""))[:2000],
            parent         = (ev[0] if ev else None),
            speaker        = "agent",
            model          = f"distilled:{distiller}",
            agent_role     = "curator",
            memory_tier    = tier,
            tags           = tags,
        )
        if timestamp:                     # date to the real source date, not now()
            node.timestamp = timestamp
        node = vault.write(node, commit=commit)
        written.append(node.id)
    if not commit:
        vault.conn.commit()
    return written
