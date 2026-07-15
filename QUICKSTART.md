# Cairn — Field Manual

> **The complete human manual.** New here? The [README](README.md) gets you running in
> ~5 minutes; this is the deep reference for when you want every option and fix.

> **Build knowledge. Leave signals.**
> Tools for modern explorers. Local-first episodic memory for you and your AI.

A cairn is a stack of stones that marks a trail for whoever comes next. This one
marks the trail of your *thinking* — every decision, dead end, and reason becomes
a node you (and any model) can find again, across sessions and across model
generations. **Nothing leaves your machine.**

---

## Before you set out
- **Python 3.11+** — check with `python --version`
- ~**1 GB** free disk — mostly PyTorch (the embedder); the model itself downloads once, on first use
- **Windows:** if any output looks garbled, prefix commands with `python -X utf8 -m cairn …`
  (the console needs UTF-8 to print Cairn's maps). The installer already does this for you.

---

## 1 · Install — one command
From the `cairn` folder:

**Windows**
```powershell
.\install.ps1
```
> If Windows blocks it with *"running scripts is disabled on this system,"* run:
> `powershell -ExecutionPolicy Bypass -File .\install.ps1`

**macOS / Linux**
```bash
./install.sh
```
> Downloaded the ZIP instead of `git clone`? The run bit can be stripped — use
> `bash install.sh` (or `chmod +x install.sh` first).

Prefer to do it by hand? That's all the script does:
```bash
pip install -e ".[all]"      # the package + embedder + dashboard
```
Lighter builds: `".[embeddings]"` (no dashboard) · `".[dashboard]"` (no embedder).

Your **vault auto-creates** at `~/.cairn/` on first run. No account, no cloud, no sign-in.

---

## 2 · Verify the wiring
```bash
python -m cairn doctor   # 'cairn doctor' also works once your PATH picks up the new command
```
One glance confirms everything's in place — package, dependencies, the vault, the
embedder, capture scope. It ends with **`all set.`** when you're good. Run it any
time something feels off; it's always the first move.

---

## 3 · Open the brain
```bash
cairn dashboard          # → http://localhost:7331
```
A fresh vault shows an empty galaxy — that's correct, you haven't left any signals
yet. **Galaxy** is the full map; **garden** (`/garden`) is the human-readable
Hub / Book / Index.

---

## 4 · Leave your first signal
```bash
cairn note --kind=decision "chose SQLite over Postgres — local-first, zero ops"
cairn fetch "what did I decide about the database"
```
`fetch` is token-budgeted recall; `cairn wander "<topic>"` walks the weak ties for
serendipity.

---

## 5 · Turn on ambient memory  *(optional — off by default)*
Let Cairn capture as you work with an AI, instead of noting everything by hand:

| Scope | Command | Captures |
|---|---|---|
| **Off** (default) | — | only what you `cairn note` |
| **This project** | `cairn connect` | every chat in this repo |
| **Everywhere** | `cairn connect --global` | every Claude Code chat on this machine |

A connected session **auto-orients** at the start, **records as it works** — the
conversation *and* the tools it runs, grouped under each turn — and **compiles +
embeds** at the end. Per-project and global are mutually exclusive; `cairn doctor`
flags a conflict. Reverse anytime: `cairn disconnect [--global]`.

**Privacy controls** — not every chat belongs in your brain:
- Skip one chat: `set CAIRN_CAPTURE=0` in that shell.
- Pause everywhere: `cairn capture off` (resume: `cairn capture on`).
- Secrets are scrubbed before write (append-only, fail-closed).

> Using **Claude Desktop / Cursor / any MCP client**? Point it at `python -m cairn mcp`
> and you get all eight as native tools — `cairn_orient / fetch / search / recent /
> read / logs / wander / note` — see the [README](README.md#connect-it-to-your-ai-assistant) for
> the config block. Using **OpenAI Codex**? See [§6](#6--use-cairn-from-codex).

---

## 6 · Use Cairn from Codex  *(optional)*
**OpenAI Codex** (desktop app + CLI) gets the vault three ways: the eight tools over
MCP, optional agentic capture of notify-fired turns (agentic events + filtered helper
calls, **not** plain chat), and a protocol file that makes Codex use memory without
being asked. All three are opt-in.

### 6a · Tools via MCP
Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.cairn]
command = '<full-path-to-python>'
args = ["-X", "utf8", "-m", "cairn", "mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 120
default_tools_approval_mode = "approve"
```

- `<full-path-to-python>` — the interpreter that can `import cairn` (the one your
  install used). On Windows use forward slashes or escaped backslashes inside the
  quotes.
- `-X utf8` — required on Windows so the server can print Cairn's Unicode output.
- `default_tools_approval_mode = "approve"` — lets Codex call the tools without a
  per-call approval popup, which would otherwise auto-cancel in a headless run.

**Restart Codex** (fully quit the desktop app, or start a new CLI thread) so it
re-reads the config and launches the server.

**Smoke test:** in a Codex chat, ask it to *call the `cairn_orient` tool*. A fresh
vault returns an empty-but-valid digest — that's success. You now have all eight:
`cairn_orient`, `cairn_fetch`, `cairn_search`, `cairn_recent`, `cairn_read`,
`cairn_logs`, `cairn_wander`, `cairn_note`.

### 6b · Agentic capture  *(optional — off by default)*
Wrap Codex's `notify` hook so notify-fired turns (agentic/computer-use turns and
filtered backstage helper events) are captured as `conversation_turn` nodes (session
`codex-<thread-id>`, tagged `codex` + `conversation`), no manual noting. Note: Codex
does **not** fire `notify` for plain conversational chat, so those turns aren't
captured here — full plain-chat capture is a separate command, `import codex-sessions`
(§6d below). `cairn_note` remains the on-demand path for salience:

```bash
python -X utf8 -m cairn codex-hook install
```

- **Wraps, never replaces** — if you already had a `notify` command, it keeps
  running (it's replayed after capture). `config.toml` is backed up once first.
- **Fail-safe by design** — capture can never break or delay Codex; any failure is
  logged and the turn is skipped.
- **Reverse it:** `python -X utf8 -m cairn codex-hook uninstall` restores the exact
  original notify.
- **Check it:** `python -X utf8 -m cairn codex-hook status` shows whether it's
  installed, the current notify line, and the tail of the debug log.

**Live test:** run one turn in Codex, then `python -X utf8 -m cairn codex-hook status`
(or `python -m cairn fetch "codex"`) to see the captured turns.

### 6c · The `AGENTS.md` protocol
A global `~/.codex/AGENTS.md` makes Codex orient at session start, fetch before
answering personal/history questions, and write salience notes tagged `codex`.
Create the file and paste this in:

```markdown
# Cairn memory protocol — MANDATORY in every session

You have eight Cairn MCP tools: `cairn_orient`, `cairn_fetch`, `cairn_search`,
`cairn_recent`, `cairn_read`, `cairn_logs`, `cairn_wander`, `cairn_note`. Cairn is the owner's local,
append-only, cross-model memory vault. Other AI sessions read and write the same
vault you do. Your job in EVERY chat: use it like a native.

## FIRST ACTION — before answering the user's first message
Call `cairn_orient`. Then begin your first reply with one line:
`[cairn: oriented — N nodes]` so the owner can see the protocol is live.
Do this in every new chat, unprompted, even for casual questions.

## READ — reach into memory before guessing
Call `cairn_fetch` BEFORE answering whenever the user asks about:
- their life, history, preferences, vehicles, family, purchases, projects
- anything that happened in past sessions (any model's)
- prior decisions, plans, or "what was I doing with X"
Never answer "I don't know about your personal context" without fetching first.
Use `cairn_wander` when brainstorming; `cairn_recent` for "what's been going on".

## READ DEEPER — gists are the index, not the text
`search`/`recent`/`logs`/`wander` return short gists; `fetch` returns capped
previews. The COMPLETE stored text of any node is one call away: `cairn_read`
with the id(s) (`max_chars` dials depth). Before relying on a summary of
something that matters — an audit, a decision, "what did X say" — read the
full node. Never quote a gist as if it were the whole record.

## WRITE — cairn_note without being asked, when these happen
- The user states a DECISION, preference, or plan → kind=decision
- The user shares a durable FACT about their life/work → kind=insight
- A problem gets diagnosed or solved → kind=resolved
- A risk, gotcha, or mistake is discovered → kind=warning
- Something is left unfinished / to do → kind=open_item
- A question is raised but not answered → kind=question

Rules for every note:
- tags MUST include `"codex"` plus topical tags (e.g. `"cairn"`, `"trucks"`).
  The codex tag is your provenance stamp — never omit it.
- One note per salient moment. Not a transcript: a casual chat may deserve
  0 notes; a working session may deserve 5. Capture when the STATE of things
  changed, not every message.
- Write notes in plain, complete sentences with concrete specifics.
  GOOD: "User decided to hold the X build until after launch; protocol-only
  for now." BAD: "talked about stuff."
- Capture SILENTLY — do NOT announce each note. A `[cairn: noted]` on every
  reply is UI noise; the owner sees what's captured in the dashboard. One
  `[cairn: oriented — N]` at the START of a session is plenty
  — never a marker per message.

## NEVER
- Never delete/overwrite memory — impossible by design (append-only). If a
  memory is wrong, write a correcting note (kind=warning) that names the
  wrong node's id.
- Never send vault contents to any external service beyond your own context.
- Never modify Cairn's code (your Cairn checkout) unless the owner explicitly
  asks in the current chat.
- Nothing about this project is public. No sharing, no publishing.

## If the owner says "follow your Cairn protocol"
That means you have drifted: immediately cairn_orient, re-read this file's
rules, and resume the READ/WRITE habits above.
```

### 6d · Full plain-chat capture  *(optional — `import codex-sessions`)*
The notify hook (§6b) only sees agentic/notify-fired turns. Your **plain**
conversation with Codex is written to `~/.codex/sessions/**/rollout-*.jsonl` — this
command reads those (READ-ONLY) and files them as `conversation_turn` nodes, deduped
against the hook so the two never double-capture.

```bash
python -X utf8 -m cairn import codex-sessions            # dry-run: scope + counts, writes nothing
python -X utf8 -m cairn import codex-sessions --apply    # write the new-going-forward turns
```

- **Dry-run by default** — prints threads/turns/date-span and changes nothing. Review
  the numbers, then re-run with `--apply` (a reversible manifest is saved to `~/.cairn/`
  first; imported nodes are append-only and can be voided).
- **Forward-only by default** — the first `--apply` sets a watermark, so existing
  history stays on disk untouched. Add `--include-before=YYYY-MM-DD` to backfill a
  bounded window of past chat on purpose.
- **Attribution** — `--account=NAME` stamps + locks the account. The store has no
  per-record account id, so everything is attributed to one account: a second OpenAI
  login on the same machine can't be told apart (a documented known limit).

The three Codex→vault paths, kept distinct: `cairn_note` = salience · `notify` =
agentic events · `import codex-sessions` = full plain chat.

### Troubleshooting
- **Tools don't show up?** Restart Codex (quit the desktop app fully, or open a
  new CLI thread) so it relaunches the MCP server after a config change.
- **Garbled output / Unicode errors on Windows?** The `-X utf8` flag in `args` is
  what fixes it — make sure it's there.
- **A turn asks for approval, or the tool call cancels itself?** Set
  `default_tools_approval_mode = "approve"` in the config block above.
- **Capture not landing?** Read the debug log at `~/.cairn/codex_hook_debug.log`
  (also shown by `codex-hook status`) — every capture failure records the raw
  payload there.

---

## Where your memory lives · back this up
| Folder | What | Back up? |
|---|---|---|
| the **code** (this repo) | replaceable | no |
| **`~/.cairn/`** | your **vault** — `cairn.db`, irreplaceable | **yes — this is your memory** |

To back up Cairn, copy `~/.cairn/`. To move machines, copy it across. That's the whole story.

---

## If something's off
1. **`cairn doctor`** — always first; it names exactly what's missing.
2. **Garbled output on Windows?** Use `python -X utf8 -m cairn …`.
3. **Dashboard won't load, or shows the old UI?** Restart it and **hard-refresh**
   the browser (`Ctrl+Shift+R`) — the page is served inline and caches hard.
4. **`cairn` not found?** The editable install puts it on your PATH — re-run the
   installer, or just use `python -m cairn …`.

---

## The laws
**Local-first** (nothing leaves this machine) · **append-only** (memories are
voided, never deleted) · **model-agnostic** · **no heavy deps**.

*Knowledge is a trail, not a destination.*
