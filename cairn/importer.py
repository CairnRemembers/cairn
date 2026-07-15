"""
cairn/importer.py — bring your AI history home.

Ingests official data exports from Claude (claude.ai), ChatGPT, and Gemini
into the vault as conversation_turn nodes: speaker-attributed, model-
attributed, ORIGINAL timestamps preserved, chained sequentially so old
conversations become walkable reasoning paths.

Imported memory lands COLD (tier 2) by default: fully searchable and
consolidatable, but never injected — years of history shouldn't flood the
heartbeat. The sleep cycle's consolidation pass mines it gradually; anything
that matters gets promoted by use.

SCALE: exports get big (a heavy ChatGPT history is multi-GB). The reader
STREAMS the top-level JSON array one conversation at a time, in pure stdlib
(Cairn law: no external deps — no ijson), so memory stays flat no matter the
file size. Each conversation is one atomic transaction, so a crash mid-import
resumes cleanly on re-run (finished conversations are skipped by session id).

FULL FIDELITY: the display fields query/output_preview keep their size caps
(TRUNC_QUERY/TRUNC_PREVIEW — UI + embedding conventions depend on them), but a
turn longer than the preview cap ALSO carries its COMPLETE text into the derived
episodic_text (via MicroNode.episodic_full), so nothing past 2000 chars is lost.

CONTINUED CONVERSATIONS: dedup is turn-level, not wholesale. A conversation
continued since the last import keeps the same deterministic session id; instead
of skipping it whole, the importer counts the turns already stored and appends
only the new tail (positional — exports append in order), chaining it to the
session's last node. A shorter re-export (fewer turns than stored) is reported
as "shrunk" and touched not at all (append-only). Re-running an unchanged export
is a no-op.

RICH CONTENT: Cairn is a text memory — artifacts live in their native store
(git for code, ~/.cairn/media for images), the node holds a faithful text
representation + pointer. So code is captured as text (tagged `code`), images
become a searchable `[image: name]` marker (tagged `image`), and export UI
cruft ("This block is not supported…", error echoes) is dropped at the door.

How to get your exports:
  Claude:  claude.ai -> Settings -> Privacy -> Export data
           (email arrives with a zip containing conversations.json)
  ChatGPT: chatgpt.com -> Settings -> Data Controls -> Export data
           (zip with conversations.json — point the importer at THAT file,
            not the whole unzipped folder of media)
  Gemini:  takeout.google.com -> deselect all -> select Gemini Apps (JSON)

Usage:
  python -m cairn import conversations.json --source=chatgpt
  python -m cairn import conversations.json --source=claude --since=2025-01-01
  python -m cairn import MyActivity.json    --source=gemini
  options: --tier=1|2  --limit=N (conversations)  --dry-run  --account=name
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from cairn.vault import Vault, MicroNode

TRUNC_QUERY   = 500
TRUNC_PREVIEW = 2000

# Export cruft that is NOT memory — client UI placeholders + error echoes that
# ride along inside official exports. Dropped at import so the vault lands clean
# (the import-side mirror of the live turn_hook salience gate). Substring match,
# case-insensitive. Keep this list tight: only things that are never real signal.
_IMPORT_NOISE = (
    "this block is not supported on your current",
    "an error occurred while trying to run the generate",
    "error occurred while generating",
    "viewing artifacts created via the analysis tool",
    "this content is no longer available",
    "unsupported message type",
)


def _is_noise(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(p in t for p in _IMPORT_NOISE)


def _full_text(text: str) -> Optional[str]:
    """Full-fidelity text for episodic_text, ONLY when the turn overflows the
    output_preview display cap. Under the cap → None, so short turns keep the
    normal derived (capped) episodic_text and don't carry a redundant copy —
    the same overflow-only pattern capture.write_turn and codex_hook use. Over
    the cap → the whole string, so the tail past TRUNC_PREVIEW survives."""
    return text if len(text) > TRUNC_PREVIEW else None


def _session_turn_tail(v: Vault, session: str, incoming: int):
    """Turn-level dedup for a session id that already exists (a conversation
    continued since the last import shares the same deterministic id).

    Returns (start_index, last_node_id, shrunk):
      start_index  — how many turns to skip; import only clean[start_index:].
                     Equals the stored active turn count when incoming has MORE
                     (import the tail); equals `incoming` when nothing is new.
      last_node_id — the session's most recent conversation_turn (chain anchor
                     for the tail), or None when the session has no turns.
      shrunk       — True when the stored count EXCEEDS incoming (a weird/
                     truncated export): never modify anything (append-only), skip.

    Positional matching by design: exports append turns in order, so the first
    `stored` incoming turns are the ones already in the vault and the rest are
    the new tail. No content-hash matching (deliberately — see task spec)."""
    rows = v.conn.execute(
        "SELECT id FROM nodes "
        "WHERE session=? AND kind='conversation_turn' AND status!='void' "
        "ORDER BY timestamp ASC, rowid ASC",
        (session,),
    ).fetchall()
    stored = len(rows)
    last_id = rows[-1]["id"] if rows else None
    if stored > incoming:
        return stored, last_id, True          # shrunk export → skip, touch nothing
    return stored, last_id, False             # start at `stored` (==incoming → no-op)


def _slug(text: str, n: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower()).strip("-")
    return s[:n] or "untitled"


def _iso(ts) -> str:
    """Best-effort timestamp → ISO. Accepts unix seconds, ISO strings."""
    try:
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if isinstance(ts, str) and ts:
            return datetime.fromisoformat(
                ts.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return datetime.now(timezone.utc).isoformat()


# ── streaming JSON-array reader (stdlib only — Cairn law: no external deps) ────
def _stream_array(path: Path, chunk: int = 1 << 20) -> Iterator:
    """Yield each top-level element of a JSON array, parsing incrementally so a
    multi-GB file never loads whole. Reads `chunk` bytes at a time; uses
    JSONDecoder.raw_decode to peel off one complete element as soon as the buffer
    holds it. String-aware (raw_decode handles quotes/escapes/nesting), so
    brackets or commas inside message text don't fool it. Assumes the top level
    is an array (true for Claude/ChatGPT/Gemini exports)."""
    dec = json.JSONDecoder()
    buf = ""
    started = False
    eof = False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        while not eof:
            data = f.read(chunk)
            if data:
                buf += data
            else:
                eof = True
            if not started:
                i = buf.find("[")
                if i == -1:
                    continue          # array not opened yet; keep reading
                buf = buf[i + 1:]
                started = True
            # drain every complete element currently in the buffer
            while True:
                j = 0
                while j < len(buf) and buf[j] in " \t\r\n,":
                    j += 1            # skip whitespace + element separators
                buf = buf[j:]
                if not buf:
                    break
                if buf[0] == "]":
                    return            # end of array
                try:
                    obj, end = dec.raw_decode(buf)
                except ValueError:
                    break             # element not fully buffered yet → read more
                yield obj
                buf = buf[end:]


# ── per-source, per-conversation extractors ──────────────────────────────────
# Each takes ONE conversation object and returns (title, created_iso,
# [(role, text, msg_iso, extra_tags), ...]) — role is 'user'|'agent'. Defensive
# everywhere; export formats drift.

def _chatgpt_text(msg: dict) -> tuple:
    """Pull display text + content tags from a ChatGPT message, across content
    types. Code → the code (tag 'code'); execution output → the output; images
    → a '[image: name]' marker (tag 'image') so the turn survives & is findable;
    plain/multimodal text → the string parts."""
    content = msg.get("content") or {}
    ctype = content.get("content_type") or "text"
    if ctype == "code":
        return (content.get("text") or "").strip(), ["code"]
    if ctype == "execution_output":
        return (content.get("text") or "").strip(), ["code", "output"]
    chunks, extra = [], []
    for p in (content.get("parts") or []):
        if isinstance(p, str):
            if p.strip():
                chunks.append(p)
        elif isinstance(p, dict):
            ptype = p.get("content_type") or ""
            if "image" in ptype or p.get("asset_pointer"):
                name = str(p.get("asset_pointer") or "image").split("/")[-1][:40]
                chunks.append(f"[image: {name}]")
                if "image" not in extra:
                    extra.append("image")
            elif p.get("text"):
                chunks.append(str(p["text"]))
    return " ".join(chunks).strip(), extra


def _conv_chatgpt(conv: dict) -> tuple:
    """ChatGPT conversations.json: one conversation with a 'mapping' graph."""
    title   = conv.get("title") or "untitled"
    created = _iso(conv.get("create_time"))
    turns   = []
    for node in (conv.get("mapping") or {}).values():
        msg = (node or {}).get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role") or ""
        if role not in ("user", "assistant", "tool"):
            continue  # skip system plumbing
        ctype = (msg.get("content") or {}).get("content_type") or "text"
        if role == "tool" and ctype not in ("code", "execution_output"):
            continue  # keep code-interpreter I/O, drop browsing/plugin chatter
        text, extra = _chatgpt_text(msg)
        if not text:
            continue
        spk = "user" if role == "user" else "agent"
        turns.append((spk, text, _iso(msg.get("create_time")),
                      msg.get("create_time") or 0, extra))
    turns.sort(key=lambda t: t[3])  # chronological within conversation
    return title, created, [(r, x, ts, ex) for r, x, ts, _, ex in turns]


def _conv_claude(conv: dict) -> tuple:
    """claude.ai export: one conversation with chat_messages
    [{sender: human|assistant, text|content, created_at}]."""
    title   = conv.get("name") or conv.get("title") or "untitled"
    created = _iso(conv.get("created_at"))
    turns   = []
    for m in (conv.get("chat_messages") or conv.get("messages") or []):
        sender = m.get("sender") or m.get("role") or ""
        if sender not in ("human", "user", "assistant"):
            continue
        text, extra = (m.get("text") or "").strip(), []
        if not text:
            chunks = []
            for b in (m.get("content") or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type") or ""
                if b.get("text"):
                    chunks.append(b["text"])
                elif "image" in bt:
                    chunks.append("[image]")
                    if "image" not in extra:
                        extra.append("image")
                elif bt in ("tool_use", "tool_result"):
                    if "code" not in extra:
                        extra.append("code")
                    inp = b.get("input") or b.get("content")
                    if isinstance(inp, str):
                        chunks.append(inp)
            text = " ".join(c for c in chunks if isinstance(c, str)).strip()
        if not text:
            continue
        spk = "user" if sender in ("human", "user") else "agent"
        turns.append((spk, text, _iso(m.get("created_at")), extra))
    return title, created, turns


def _extract_gemini(data) -> Iterator[tuple]:
    """Google Takeout Gemini (MyActivity.json): flat activity items grouped by
    day (Takeout has no conversation ids). Loaded whole — Takeout is small."""
    if not isinstance(data, list):
        return
    by_day: dict[str, list] = {}
    for item in data:
        title = item.get("title") or ""
        if not title.startswith("Prompted"):
            continue
        prompt = title[len("Prompted"):].strip().strip('"')
        ts     = _iso(item.get("time"))
        resp   = ""
        for s in (item.get("subtitles") or []):
            nm = s.get("name", "") if isinstance(s, dict) else ""
            if nm and not nm.startswith("Prompted"):
                resp = nm
                break
        by_day.setdefault(ts[:10], []).append((prompt, resp, ts))
    for day, items in sorted(by_day.items()):
        items.sort(key=lambda x: x[2])
        turns = []
        for prompt, resp, ts in items:
            turns.append(("user", prompt, ts, []))
            if resp:
                turns.append(("agent", resp, ts, []))
        yield f"gemini activity {day}", items[0][2], turns


# ── modern ChatGPT export (.zip: sharded conversations-NNN.json + .dat media) ──
# The current ChatGPT export shards conversations across conversations-000.json …
# NNN.json (the top-level conversations.json is just a title index) and stores
# media as file-<id>.dat blobs, mapped to original names in
# conversation_asset_file_names.json. Stream shard-by-shard (flat memory), resolve
# image pointers → .dat → a searchable "[image · made <date> · id <id>]" marker,
# and optionally copy the bytes to ~/.cairn/media so the file opens by id.
def _dat_for_pointer(asset_pointer: str) -> tuple:
    """'file-service://file-XXX' → ('file-XXX.dat', 'file-XXX')."""
    fid = (asset_pointer or "").split("//")[-1].strip()
    return f"{fid}.dat", fid


def _chatgpt_msg(msg: dict, asset_names: dict, conv_model) -> tuple:
    """One ChatGPT message → (text, extra_tags, model, image_refs). image_refs is
    a list of (file_id, dat_member, ext) so the caller can pull the bytes."""
    content = msg.get("content") or {}
    ctype = content.get("content_type") or "text"
    model = (msg.get("metadata") or {}).get("model_slug") or conv_model or "gpt-imported"
    if ctype == "code":
        return (content.get("text") or "").strip(), ["code"], model, []
    if ctype == "execution_output":
        return (content.get("text") or "").strip(), ["code", "output"], model, []
    chunks, extra, imgs = [], [], []
    day = _iso(msg.get("create_time"))[:10]
    for p in (content.get("parts") or []):
        if isinstance(p, str):
            if p.strip():
                chunks.append(p)
        elif isinstance(p, dict):
            if "image" in (p.get("content_type") or "") or p.get("asset_pointer"):
                dat, fid = _dat_for_pointer(p.get("asset_pointer", ""))
                ext = (Path(asset_names.get(dat, "")).suffix or ".png").lstrip(".").lower()
                chunks.append(f"[image · made {day} · id {fid} · {ext}]")
                imgs.append((fid, dat, ext))
                if "image" not in extra:
                    extra.append("image")
            elif p.get("text"):
                chunks.append(str(p["text"]))
    return " ".join(chunks).strip(), extra, model, imgs


def _chatgpt_turns(conv: dict, asset_names: dict) -> tuple:
    """One conversation → (title, created, [(role, text, ts, tags, model, imgs)])."""
    title = conv.get("title") or "untitled"
    created = _iso(conv.get("create_time"))
    cmodel = conv.get("default_model_slug")
    rows = []
    for node in (conv.get("mapping") or {}).values():
        msg = (node or {}).get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role") or ""
        if role not in ("user", "assistant", "tool"):
            continue
        ctype = (msg.get("content") or {}).get("content_type") or "text"
        if role == "tool" and ctype not in ("code", "execution_output"):
            continue
        text, extra, model, imgs = _chatgpt_msg(msg, asset_names, cmodel)
        if not text:
            continue
        spk = "user" if role == "user" else "agent"
        rows.append((spk, text, _iso(msg.get("create_time")),
                     msg.get("create_time") or 0, extra, model, imgs))
    rows.sort(key=lambda r: r[3])
    return title, created, [(r[0], r[1], r[2], r[4], r[5], r[6]) for r in rows]


def import_chatgpt_zip(zip_path, vault: Optional[Vault] = None, tier: int = 2,
                       limit: Optional[int] = None, since: Optional[str] = None,
                       dry_run: bool = False, account: Optional[str] = None,
                       progress: Optional[Callable[[str], None]] = None,
                       copy_media: bool = False) -> dict:
    """Import a modern ChatGPT export .zip (sharded conversations + .dat media).
    Streams shard by shard (flat memory), per-conversation atomic commit,
    idempotent by session id. copy_media also extracts referenced images to
    ~/.cairn/media/<id>.<ext> so they open by id."""
    import zipfile
    z = zipfile.ZipFile(zip_path)
    names = set(z.namelist())
    asset_names = {}
    if "conversation_asset_file_names.json" in names:
        with z.open("conversation_asset_file_names.json") as f:
            asset_names = json.load(f)
    shards = sorted(n for n in names if re.fullmatch(r"conversations-\d+\.json", Path(n).name))
    if not shards and "conversations.json" in names:
        shards = ["conversations.json"]
    v = vault or Vault()
    existing = {r["id"] for r in v.all_sessions()}
    media_dir = Path.home() / ".cairn" / "media"
    if copy_media and not dry_run:
        media_dir.mkdir(parents=True, exist_ok=True)
    report = {"conversations": 0, "resumed": 0, "skipped": 0, "shrunk": 0,
              "turns": 0, "dropped": 0,
              "images": 0, "media_copied": 0, "sessions": []}
    stop = False
    for shard in shards:
        if stop:
            break
        with z.open(shard) as f:
            convs = json.load(f)          # one shard (<10 MB) at a time → flat memory
        for conv in convs:
            if limit and report["conversations"] >= limit:
                stop = True
                break
            try:
                title, created, turns = _chatgpt_turns(conv, asset_names)
            except Exception:
                continue
            if since and created[:10] < since:
                report["skipped"] += 1
                continue
            clean = [t for t in turns if not _is_noise(t[1])]
            report["dropped"] += len(turns) - len(clean)
            if not clean:
                report["skipped"] += 1
                continue
            session = f"import-chatgpt-{created[:10]}-{_slug(title)}"
            # ── turn-level dedup: new session imports whole; existing one
            #    imports only its new tail (see _session_turn_tail). ──────────
            start, parent, resumed = 0, None, False
            if session in existing:
                start, parent, shrunk = _session_turn_tail(v, session, len(clean))
                if shrunk:
                    report["shrunk"] += 1
                    continue
                if start >= len(clean):          # nothing new → true no-op
                    report["skipped"] += 1
                    continue
                resumed = True
            existing.add(session)
            new_turns = clean[start:]            # only the tail on a resume
            if resumed:
                report["resumed"] += 1
            else:
                report["conversations"] += 1
                if len(report["sessions"]) < 50:
                    report["sessions"].append(session)
            if dry_run:
                report["turns"] += len(new_turns)
                report["images"] += sum(len(t[5]) for t in new_turns)
                continue
            base = ["import", "chatgpt"] + ([f"account:{account}"] if account else [])
            for role, text, ts, extra, model, imgs in new_turns:
                node = v.write(MicroNode(
                    session=session, kind="conversation_turn",
                    query=text[:TRUNC_QUERY], output_preview=text[:TRUNC_PREVIEW],
                    episodic_full=_full_text(text),      # FULL text if over cap
                    parent=parent, speaker=role,
                    model="human" if role == "user" else (model or "gpt-imported"),
                    agent_role="worker", memory_tier=tier, timestamp=ts,
                    tags=base + extra,
                ), commit=False)
                parent = node.id
                report["turns"] += 1
                report["images"] += len(imgs)
                if copy_media:
                    for fid, dat, ext in imgs:
                        try:
                            dest = media_dir / f"{fid}.{ext}"
                            if not dest.exists() and dat in names:
                                with z.open(dat) as src:
                                    dest.write_bytes(src.read())
                                report["media_copied"] += 1
                        except Exception:
                            pass
            if account:
                # explicit --account is a human-declared label -> LOCKED, so a
                # later live write can never silently overwrite an import.
                v.conn.execute(
                    "INSERT INTO sessions (id, started_at, account, account_locked) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "account = excluded.account, account_locked = 1",
                    (session, created, account))
            v.conn.commit()
            if progress and report["conversations"] % 200 == 0:
                progress(f"  …{report['conversations']} convs, {report['turns']} turns, "
                         f"{report['images']} images, {report['dropped']} dropped")
    return report


# source → (per-conversation extractor or None for the load-path, model name)
_CONV = {
    "chatgpt": (_conv_chatgpt, "gpt-imported"),
    "claude":  (_conv_claude,  "claude-imported"),
    "gemini":  (None,          "gemini-imported"),   # day-grouped, load-path
}
EXTRACTORS = _CONV   # kept for cmd_import's `source not in EXTRACTORS` check


def import_export(
    path: Path,
    source: str,
    vault: Optional[Vault] = None,
    tier: int = 2,
    limit: Optional[int] = None,
    since: Optional[str] = None,
    dry_run: bool = False,
    account: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Import an AI provider's data export. Streams the file (flat memory at any
    size). Idempotent: a conversation whose deterministic session id already
    exists is skipped, so re-running on a crashed or newer export only adds
    what's new. Each conversation commits as one transaction (crash-safe).
    """
    # modern ChatGPT export is a .zip (sharded conversations + .dat media)
    if str(path).lower().endswith(".zip") and source == "chatgpt":
        return import_chatgpt_zip(path, vault=vault, tier=tier, limit=limit,
                                  since=since, dry_run=dry_run, account=account,
                                  progress=progress)

    conv_extractor, model_name = _CONV[source]
    v = vault or Vault()
    existing = {r["id"] for r in v.all_sessions()}
    report = {"conversations": 0, "resumed": 0, "skipped": 0, "shrunk": 0,
              "turns": 0, "dropped": 0, "sessions": []}

    # gemini is day-grouped (needs the whole file); the others stream.
    if source == "gemini":
        conv_iter = _extract_gemini(json.loads(Path(path).read_text(encoding="utf-8")))
        prebuilt = True
    else:
        conv_iter = _stream_array(Path(path))
        prebuilt = False

    for item in conv_iter:
        if limit and report["conversations"] >= limit:
            break
        try:
            if prebuilt:
                title, created, turns = item
            else:
                title, created, turns = conv_extractor(item)
        except Exception:
            continue  # one malformed conversation never kills the import

        if since and created[:10] < since:
            report["skipped"] += 1
            continue

        # drop export cruft before anything else
        clean = []
        for role, text, ts, extra in turns:
            if _is_noise(text):
                report["dropped"] += 1
                continue
            clean.append((role, text, ts, extra))
        if not clean:
            report["skipped"] += 1
            continue

        session = f"import-{source}-{created[:10]}-{_slug(title)}"
        # ── turn-level dedup ─────────────────────────────────────────────────
        # A brand-new session imports whole; an existing one imports only its new
        # tail (a conversation continued since the last import keeps the same
        # deterministic id, so wholesale skipping stranded its new turns).
        start, parent, resumed = 0, None, False
        if session in existing:
            start, parent, shrunk = _session_turn_tail(v, session, len(clean))
            if shrunk:
                report["shrunk"] += 1
                continue
            if start >= len(clean):              # nothing new → true no-op
                report["skipped"] += 1
                continue
            resumed = True                       # tail present → resume this convo
        existing.add(session)
        new_turns = clean[start:]                # only the tail on a resume
        if resumed:
            report["resumed"] += 1
        else:
            report["conversations"] += 1
            if len(report["sessions"]) < 50:     # sample only — huge imports
                report["sessions"].append(session)
        if dry_run:
            report["turns"] += len(new_turns)
            continue

        base_tags = ["import", source] + ([f"account:{account}"] if account else [])
        for role, text, ts, extra in new_turns:
            node = v.write(MicroNode(
                session        = session,
                kind           = "conversation_turn",
                query          = text[:TRUNC_QUERY],
                output_preview = text[:TRUNC_PREVIEW],
                episodic_full  = _full_text(text),       # FULL text if over cap
                parent         = parent,                 # old convos → chains
                speaker        = role,
                model          = "human" if role == "user" else model_name,
                agent_role     = "worker",
                memory_tier    = tier,
                timestamp      = ts,                     # ORIGINAL time preserved
                tags           = base_tags + extra,
            ), commit=False)                             # batch the conversation
            parent = node.id
            report["turns"] += 1

        if account:
            # explicit --account is a human-declared label -> LOCKED, so a later
            # live write can never silently overwrite an import.
            v.conn.execute(
                "INSERT INTO sessions (id, started_at, account, account_locked) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET "
                "account = excluded.account, account_locked = 1",
                (session, created, account))
        v.conn.commit()  # one atomic commit per conversation → crash-safe resume

        if progress and report["conversations"] % 200 == 0:
            progress(f"  …{report['conversations']} conversations, "
                     f"{report['turns']} turns, {report['dropped']} dropped")

    if not dry_run:
        v.conn.commit()
    return report
