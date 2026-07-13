# Cairn

> **Build knowledge. Leave signals.** · Tools for modern explorers.

**Local-first episodic memory for AI agents — and for you.**

**👤 New here?** [**Setup**](#setup) gets you running in ~5 minutes — the human steps *or* the ⚡ "let your AI do it" lane. Full reference: [`QUICKSTART.md`](QUICKSTART.md).

A cairn is a stack of stones that marks a path. This one marks the path of your
thinking: every decision, dead end, and reason gets captured as a node, embedded,
and made retrievable across sessions and across model generations. Nothing leaves
your machine.

- **Local-first** — everything lives in `~/.cairn/cairn.db`. No cloud, no accounts.
- **Append-only** — memories are voided, never deleted. The record is the record.
- **Model-agnostic** — works with any agent that can run a shell command or speak MCP.
- **No heavy deps** — stdlib + numpy, plus an optional embedder and dashboard.

## Setup

Two ways in — **both tested** (a real person set it up by hand; the agent lane works too).

### 🤖 Fastest — let your AI do it

Open Claude Code, Codex, or Cursor and paste:

> Install and set up Cairn for me from https://github.com/CairnRemembers/cairn

It clones, installs, and asks you one yes/no question. That's the whole thing.

### 🧑 Or do it yourself — 5 steps

Run each block, then move to the next. On Windows, type `python -X utf8 …` (as shown) so Cairn's output prints cleanly.

**Step 1 of 5 — Get the code**

```
git clone https://github.com/CairnRemembers/cairn
cd cairn
```

**Step 2 of 5 — Install** *(needs Python 3.11+)*

```
.\install.ps1
```

*(macOS / Linux: `./install.sh`)* — the installer finds Python, installs everything (first run downloads PyTorch, a few minutes), and checks itself.

**Step 3 of 5 — Turn on memory**

```
python -X utf8 -m cairn setup
```

Type **`y`** when it asks about Claude / Codex. This is the step that makes Cairn actually remember — capture is off until you do it.

**Step 4 of 5 — Open a new AI chat**

Start a fresh chat in your AI. Memory switches on for *new* chats — not the one you're already in.

**Step 5 of 5 — Check it worked**

```
python -X utf8 -m cairn doctor
```

The **`capture`** line shows a ✓. Done — from now on, new chats remember. See your memory map any time with `python -X utf8 -m cairn dashboard` (opens http://127.0.0.1:7331).

---

**If something snags**

- **First move, always:** `python -X utf8 -m cairn doctor` — it names exactly what's missing.
- **Windows won't run `.\install.ps1`?** Use `powershell -ExecutionPolicy Bypass -File .\install.ps1`.
- **"No supported AI harnesses detected"?** Install Claude Code or Codex first, then re-run Step 3.
- **"requires fastapi and uvicorn" even after installing?** You have two Pythons — reinstall with the same one: `python -m pip install -e ".[all]"` (keep the quotes exactly).
- **Want your AI to *search* the vault as tools** (Claude Desktop / Cursor / Codex)? See [QUICKSTART §6](QUICKSTART.md#6--use-cairn-from-codex).

## What to back up

Only one folder matters: your **vault** at `~/.cairn/` (that's `cairn.db` — your
actual memories). **Back that one up.** Everything else is replaceable — the code
lives on GitHub, and reinstalling never touches your vault.

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
