# For the AI working in this project

This project uses **Cairn** — a local-first episodic memory vault shared across
every model that works here. You are one worker in a multi-model system: other
models have written to this vault before you, and others will after. Read this
once, then follow it.

> **Human reading this?** You want [README.md](README.md) / [QUICKSTART.md](QUICKSTART.md) —
> this file is the *agent's* operating protocol, not a human install guide.

## Step 0 — install Cairn if it isn't already

If `python -m cairn doctor` fails with "No module named cairn", install it first:

```
git clone https://github.com/CairnRemembers/cairn
cd cairn
# Windows:      .\install.ps1     (blocked? powershell -ExecutionPolicy Bypass -File .\install.ps1)
# macOS/Linux:  ./install.sh
```

The installer finds Python 3.11+, installs deps, and runs `doctor` — then continue below.
If `doctor` already resolves, you're installed; skip to Step 1.

## Step 1 — ask the system what exists. Don't guess.

```
python -m cairn doctor
```

It prints, with a fix for each gap:

- `✓` already there — **never recreate it** (the vault is irreplaceable; do not
  re-import history, re-init, or re-run setup on anything marked ✓)
- `⚠ / ✗` — do **only** these, using the exact command shown
- `○` optional — ignore unless asked

**If `doctor` says the vault is EMPTY:** you are either a fresh install (fine —
just start capturing) **or a remote/cloud agent with no access to the user's real
vault.** In the second case, do NOT import anything or assume prior context — say
so and ask the user. Cairn is local-first; the real memory lives on the user's
machine, not in your sandbox.

## Step 2 — at the start of work

```
python -m cairn orient          # prints inherited context — your memory from before
```

Read it before your first tool call. Then `cairn fetch "question"` instead of
re-reading files/history.

**You don't start the session — the harness does.** Each chat has its own session
id, and the hooks stamp it as you work, so your captures and `cairn` commands all
attach to *this* chat. A new chat is already a new session; `orient` *reads* the
carryover, it doesn't begin one. Only in a runtime where the hooks never fire (no
auto-orient at the start) do you open one by hand — `python -m cairn session --new`
once, before your first `note`.

**Say which model you are.** Claude Code and Codex detect their own model
automatically. In any other runtime — a local model, or one reached over MCP —
set `CAIRN_MODEL` to your model name (e.g. `export CAIRN_MODEL="llama3-70b"`) so
your notes are credited to you. Skip it and they still save, just attributed to
"unknown" instead of you. Nothing else about identity needs setting.

## Step 3 — capture as you work (this is the point)

> **If this project is connected (`cairn connect` / `--global`),** Claude Code
> captures tool calls *and* conversation turns automatically via hooks — you
> don't have to note every exchange by hand. Still write an explicit `note` for a
> crisp decision/warning/insight: a hand-framed node beats a raw transcript turn,
> and consolidation will thank you. Agents without hook support (or in
> unconnected projects) capture by hand, as below. To skip recording a sensitive
> chat, the user sets `CAIRN_CAPTURE=0`; don't work around it.

Write a node when the **state of the work changes** — a decision made, an option
rejected and why, a problem found, something learned, the user stating what they
want. Capture the *path*, not just the destination.

```
python -m cairn note --kind=decision  "chose X over Y because Z"
python -m cairn note --kind=warning   "this breaks if W"
python -m cairn note --kind=open_item "still need to: V"
python -m cairn note --speaker=user   "user wants U"
```

**Not a quota** — some exchanges produce zero nodes (pure mechanical back-and-forth),
some produce several (a dense decision with rejected alternatives). Capture by
*salience*, not by count. Don't narrate; record what's worth remembering.

Kinds: `decision · warning · open_item · insight · idea · hypothesis · procedure ·
resolved · question · context_stamp · conversation_turn`.

## Step 4 — don't break the laws

- **Local-first** — nothing leaves the machine.
- **Append-only** — `void`, never delete.
- **Don't recreate what `doctor` marked ✓.**

## Step 5 — before you finish

Claude Code compiles the session summary (`PROTOCOL.md`) automatically at the end,
so the next session opens oriented. In any other runtime, run it yourself before
you stop:

```
python -m cairn compile
```

That's what lets the next model — or the next you — pick up where this session left off.
