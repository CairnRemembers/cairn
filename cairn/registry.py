"""
The project registry — no project ever lost (Lane B).

Born from a correction: a session marked the owner's dormant projects
"don't revisit" and he ruled hard the other way — parked with state,
never ignored; dormant is not dead. The registry is that rule as
structure instead of prose.

Rows live as APPEND-ONLY vault nodes (the house pattern: nodes canonical,
files compiled out). A project's current registry state = the NEWEST
registry node for its slug; history is every older one, never edited.
Agents PROPOSE rows; only the human BLESSES them into declared projects
(or passes — revivably). Registry nodes carry the 'registry' tag, which
the display register already treats as machine work, so twenty seeded
proposals never flood Today/Fresh — the Projects tab reads them directly.

Status vocabulary (registry lifecycle, human-driven):
    proposed  — an agent found evidence this was/is a project
    blessed   — the human confirmed it; it becomes a declared project
    passed    — the human said "not a project" (kept, revivable)
    archived  — the human shelved a blessed row (kept, revivable)
    revived   — back from passed/archived
Life-status (active/dormant) stays OBSERVED from node activity elsewhere —
the registry never asserts liveness it can't prove.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

_BLOB = re.compile(r"<registry-json>(\{.*?\})</registry-json>", re.S)
ACTIONS = ("bless", "pass", "archive", "revive")
_NEXT = {"bless": "blessed", "pass": "passed",
         "archive": "archived", "revive": "revived"}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:48]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rows(vault) -> dict:
    """Fold the append-only ledger: newest registry node per slug wins.
    Returns {slug: state-dict}; unparseable nodes are skipped, never fatal."""
    out: dict = {}
    for r in vault.conn.execute("""
            SELECT output_preview, timestamp FROM nodes
            WHERE status='active' AND tags LIKE '%"registry-row"%'
            ORDER BY timestamp ASC"""):
        m = _BLOB.search(r["output_preview"] or "")
        if not m:
            continue
        try:
            st = json.loads(m.group(1))
        except Exception:
            continue
        slug = st.get("slug")
        if slug:
            st["as_of"] = r["timestamp"]
            out[slug] = st          # ASC order → last write is newest
    return out


def _write_row(vault, state: dict, line: str):
    from cairn.vault import MicroNode
    body = f"{line}\n<registry-json>{json.dumps(state, ensure_ascii=False)}</registry-json>"
    return vault.write(MicroNode(
        session="registry",
        kind="decision",
        query=line[:500],
        output_preview=body,
        model=state.get("by") or "registry",
        tags=["registry", "registry-row", f"proj:{state['slug']}"],
    ))


def propose(vault, name: str, aliases: list | None = None,
            evidence: int = 0, span: str = "", code: str = "",
            why: str = "", by: str = "agent", account: str = "") -> str | None:
    """Append a PROPOSED row. Refuses (returns None) when the slug already
    has a registry row or is already a declared project — proposals never
    shout over existing state."""
    slug = slugify(name)
    if not slug or slug in rows(vault):
        return None
    try:
        from pathlib import Path
        pf = Path.home() / ".cairn" / "projects.json"
        if pf.exists() and slug in json.loads(pf.read_text(encoding="utf-8")):
            return None
    except Exception:
        pass
    state = {"slug": slug, "name": name, "status": "proposed",
             "aliases": [a for a in (aliases or []) if isinstance(a, str)][:40],
             "evidence": int(evidence), "span": span[:60], "code": code[:160],
             "why": why[:300], "by": by, "account": account,
             "proposed_at": _now()}
    line = (f"REGISTRY {slug} — proposed — {name}"
            f" ({evidence} notes{', ' + span if span else ''}) — {why[:120]}")
    return _write_row(vault, state, line).id


def act(vault, slug: str, action: str, reason: str = "",
        by: str = "human") -> dict | None:
    """Append a status change. Returns the NEW state dict, or None when the
    slug is unknown or the action invalid. bless/pass/archive/revive only —
    there is no delete; nothing leaves the ledger."""
    if action not in ACTIONS:
        return None
    cur = rows(vault).get(slug)
    if not cur:
        return None
    state = dict(cur)
    state.pop("as_of", None)
    state["status"] = _NEXT[action]
    state["last_action"] = {"action": action, "reason": reason[:200],
                            "by": by, "at": _now()}
    line = (f"REGISTRY {slug} — {state['status']} by {by}"
            f"{' — ' + reason[:120] if reason else ''}")
    _write_row(vault, state, line)
    return state


def compile_finish_lines(vault, path=None) -> int:
    """Compile the ledger out to FINISH-LINES.md (BOOK.md pattern: derived
    file, rerun any time; the nodes stay canonical). Returns row count."""
    from pathlib import Path
    path = path or (Path.home() / ".cairn" / "FINISH-LINES.md")
    state = rows(vault)
    order = {"blessed": 0, "proposed": 1, "revived": 2,
             "archived": 3, "passed": 4}
    lines = ["# FINISH LINES — every project ever, no silent drops",
             f"_compiled {_now()[:16].replace('T', ' ')} UTC from the "
             f"append-only registry ledger; edit nothing here — bless/pass "
             f"in the Garden's Projects tab._", ""]
    for st in sorted(state.values(),
                     key=lambda s: (order.get(s.get("status"), 9),
                                    -(s.get("evidence") or 0))):
        lines.append(
            f"- **{st.get('name', st['slug'])}** — {st.get('status')} · "
            f"{st.get('evidence', 0)} notes"
            f"{' · ' + st['span'] if st.get('span') else ''}"
            f"{' · code: ' + st['code'] if st.get('code') else ''}"
            f"{' — ' + st['why'] if st.get('why') else ''}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(state)
