# Cairn

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Version 0.2.1](https://img.shields.io/badge/version-0.2.1-green)
![Patent pending](https://img.shields.io/badge/patent-pending-orange)
![Local-first](https://img.shields.io/badge/local--first-no%20cloud-brightgreen)

> **Build knowledge. Leave signals.** — Tools for modern explorers.

**Local-first episodic memory for AI agents — and for you.**

<!-- hero image slots here: assets/cairn-hero.png (the galaxy — from the brand pass) -->

A cairn is a stack of stones that marks a trail. This one marks the trail of your
*thinking*: every decision, dead end, and reason becomes a node you — and any model —
can find again, across sessions and across model generations.

- **Local-first** — everything lives in one SQLite file at `~/.cairn/`. No cloud, no account, no telemetry.
- **Model-agnostic** — any agent that runs a shell command or speaks MCP: Claude, GPT/Codex, Gemini, local models.
- **Append-only** — memories are voided, never deleted. The record is the record.
- **Yours** — Cairn sends nothing off your machine. Your chat still goes to whatever model you chose, exactly as it would without Cairn — use a local model and nothing leaves at all.

**Two ways in:**
- 🧑 **A person setting this up?** Keep reading — [Quick start](#quick-start) takes about 5 minutes. Every option and fix: [QUICKSTART.md](QUICKSTART.md).
- 🤖 **An AI agent installing Cairn for someone?** → [SETUP_FOR_AGENTS.md](SETUP_FOR_AGENTS.md) is written for you (install, consent, attribution).

---

## Contents
- [Quick start](#quick-start)
- [Wire up your AI](#wire-up-your-ai)
- [What you get](#what-you-get)
- [Advanced install](#advanced-install)
- [Multiple accounts](#multiple-accounts)
- [What to back up](#what-to-back-up)
- [Common commands](#common-commands)
- [License](#license)

---

## Quick start

> **Two places, never mixed up:**
> 🖥️ **Your terminal** (PowerShell / Terminal) — every command on this page runs here, on your computer. Wiring is always a terminal command or a config-file edit — an AI can run those terminal steps *for* you (that's the fastest path below), but wiring is never something you paste into the chat box.
> 💬 **The AI chat** — where memory shows up, and where you *test* the wiring by asking the AI to use it.

### Fastest — let your AI do it
Open Claude Code, Codex, or Cursor and paste:

> Install and set up Cairn for me from https://github.com/CairnRemembers/cairn

Your AI runs the terminal steps and asks before switching memory on — one yes/no per
AI on your machine, default **No**. Nothing records without your yes. *(Using Codex?
There's one extra paste after that — see [Wire up your AI](#wire-up-your-ai).)*

### Or do it yourself

**1 — Get the code** 🖥️
```bash
git clone https://github.com/CairnRemembers/cairn
cd cairn
```

**2 — Install** 🖥️ *(installs software only — records nothing, the vault starts empty)*
```bash
# Windows:      .\install.ps1      (blocked? powershell -ExecutionPolicy Bypass -File .\install.ps1)
# macOS/Linux:  ./install.sh
```
The installer finds Python 3.11+, installs everything (first run downloads PyTorch — a
few minutes), and checks itself.

**3 — Wire up your AI.** This is where memory actually turns on, and **each AI needs
different wiring** — a `y` in setup finishes the job for Claude Code, but *not* for
Codex or Claude Desktop. Find your AI below and follow it to its ✅.

---

## Wire up your AI

Cairn has **two separate wires**, and knowing the difference prevents every common surprise:

- **Capture** — your chats get *remembered* automatically (writes to the vault).
- **Tools** — the AI can *search and note* your vault from inside the chat (reads + on-demand writes).

Some AIs need one wire, some need both. Don't stop at the first ✓ — follow your AI to its ✅ line.

### Claude Code — one command
🖥️ In the terminal:
```bash
python -X utf8 -m cairn setup        # answer y for Claude Code
```
That wires **capture**: every **new** Claude Code chat auto-orients (you'll see the
banner), records as you work, and compiles when it ends. Machine-wide, one-time,
reversible (`cairn disconnect --global`). Prefer one project only? Run `cairn connect`
inside that repo instead (global and per-project are mutually exclusive — `doctor` flags it).

✅ **Done when:** a **new** chat opens with a **"CAIRN — inherited context"** banner,
and 🖥️ `python -X utf8 -m cairn doctor` shows **✓ capture**.

*Optional — native tools:* Claude Code can already read the vault by running `cairn`
commands in its shell. For native `cairn_*` tools instead, register the MCP server
user-wide, pointing at the Python that can `import cairn` (a bare `python` that can't
is the #1 failure — [prove the path first, QUICKSTART §6a](QUICKSTART.md#6a--tools-via-mcp)):
```bash
claude mcp add --scope user cairn -- <full-path-to-python> -X utf8 -m cairn mcp
```
💬 Proof: ask the chat to *"call cairn_orient"*. *(`doctor` can't see this wire — the ask is the test.)*

### OpenAI Codex — three pieces, each does a different job
1. **Capture** 🖥️ — `python -X utf8 -m cairn setup` → `y` for Codex (= `codex-hook install`).
   Captures **agent-turn events** as they happen. Plain conversation is *not* captured
   this way — pull it in whenever you want with 🖥️ `python -X utf8 -m cairn import codex-sessions --apply`.
2. **Tools** 📄 — add to `~/.codex/config.toml`, then fully restart Codex
   ([full §6 walk-through](QUICKSTART.md#6--use-cairn-from-codex)):
```toml
[mcp_servers.cairn]
command = "<full-path-to-python>"
args = ["-X", "utf8", "-m", "cairn", "mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 120
default_tools_approval_mode = "approve"
```
3. **Habit** 📄 — create `~/.codex/AGENTS.md` and paste the memory protocol from
   [QUICKSTART §6c](QUICKSTART.md#6--use-cairn-from-codex) so Codex orients, fetches,
   and notes **unprompted**. *(Needs piece 2 — the protocol calls those tools.)*

✅ **Done when:** 🖥️ `python -X utf8 -m cairn codex-hook status` prints **INSTALLED**,
and 💬 a Codex chat answers *"call cairn_orient"* with a digest (with piece 3, its first
reply starts `[cairn: oriented — N]`).
⚠️ *Honest note:* `cairn doctor` verifies neither Codex wire yet — it may mention Codex
sessions it finds, but it is not a Codex-completion certificate. The checks above are the real proof.

### Claude Desktop / Cursor — one paste
📄 Add to `claude_desktop_config.json` (or Cursor's MCP settings), then restart the app:
```json
{
  "mcpServers": {
    "cairn": { "command": "python", "args": ["-X", "utf8", "-m", "cairn", "mcp"] }
  }
}
```
That's the **tools** wire — search, fetch, wander, note from inside the chat. These
surfaces have **no ambient capture**; what you ask the AI to `cairn_note` is what lands.
*(If the app can't find Python, use the full path of the Python that installed Cairn.)*

✅ **Done when:** `doctor` shows **✓ MCP — registered in Claude Desktop config**
(Desktop), or 💬 the *"call cairn_orient"* smoke test answers (Cursor).

---

**Applies to every AI above:**
- **Wiring is one-time.** New chats just remember — you never activate per chat. `orient` *reads* memory; it never switches anything on. Scope varies by wire: Claude Code hooks are machine-wide (or one project via `cairn connect`); Desktop/Cursor and Codex live in each app's own config, per account.
- **Only NEW chats pick up new wiring** — finish wiring, then open a fresh chat. (Long-lived MCP clients re-read the tools only on a full restart.)
- **Privacy controls:** skip one chat — `CAIRN_CAPTURE=0` in that shell (PowerShell `$env:CAIRN_CAPTURE="0"` · cmd `set CAIRN_CAPTURE=0` · bash `export CAIRN_CAPTURE=0`) · pause everywhere: `cairn capture off` / `on` · secrets scrubbed before write (append-only, fail-closed).
- **Undo:** `cairn disconnect [--global]` · `cairn codex-hook uninstall` · re-run `cairn setup` to review.

---

## What you get

- **One vault, every model.** Any agent that speaks MCP or can run `cairn` reads and writes the same memory — so one agent builds on what another wrote, even across rival vendors.
- **Keep your place across a usage cap.** Hit a limit on one model, continue on another, and point it at where you left off — the trail is in the vault, not in one model's context.
- **Captured as you work** — decisions, dead ends, tool calls, and turns become searchable nodes, from the moment you wire it.
- **Nothing worth keeping disappears.** Every captured turn stores its complete text: search results are gists — an index — `cairn read <id>` prints any node in full, and MCP `cairn_read` pulls it whole with a raised `max_chars`.
- **A local maintenance pass you run** — `cairn sleep`, nightly by habit or on your own scheduler (it doesn't schedule itself): embed → consolidate → prune → rebuild the graph → compile, all on your machine. One exception to "no network": the very first embed downloads the ~80 MB model, once.
- **A map of your thinking** — the dashboard galaxy (`cairn dashboard` → http://127.0.0.1:7331), plus a human-readable Hub / Book / Index.
- **Backfill** — distill old conversations into sharp, connected `claim` nodes.

---

## Advanced install

<details>
<summary>Manual install, lighter builds, and venvs</summary>

**By hand** (what the installer runs):
```bash
pip install -e ".[all]"      # package + embedder + dashboard
```
**Lighter builds:**
```bash
pip install -e ".[embeddings]"   # no dashboard
pip install -e ".[dashboard]"    # no embedder
```
Base install is stdlib + `numpy`. Extras add the embedder (`sentence-transformers` — the ~80 MB model downloads once, on first use) and the dashboard (`fastapi` + `uvicorn`).

> **Note:** Cairn installs from this repo — there is no `pip install cairn-remembers` package yet. Clone, then install as above.

**PEP-668 "externally-managed-environment"** (Ubuntu/Debian/Homebrew/WSL): install into a venv first —
```bash
python3 -m venv .venv && source .venv/bin/activate && ./install.sh
```
</details>

---

## Multiple accounts

**One login per AI? Skip this — it just works.** Each AI signs in with its own
account, and Cairn files that AI's work under its own galaxy automatically —
Claude and GPT never mix, with zero setup.

**Read on only if you run two accounts of the *same* AI** (two Claude logins, two
ChatGPT/Codex logins — say, **personal and company**). Galaxies are keyed to each
login's stable id and **never merge** — but with two same-AI logins on one machine,
Cairn can't always *prove* which one is active. The rule that keeps it clean:

> **Declare, don't detect: set `CAIRN_ACCOUNT` per account, up front.**

```bash
# Claude Code — launch each account with its label:
export CAIRN_ACCOUNT=work && claude        # bash/zsh (or set it in that profile)
#   PowerShell: $env:CAIRN_ACCOUNT="work"; claude
# Codex — put it in that account's ~/.codex/config.toml:
#   [mcp_servers.cairn]  env = { CAIRN_ACCOUNT = "work" }
# Importing old history? Always pass the flag:
cairn import <export> --source=claude --account=work
```

Name and manage them anytime:
```bash
cairn account                          # list galaxies + node counts
cairn account rename <key> "Company"   # display label only — never merges or deletes
cairn account doctor                   # read-only check — prints the exact fix command per mismatch
cairn account fix-session <session-id> <slug>   # re-file ONE named session (backed up, then locked)
cairn account fix-session <slug>                # same, for the current session only
```

**Honest limits** — so you're never surprised:
- **Claude Desktop** proves the active account per session automatically. **Claude Code CLI and Codex can't** — they follow the machine's current login file, so an account switch mid-stream can label sessions with the previous account, silently. `CAIRN_ACCOUNT` is the guarantee; detection is not.
- `account doctor` verifies what's *provable* (Claude Desktop sessions); it can't audit pure-CLI or Codex history.
- Append-only applies here too: renames change display labels only; nothing merges, nothing deletes.

---

## What to back up

One folder: your vault at **`~/.cairn/`** (that's `cairn.db` — your actual memories). Back that up. Everything else is replaceable — the code lives here on GitHub, and reinstalling never touches your vault.

---

## Common commands

`orient` · `note` · `fetch` · `wander` · `query` · `read` (any node in full) · `dashboard` · `doctor` · `setup` · `connect` / `disconnect` / `capture` · `account` · `backfill` · `sleep` (the maintenance cycle — you run it) · `edges` · `book` · `import`

Full reference with every option: [QUICKSTART.md](QUICKSTART.md).

---

## License

**Free for personal and non-commercial use** under the [Business Source License 1.1](LICENSE) — read it, run it, modify it, self-host it. **Commercial or business use requires a commercial license** — email **licensing@cairnremembers.com**. Source-available (not OSI "open source"); each release converts to the permissive MIT License on the Change Date in its LICENSE.

Patent pending — a U.S. provisional application (filed 2026-07-07) covers Cairn's core mechanisms. **Cairn Remembers™** is a trademark of James Wescott Maitland IV.

---

*Knowledge is a trail, not a destination.*
