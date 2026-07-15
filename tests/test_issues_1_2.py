"""
tests/test_issues_1_2.py — GitHub issue #1 (importer: long-path, Desktop UI
audit.jsonl, --history-from) and issue #2 (doctor sees the Codex MCP wire).

The load-bearing test is test_audit_jsonl_allowlist_drops_all_telemetry: the real
Desktop audit.jsonl is a HYBRID (real turns interleaved with HMAC-signed telemetry
— system / rate_limit_event / result records). This proves the importer captures
ONLY genuine user/assistant turns and never lets telemetry / cost / HMAC into a node.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.local_agent_reader as lar
from cairn.vault import Vault


# ── issue #1: long-path helper ────────────────────────────────────────────────
def test_lp_prefixes_only_absolute_on_windows():
    rel = "a/b/c.jsonl"
    assert lar._lp(rel) == rel                       # relative → never prefixed
    absdir = str(Path.cwd())
    out = lar._lp(absdir)
    if sys.platform == "win32":
        assert out.startswith("\\\\?\\")             # absolute → extended-length
        assert lar._lp(out) == out                   # idempotent (already prefixed)
        unc = lar._lp("\\\\server\\share\\deep\\file.jsonl")
        assert unc.startswith("\\\\?\\UNC\\")        # UNC → \\?\UNC\server\share
        assert lar._lp(unc) == unc                   # idempotent on UNC too
    else:
        assert out == absdir                          # no-op off Windows


# ── issue #1: discovery includes the conversation audit.jsonl, excludes telemetry ─
def _mkfile(p: Path, records: list):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_longpath_over_260_discovered_and_read(tmp_path):
    # a REAL >260-char transcript path must be both discovered AND readable — the
    # exact silent-drop GitHub issue #1 reported. Created via the extended-length
    # prefix so it exists even on a default (LongPathsEnabled-off) Windows box.
    import os as _os
    store = tmp_path / "store"
    long_seg = "d" * 180                                   # push well past MAX_PATH
    deep = (store / "ws" / "host" / "agent" / "local_ditto_L"
            / ".claude" / "projects" / long_seg)
    fpath = deep / "sess.jsonl"
    assert len(str(fpath)) > 260                           # precondition: exceeds 260
    _os.makedirs(lar._lp(str(deep)), exist_ok=True)
    rec = {"type": "user", "uuid": "u1", "timestamp": "2026-07-07T12:00:00Z",
           "sessionId": "LP", "message": {"role": "user",
           "content": "A genuine long-path transcript turn that must be discovered and read"}}
    with open(lar._lp(str(fpath)), "w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    found = lar._walk_transcripts([store])
    hits = [p for p in found if str(p).endswith("sess.jsonl")]
    assert hits, "long-path transcript was NOT discovered"
    turns, sid, _ = lar._iter_turns(hits[0])              # opens via _lp → reads past 260
    assert sid == "LP" and len(turns) == 1
    assert "long-path transcript turn" in (turns[0]["user_text"] or "")
    # clean up the long path ourselves (pytest's rmtree can choke past MAX_PATH)
    try:
        _os.remove(lar._lp(str(fpath)))
    except Exception:
        pass


def test_walk_surfaces_enumeration_errors(tmp_path, monkeypatch):
    # a directory os.walk cannot enumerate must be SURFACED via on_error, not
    # silently skipped (Codex hardening item #2).
    real_walk = lar.os.walk
    def _boom(top, **kw):
        cb = kw.get("onerror")
        if cb:
            cb(OSError("permission denied"))
        return iter(())
    monkeypatch.setattr(lar.os, "walk", _boom)
    seen = []
    lar._walk_transcripts([tmp_path / "store"], on_error=lambda e: seen.append(e))
    assert seen, "enumeration error was swallowed silently"
    monkeypatch.setattr(lar.os, "walk", real_walk)


def test_walk_includes_conversation_audit_excludes_dotclaude_audit(tmp_path):
    store = tmp_path / "store"
    sess = store / "ws" / "host" / "agent" / "local_ditto_X"
    # (a) a normal Claude Code transcript — INCLUDED
    tx = sess / ".claude" / "projects" / "enc" / "sess.jsonl"
    _mkfile(tx, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    # (b) telemetry audit.jsonl UNDER .claude/ — EXCLUDED
    tel = sess / ".claude" / "projects" / "enc" / "audit.jsonl"
    _mkfile(tel, [{"type": "system"}])
    # (c) the Desktop UI-chat audit.jsonl at the local_ditto_X top level — INCLUDED
    ui = sess / "audit.jsonl"
    _mkfile(ui, [{"type": "user", "message": {"role": "user", "content": "hi"}}])

    found = {str(p).replace("\\", "/") for p in lar._walk_transcripts([store])}
    assert any(f.endswith("/enc/sess.jsonl") for f in found)          # transcript in
    assert any(f.endswith("/local_ditto_X/audit.jsonl") for f in found)  # UI chat in
    assert not any(f.endswith("/enc/audit.jsonl") for f in found)     # telemetry OUT


# ── issue #1: the allowlist — hybrid audit.jsonl, zero telemetry leak ──────────
def test_audit_jsonl_allowlist_drops_all_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(lar, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(lar, "CAIRN_HOME", tmp_path)
    store = tmp_path / "store"
    audit = store / "ws" / "host" / "agent" / "local_ditto_S" / "audit.jsonl"
    HMAC_A, HMAC_B = "HMAC_AAAA_SECRET", "HMAC_BBBB_SECRET"
    records = [
        # real turns (snake_case schema: session_id, _audit_timestamp)
        {"type": "user", "uuid": "u1", "session_id": "S",
         "_audit_timestamp": "2026-07-07T12:00:00.000Z", "_audit_hmac": HMAC_A,
         "message": {"role": "user", "content": "Please summarize the launch plan status"}},
        {"type": "assistant", "uuid": "a1", "session_id": "S",
         "_audit_timestamp": "2026-07-07T12:00:03.000Z", "_audit_hmac": HMAC_B,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "The launch has four blockers tracked in the vault and needs a final review before we ship."}]}},
        # TELEMETRY — must never appear in any node
        {"type": "system", "uuid": "s1", "session_id": "S", "_audit_hmac": "H1",
         "_audit_timestamp": "2026-07-07T12:00:04.000Z",
         "tool_name": "SYSTEM_TELEMETRY_MARKER", "estimated_tokens": 999},
        {"type": "rate_limit_event", "uuid": "r1", "session_id": "S", "_audit_hmac": "H2",
         "_audit_timestamp": "2026-07-07T12:00:05.000Z",
         "rate_limit_info": {"marker": "RATELIMIT_TELEMETRY_MARKER"}},
        {"type": "result", "uuid": "res1", "session_id": "S", "_audit_hmac": "H3",
         "_audit_timestamp": "2026-07-07T12:00:06.000Z",
         "total_cost_usd": 0.42, "result": "RESULT_COST_TELEMETRY_MARKER"},
        # synthetic / replay conversation-shaped records — must be dropped
        {"type": "user", "uuid": "u2", "session_id": "S", "isSynthetic": True,
         "_audit_timestamp": "2026-07-07T12:00:07.000Z", "_audit_hmac": "H4",
         "message": {"role": "user", "content": "SYNTHETIC_INJECTED_MARKER"}},
        {"type": "assistant", "uuid": "a2", "session_id": "S", "isReplay": True,
         "_audit_timestamp": "2026-07-07T12:00:08.000Z", "_audit_hmac": "H5",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "REPLAYED_TELEMETRY_MARKER wsjttext padding to exceed forty chars"}]}},
        # snake_case synthetic (schema-drift defense) — must also be dropped
        {"type": "user", "uuid": "u3", "session_id": "S", "is_synthetic": True,
         "_audit_timestamp": "2026-07-07T12:00:09.000Z", "_audit_hmac": "H6",
         "message": {"role": "user", "content": "SNAKE_SYNTHETIC_MARKER never a real turn"}},
    ]
    _mkfile(audit, records)

    v = Vault(db_path=str(tmp_path / "t.db"))
    r = lar.read_local_agent_sessions(
        root=str(store), vault=v, dry_run=False,
        watermark="2026-01-01T00:00:00.000Z")     # old watermark → the turns are "forward"

    # every stored node's searchable text, concatenated
    rows = v.conn.execute(
        "SELECT query, output_preview, episodic_text FROM nodes").fetchall()
    blob = "\n".join((x["query"] or "") + (x["output_preview"] or "")
                     + (x["episodic_text"] or "") for x in rows)

    assert "Please summarize the launch plan status" in blob      # real user turn captured
    assert "four blockers tracked in the vault" in blob           # real agent turn captured
    for leak in ("SYSTEM_TELEMETRY_MARKER", "RATELIMIT_TELEMETRY_MARKER",
                 "RESULT_COST_TELEMETRY_MARKER", "SYNTHETIC_INJECTED_MARKER",
                 "REPLAYED_TELEMETRY_MARKER", "SNAKE_SYNTHETIC_MARKER",
                 HMAC_A, HMAC_B, "0.42"):
        assert leak not in blob, f"TELEMETRY LEAKED INTO VAULT: {leak!r}"
    assert r["written_user"] == 1 and r["written_agent"] == 1      # exactly the two real turns


# ── issue #1: --history-from is an alias for the historical floor ──────────────
def test_history_from_alias_maps_to_include_before(monkeypatch):
    captured = {}

    def _fake(**kw):
        captured.update(kw)
        return {  # minimal report so cmd_import_local_agent doesn't crash
            "account": None, "root": None, "files_scanned": 0, "threads_found": 0,
            "forward_new": 0, "forward_user": 0, "forward_agent": 0,
            "historical_turns": 0, "historical_new": 0, "already_captured": 0,
            "bad_lines": 0, "dropped": 0, "truncated_files": 0, "preview": None,
            "date_min": None, "date_max": None, "first_run": False,
            "provisional_watermark": False,
        }
    import cairn.local_agent_reader as _lar
    monkeypatch.setattr(_lar, "read_local_agent_sessions", _fake)
    from cairn.__main__ import cmd_import_local_agent_sessions
    cmd_import_local_agent_sessions(["--history-from=2026-07-01"])
    assert captured.get("include_before") == "2026-07-01"          # honest name works
    captured.clear()
    cmd_import_local_agent_sessions(["--include-before=2026-06-01"])
    assert captured.get("include_before") == "2026-06-01"          # back-compat alias works


def test_walk_error_surfaced_when_zero_files(monkeypatch, capsys):
    # a totally inaccessible tree yields files_scanned=0 AND walk_errors=1 — the
    # warning must STILL print (it was hidden behind the zero-files early return).
    import cairn.local_agent_reader as _lar
    monkeypatch.setattr(_lar, "read_local_agent_sessions",
                        lambda **_: {"account": None, "root": "X",
                                     "files_scanned": 0, "walk_errors": 1})
    from cairn.__main__ import cmd_import_local_agent_sessions
    cmd_import_local_agent_sessions([])
    out = capsys.readouterr().out
    assert "could not be listed" in out and "1 folder" in out       # surfaced, not hidden
    assert "no transcripts found" in out                            # both messages shown


# ── issue #2: doctor sees the Codex MCP wire ──────────────────────────────────
def test_doctor_reports_codex_mcp(tmp_path, monkeypatch):
    import cairn.vault as vaultmod
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)
    (tmp_path / ".codex").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.cairn]\n'
        'command = "C:/venv/Scripts/python.exe"\n'
        'args = ["-X", "utf8", "-m", "cairn", "mcp"]\n', encoding="utf-8")
    from cairn.__main__ import cmd_doctor
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_doctor([])
    out = buf.getvalue()
    assert "MCP (Codex)" in out
    assert "registered" in out                       # the [mcp_servers.cairn] entry is seen
