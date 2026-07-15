# For the AI working in this project

This project uses **Cairn** — a local-first episodic memory vault **shared across
every model that works here**. You are one worker in a multi-model system: other
models have written to this vault before you, and others will after. There is one
vault on this machine (`~/.cairn/cairn.db`); Claude, Codex, and any other connected
AI all read and write it. Read this once, then follow it.

> **Human reading this?** You want [README.md](README.md) / [QUICKSTART.md](QUICKSTART.md) —
> this file is the *agent's* operating protocol, not a human install guide.

## Step 0 — install Cairn if it isn't already

If `python -m cairn doctor` fails with "No module named cairn", install it first:

```
git clone https://github.com/CairnRemembers/cairn
cd cairn
# Windows:      powershell -ExecutionPolicy Bypass -File .\install.ps1
# macOS/Linux:  ./install.sh   (externally-managed Python? make a venv first:
#               python3 -m venv .venv && source .venv/bin/activate, then ./install.sh)
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

> **Known doctor gap — do not "fix" what isn't broken:** doctor's **MCP** line reads
> Claude Desktop's config only. On a Codex-wired machine it can say *"MCP: not
> found"* while Codex's `[mcp_servers.cairn]` works fine. Doctor may mention Codex
> sessions it finds, but it does **not** verify the Codex MCP wire or the notify
> hook — a Claude-side check, never a Codex-completion certificate. Verify Codex
> with `codex mcp list` / `cairn codex-hook status` and move on — never rewrite
> configs because of that one line.

**If `doctor` says the vault is EMPTY:** you are either a fresh install (fine —
just start capturing) **or a remote/cloud agent with no access to the user's real
vault.** In the second case, do NOT import anything or assume prior context — say
so and ask the user. Cairn is local-first; the real memory lives on the user's
machine, not in your sandbox.

## Step 2 — turn on capture for the human (their call, off by default)

Capture is **off** until the human opts in. This is a **one-time, machine-wide**
switch — once on, every future chat and every account is captured; the human never
repeats it, and adding another AI account later does **not** need a re-install.

- **Check first — this is a one-time question.** Read `~/.cairn/consent.json` (or run
  `python -m cairn setup`, which shows status). If a harness already has an answer
  recorded — yes *or* no — **do not raise it again.** The human reopens it themselves.
- **If unanswered, explain the choice plainly, then ask.** Ambient capture means every
  session automatically writes its conversation into their local vault. Nothing leaves
  their machine. **Recommended** — it's the memory doing its job.
- **On their yes, run the one crowned command — in the terminal, not the chat:**

```
python -m cairn setup
```

`setup` detects their harnesses and runs the **right command for each** (Claude Code →
`connect --global`; Codex → `codex-hook install`). **Do not hand-pick or improvise
per-harness commands** — `connect --global` is Claude-only and does *not* cover Codex;
`setup` is the one path that gets both right. Capture takes effect on **new** chats.

- **Tell them how to undo it:** `cairn disconnect --global` / `cairn codex-hook uninstall`
  turns it off; `cairn capture off` pauses without disconnecting; `cairn setup` reviews anytime.

**Capture is one wire; tools are the other.** `setup` wires *capture*. For an AI to
search and note the vault from **inside the chat** (the `cairn_*` tools), each client
has its own one-time wire — full walk-throughs in [QUICKSTART.md](QUICKSTART.md):

- **Claude Code:** `claude mcp add --scope user cairn -- <full-path-to-python> -X utf8 -m cairn mcp`
  — use the interpreter that can `import cairn`; a bare `python` that can't is the
  #1 failure ([proof one-liner in QUICKSTART §6a](QUICKSTART.md#6a--tools-via-mcp)).
- **Codex:** the `[mcp_servers.cairn]` block in `~/.codex/config.toml`, plus the memory
  protocol in `~/.codex/AGENTS.md` — **append to an existing AGENTS.md; never replace
  what's there** ([QUICKSTART §6](QUICKSTART.md#6--use-cairn-from-codex)).
- **Claude Desktop / Cursor / any MCP client:** an MCP entry in that client's own
  config pointing at `python -X utf8 -m cairn mcp` (the MCP-client note in
  [QUICKSTART.md](QUICKSTART.md)).

## Step 3 — at the start of every session

```
python -m cairn orient          # prints inherited context — your memory from before
```

Read it before your first tool call. Then `cairn fetch "question"` instead of
re-reading files/history.

**Gists are the index, not the text.** `fetch`/`search`/`logs` return short
gists and capped previews; the complete stored text of any node is one step
away — `python -m cairn read <id>` (MCP: `cairn_read`). Before relying on a
summary of something important, read it in full.

**You don't start the session — the harness does.** Each chat has its own session
id, and the hooks stamp it as you work, so your captures and `cairn` commands all
attach to *this* chat. A new chat is already a new session; `orient` *reads* the
carryover (it drops one bookmark as it reads), it doesn't begin a session or switch
capture on. Only in a runtime where the hooks never fire (no auto-orient at the
start) do you open one by hand — `python -m cairn session --new` once, before your
first `note`.

**Say which model you are.** Claude Code and Codex detect their own model
automatically. In any other runtime — a local model, or one reached over MCP —
set `CAIRN_MODEL` (e.g. `export CAIRN_MODEL="llama3-70b"`) so your notes are credited
to you. Skip it and they still save, just attributed to "unknown" instead of you.

## Step 4 — capture as you work (this is the point)

> **If capture is on,** Claude Code records tool calls *and* conversation turns
> automatically via hooks — you don't have to note every exchange by hand. Still write
> an explicit `note` for a crisp decision/warning/insight: a hand-framed node beats a
> raw transcript turn. Agents without hook support capture by hand, as below. To skip a
> sensitive chat, the user sets `CAIRN_CAPTURE=0`; don't work around it.

Write a node when the **state of the work changes** — a decision made, an option
rejected and why, a problem found, something learned, the user stating what they want.
Capture the *path*, not just the destination.

```
python -m cairn note --kind=decision  "chose X over Y because Z"
python -m cairn note --kind=warning   "this breaks if W"
python -m cairn note --kind=open_item "still need to: V"
python -m cairn note --speaker=user   "user wants U"
```

**Not a quota** — capture by *salience*, not by count. Some exchanges produce zero
nodes; a dense decision produces several. Don't narrate; record what's worth remembering.

**Pass complete text.** Notes store uncapped, and a summary saved as a note has
no fuller text behind it — the summary becomes the whole memory. For a large
artifact (an audit, a spec, a report), save it as a file and put the path in the
note.

Kinds: `decision · warning · open_item · insight · idea · hypothesis · procedure ·
resolved · question · context_stamp · conversation_turn`.

## Step 5 — account attribution (which galaxy) — get it TRUE, never guess

Each memory is attributed to a **galaxy** = the AI account it came from. Cairn reads
the real account id from the login file (`~/.claude.json` / `~/.codex/auth.json`) and
stamps it automatically. Your job is to **not corrupt that**:

- **A new or second account is already captured** into its own galaxy the moment it's
  used — never tell the human to re-run setup to "add an account." Activation already
  covers it; attribution is just a label.
- **Never guess or assert an account.** Label a session only when **(a)** you have a
  verified signal, or **(b)** the human explicitly tells you ("this is my work account").
  Then, and only then:

```
python -m cairn account doctor                            # read-only — prints the exact
                                                          #   fix-session command per provable mismatch
python -m cairn account fix-session <session-id> <slug>   # repair ONE named session (backed up, then LOCKED)
python -m cairn account fix-session <slug>                # same, for the CURRENT session only
python -m cairn account rename <key> "My Work Claude"     # renames a galaxy's display label only
```

- **Watch-outs on a multi-account machine:** a stale `CAIRN_ACCOUNT` env var, or a
  hardcoded value in `~/.codex/config.toml`, will stamp the *wrong* name on this
  session's memories. Two logins of the same AI can share one galaxy label until
  corrected. Attribution is append-only — a wrong guess is permanent — so when unsure,
  leave it to Cairn's automatic id or ask the human. Truth over a confident label.
- **The dashboard map is NOT an attribution check before the first sleep.** A
  never-slept vault renders as one center cloud no matter how many accounts exist.
  Run `cairn sleep` once (then restart the dashboard) before concluding galaxies
  are merged.

## Step 6 — don't break the laws

- **Local-first** — nothing leaves the machine.
- **Append-only** — `void`, never delete.
- **Don't recreate what `doctor` marked ✓.**
- **Never modify Cairn's own code** unless the owner explicitly asks in this chat.

## Step 7 — before you finish

Claude Code compiles the session summary (`PROTOCOL.md`) automatically at the end,
so the next session opens oriented. In any other runtime, run it yourself before
you stop:

```
python -m cairn compile
```

That's what lets the next model — or the next you — pick up where this session left off.
