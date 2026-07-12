"""
tests/test_codex_hook.py — the Codex notify-hook suite.

codex_hook.py stands between OpenAI Codex and OpenAI's own notify plumbing, so
two things must hold at once and never trade off:

  CAPTURE   a well-formed agent-turn-complete payload becomes the right nodes —
            user turn (model=human) + agent turn (model from config), chained,
            in a codex-<thread> session, tagged, idempotent by turn-id, and
            inheriting the vault's secret redaction.
  FAIL-SAFE anything malformed, missing, or renamed must NOT crash — it logs to
            the debug file and STILL runs the chain. The hook can't break Codex.

Everything runs on a THROWAWAY vault + a temp ~/.cairn (via monkeypatch) and a
temp config.toml — the live vault and ~/.codex are never touched.

Run: python -m pytest tests/test_codex_hook.py -q   (stdlib only, no net, no GPU)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault
import cairn.codex_hook as ch


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "codex.db")


@pytest.fixture()
def wired(tmp_path, vault, monkeypatch):
    """Point codex_hook's capture at the throwaway db and its ~/.cairn (debug log)
    at tmp. codex_hook does `from cairn.vault import Vault` at call time, so we
    patch the name at its source (cairn.vault.Vault). Returns the vault so a test
    can assert on what got written."""
    import cairn.vault as vault_mod
    monkeypatch.setattr(vault_mod, "Vault", lambda *a, **k: vault)
    monkeypatch.setattr(ch, "CAIRN_HOME", tmp_path / ".cairn")
    monkeypatch.setattr(ch, "DEBUG_LOG", tmp_path / ".cairn" / "codex_hook_debug.log")
    # a stable model so the agent turn is deterministic, without reading ~/.codex
    monkeypatch.setattr(ch, "_codex_model", lambda: "gpt-5.5")
    # ...and no real rollout lookups either: sessions root points at (empty) tmp,
    # so the per-thread model probe finds nothing unless a test plants a rollout.
    monkeypatch.setattr(ch, "CODEX_SESSIONS", tmp_path / ".codex" / "sessions")
    return vault


def _payload(user="deploy the auth fix to staging",
             agent="Done — pushed the auth fix and it is green on staging.",
             thread="t-abc123", turn="turn-001", **extra):
    p = {
        "type": "agent-turn-complete",
        "thread-id": thread,
        "turn-id": turn,
        "input-messages": [user] if user is not None else [],
        "last-assistant-message": agent,
    }
    p.update(extra)
    return json.dumps(p)


def _turns(v):
    return v.conn.execute(
        "SELECT * FROM nodes WHERE kind='conversation_turn' ORDER BY timestamp ASC"
    ).fetchall()


# ── argv parsing ──────────────────────────────────────────────────────────────
class TestArgvParse:
    def test_payload_is_always_last(self):
        chain, payload = ch._parse_argv(["--chain", "prog.exe", "turn-ended", "--", "{}"])
        assert payload == "{}"
        assert chain == ["prog.exe", "turn-ended"]

    def test_no_chain(self):
        chain, payload = ch._parse_argv(["{}"])
        assert chain == [] and payload == "{}"

    def test_chain_arg_with_spaces_survives(self):
        # a chain token containing spaces stays ONE element (own argv slot)
        chain, payload = ch._parse_argv(
            ["--chain", "C:\\Program Files\\x.exe", "turn ended", "--", "{}"])
        assert chain == ["C:\\Program Files\\x.exe", "turn ended"]
        assert payload == "{}"

    def test_missing_closing_sentinel_is_defensive(self):
        chain, payload = ch._parse_argv(["--chain", "prog.exe", "arg", "{}"])
        assert payload == "{}" and chain == ["prog.exe", "arg"]


# ── capture: payload → nodes ──────────────────────────────────────────────────
class TestCapture:
    def test_writes_user_and_agent_turn(self, wired):
        n = ch._capture(_payload(), deadline=_far())
        assert n == 2
        rows = _turns(wired)
        assert len(rows) == 2
        user, agent = rows
        assert user["speaker"] == "user" and user["model"] == "human"
        assert agent["speaker"] == "agent" and agent["model"] == "gpt-5.5"

    def test_session_is_codex_thread(self, wired):
        ch._capture(_payload(thread="t-XYZ"), deadline=_far())
        assert all(r["session"] == "codex-t-XYZ" for r in _turns(wired))

    def test_session_fallback_when_no_thread(self, wired):
        ch._capture(_payload(thread=""), deadline=_far())
        rows = _turns(wired)
        assert rows and rows[0]["session"].startswith("codex-unknown-")

    def test_tags_present(self, wired):
        ch._capture(_payload(turn="turn-77"), deadline=_far())
        for r in _turns(wired):
            tags = json.loads(r["tags"])
            assert "codex" in tags and "conversation" in tags
            assert "turn:turn-77" in tags

    def test_agent_chains_onto_user(self, wired):
        ch._capture(_payload(), deadline=_far())
        user, agent = _turns(wired)
        assert agent["parent"] == user["id"]
        assert user["parent"] is None      # first node in a fresh session

    def test_second_turn_chains_onto_first(self, wired):
        ch._capture(_payload(turn="t1", user="first ask", agent="first big answer here"),
                    deadline=_far())
        ch._capture(_payload(turn="t2", user="second ask", agent="second big answer here"),
                    deadline=_far())
        rows = _turns(wired)
        assert len(rows) == 4
        # turn 2's user parent is turn 1's agent (walkable thread)
        assert rows[2]["parent"] == rows[1]["id"]

    def test_structured_input_message_forms(self, wired):
        # Codex may send structured blocks instead of a bare string — defensive.
        raw = _payload(user=None)
        p = json.loads(raw)
        p["input-messages"] = [{"type": "text", "text": "structured user prompt"}]
        n = ch._capture(json.dumps(p), deadline=_far())
        assert n == 2
        assert _turns(wired)[0]["query"] == "structured user prompt"

    def test_uses_last_input_message(self, wired):
        raw = _payload(user=None)
        p = json.loads(raw)
        p["input-messages"] = ["older", "newer prompt that drove this turn"]
        ch._capture(json.dumps(p), deadline=_far())
        assert _turns(wired)[0]["query"] == "newer prompt that drove this turn"


# ── idempotency ───────────────────────────────────────────────────────────────
class TestIdempotency:
    def test_same_turn_id_twice_is_one_pair(self, wired):
        ch._capture(_payload(turn="dup-1"), deadline=_far())
        ch._capture(_payload(turn="dup-1"), deadline=_far())   # re-fire
        assert len(_turns(wired)) == 2      # not 4

    def test_different_turn_ids_both_written(self, wired):
        ch._capture(_payload(turn="a"), deadline=_far())
        ch._capture(_payload(turn="b"), deadline=_far())
        assert len(_turns(wired)) == 4


# ── fail-safe: malformed payloads never crash, still chain ────────────────────
class TestFailSafe:
    def test_bad_json_no_crash_and_logs(self, wired, tmp_path):
        n = ch._capture("{not json", deadline=_far())
        assert n == 0
        log = tmp_path / ".cairn" / "codex_hook_debug.log"
        assert log.exists() and "not valid JSON" in log.read_text(encoding="utf-8")

    def test_wrong_event_type_skips_capture(self, wired):
        raw = _payload()
        p = json.loads(raw); p["type"] = "some-other-event"
        assert ch._capture(json.dumps(p), deadline=_far()) == 0
        assert _turns(wired) == []

    def test_empty_turn_writes_nothing(self, wired):
        assert ch._capture(_payload(user="", agent=""), deadline=_far()) == 0

    def test_missing_fields_no_crash(self, wired):
        # only a type, nothing else — must not raise
        assert ch._capture(json.dumps({"type": "agent-turn-complete"}),
                           deadline=_far()) == 0

    def test_main_always_runs_chain_even_on_capture_failure(self, wired, monkeypatch):
        """The core contract: capture blowing up must not stop the chain."""
        calls = {}

        def boom(*a, **k):
            raise RuntimeError("vault exploded")
        monkeypatch.setattr(ch, "_capture", boom)

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            class R:  # noqa: E306
                returncode = 0
            return R()
        monkeypatch.setattr(ch.subprocess, "run", fake_run)

        rc = ch.main(["--chain", "orig.exe", "turn-ended", "--", _payload()])
        assert rc == 0
        # chain ran with original args + payload appended last (as Codex would)
        assert calls["cmd"][:2] == ["orig.exe", "turn-ended"]
        assert calls["cmd"][-1] == _payload()

    def test_main_chains_after_successful_capture(self, wired, monkeypatch):
        calls = {}
        monkeypatch.setattr(ch.subprocess, "run",
                            lambda cmd, **k: calls.setdefault("cmd", cmd))
        ch.main(["--chain", "orig.exe", "--", _payload(turn="chain-ok")])
        assert calls["cmd"][:1] == ["orig.exe"]
        assert len(_turns(wired)) == 2

    def test_no_chain_no_subprocess(self, wired, monkeypatch):
        called = {"n": 0}

        def fake_run(*a, **k):
            called["n"] += 1
        monkeypatch.setattr(ch.subprocess, "run", fake_run)
        ch.main([_payload(turn="nochain")])
        assert called["n"] == 0            # nothing to chain → no subprocess
        assert len(_turns(wired)) == 2


# ── lossless capture: a turn over the display cap keeps its COMPLETE text ─────
class TestLosslessCapture:
    def test_long_turn_not_cut_off(self, wired):
        # owner bar: "full capture of whatever I say." A user turn longer than the
        # display cap must survive VERBATIM — the tail lands in episodic_text via
        # episodic_full, while the display fields stay bounded.
        long_user = "START-" + ("x" * (ch.TRUNC_PREVIEW + 5000)) + "-END"
        assert len(long_user) > ch.TRUNC_PREVIEW
        ch._capture(_payload(user=long_user, agent="ok", turn="long-1"), deadline=_far())
        user = _turns(wired)[0]
        assert len(user["query"]) == ch.TRUNC_QUERY                # display capped
        assert len(user["output_preview"]) == ch.TRUNC_PREVIEW     # display capped
        et = user["episodic_text"] or ""
        assert long_user in et                                     # complete text kept
        assert et.endswith("-END")                                 # the tail is NOT lost


# ── redaction inherited from the vault write-gate ─────────────────────────────
class TestRedaction:
    def test_secret_in_payload_is_scrubbed(self, wired):
        # a real MUST_REDACT shape from tests/test_redact_corpus.py (OpenAI key)
        secret = "sk-" + "proj1234567890ABCDEFxyz"
        ch._capture(_payload(user=f"here is my key {secret} keep it safe",
                             agent="I will not store that."), deadline=_far())
        rows = _turns(wired)
        blob = " ".join((r["query"] or "") + (r["output_preview"] or "")
                        + (r["episodic_text"] or "") for r in rows)
        assert secret not in blob                 # never lands in the vault
        assert "[REDACTED:OPENAI_KEY]" in blob    # scrubbed, not merely dropped


# ── install / uninstall round-trip on a TEMP config.toml ──────────────────────
# Copies the real machine's config structure (notify + model + [mcp_servers]) so
# byte-preservation of untouched lines is actually exercised.
SAMPLE_TOML = (
    'notify = [ "C:\\\\Tools\\\\codex-computer-use.exe", "turn-ended" ]\n'
    'model = "gpt-5.5"\n'
    'model_reasoning_effort = "high"\n'
    '\n'
    '[mcp_servers.node_repl]\n'
    'args = []\n'
    "command = 'C:\\\\Tools\\\\node_repl.exe'\n"
    'startup_timeout_sec = 120\n'
)


def _install_cli(monkeypatch, conf_path: Path):
    """Run cmd_codex_hook with ~/.codex/config.toml redirected to a temp file."""
    import cairn.__main__ as m
    monkeypatch.setattr(m, "_codex_conf_path", lambda: conf_path)
    return m


class TestInstallUninstall:
    def _conf(self, tmp_path, body=SAMPLE_TOML):
        d = tmp_path / ".codex"
        d.mkdir()
        f = d / "config.toml"
        f.write_text(body, encoding="utf-8")
        return f

    def test_install_wraps_original_and_backs_up(self, tmp_path, monkeypatch, capsys):
        conf = self._conf(tmp_path)
        m = _install_cli(monkeypatch, conf)
        m.cmd_codex_hook(["install"])
        text = conf.read_text(encoding="utf-8")
        # notify now points at the hook AND encodes the original as chain args
        notify = m._read_notify_array(text)
        assert m._codex_is_installed(notify)
        assert "--chain" in notify and "turn-ended" in notify
        assert "C:\\Tools\\codex-computer-use.exe" in notify
        # backup created
        assert list(conf.parent.glob("config.toml.bak-cairn-hook-*"))
        # untouched lines preserved byte-for-byte
        assert 'model = "gpt-5.5"' in text
        assert "[mcp_servers.node_repl]" in text
        assert "startup_timeout_sec = 120" in text

    def test_install_is_idempotent(self, tmp_path, monkeypatch):
        conf = self._conf(tmp_path)
        m = _install_cli(monkeypatch, conf)
        m.cmd_codex_hook(["install"])
        first = conf.read_text(encoding="utf-8")
        m.cmd_codex_hook(["install"])              # second install = no-op
        assert conf.read_text(encoding="utf-8") == first
        # only ONE backup ever
        assert len(list(conf.parent.glob("config.toml.bak-cairn-hook-*"))) == 1

    def test_round_trip_restores_original_notify(self, tmp_path, monkeypatch):
        conf = self._conf(tmp_path)
        m = _install_cli(monkeypatch, conf)
        original_notify = m._read_notify_array(conf.read_text(encoding="utf-8"))
        m.cmd_codex_hook(["install"])
        m.cmd_codex_hook(["uninstall"])
        restored = m._read_notify_array(conf.read_text(encoding="utf-8"))
        assert restored == original_notify         # exact restore

    def test_untouched_lines_survive_round_trip(self, tmp_path, monkeypatch):
        conf = self._conf(tmp_path)
        m = _install_cli(monkeypatch, conf)
        m.cmd_codex_hook(["install"])
        m.cmd_codex_hook(["uninstall"])
        text = conf.read_text(encoding="utf-8")
        for line in ('model = "gpt-5.5"', 'model_reasoning_effort = "high"',
                     "[mcp_servers.node_repl]", "startup_timeout_sec = 120"):
            assert line in text

    def test_install_without_notify_key(self, tmp_path, monkeypatch):
        # a config that has NO notify line → install adds one with no chain
        body = 'model = "gpt-5.5"\n[features]\njs_repl = false\n'
        conf = self._conf(tmp_path, body)
        m = _install_cli(monkeypatch, conf)
        m.cmd_codex_hook(["install"])
        notify = m._read_notify_array(conf.read_text(encoding="utf-8"))
        assert m._codex_is_installed(notify)
        assert "--chain" not in notify             # nothing to wrap
        # uninstall removes the line we added entirely
        m.cmd_codex_hook(["uninstall"])
        assert m._read_notify_array(conf.read_text(encoding="utf-8")) is None

    def test_uninstall_when_not_installed_is_noop(self, tmp_path, monkeypatch):
        conf = self._conf(tmp_path)
        m = _install_cli(monkeypatch, conf)
        before = conf.read_text(encoding="utf-8")
        m.cmd_codex_hook(["uninstall"])
        assert conf.read_text(encoding="utf-8") == before

    def test_install_refuses_to_clobber_unparseable_notify(self, tmp_path, monkeypatch):
        # a notify line with an UNESCAPED backslash is invalid TOML; install must
        # refuse rather than silently drop OpenAI's plumbing.
        body = 'notify = [ "C:\\Bad\\path.exe", "turn-ended" ]\nmodel = "gpt-5.5"\n'
        conf = self._conf(tmp_path, body)
        m = _install_cli(monkeypatch, conf)
        before = conf.read_text(encoding="utf-8")
        m.cmd_codex_hook(["install"])
        assert conf.read_text(encoding="utf-8") == before   # untouched
        assert not list(conf.parent.glob("config.toml.bak-cairn-hook-*"))


# ── model truth: the thread's rollout beats the global config key ─────────────
class TestThreadModel:
    """The owner's 2026-07-11 repro, frozen as a test: picker Luna→Terra→Sol
    mid-thread while config.toml's `model` key never moved. The stamp must come
    from the thread's own rollout (last turn_context), not the config key."""

    def _rollout(self, tmp_path, thread="t-abc123",
                 models=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")):
        day = tmp_path / ".codex" / "sessions" / "2026" / "07" / "11"
        day.mkdir(parents=True, exist_ok=True)
        p = day / f"rollout-2026-07-11T02-37-49-{thread}.jsonl"
        lines = [json.dumps({"type": "session_meta",
                             "payload": {"model_provider": "openai"}})]
        lines += [json.dumps({"type": "turn_context", "payload": {"model": m}})
                  for m in models]
        lines += [json.dumps({"type": "event_msg",
                              "payload": {"info": {"model_context_window": 1}}})]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_agent_model_from_thread_rollout_not_config(self, wired, tmp_path):
        self._rollout(tmp_path)                     # thread ended on sol
        ch._capture(_payload(), deadline=_far())    # config still says gpt-5.5
        user, agent = _turns(wired)
        assert agent["model"] == "gpt-5.6-sol"      # last turn_context wins
        assert user["model"] == "human"             # user side untouched

    def test_falls_back_to_config_without_rollout(self, wired):
        ch._capture(_payload(thread="t-no-rollout"), deadline=_far())
        _, agent = _turns(wired)
        assert agent["model"] == "gpt-5.5"

    def test_turn_context_beyond_first_scan_step(self, wired, tmp_path):
        # turn_context sits at TURN START, so a long final turn pushes it far
        # from EOF — the live 2026-07-11 probe caught exactly this (real threads
        # resolved to fallback past a one-shot 256 KB tail). The backward scan
        # must extend past the first step and still find it.
        p = self._rollout(tmp_path)
        big = json.dumps({"type": "event_msg", "payload": {"data": "x" * 300_000}})
        with open(p, "a", encoding="utf-8") as f:
            f.write(big + "\n")
        ch._capture(_payload(), deadline=_far())
        _, agent = _turns(wired)
        assert agent["model"] == "gpt-5.6-sol"

    def test_torn_tail_line_is_skipped(self, wired, tmp_path):
        # the app can be mid-write when the hook fires: a torn final line must
        # neither crash nor mislabel — the reversed scan skips it and finds the
        # last complete turn_context.
        p = self._rollout(tmp_path)
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"type": "turn_context", "payload": {"model": ')
        ch._capture(_payload(), deadline=_far())
        _, agent = _turns(wired)
        assert agent["model"] == "gpt-5.6-sol"


def _far() -> float:
    """A deadline far in the future — capture is never budget-bound in tests."""
    import time
    return time.perf_counter() + 1e6


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
