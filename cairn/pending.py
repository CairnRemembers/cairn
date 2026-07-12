"""
cairn/pending.py — the per-session tool-call buffer (5a).

The node-per-tool-call explosion is gone. PostToolUse (hook.py) no longer
writes a vault node for every call; it APPENDS a one-line JSON record here, to
~/.cairn/pending_tools/<session>.jsonl. At turn-end (turn_hook.py, Stop) the
buffer is drained and baked into the agent's conversation_turn as metadata —
one exchange ≈ one node. The live dashboard tails these files so tools still
show AS THEY FIRE, mid-turn, before the turn node exists.

Single source of truth for the path + record shape so the three callers
(hook, turn_hook, dashboard) never drift.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

PENDING_DIR = Path.home() / ".cairn" / "pending_tools"


def _safe_name(session: str) -> str:
    """Filesystem-safe session filename (session ids are uuids, but be defensive)."""
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (session or "unknown"))
    return (keep or "unknown")[:120]


def buffer_path(session: str) -> Path:
    return PENDING_DIR / f"{_safe_name(session)}.jsonl"


def append(session: str, record: dict) -> None:
    """Append one tool-call record. Best-effort — must never break capture."""
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        with open(buffer_path(session), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read(session: str) -> list[dict]:
    """Read buffered records without removing them (the live feed uses this)."""
    p = buffer_path(session)
    if not p.exists():
        return []
    out = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        return []
    return out


def drain(session: str) -> list[dict]:
    """Read + clear the buffer atomically-enough (rename then read). Returns the
    records that were pending for this session. Called once per turn at Stop."""
    p = buffer_path(session)
    if not p.exists():
        return []
    tmp = p.with_suffix(".jsonl.draining")
    try:
        p.rename(tmp)              # claim the buffer so a concurrent append starts fresh
    except Exception:
        tmp = p                    # rename failed (locked?) — fall back to read-in-place
    out = []
    try:
        for ln in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        out = []
    finally:
        try:
            if tmp != p and tmp.exists():
                tmp.unlink()
            elif tmp == p and p.exists():
                p.unlink()
        except Exception:
            pass
    return out


def sweep_stale(max_age_hours: float = 24.0) -> int:
    """Delete buffer files untouched for `max_age_hours` — leftovers from sessions
    that ended uncleanly (crash / kill) and never reached drain(). Without this they
    pile up and the live feed replays them as 'live'. The records inside are orphaned
    tool-calls whose turn never finalized — granular activity, not primary memory
    (captured turns persist as their own nodes), so dropping them is safe. Best-effort;
    run at startup. Returns the count removed."""
    import time
    if not PENDING_DIR.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for p in PENDING_DIR.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            pass
    return removed
