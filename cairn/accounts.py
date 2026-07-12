"""
cairn/accounts.py — account -> galaxy identity map.
Personal handles never ship in source. Load from ~/.cairn/accounts.json:
  {"<handle>": {"label":"Claude-<Handle>","color":"#RRGGBB","hue":201}, ...}
Falls back to a generic (empty-of-personal) default. Unknown accounts are
auto-assigned a stable hashed hue/color at render time, so imports work
with zero config. Mirrors garden._load_projects().
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

# Shipped default: NO personal handles. "gpt" is a generic public product
# name, safe to keep; the live/no-account case is handled by callers.
_ACCOUNTS_DEFAULT = {
    "gpt": {"label": "GPT", "color": "#D29922", "hue": 40},
}

def _load_accounts() -> dict:
    f = Path.home() / ".cairn" / "accounts.json"
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[str(k).lower()] = {
                        "label": str(v.get("label", "")),
                        "color": str(v.get("color", "")),
                        "hue":   v.get("hue"),
                    }
            if out:
                return out
        except Exception:
            pass
    return dict(_ACCOUNTS_DEFAULT)

ACCOUNTS = _load_accounts()

def galaxy_label(acct: str) -> str:
    """Account handle -> galaxy label. Unknown -> Titlecase; blank -> Claude-Code."""
    a = (acct or "").replace("claude backfill-", "").lower()
    if a in ACCOUNTS and ACCOUNTS[a].get("label"):
        return ACCOUNTS[a]["label"]
    return (a[:1].upper() + a[1:]) if a else "Claude-Code"


# ── attribution v2 (Spec A / A1): maker + harness-native account identity ──
# Personal ids/emails live in the user's own harness config, never in source and
# never logged. These readers return the stable ACCOUNT ID and identity hints
# ONLY — never access/refresh tokens.
_MAKER_MAP = [
    ("claude", "Claude"), ("anthropic", "Claude"),
    ("chatgpt", "GPT"), ("openai", "GPT"), ("codex", "GPT"), ("gpt", "GPT"),
    ("o1", "GPT"), ("o3", "GPT"), ("o4", "GPT"),
    ("gemini", "Gemini"), ("google", "Gemini"),
]


def maker_of(model_or_harness: str) -> str:
    """Model or harness string -> canonical maker, alias-collapsed
    (chatgpt/openai/codex -> GPT). Unknown -> titlecased family, else 'Unknown'."""
    s = (model_or_harness or "").lower()
    for needle, maker in _MAKER_MAP:
        if needle in s:
            return maker
    fam = s.split("/")[-1].split("-")[0].strip()
    return (fam[:1].upper() + fam[1:]) if fam else "Unknown"


def _mask_email(e: str) -> str:
    if "@" not in (e or ""):
        return ""
    lp, dom = e.split("@", 1)
    return lp[:2] + "***@" + dom


_IDENTITY_MEMO: dict = {}


def claude_identity() -> dict | None:
    """The currently-authenticated Claude Code account, read defensively from
    ~/.claude.json. Returns {maker,id,email,label_hint} or None. IDS ONLY — never
    tokens. Cached (the file is large). A miss just returns None -> callers fall back."""
    if "claude" in _IDENTITY_MEMO:
        return _IDENTITY_MEMO["claude"]
    res = None
    try:
        f = Path.home() / ".claude.json"
        if f.exists():
            oa = (json.loads(f.read_text(encoding="utf-8")) or {}).get("oauthAccount") or {}
            uid = oa.get("accountUuid")
            if uid:
                email = str(oa.get("emailAddress") or "")
                hint = (email.split("@")[0] if "@" in email else "") or str(oa.get("organizationName") or "")
                res = {"maker": "Claude", "id": str(uid), "email": email, "label_hint": hint}
    except Exception:
        res = None
    _IDENTITY_MEMO["claude"] = res
    return res


def codex_identity() -> dict | None:
    """The currently-authenticated Codex/OpenAI account, from ~/.codex/auth.json.
    Prefers tokens.account_id; else decodes ONLY the id_token PAYLOAD for the
    non-secret account-id/email/name claims. Returns {maker,id,email,label_hint} or
    None. IDS ONLY — access/refresh tokens are never read or returned. Cached."""
    if "codex" in _IDENTITY_MEMO:
        return _IDENTITY_MEMO["codex"]
    res = None
    try:
        f = Path.home() / ".codex" / "auth.json"
        if f.exists():
            a = json.loads(f.read_text(encoding="utf-8")) or {}
            toks = a.get("tokens") or {}
            acct = toks.get("account_id")
            email = ""
            name = ""
            idt = toks.get("id_token")
            if isinstance(idt, str) and idt.count(".") >= 2:
                import base64
                p = idt.split(".")[1]
                p += "=" * (-len(p) % 4)
                try:
                    claims = json.loads(base64.urlsafe_b64decode(p).decode("utf-8", "ignore")) or {}
                    email = str(claims.get("email") or "")
                    name = str(claims.get("name") or "")
                    authc = claims.get("https://api.openai.com/auth") or {}
                    acct = acct or authc.get("chatgpt_account_id")
                except Exception:
                    pass
            if acct:
                hint = (email.split("@")[0] if "@" in email else "") or name
                res = {"maker": "GPT", "id": str(acct), "email": email, "label_hint": hint}
    except Exception:
        res = None
    _IDENTITY_MEMO["codex"] = res
    return res


def slug_register(identity: dict | None) -> str | None:
    """Given a harness identity {maker,id,label_hint,email}, return a stable account
    slug, registering it in ~/.cairn/accounts.json keyed to the STABLE id. ID WINS:
    if this stable_id is already registered under a slug, return that slug (prevents
    duplicate galaxies and survives display-name renames). Existing entries are never
    clobbered. Returns None if the identity has no id."""
    if not identity or not identity.get("id"):
        return None
    sid = str(identity["id"])
    try:
        f = Path.home() / ".cairn" / "accounts.json"
        raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
        for slug, v in raw.items():
            if isinstance(v, dict) and str(v.get("stable_id") or "") == sid:
                return slug
        base = "".join(c for c in str(identity.get("label_hint") or identity.get("maker") or "acct").lower()
                       if c.isalnum() or c in "-_") or "acct"
        base = base[:20]   # room for a collision suffix within the [:24] account cap
        slug, n = base, 2
        while slug in raw:
            slug = f"{base}-{n}"
            n += 1
        raw[slug] = {
            "label": str(identity.get("label_hint") or slug),
            "maker": str(identity.get("maker") or ""),
            "stable_id": sid,
            "email_mask": _mask_email(str(identity.get("email") or "")),
        }
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        return slug
    except Exception:
        return None


def resolve_slug_for_setup(harness: str) -> str:
    """Best-effort account slug for keying consent at SETUP time (no session id yet).
    Mirrors vault._live_account's ladder minus the session-prefix channels:
    CAIRN_ACCOUNT env -> me.json handle -> harness-native stable id -> 'default'.
    NOTE: for a user with a handle set (e.g. the owner), this returns the handle and
    NEVER reaches the harness-id rung, so existing accounts are unaffected."""
    import os
    env = os.environ.get("CAIRN_ACCOUNT")
    if env and env.strip():
        return env.strip()[:24].lower()
    try:
        mf = Path.home() / ".cairn" / "me.json"
        if mf.exists():
            h = str((json.loads(mf.read_text(encoding="utf-8")) or {}).get("handle") or "").strip()
            if h:
                return h[:24].lower()
    except Exception:
        pass
    ident = codex_identity() if "codex" in (harness or "").lower() else claude_identity()
    return slug_register(ident) or "default"


def set_handle(name: str) -> str:
    """Set the machine-default account handle in ~/.cairn/me.json (preserving any
    channels), normalized the same way resolution reads it. Used by the setup
    walk's 'ask when nothing readable' fallback so a user-named account STICKS for
    live capture and is never re-asked. Returns the normalized handle ('' if blank)."""
    h = "".join(c for c in (name or "").strip().lower() if c.isalnum() or c in "-_")[:24]
    if not h:
        return ""
    try:
        f = Path.home() / ".cairn" / "me.json"
        cfg = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["handle"] = h
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return h


# ── Claude Desktop per-session PROOF (attribution v2 / confidence) ────────────
# The CLI ~/.claude.json oauthAccount is SINGLE-SLOT and does NOT track the
# Desktop app switching accounts (it can read one account while the Desktop UI is a
# different account). The Desktop app, however, files every code session on disk
# under the accountUuid that owns it. Matching the cairn session id to that
# file's cliSessionId is the ONLY signal that reflects Desktop-account truth —
# the load-bearing "proof" rung. IDs/paths only; the transcript body is never
# read (we scan just the small metadata head for the cliSessionId field).
_DESKTOP_MEMO: dict = {}


def _desktop_store_roots() -> list:
    """Readable Claude-Desktop claude-code-sessions roots, PACKAGED path first.
    A plain python process CANNOT traverse the %APPDATA%\\Claude reparse point
    (is_dir() -> False on a packaged/MSIX install), so the real backing path
    under %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude is
    preferred; %APPDATA%\\Claude is the fallback for non-packaged installs.
    Discovered via glob (the Claude_* package-family hash can differ per
    machine), never hardcoded. Only roots that actually exist are returned."""
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            for pkg in sorted(Path(local, "Packages").glob("Claude_*")):
                p = pkg / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions"
                if p.is_dir():
                    roots.append(p)
        except Exception:
            pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        try:
            p = Path(appdata) / "Claude" / "claude-code-sessions"
            if p.is_dir():
                roots.append(p)
        except Exception:
            pass
    # macOS: Claude Desktop files code sessions under Application Support.
    # No packaged-reparse quirk on mac, so a single path. .is_dir() below
    # filters it out on Windows/Linux, so appending it is cross-platform-safe
    # (append-only to the list — never displaces the Windows roots above).
    try:
        mac = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
        if mac.is_dir():
            roots.append(mac)
    except Exception:
        pass
    return roots


def _slug_for_account_uuid(account_uuid: str) -> "str | None":
    """Registered slug for a Claude accountUuid via accounts.json stable_id
    (read DIRECTLY — _load_accounts() whitelists only label/color/hue and would
    drop stable_id). None if the uuid is not registered."""
    aid = str(account_uuid or "")
    if not aid:
        return None
    try:
        f = Path.home() / ".cairn" / "accounts.json"
        raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if isinstance(raw, dict):
            for slug, v in raw.items():
                if isinstance(v, dict) and str(v.get("stable_id") or "") == aid:
                    return str(slug)
    except Exception:
        pass
    return None


def desktop_account(session_id: str) -> "dict | None":
    """Claude Desktop per-session PROOF. Find the desktop session file whose
    cliSessionId == the cairn session id and return the account it is filed
    under: {"slug", "account_uuid", "org_uuid"}. EXACT + UNIQUE match only —
    0 or >1 matching account folders -> None (fall through, never guess). An
    unregistered accountUuid gets a SEPARATE pending bucket 'claude-<uuid8>'
    rather than falling into a known galaxy. Memoized per session id (a
    session's owning account never changes). Any error -> None."""
    sid = str(session_id or "").strip()
    if sid in _DESKTOP_MEMO:
        return _DESKTOP_MEMO[sid]
    res = _desktop_account_uncached(sid)
    _DESKTOP_MEMO[sid] = res
    return res


def _desktop_account_uncached(sid: str) -> "dict | None":
    # only a bare 36-char uuid cairn session id can match — codex-/import-/mcp-
    # ids never appear as a Desktop cliSessionId, so skip the scan for them.
    if len(sid) != 36 or sid.count("-") != 4:
        return None
    # tolerate optional whitespace: real Desktop files are minified, but stay
    # robust if a future build pretty-prints the JSON.
    pat = re.compile('"cliSessionId"\\s*:\\s*"' + re.escape(sid) + '"')
    matches = []
    try:
        files = []
        for root in _desktop_store_roots():
            files.extend(root.glob("*/*/local_*.json"))
        # newest first: the live session's file is the freshest, so the common
        # case matches on the first read; the full scan only guards uniqueness.
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for f in files:
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:8192]
            except Exception:
                continue
            if pat.search(head):
                # <root>/<accountUuid>/<orgUuid>/local_*.json
                matches.append((f.parent.parent.name, f.parent.name))
    except Exception:
        return None
    uniq = {a for a, _ in matches}
    if len(uniq) != 1:
        return None
    acct, org = matches[0]
    slug = _slug_for_account_uuid(acct) or ("claude-" + acct[:8])
    return {"slug": slug, "account_uuid": acct, "org_uuid": org}
