# Cairn

> **Build knowledge. Leave signals.** · Tools for modern explorers.

**Local-first episodic memory for AI agents — and for you.**

**New here? → [`QUICKSTART.md`](QUICKSTART.md)** gets you running in five steps.

A cairn is a stack of stones that marks a path. This one marks the path of your
thinking: every decision, dead end, and reason gets captured as a node, embedded,
and made retrievable across sessions and across model generations. Nothing leaves
your machine.

- **Local-first** — everything lives in `~/.cairn/cairn.db`. No cloud, no accounts.
- **Append-only** — memories are voided, never deleted. The record is the record.
- **Model-agnostic** — works with any agent that can run a shell command or speak MCP.
- **No heavy deps** — stdlib + numpy, plus an optional embedder and dashboard.

## Install

Requires Python 3.11+. On Windows, prefix commands with `python -X utf8` so the
console can print Cairn's box-drawing + emoji output. Tested on Windows and
Linux (including WSL); macOS should work the same way — macOS reports welcome.

**One command** — from the `cairn` folder:

**Windows** (PowerShell) — if it's blocked with *"running scripts is disabled,"* use `powershell -ExecutionPolicy Bypass -File .\install.ps1`:
```powershell
.\install.ps1
```
**macOS / Linux:**
```bash
./install.sh
```

It installs the package + deps and runs `cairn doctor` to confirm everything's
wired. By hand instead:

```bash
cd <path-to>/cairn
pip install -e ".[all]"        # embedder + dashboard
cairn doctor                    # verify; the vault auto-creates on first run
```

Lighter installs: `".[embeddings]"` (no dashboard) · `".[dashboard]"` (no embedder).
The first `embed` downloads the ~80 MB `all-MiniLM-L6-v2` model from HuggingFace
once (semantic `fetch`/`query` use it after that; before any nodes are embedded
they fall back to keyword search) — the only time Cairn itself reaches out.

## First run

```bash
python -m cairn orient          # inherited context (empty on a fresh vault — fine)
python -m cairn note --kind=decision "chose X over Y because Z"
python -m cairn embed           # build the semantic index (downloads the model once)
python -m cairn fetch "what did we decide about X"
python -m cairn dashboard       # the brain at http://localhost:7331
```

## What to back up

Only one folder matters: your **vault** at `~/.cairn/` (that's `cairn.db` — your
actual memories). **Back that one up.** Everything else is replaceable — the code
lives on GitHub, and reinstalling never touches your vault.

## Connect it to your AI assistant

**Claude Desktop / Cursor / any MCP client** — add to your client's MCP config:

```json
{ "mcpServers": { "cairn": {
    "command": "<full-path-to-python>",
    "args": ["-X", "utf8", "-m", "cairn", "mcp"] } } }
```

You'll get all eight as native tools: `cairn_orient`, `cairn_fetch`, `cairn_search`,
`cairn_recent`, `cairn_read`, `cairn_logs`, `cairn_wander`, `cairn_note`.

**Claude Code CLI** — `cairn connect` opts a project into ambient memory: it
writes hooks into `.claude/settings.json` so every session **auto-orients** at
the start and **captures as it works** — *tool calls and the conversation itself*
(decisions, preferences) — then **compiles + embeds** at the end. Nothing leaves
your machine; it all lands in the local vault.

## Use Cairn from Codex

**OpenAI Codex** (desktop app + CLI) reaches the vault the same way any MCP client
does, plus two Codex-specific extras. Full walkthrough in
[`QUICKSTART.md`](QUICKSTART.md#6--use-cairn-from-codex); the shape:

**Tools via MCP** — add to `~/.codex/config.toml`, then restart Codex:

```toml
[mcp_servers.cairn]
command = '<full-path-to-python>'
args = ["-X", "utf8", "-m", "cairn", "mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 120
default_tools_approval_mode = "approve"
```

You get all eight tools: `cairn_orient`, `cairn_fetch`, `cairn_search`, `cairn_recent`,
`cairn_read`, `cairn_logs`, `cairn_wander`, `cairn_note`. `-X utf8` matters on Windows (the server prints Unicode).
`approve` mode lets Codex run the tools without a per-call popup that would otherwise
auto-cancel in a headless run. The `<python>` you name must be able to `import cairn`.

**Optional agentic capture** — one command wraps Codex's `notify` so notify-fired
turns (agentic/computer-use turns plus filtered backstage helper events) land in the
vault as `conversation_turn` nodes (tagged `codex`, `conversation`), no per-turn effort.
Codex does **not** fire `notify` for plain conversational chat, so those turns aren't
captured this way — full plain-chat capture is a separate command,
`cairn import codex-sessions` (below). `cairn_note` stays the on-demand path for
salience notes:

```bash
python -X utf8 -m cairn codex-hook install     # status · uninstall to reverse
```

It **wraps** any existing notify — OpenAI's own plumbing keeps running, replayed
after capture — backs up `config.toml` first, and is fail-safe: capture can never
break or delay Codex. `uninstall` restores the original notify exactly. Off by default.

**Full plain-chat capture** — `cairn import codex-sessions` reads the transcripts
Codex writes to `~/.codex/sessions` (the plain conversation the notify hook can't see)
and files them as `conversation_turn` nodes, deduped against the hook so the two never
double-capture. **Dry-run by default** — it prints scope/counts and changes nothing;
`--apply` writes (a reversible manifest is saved first). **Forward-only by default:** a
watermark set on the first `--apply` separates history-on-disk from new-going-forward,
so you're never silently backfilled — `--include-before=YYYY-MM-DD` opts into a bounded
history import. Attribution is single-account via `--account` (the store has no
per-record account id — a documented limit).

```bash
python -X utf8 -m cairn import codex-sessions            # dry-run preview
python -X utf8 -m cairn import codex-sessions --apply    # write forward turns
```

So the three Codex→vault paths stay distinct: **`cairn_note`** = salience notes ·
**notify** = agentic/notify-fired events · **`import codex-sessions`** = full plain chat.

**The `AGENTS.md` protocol** — a global `~/.codex/AGENTS.md` tells Codex to orient at
session start, fetch before answering personal/history questions, and write salience
notes tagged `codex`. QUICKSTART has the full copy-paste template.

## If you are an AI agent installing Cairn for a human — the consent walk

You are probably the one reading this. After the install works, do this,
in the human's own language, before wiring anything:

0. **Check whether it's already been answered — this is a ONE-TIME question.**
   Run `python -m cairn setup` (non-interactive shows status) or read
   `~/.cairn/consent.json`. If a harness has an answer recorded — yes OR no —
   **do not raise the subject again, ever.** The human reopens it themselves
   with `cairn setup` when and if they want to.

1. **Explain the choice plainly.** Ambient capture means every session with
   you (and any other connected agent) automatically writes its conversation
   into their local vault — orient at the start, capture while they work,
   compile at the end. Nothing ever leaves their machine. **Recommended** —
   it is the memory doing its job. Off means the vault only gets what they
   explicitly ask to note.
2. **Ask them.** Do not install a hook without their yes in the chat.
3. **On yes, run it:** `python -m cairn connect --global` (Claude Code) ·
   `python -m cairn codex-hook install` (Codex). Or `python -m cairn setup`
   for the guided version.
4. **Tell them how to change their mind later** — say it out loud:
   `cairn disconnect --global` / `cairn codex-hook uninstall` turns it off;
   `cairn setup` reviews everything anytime.

## Capture scope — your call, off by default

> **Claude Code sessions only.** Codex has its own separate toggle — the
> `codex-hook` section above — and its installed state is a per-machine fact:
> check `python -X utf8 -m cairn codex-hook status` (or ask the vault) rather
> than assuming from this document.

| Scope | Command | Captures |
|---|---|---|
| **Off** (default) | *(nothing)* | only what you explicitly `cairn note` |
| **Per-project** | `cairn connect` | every chat *in that repo* |
| **Global** | `cairn connect --global` | every Claude Code chat on the machine |

Global and per-project are **mutually exclusive** (both on = double-write):
`cairn connect` refuses it and `cairn doctor` flags it. Reverse either with
`cairn disconnect [--global]`.

**Privacy controls** — because not every chat belongs in your brain:
- Skip recording one chat (even under global): `set CAIRN_CAPTURE=0` in that shell.
- Pause capture everywhere: `cairn capture off` (resume: `cairn capture on`).
- Secrets are scrubbed before write (append-only, fail-closed); opt out only with `CAIRN_NO_REDACT=1`.

## Sessions — a new chat is a new session

You don't start a session by hand. When a project is connected (`cairn connect` /
`--global`), the harness gives every chat its own session id and Cairn's hooks
stamp it on each tool call — so **each new Claude Code chat is a new session**.
`orient` opens it by printing what carried over (decisions, open items, the delta
from last time) — it *reads* the past, it doesn't begin the session. Inside a
connected chat the hook owns the session id, so there's nothing to run; a manual
`cairn session` would just get re-stamped on your next tool call.

The exception is a runtime where the hooks never fire — Claude Desktop agent mode,
a local-model frontend, a bare terminal, cron. There nothing records the session,
so notes would attach to the *previous* one. Stamp a clean session yourself:

```bash
python -m cairn session --new        # auto-named session-YYYY-MM-DD-HHMM
python -m cairn session my-feature    # or name it
python -m cairn session               # show current session + where it came from
```

It also clears the parent chain, so the new session won't link back to the old one.

## Backfill — distill your history into connected memory

Old conversations — imported history, or sessions captured before you connected
Cairn — land as raw turns: searchable, but weakly linked. **Backfill distills
them** into sharp `claim` nodes (the decisions, insights, and ideas each
conversation actually holds). Claims embed far better than raw chat and connect
both by topic *and* by shared entity — the bridges that link your separate
"solar systems."

```bash
python -m cairn backfill native --estimate   # show the token cost first — no surprises
python -m cairn backfill native               # distill your own captured work
python -m cairn backfill claude --source-file=<claude-export>/conversations.json
python -m cairn backfill finalize             # embed new claims + rebuild edges
```

- **Cost-warned** — `--estimate` prints the conversation count + token estimate before you spend.
- **Idempotent** — already-distilled sessions are skipped; safe to stop and resume.
- **`--reset`** — void and redo a session's claims (replace, never duplicate).
- **Agent-driven** — the connected model does the extraction (no bundled LLM, per the charter); for imports, `--source-file` reads the full original text, not the truncated stub.

> Not to be confused with `cairn backfill --prompt`, which prints a paste-in
> prompt to *reconstruct* a session that was never captured. `cairn backfill`
> *distills* conversations you already have.

## Common commands

`orient` · `note` · `fetch` · `wander` · `query` / `xquery` · `embed` · `edges`
(rebuild the graph + atlas) · `book` · `sleep` (nightly: embed → consolidate →
prune → edges → book → compile → audit → registry) · `dashboard` · `connect` / `disconnect` /
`capture` · `doc` · `import` · `backfill` (distill history → connected claims).

## Charter

Local-first forever. Append-only. Minimal deps (stdlib + numpy / sentence-transformers
/ fastapi+uvicorn only). Model-agnostic.

## License

**Free for personal and non-commercial use** under the
[Business Source License 1.1](LICENSE) — read it, run it, modify it, self-host
it, keep it. **Commercial or business use requires a commercial license** — email
**licensing@cairnremembers.com** and we'll sort out terms — that's what keeps Cairn
independent and maintained. Source-available, not OSI "open source"; each release converts to
the permissive MIT License on the Change Date stated in its LICENSE.

Patent pending — a U.S. provisional patent application (filed 2026-07-07) covers
Cairn's core mechanisms. **Cairn Remembers™** is a trademark of James Wescott Maitland IV.
