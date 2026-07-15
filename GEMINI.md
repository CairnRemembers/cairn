<!-- cairn:start v2 -->
## Cairn memory (local-first)
This project uses Cairn. Before working:
1. `python -m cairn doctor` — act ONLY on its ✗/⚠ items; never recreate ✓ items.
   If it says the vault is EMPTY and you're a remote/cloud agent, do NOT import
   history or assume context — ask the user (the real vault is on their machine).
2. `python -m cairn orient` — load inherited context before your first action.
   New chat = new session automatically; orient only reads the carryover. No
   hooks in your runtime? run `python -m cairn session --new` once before you note.
3. `python -m cairn fetch "question"` instead of re-reading files/history.
4. Fetch/search return GISTS — an index, not the text. Before relying on a
   summary, `python -m cairn read <id>` prints the node IN FULL.
Capture (Claude does this automatically via hooks; other agents by hand),
by SALIENCE not quota — some exchanges = 0 nodes, some = several:
  `python -m cairn note --kind=decision|warning|open_item|insight|resolved "..."`
  `python -m cairn note --speaker=user "what the user wants"`
  Write the complete salient fact without truncating it — but don't paste
  transcripts (turns are captured separately); large artifact → save the
  file, note its path and purpose.
Laws: local-first (nothing leaves the machine) · append-only (void, never delete).
<!-- cairn:end -->
