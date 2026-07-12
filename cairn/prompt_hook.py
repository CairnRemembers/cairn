"""
cairn/prompt_hook.py — UserPromptSubmit hook (fires the INSTANT a prompt is sent)

Captures the user's message at SEND time, so it lands in the vault with the
correct timestamp — BEFORE the turn's tool calls and the agent's reply. This is
the proper fix for the ordering scramble: the Stop hook (turn_hook.py) stamps a
user turn at turn-END, which floats it above the very actions it triggered.
Capturing here makes the live feed read in true order: prompt -> actions -> reply,
and it's interrupt-proof — the message is already saved if the turn is cut short.

Cheap + safe: a salience gate + one SQLite write, NO embedding model loaded.
Every error path exits 0 so it can never block or slow a turn. Prints NOTHING
(UserPromptSubmit stdout can be injected into context — we never want that).
The Stop hook keeps a de-duplicated fallback sweep, so a prompt this hook somehow
misses is still caught later.

Claude Code settings.json:
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
    "command": "python -X utf8 <path>/cairn/prompt_hook.py", "timeout": 10 }] }]
"""
import sys, os, json
from pathlib import Path

# self-contained: add cairn package root to path without needing pip install
sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.turn_hook import _salient   # reuse the EXACT same salience gate


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    # capture mute — same switches the Stop hook honors
    if os.environ.get("CAIRN_CAPTURE") == "0":
        sys.exit(0)
    if (Path.home() / ".cairn" / "CAPTURE_OFF").exists():
        sys.exit(0)

    prompt = (event.get("prompt") or "").strip()
    if not _salient(prompt, "user"):
        sys.exit(0)   # greetings/acks/empty — not worth a node

    session_id = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")
    try:
        from cairn.capture import write_turn
        write_turn(prompt, speaker="user", session=session_id)
    except Exception:
        pass   # capture must NEVER break or delay the turn
    sys.exit(0)


if __name__ == "__main__":
    main()
