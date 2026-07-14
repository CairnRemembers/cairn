# Cairn

**Every model. One memory.**

**Local-first episodic memory for AI agents — and for you.** Claude, Codex, and any
other AI you connect read and write **one shared vault on your machine** — what you
worked out with one model, the next one already knows. Run out of usage on one
model? Open another: **your work is already there.**

> **Build knowledge. Leave signals.** · Tools for modern explorers.

**👤 New here?** [**Setup**](#setup) gets you running in ~5 minutes — the ⚡ "let your AI do it" lane or the human steps. Full reference: [`QUICKSTART.md`](QUICKSTART.md).

A cairn is a stack of stones that marks a path. This one marks the path of your
thinking: every decision, dead end, and reason gets captured as a node, embedded,
and made retrievable across sessions **and across models**. Nothing leaves your
machine.

- **Local-first** — everything lives in `~/.cairn/cairn.db`. No cloud, no sign-up.
  The only thing that ever leaves your machine is a one-time model download —
  see [What leaves your machine](#what-leaves-your-machine).
- **One vault, every AI** — Cairn reads the login you already have; each AI account
  gets its own **galaxy** in the same local vault. Nothing to register.
- **Every memory keeps its source** — which model, which account, which session:
  provenance is first-class, so you always know who wrote what.
- **Append-only** — memories are voided, never deleted. The record is the record.
- **Model-agnostic** — any agent that can run a shell command or speak MCP; most
  extensively tested with **Claude Code and Codex** (the proof pair, not the boundary).
- **Light** — stdlib + numpy at the core; the embedder and dashboard are optional extras.

## Setup

> ### ⌨️ These commands run in your terminal — not in your AI chat
> Everything below (`git`, `install`, `cairn …`) goes in **PowerShell / Terminal**,
> the same place you run `git`. The **only** thing you type *into your AI* is the one
> line in the ⚡ lane. Pasting `cairn` commands into a chat box won't set anything up.

Two ways in — pick one.

### ⚡ Fastest — let your AI set it up

Open Claude Code, Codex, or Cursor and paste this to your AI:

> Install and set up Cairn for me from https://github.com/CairnRemembers/cairn

Your AI clones it, runs the installer, and walks you through `cairn setup` — one
short yes/no per AI you use. It follows [`SETUP_FOR_AGENTS.md`](SETUP_FOR_AGENTS.md),
so it runs the right command for each harness instead of guessing. This is the
least-effort path.

### 🧑 Or do it yourself — 5 steps

Run each block in your terminal, then move to the next.

#### Step 1 — Get the code

```
git clone https://github.com/CairnRemembers/cairn
cd cairn
```

#### Step 2 — Install *(needs Python 3.11+)*

**Windows** — run it this way so Windows' script policy can't block it:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**macOS / Linux:**

```bash
./install.sh
```

> On a modern Linux/mac where the system Python is *"externally managed"* (Debian 12+,
> Ubuntu 23.04+, Fedora, Arch, Homebrew), make a virtual environment first, then install:
> `python3 -m venv .venv && source .venv/bin/activate` → `./install.sh`.

The installer finds Python, installs everything, and checks itself. First run pulls
PyTorch (a few hundred MB, a few minutes) — that's the heaviest part, and it's the
only large download at install time.

#### Step 3 — Turn on memory

```
python -X utf8 -m cairn setup
```

This is the step that makes Cairn actually remember — **capture is off until you do
it.** Setup asks **one yes/no question per AI you use** (default *No*): a typical
person with both Claude Code and Codex answers **two** prompts and is done. It runs
the correct command for each — you don't pick commands yourself.

#### Step 4 — Open a new AI chat

Start a *fresh* chat in your AI. Memory switches on for **new** chats — not the one
you're already in.

#### Step 5 — Check it worked

```
python -X utf8 -m cairn doctor
```

The **`capture`** line shows a ✓. Done — from now on, new chats remember. See your
memory map any time with `python -X utf8 -m cairn dashboard` (opens
http://127.0.0.1:7331, local only).

---

**If something snags**

- **First move, always:** `python -X utf8 -m cairn doctor` — it names exactly what's
  missing. *(One known gap: doctor's **MCP** line checks Claude Desktop only — a
  correctly-wired **Codex** can read "MCP: not found". For Codex, trust
  `codex mcp list` + `cairn codex-hook status` instead.)*
- **Memory map shows one big cloud in the middle?** Normal for a vault before its
  first sleep — not a bug, not merged accounts. Run `python -X utf8 -m cairn sleep`
  once, restart the dashboard, hard-refresh: galaxies separate after that first bake.
- **"No supported AI harnesses detected"?** Install Claude Code or Codex first, then re-run Step 3.
- **"requires fastapi and uvicorn" even after installing?** You have two Pythons — reinstall with the same one: `python -m pip install -e ".[all]"` (keep the quotes exactly).
- **Garbled output on Windows?** Prefix commands with `python -X utf8 -m cairn …`.
- **Want your AI to *search* the vault as tools** (Claude Desktop / Cursor / Codex)? See [QUICKSTART §6](QUICKSTART.md#6--use-cairn-from-codex).

## What actually turns on — and how you know

Two separate ideas. Keeping them apart clears up most confusion:

- **Activation** = turning capture on. **One terminal command, once — then it's
  machine-wide.** `cairn setup` (or `cairn connect --global` for Claude directly)
  wires it for *every* future chat and every account on this machine. You never
  repeat it, and adding another AI account later never needs a re-install.
- **Attribution** = which **galaxy** a chat's memories land in — just a label. See
  [Accounts & galaxies](#accounts--galaxies).

**How you know it's live:** open a new chat. The `CAIRN — inherited context` banner
at the top means the hooks fired — capture is on. (On a brand-new vault the very
first chat prints `starting fresh` instead — same proof, the hooks fired.) `cairn doctor` confirms it too.
Running `orient` is *not* what turns capture on — it only **reads** your memory and
prints what carried over. Proof-of-capture is: hooks installed **and** a fresh chat
started after you installed them.

**Capture scope — recommended: global.**

| Scope | Command | Captures |
|---|---|---|
| **🌍 Global** *(recommended)* | `cairn connect --global` | every Claude Code chat on this machine |
| **Per-project** | `cairn connect` | every chat *in that one repo* |
| **Off** (default) | *(nothing)* | only what you explicitly `cairn note` |

Most people want **global** — one command, every chat, nothing to repeat. `cairn
setup` chooses global for you. Global and per-project are mutually exclusive; reverse
either with `cairn disconnect [--global]`.

> **Codex is a separate switch.** `connect --global` is **Claude Code only.** Codex
> capture is turned on by `cairn codex-hook install` (`cairn setup` does this for you
> if Codex is present). See [Using Cairn from Codex](#using-cairn-from-codex).

**Privacy controls** — because not every chat belongs in your brain:
- Skip recording one chat (even under global): `set CAIRN_CAPTURE=0` in that shell.
- Pause everywhere: `cairn capture off` (resume: `cairn capture on`).
- Secrets are scrubbed before write (append-only, fail-closed); opt out only with `CAIRN_NO_REDACT=1`.

## Accounts & galaxies

You never make a Cairn account. Cairn reads the AI login you already have and gives
each one its own **galaxy** inside your one local vault, so your Claude work and your
Codex work don't blur together.

- **One account per AI (the common case):** captured automatically into its own
  galaxy, labeled from your login — nothing to do.
- **A second account of the *same* AI on one machine:** Cairn keeps them apart **when
  it can read a distinct identity for each** — Claude Desktop's per-session proof, or
  a distinct login id. Where it *can't* tell them apart cleanly (two logins that fall
  back to the same name — e.g. a shared handle, or an unreadable login), the newer
  one may land under the first one's label until you correct it. That label is
  provisional and fixable — it never overwrites a confirmed one. To set it straight:

```
python -X utf8 -m cairn account fix-session "My Work Claude"   # label this session
python -X utf8 -m cairn account rename <key> "My Work Claude"   # rename the galaxy
```

The name is cosmetic; Cairn anchors each memory to the real account id from your
login file, so renaming never merges or moves memories. If you run two accounts of
the same AI and want them cleanly split, `cairn account doctor` shows how each
session was attributed.

## Using Cairn from Codex

Codex reaches the vault three ways, all opt-in (full details in
[QUICKSTART §6](QUICKSTART.md#6--use-cairn-from-codex)):

1. **Tools over MCP** — `cairn_orient / fetch / search / note …` as native Codex
   tools. Two things bite here: the config must point at the **exact Python your
   install used** (venv: `…\.venv\Scripts\python.exe`), and Codex needs a **full
   restart** after the config edit — working block in [QUICKSTART §6a](QUICKSTART.md#6a--tools-via-mcp).
2. **`cairn codex-hook install`** — captures Codex's *agentic* (notify-fired) turns.
   **Not** plain conversational chat.
3. **`cairn import codex-sessions --apply`** — files your plain Codex chat (optional,
   forward-only, dry-run first). Note: `--include-before=DATE` is a **lower bound** —
   it imports turns dated *on or after* that date up to now, not everything before it.
   On Windows, very deep session paths can exceed the 260-char limit and fail to read
   — the import report counts them, so check it if a thread doesn't land.

## What leaves your machine

Cairn is local-first, and here is the whole truth of it:

- **At install:** `pip` pulls the declared dependencies from PyPI — including PyTorch
  (hundreds of MB) if you install the embedder.
- **On your first semantic search or embed:** a one-time **~80 MB** download of the
  open-source embedding model `all-MiniLM-L6-v2` from **huggingface.co** — fetched
  **anonymously, no account or token**. It is *not* downloaded at install, at import,
  or when a memory is captured. After that one download, Cairn forces offline mode so
  the embedder never touches the network again.
- **Everything else runs with zero network:** capturing, notes, keyword-fallback
  search, and the dashboard (bound to `127.0.0.1` only). **No telemetry, no analytics,
  no other host is ever contacted.**

Skip even that one download with the lighter `pip install -e ".[dashboard]"` (no
embedder): capture still works fully; search falls back to keyword matching.

## What to back up

Only one folder matters: your **vault** at `~/.cairn/` (that's `cairn.db` — your
actual memories). **Back that one up.** Everything else is replaceable — the code
lives on GitHub, and reinstalling never touches your vault.

## Sessions — a new chat is a new session

You don't start a session by hand. When capture is on, the harness gives every chat
its own session id and Cairn's hooks stamp it on each tool call — so **each new chat
is a new session**. `orient` opens it by printing what carried over (decisions, open
items, the delta from last time) — it *reads* the past, it doesn't begin the session.

The exception is a runtime where the hooks never fire — Claude Desktop agent mode,
a local-model frontend, a bare terminal, cron. There nothing records the session,
so notes would attach to the *previous* one. Stamp a clean session yourself:

```bash
python -m cairn session --new        # auto-named session-YYYY-MM-DD-HHMM
python -m cairn session my-feature    # or name it
python -m cairn session               # show current session + where it came from
```

## Backfill — distill your history into connected memory

Old conversations — imported history, or sessions captured before you connected
Cairn — land as raw turns: searchable, but weakly linked. **Backfill distills
them** into sharp `claim` nodes (the decisions, insights, and ideas each
conversation actually holds).

```bash
python -m cairn backfill native --estimate   # show the token cost first — no surprises
python -m cairn backfill native               # distill your own captured work
python -m cairn backfill claude --source-file=<claude-export>/conversations.json
python -m cairn backfill finalize             # embed new claims + rebuild edges
```

- **Cost-warned** — `--estimate` prints the conversation count + token estimate before you spend.
- **Idempotent** — already-distilled sessions are skipped; safe to stop and resume.
- **Agent-driven** — the connected model does the extraction (no bundled LLM, per the charter).

## Common commands

`orient` · `note` · `fetch` · `wander` · `query` / `xquery` · `embed` · `edges`
(rebuild the graph + atlas) · `book` · `sleep` (nightly: embed → consolidate →
prune → edges → book → compile → audit → registry) · `dashboard` · `connect` /
`disconnect` / `capture` · `account` · `doc` · `import` · `backfill`.

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
