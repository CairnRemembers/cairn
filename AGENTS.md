<!-- cairn:start -->
## Cairn memory (local-first)
This project uses Cairn. Before working:
1. `python -m cairn doctor` — act ONLY on its ✗/⚠ items; never recreate ✓ items.
   (Its MCP line reads Claude Desktop's config only — never "fix" a Codex wire
   because of it; check `codex mcp list` / `cairn codex-hook status` instead.)
   If it says the vault is EMPTY and you're a remote/cloud agent, do NOT import
   history or assume context — ask the user (the real vault is on their machine).
2. `python -m cairn orient` — load inherited context before your first action.
   New chat = new session automatically; orient only reads the carryover. No
   hooks in your runtime? run `python -m cairn session --new` once before you note.
3. `python -m cairn fetch "question"` instead of re-reading files/history.
Capture as you work, by SALIENCE not quota (some exchanges = 0 nodes, some = several):
  `python -m cairn note --kind=decision|warning|open_item|insight|resolved "..."`
  `python -m cairn note --speaker=user "what the user wants"`
Laws: local-first (nothing leaves the machine) · append-only (void, never delete).
<!-- cairn:end -->
