"""
tests/test_codex_reader.py — the Codex session-store reader (`cairn import
codex-sessions`). Builds tiny synthetic rollout-*.jsonl fixtures and asserts the
clean-event-stream parse, cross-lane turn:<id> dedup (vs the notify hook),
forward-only watermark split, historical --include-before opt-in, the reversible
apply manifest, single-account attribution, and schema-drift tolerance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cairn.vault as vaultmod
import cairn.codex_reader as rdr
from cairn.vault import MicroNode

THREAD  = "019f2000-aaaa-7000-8000-000000000001"
PARENT  = "019f2000-bbbb-7000-8000-000000000002"   # a forked/parent meta id
T1      = "019f2000-1111-7000-8000-000000000011"
T2      = "019f2000-2222-7000-8000-000000000022"
PAST_WM = "2020-01-01T00:00:00+00:00"              # everything is "forward"
MID_WM  = "2026-07-03T00:00:00+00:00"


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = tmp_path / ".cairn"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rdr, "CAIRN_HOME", home)
    monkeypatch.setattr(rdr, "STATE_FILE", home / "codex_import_state.json")
    monkeypatch.setattr(rdr, "DIAG_LOG", home / "import_codex_diag.log")
    monkeypatch.setattr(vaultmod, "VAULT_ROOT", tmp_path)


def _v(tmp_path):
    return vaultmod.Vault(db_path=tmp_path / "cairn.db")


# ── rollout fixture builders ──────────────────────────────────────────────────
def meta(tid, ts):
    return {"type": "session_meta", "timestamp": ts,
            "payload": {"id": tid, "model_provider": "openai", "cli_version": "0.142.5"}}

def tctx(turn_id, ts, model="gpt-5.5"):
    return {"type": "turn_context", "timestamp": ts,
            "payload": {"turn_id": turn_id, "model": model}}

def umsg(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "user_message", "message": text}}

def amsg(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "agent_message", "message": text}}

def done(turn_id, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "task_complete", "turn_id": turn_id}}

def compacted(ts):
    return {"type": "compacted", "timestamp": ts,
            "payload": {"window_id": "w1",
                        "replacement_history": [{"role": "user",
                                                 "content": [{"type": "input_text", "text": "OLD"}]}]}}

def respitem(role, text, ts):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text", "text": text}]}}

def junk(rtype, ts):
    return {"type": rtype, "timestamp": ts, "payload": {"type": rtype}}


def write_file(root, ymd, created, thread_id, records, raw_extra=None):
    d = Path(root) / ymd
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"rollout-{created}-{thread_id}.jsonl"
    body = "\n".join(json.dumps(r) for r in records)
    if raw_extra:
        body += "\n" + raw_extra
    fp.write_text(body + "\n", encoding="utf-8")
    return fp


def one_clean_turn(ts="2026-07-02T10:00:00+00:00"):
    return [meta(THREAD, ts), tctx(T1, ts), umsg("hello there", ts),
            amsg("hi back", ts), done(T1, ts)]


def session_of(v, thread=THREAD):
    return f"codex-{thread}"


def nodes(v, session):
    return v.conn.execute(
        "SELECT speaker, model, tags, output_preview FROM nodes "
        "WHERE session=? ORDER BY timestamp ASC, rowid ASC", (session,)).fetchall()


# ── tests ─────────────────────────────────────────────────────────────────────
def test_parse_clean_turn(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    rows = nodes(v, session_of(v))
    assert len(rows) == 2
    assert rows[0]["speaker"] == "user"  and rows[0]["model"] == "human"
    assert rows[1]["speaker"] == "agent" and rows[1]["model"] == "gpt-5.5"
    for row in rows:
        assert f"turn:{T1}" in row["tags"]
        assert '"codex"' in row["tags"] and '"conversation"' in row["tags"]
    assert r["written_nodes"] == 2


def test_collapse_multi_agent_message(tmp_path):
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("q", "2026-07-02T10:00:00+00:00"),
            amsg("part one", "2026-07-02T10:00:01+00:00"),
            amsg("part two", "2026-07-02T10:00:02+00:00"),
            amsg("part three", "2026-07-02T10:00:03+00:00"),
            done(T1, "2026-07-02T10:00:04+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    rows = nodes(v, session_of(v))
    agent = [x for x in rows if x["speaker"] == "agent"]
    assert len(agent) == 1                                   # collapsed to ONE node
    assert agent[0]["output_preview"] == "part one\npart two\npart three"


def test_turn_id_comes_from_turn_context(tmp_path):
    # user_message / agent_message payloads carry NO turn_id — it must come from
    # the enclosing turn_context and still land on both emitted nodes.
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    for row in nodes(v, session_of(v)):
        assert f"turn:{T1}" in row["tags"]


def test_session_key_is_filename_uuid(tmp_path):
    # session_meta id disagrees with the filename uuid → filename wins (canonical).
    recs = [meta(PARENT, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("q", "2026-07-02T10:00:00+00:00"), amsg("a", "2026-07-02T10:00:00+00:00"),
            done(T1, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert nodes(v, f"codex-{THREAD}")                       # keyed to filename uuid
    assert not nodes(v, f"codex-{PARENT}")                   # NOT the parent meta id


def test_multi_session_meta_fork(tmp_path):
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"),
            meta(PARENT, "2026-07-02T10:00:01+00:00"),      # 2nd forked meta — ignored for keying
            tctx(T1, "2026-07-02T10:00:02+00:00"),
            umsg("q", "2026-07-02T10:00:02+00:00"), amsg("a", "2026-07-02T10:00:02+00:00"),
            done(T1, "2026-07-02T10:00:03+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert nodes(v, f"codex-{THREAD}")
    assert not nodes(v, f"codex-{PARENT}")


def test_glob_all_date_dirs_and_long_lived(tmp_path):
    # file created 07-01 but its turn is timestamped 07-05 → imported in full,
    # nothing filtered by the directory date.
    recs = [meta(THREAD, "2026-07-01T20:00:00+00:00"), tctx(T1, "2026-07-05T09:00:00+00:00"),
            umsg("late turn", "2026-07-05T09:00:00+00:00"),
            amsg("reply", "2026-07-05T09:00:00+00:00"), done(T1, "2026-07-05T09:00:00+00:00")]
    write_file(tmp_path, "2026/07/01", "2026-07-01T20-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["written_nodes"] == 2 and len(nodes(v, session_of(v))) == 2


def test_dedup_vs_hook(tmp_path):
    # pre-seed a node with turn:T1 (as the notify hook would) → importer skips it.
    v = _v(tmp_path)
    v.write(MicroNode(session=f"codex-{THREAD}", kind="conversation_turn",
                      query="q", output_preview="q", speaker="user", model="human",
                      tags=["codex", "conversation", "user", f"turn:{T1}"]))
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["already_captured"] >= 1
    assert r["written_nodes"] == 0
    assert len(nodes(v, f"codex-{THREAD}")) == 1             # only the seeded node


def test_idempotent_reapply(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    r2 = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r2["written_nodes"] == 0                          # second apply is a no-op
    assert len(nodes(v, session_of(v))) == 2


def test_forward_only_default(tmp_path):
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"),
            tctx(T1, "2026-07-02T10:00:00+00:00"),          # historical (< MID_WM)
            umsg("old q", "2026-07-02T10:00:00+00:00"), amsg("old a", "2026-07-02T10:00:00+00:00"),
            done(T1, "2026-07-02T10:00:00+00:00"),
            tctx(T2, "2026-07-04T10:00:00+00:00"),          # forward (>= MID_WM)
            umsg("new q", "2026-07-04T10:00:00+00:00"), amsg("new a", "2026-07-04T10:00:00+00:00"),
            done(T2, "2026-07-04T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=MID_WM, dry_run=False)
    prev = [x["output_preview"] for x in nodes(v, session_of(v))]
    assert "new q" in prev and "new a" in prev              # forward imported
    assert "old q" not in prev and "old a" not in prev      # historical NOT imported
    assert r["historical_turns"] >= 1 and r["forward_new"] >= 1


def test_include_before_backfills_history(tmp_path):
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"),
            tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("old q", "2026-07-02T10:00:00+00:00"), amsg("old a", "2026-07-02T10:00:00+00:00"),
            done(T1, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=MID_WM,
                                include_before="2026-07-01", dry_run=False)
    prev = [x["output_preview"] for x in nodes(v, session_of(v))]
    assert "old q" in prev and "old a" in prev              # now backfilled
    assert r["historical_new"] >= 1


def test_include_before_floor_excludes_older(tmp_path):
    # history at 07-02, include-before floor at 07-03 → still excluded (older than floor)
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("old q", "2026-07-02T10:00:00+00:00"), amsg("old a", "2026-07-02T10:00:00+00:00"),
            done(T1, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=MID_WM,
                            include_before="2026-07-03", dry_run=False)
    assert not nodes(v, session_of(v))                      # 07-02 < 07-03 floor → excluded


def test_dry_run_writes_nothing(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=True)
    assert r["forward_new"] >= 1                            # it SEES the turn
    assert not nodes(v, session_of(v))                      # but writes nothing
    assert r["backup"] is None


def test_apply_writes_reversible_manifest(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["backup"] and Path(r["backup"]).exists()
    manifest = json.loads(Path(r["backup"]).read_text(encoding="utf-8"))
    assert manifest["count"] == 2 and len(manifest["added_node_ids"]) == 2


def test_compacted_skipped(tmp_path):
    recs = one_clean_turn() + [compacted("2026-07-02T11:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["compacted"] >= 1
    prev = [x["output_preview"] for x in nodes(v, session_of(v))]
    assert "OLD" not in prev                                # replacement_history NOT imported
    assert len(prev) == 2                                   # only the real turn


def test_unknown_record_types_ignored(tmp_path):
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"),
            junk("token_count", "2026-07-02T10:00:00+00:00"),
            junk("reasoning", "2026-07-02T10:00:00+00:00"),
            junk("function_call", "2026-07-02T10:00:00+00:00"),
            tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("real q", "2026-07-02T10:00:00+00:00"), amsg("real a", "2026-07-02T10:00:00+00:00"),
            junk("web_search", "2026-07-02T10:00:00+00:00"), done(T1, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["written_nodes"] == 2


def test_response_item_not_imported(tmp_path):
    # A turn represented ONLY by polluted response_item records (no clean event_msg)
    # yields nothing — v1 ignores response_item entirely.
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            respitem("user", "<environment_context>secret wrapper</environment_context>",
                     "2026-07-02T10:00:00+00:00"),
            respitem("assistant", "polluted reply", "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["written_nodes"] == 0
    assert not nodes(v, session_of(v))


def test_bad_line_non_fatal(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn(),
               raw_extra="{ this is not valid json ]")
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["bad_lines"] >= 1
    assert r["written_nodes"] == 2                          # the rest still imported


def test_attribution_locked_with_account(tmp_path):
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    rdr.read_codex_sessions(root=tmp_path, vault=v, account="bigco",
                            watermark=PAST_WM, dry_run=False)
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id=?",
                         (f"codex-{THREAD}",)).fetchone()
    assert row["account"] == "bigco" and row["account_locked"] == 1


def test_generic_root_no_codex_reference(tmp_path):
    # Proves machine-independence: an arbitrary --root works with no ~/.codex.
    other = tmp_path / "somewhere" / "else"
    write_file(other, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=other, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["files_scanned"] == 1 and r["written_nodes"] == 2


def test_empty_store_is_clean(tmp_path):
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path / "nope", vault=v, dry_run=True)
    assert r["files_scanned"] == 0 and r["written_nodes"] == 0


def test_apply_with_nothing_new_writes_no_manifest(tmp_path):
    # a recurring forward sweep where all turns are historical (watermark ahead)
    # must write zero nodes AND leave no empty backup manifest behind.
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            umsg("old", "2026-07-02T10:00:00+00:00"), amsg("older", "2026-07-02T10:00:00+00:00"),
            done(T1, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=MID_WM, dry_run=False)
    assert r["written_nodes"] == 0 and r["backup"] is None
    assert not list((tmp_path / ".cairn").glob("import-codex-backup-*.json"))


def test_missing_timestamp_turn_is_dropped_not_now(tmp_path):
    # user/agent events with NO top-level timestamp → the turn has no real time.
    # It must be DROPPED, never stamped with now() (which would mis-file it as
    # a forward turn and break the watermark boundary).
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(T1, "2026-07-02T10:00:00+00:00"),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "no ts q"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "no ts a"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": T1}}]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=MID_WM, dry_run=False)
    assert r["written_nodes"] == 0 and r["dropped"] >= 1
    assert not nodes(v, session_of(v))


def test_account_relock_does_not_clobber_existing_lock(tmp_path):
    # session already LOCKED to alice; a later import with --account=bob writes a
    # new turn but must NOT overwrite the locked account (guarded ON CONFLICT).
    v = _v(tmp_path)
    v.conn.execute("INSERT INTO sessions (id, started_at, account, account_locked) "
                   "VALUES (?,?,?,1)", (f"codex-{THREAD}", "2026-07-02T10:00:00+00:00", "alice"))
    v.conn.commit()
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, one_clean_turn())
    rdr.read_codex_sessions(root=tmp_path, vault=v, account="bob",
                            watermark=PAST_WM, dry_run=False)
    row = v.conn.execute("SELECT account, account_locked FROM sessions WHERE id=?",
                         (f"codex-{THREAD}",)).fetchone()
    assert row["account"] == "alice" and row["account_locked"] == 1   # bob rejected
    assert len(nodes(v, session_of(v))) == 2                          # turn still imported


def test_unsafe_turn_id_becomes_idless(tmp_path):
    # a turn_id containing a quote can't be trusted in the turn:<id> JSON tag /
    # LIKE dedup → treated as id-less (no turn tag), never embedded raw.
    bad = 'abc"def'
    recs = [meta(THREAD, "2026-07-02T10:00:00+00:00"), tctx(bad, "2026-07-02T10:00:00+00:00"),
            umsg("q", "2026-07-02T10:00:00+00:00"), amsg("a", "2026-07-02T10:00:00+00:00"),
            done(bad, "2026-07-02T10:00:00+00:00")]
    write_file(tmp_path, "2026/07/02", "2026-07-02T10-00-00", THREAD, recs)
    v = _v(tmp_path)
    r = rdr.read_codex_sessions(root=tmp_path, vault=v, watermark=PAST_WM, dry_run=False)
    assert r["written_nodes"] == 2
    for row in nodes(v, session_of(v)):
        assert "turn:" not in row["tags"]        # id-less
        assert 'abc"def' not in row["tags"]      # raw bad id never stored
