"""
tests/test_cairn.py — the trust suite.

A memory system's value proposition is "trust me with your memory" — a silent
SQL or scoring bug corrupts retrieval and nobody notices. These tests cover
the load-bearing invariants on a throwaway vault:

  vault     append-only trigger, void/flag transitions, metadata updates
  ledger    write-through receipts, cited marking, crash-safety semantics
  edges     kNN build, tiering, chain extraction, community detection + labels
  retrieve  budget packing, graph-RAG hop, drift walk + cross-community bonus
  inject    JSON envelope shape (additionalContext, not raw stdout)
  garden    LIKE wildcard escaping

Run: python -m pytest tests/ -q     (no GPU, no embedder, no network)
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault, MicroNode

DIM = 384


def _vec(seed: float) -> bytes:
    """Deterministic unit-ish embedding clustered by integer part of seed."""
    base = [0.0] * DIM
    i = int(seed) % DIM
    base[i] = 1.0
    base[(i + 1) % DIM] = seed % 1.0
    return struct.pack(f"{DIM}f", *base)


@pytest.fixture()
def vault(tmp_path):
    return Vault(db_path=tmp_path / "test.db")


def _write(v, kind="decision", query="choose sqlite over lancedb", session="s1",
           parent=None, tags=None, emb_seed=None):
    node = v.write(MicroNode(session=session, kind=kind, query=query,
                             parent=parent, tags=tags or [], model="test"))
    if emb_seed is not None:
        v.conn.execute("UPDATE nodes SET embedding=? WHERE id=?",
                       (_vec(emb_seed), node.id))
        v.conn.commit()
    return node


# ── vault invariants ──────────────────────────────────────────────────────────

class TestVault:
    def test_write_and_get(self, vault):
        n = _write(vault)
        row = vault.get(n.id)
        assert row["kind"] == "decision"
        assert row["status"] == "active"

    def test_append_only_blocks_content_mutation(self, vault):
        n = _write(vault)
        with pytest.raises(Exception):
            vault.conn.execute(
                "UPDATE nodes SET query='rewritten history' WHERE id=?", (n.id,))

    def test_void_is_allowed(self, vault):
        n = _write(vault)
        vault.void(n.id)
        assert vault.get(n.id)["status"] == "void"

    def test_metadata_update_allowed(self, vault):
        n = _write(vault)
        vault.conn.execute(
            "UPDATE nodes SET community='c1|test topic' WHERE id=?", (n.id,))
        vault.conn.commit()
        assert vault.get(n.id)["community"] == "c1|test topic"

    def test_importance_derived_from_kind(self, vault):
        w = _write(vault, kind="warning")
        t = _write(vault, kind="tool_call")
        assert vault.get(w.id)["importance"] > vault.get(t.id)["importance"]


# ── attention ledger ──────────────────────────────────────────────────────────

class TestLedger:
    def test_record_shown_writes_receipts(self, vault):
        a, b = _write(vault), _write(vault, query="second")
        n = vault.record_shown([a.id, b.id], channel="test",
                               session="s1", trigger="unit")
        assert n == 2
        rows = vault.conn.execute(
            "SELECT * FROM attention_ledger ORDER BY position").fetchall()
        assert [r["node_id"] for r in rows] == [a.id, b.id]
        assert rows[0]["channel"] == "test"
        assert rows[0]["position"] == 0 and rows[1]["position"] == 1
        assert all(r["cited"] == 0 for r in rows)

    def test_record_shown_stamps_last_injected(self, vault):
        a = _write(vault)
        vault.record_shown([a.id], channel="test")
        assert vault.get(a.id)["last_injected"] is not None

    def test_mark_cited_closes_loop(self, vault):
        a = _write(vault)
        vault.record_shown([a.id], channel="test")
        assert vault.mark_cited([a.id]) == 1
        row = vault.conn.execute(
            "SELECT cited, cited_at FROM attention_ledger").fetchone()
        assert row["cited"] == 1 and row["cited_at"]

    def test_empty_and_bad_input_never_raise(self, vault):
        assert vault.record_shown([], channel="x") == 0
        assert vault.mark_cited([]) == 0


# ── edges + communities ───────────────────────────────────────────────────────

class TestEdges:
    def _seed_graph(self, vault):
        # two tight clusters of 4 + a chain — embeddings cluster by int(seed)
        ids = []
        for i in range(4):
            ids.append(_write(vault, query=f"cairn architecture part {i}",
                              session="s1", emb_seed=10 + i * 0.1).id)
        for i in range(4):
            ids.append(_write(vault, query=f"golf scorecards part {i}",
                              session="s2", emb_seed=20 + i * 0.1).id)
        child = _write(vault, query="chained", parent=ids[0])
        return ids, child

    def test_build_edges_types_and_tiers(self, vault):
        from cairn.edges import build_edges
        ids, child = self._seed_graph(vault)
        rep = build_edges(vault, k=3)
        assert rep["chain"] == 1
        assert rep["semantic"] > 0
        tiers = {r["tier"] for r in vault.conn.execute(
            "SELECT DISTINCT tier FROM edges WHERE type='semantic'")}
        assert tiers <= {"strong", "medium", "weak"}
        # same-cluster nodes must be strongly connected
        strong = vault.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE tier='strong'").fetchone()["c"]
        assert strong > 0

    def test_rebuild_is_idempotent(self, vault):
        from cairn.edges import build_edges
        self._seed_graph(vault)
        r1 = build_edges(vault, k=3)
        r2 = build_edges(vault, k=3)
        assert r1["total"] == r2["total"]

    def test_communities_detected_and_labeled(self, vault):
        from cairn.edges import build_all
        self._seed_graph(vault)
        rep = build_all(vault, k=3)
        assert rep["communities"] >= 2
        comms = {r["community"] for r in vault.conn.execute(
            "SELECT community FROM nodes WHERE community IS NOT NULL")}
        assert len(comms) >= 2
        assert all("|" in c for c in comms)   # 'c<n>|<label>' format


# ── retrieval: fetch, graph hop, drift ───────────────────────────────────────

class TestRetrieve:
    def test_fetch_pack_respects_budget_shape(self, vault, monkeypatch):
        from cairn import retrieve
        nodes = [_write(vault, query=f"fact {i}", emb_seed=10 + i * 0.1)
                 for i in range(6)]
        fake_hits = [{"id": n.id, "kind": "decision", "gist": f"fact {i}",
                      "query": f"fact {i}", "score": 0.9 - i * 0.05,
                      "session": "s1", "tags": "[]",
                      "output_preview": "x" * 400} for i, n in enumerate(nodes)]
        monkeypatch.setattr(Vault, "query_episodic", lambda self, q, k=20: fake_hits)
        pack = retrieve.fetch_pack("anything", vault=vault, budget_tokens=150)
        assert pack["count"] == 6
        verbatim = [r for r in pack["results"] if r["text"]]
        gist_only = [r for r in pack["results"] if not r["text"]]
        assert verbatim and gist_only     # budget forced a split
        # receipts written for everything shown
        n_receipts = vault.conn.execute(
            "SELECT COUNT(*) c FROM attention_ledger").fetchone()["c"]
        assert n_receipts >= 6

    def test_drift_prefers_cross_community(self, vault, monkeypatch):
        from cairn import retrieve
        from cairn.edges import build_all
        a = _write(vault, query="seed topic", emb_seed=10.0, session="s1")
        same = [_write(vault, query=f"same community {i}", emb_seed=10.1 + i / 10,
                       session="s1") for i in range(3)]
        other = [_write(vault, query=f"other community {i}", emb_seed=20.0 + i / 10,
                        session="s2") for i in range(3)]
        build_all(vault, k=3)
        # bridge the clusters with one weak edge so drift can wander across
        vault.conn.execute(
            "INSERT OR REPLACE INTO edges (src,dst,type,tier,weight,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (a.id, other[0].id, "semantic", "weak", 0.65, "now"))
        vault.conn.commit()
        monkeypatch.setattr(Vault, "query_episodic",
                            lambda self, q, k=5: [{"id": a.id, "gist": "seed",
                                                   "query": "seed topic"}])
        pack = retrieve.drift_pack("seed topic", vault=vault, hops=2, k=10)
        assert pack["results"], "drift found nothing"
        found_ids = {r["id"] for r in pack["results"]}
        assert other[0].id in found_ids    # crossed the bridge
        drift_receipts = vault.conn.execute(
            "SELECT COUNT(*) c FROM attention_ledger WHERE channel='drift'"
        ).fetchone()["c"]
        assert drift_receipts == len(pack["results"])


# ── injection channel ─────────────────────────────────────────────────────────

class TestInjectChannel:
    def test_envelope_is_additional_context_json(self, capsys, monkeypatch, tmp_path):
        import cairn.inject as inj
        monkeypatch.setattr(inj, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(inj, "DB_PATH", tmp_path / "db.db")
        Vault(db_path=tmp_path / "db.db")    # create schema
        fake_rows = []
        monkeypatch.setattr(inj, "check_counter",
                            lambda *a, **k: ["BLOCK LINE ONE", "BLOCK LINE TWO"])
        monkeypatch.setattr(inj, "check_struggle", lambda *a, **k: None)
        monkeypatch.setattr(inj, "check_file_recurrence", lambda *a, **k: None)
        monkeypatch.setattr(inj, "check_drift", lambda *a, **k: None)
        inj.run_inject("Read", "x.py", 5, 100, "test-session")
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)            # MUST be valid JSON, not a box
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "BLOCK LINE ONE" in ctx and "BLOCK LINE TWO" in ctx
        assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_silent_when_no_triggers(self, capsys, monkeypatch, tmp_path):
        import cairn.inject as inj
        monkeypatch.setattr(inj, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(inj, "DB_PATH", tmp_path / "db.db")
        Vault(db_path=tmp_path / "db.db")
        for fn in ("check_counter", "check_struggle",
                   "check_file_recurrence", "check_drift"):
            monkeypatch.setattr(inj, fn, lambda *a, **k: None)
        inj.run_inject("Read", "x.py", 5, 100, "test-session")
        assert capsys.readouterr().out.strip() == ""


# ── garden LIKE escaping ──────────────────────────────────────────────────────

class TestLikeEscape:
    def test_wildcards_neutralized(self, vault):
        # replicate garden's _like helper contract directly against SQLite
        def _like(term):
            return (term.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_"))
        _write(vault, tags=["widgets"])
        _write(vault, tags=["secret"])
        hostile = "%"     # un-escaped, this matches EVERY tagged node
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE tags LIKE ? ESCAPE '\\'",
            (f'%"{_like(hostile)}"%',)).fetchall()
        assert rows == []
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE tags LIKE ? ESCAPE '\\'",
            (f'%"{_like("widgets")}"%',)).fetchall()
        assert len(rows) == 1


# ── vector index: fast path must equal the pure-Python path ─────────────────

class _FakeEmbedder:
    dim = DIM
    def encode_one(self, text):
        # deterministic across processes — built-in hash() is salted per process
        # (PYTHONHASHSEED), which made text->vector vary run to run and flaked
        # any test asserting a specific ranking.
        import hashlib
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        return _vec(float(h % 50) + 0.37)


class TestIndex:
    def _ready(self, vault, n=12):
        nodes = [_write(vault, kind="decision" if i % 3 else "warning",
                        query=f"fact about topic {i // 4} item {i}",
                        session=f"s{i % 2}", emb_seed=10 + i * 0.7)
                 for i in range(n)]
        vault._embedder = _FakeEmbedder()
        return nodes

    def test_fast_path_matches_python_path(self, vault, monkeypatch):
        from cairn.index import EmbeddingIndex
        self._ready(vault)
        fast = vault.query_episodic("fact about topic 1", k=6)
        monkeypatch.setattr(EmbeddingIndex, "ensure", lambda self, conn: False)
        vault._index = None
        slow = vault.query_episodic("fact about topic 1", k=6)
        assert [r["id"] for r in fast] == [r["id"] for r in slow]
        for f, s in zip(fast, slow):
            assert abs(f["score"] - s["score"]) < 1e-5
            assert abs(f["score_cosine"] - s["score_cosine"]) < 1e-5

    def test_index_sees_new_writes(self, vault):
        self._ready(vault, n=6)
        vault.query_episodic("anything", k=3)          # build index
        q = "brand new unique zebra fact"
        fresh = _write(vault, query=q)
        # Embed the fresh node with the SAME vector the query produces, so this
        # asserts INDEX SIGNATURE INVALIDATION (does the rebuilt index see the
        # new write?) rather than the luck of synthetic-vector ranking — which
        # is what made it flaky under per-process hash randomization.
        vault.conn.execute("UPDATE nodes SET embedding=? WHERE id=?",
                           (vault._embedder.encode_one(q), fresh.id))
        vault.conn.commit()
        hits = vault.query_episodic(q, k=3)
        assert fresh.id in [r["id"] for r in hits]      # signature invalidated → seen

    def test_fast_path_session_filter(self, vault):
        self._ready(vault)
        hits = vault.query_episodic("fact about topic 1", k=10,
                                    session_id="s1")
        assert hits and all(r["session"] == "s1" for r in hits)

    def test_voided_nodes_leave_index(self, vault):
        nodes = self._ready(vault, n=6)
        vault.query_episodic("anything", k=3)
        vault.void(nodes[0].id)
        hits = vault.query_episodic("fact about topic 0", k=10)
        assert nodes[0].id not in [r["id"] for r in hits]


# ── dashboard JS must parse — a SyntaxError blanks the entire brain UI ───────

class TestDashboardJS:
    def test_served_javascript_parses(self, tmp_path):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed — JS check unavailable")
        from cairn.dashboard import DASHBOARD_HTML
        blocks = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S)
        assert blocks, "no inline script found in DASHBOARD_HTML"
        js_file = tmp_path / "dash.js"
        js_file.write_text(blocks[-1], encoding="utf-8")
        r = subprocess.run([node, "--check", str(js_file)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"dashboard JS broken:\n{r.stderr[:500]}"


# ── secret redaction — the vault is append-only, a leaked key is forever ─────

class TestRedact:
    def test_provider_keys_redacted(self):
        from cairn.redact import redact
        for raw, label in [
            ("sk-ant-api03-aaaaaaaaaaaaaaaaaaaa", "ANTHROPIC_KEY"),
            ("AKIA1234567890ABCDEF", "AWS_KEY"),
            ("ghp_" + "a" * 36, "GITHUB_PAT"),
        ]:
            clean, n = redact(f"token={raw} trailing")
            assert n >= 1
            assert raw not in clean
            assert "REDACTED" in clean

    def test_secret_assignments_keep_key_drop_value(self):
        from cairn.redact import redact
        clean, n = redact('api_key = "supersecret12345"')
        assert "supersecret12345" not in clean
        assert "api_key" in clean and n == 1

    def test_connection_string_and_bearer(self):
        from cairn.redact import scrub
        assert "p4ssw0rd" not in scrub("postgres://u:p4ssw0rd@h:5432/db")
        assert "abcdefghij12345" not in scrub("Bearer abcdefghij12345xyz")

    def test_normal_text_untouched(self):
        from cairn.redact import redact
        clean, n = redact("the golf leaderboard updated at noon")
        assert n == 0 and clean == "the golf leaderboard updated at noon"

    def test_none_and_nonstr_safe(self):
        from cairn.redact import redact
        assert redact(None) == (None, 0)
        assert redact(12345) == (12345, 0)


class TestRedactionWriteGate:
    """The write-gate (vault.py write()): redaction must run for EVERY writer,
    not just the capture hook — so a secret in any text field is gone after a
    round-trip through Vault.write, and the derived episodic_text inherits the
    cleaned text. Pairs with tests/test_redact_corpus.py (which guards the
    patterns); this proves the chokepoint is actually wired."""

    def test_secret_scrubbed_on_write(self, vault):
        secret_a = "sk-ant-api03-" + "Z" * 20
        secret_b = "AKIA" + "B" * 16
        node = vault.write(MicroNode(
            session="s1", kind="decision", model="test",
            query=f"my key is {secret_a}",
            output_preview=f"leaked {secret_b} in the logs"))
        row = vault.get(node.id)
        assert secret_a not in (row["query"] or "")
        assert secret_b not in (row["output_preview"] or "")
        assert "[REDACTED:" in (row["query"] or "")
        # the derived episodic_text must inherit the cleaned source text
        assert secret_a not in (row["episodic_text"] or "")

    def test_clean_text_untouched_on_write(self, vault):
        node = vault.write(MicroNode(
            session="s1", kind="decision", model="test",
            query="chose sqlite over postgres for local-first"))
        assert vault.get(node.id)["query"] == "chose sqlite over postgres for local-first"

    def test_opt_out_disables_scrub(self, vault, monkeypatch):
        monkeypatch.setenv("CAIRN_NO_REDACT", "1")
        raw = "key sk-ant-api03-" + "Q" * 20
        node = vault.write(MicroNode(session="s1", kind="decision",
                                     model="test", query=raw))
        assert raw in (vault.get(node.id)["query"] or "")

    def test_secret_scrubbed_from_tags(self, vault):
        # tags also go through the write-gate — a client tagging a secret must
        # not leak it into the append-only store (real tags pass through clean)
        secret = "sk-ant-api03-" + "T" * 20
        node = vault.write(MicroNode(
            session="s1", kind="decision", model="test", query="a decision",
            tags=[f"ctx:{secret}", "kw:normal"]))
        row = vault.get(node.id)
        assert secret not in (row["tags"] or "")
        assert "[REDACTED:" in (row["tags"] or "")
        assert "kw:normal" in (row["tags"] or "")   # legit tag untouched


class TestAppendOnlyHardening:
    """The guard must freeze CONTENT + chain even on otherwise-legit mutations
    (void/flag), still allow real metadata updates, and back up cleanly. These
    pin the 'content rides an allowed transition' hole the trigger now closes."""

    def test_content_cannot_ride_a_void(self, vault):
        node = _write(vault, query="original decision")
        with pytest.raises(Exception):
            vault.conn.execute(
                "UPDATE nodes SET status='void', episodic_text='POISONED' WHERE id=?",
                (node.id,))

    def test_content_cannot_ride_a_flag(self, vault):
        node = _write(vault, query="original decision")
        with pytest.raises(Exception):
            vault.conn.execute(
                "UPDATE nodes SET flagged=1, query='POISONED' WHERE id=?", (node.id,))

    def test_plain_void_still_allowed(self, vault):
        node = _write(vault)
        vault.conn.execute("UPDATE nodes SET status='void' WHERE id=?", (node.id,))
        vault.conn.commit()
        assert vault.get(node.id)["status"] == "void"

    def test_metadata_update_still_allowed(self, vault):
        # importance + community ride the scheduling branch — must still work
        node = _write(vault)
        vault.conn.execute("UPDATE nodes SET importance=9 WHERE id=?", (node.id,))
        vault.conn.execute("UPDATE nodes SET community=3 WHERE id=?", (node.id,))
        vault.conn.commit()
        row = vault.get(node.id)
        assert row["importance"] == 9 and str(row["community"]) == "3"

    def test_backup_creates_openable_verified_copy(self, vault, tmp_path):
        _write(vault, query="keep me safe")
        out = vault.backup(tmp_path / "snap.db")
        assert out.exists()
        restored = Vault(db_path=out)        # opens clean = guard intact = valid
        n = restored.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert n >= 1


class TestDimContract:
    """A model whose vectors aren't DIM-sized must fail LOUD — refused at write,
    warned at read — instead of silently emptying search (the 'model swap empties
    search' bug). Guards: CPUEmbed.encode (write) + EmbeddingIndex.ensure (read)."""

    def test_encode_rejects_wrong_dim(self):
        import numpy as np
        from cairn.backends.embed import CPUEmbed
        e = CPUEmbed()

        class _Fake:  # stand-in model producing wrong-dim vectors, no real load
            def encode(self, texts, **kw):
                return [np.zeros(768, dtype=np.float32) for _ in texts]
        e._model = _Fake()
        with pytest.raises(Exception):
            e.encode(["hello"])

    def test_index_warns_on_dim_mismatch(self, vault, capsys):
        node = _write(vault)
        # NULL -> a 100-dim blob (allowed by the append-only trigger), i.e. wrong size
        vault.conn.execute("UPDATE nodes SET embedding=? WHERE id=?",
                           (b"\x00" * (100 * 4), node.id))
        vault.conn.commit()
        from cairn.index import EmbeddingIndex
        EmbeddingIndex().ensure(vault.conn)
        assert "dimension" in capsys.readouterr().err.lower()


class TestBackfillDating:
    """Distilled/backfilled claims must inherit the SOURCE session's real date,
    not today — else every import reads as 'today' and pollutes recency + the
    timeline. (Live `cairn note` is unaffected; this is the import-only path.)"""

    def test_claims_dated_to_session_not_today(self, vault):
        from cairn.distill import write_claims
        real_date = "2023-05-01T12:00:00+00:00"
        vault.conn.execute(
            "INSERT INTO sessions (id, started_at, node_count, account) VALUES (?,?,0,?)",
            ("oldsess", real_date, "test"))
        vault.conn.commit()
        ids = write_claims(vault, "oldsess",
                           [{"claim": "we chose sqlite over postgres", "kind": "decision"}])
        assert ids
        assert vault.get(ids[0])["timestamp"].startswith("2023-05-01")


# ── rotation benchmark sanity ────────────────────────────────────────────────

class TestRotation:
    def test_golden_positions_unique_and_aperiodic(self):
        from cairn.schedule import golden_positions
        pos = golden_positions(100)
        assert len(set(round(p, 9) for p in pos)) == 100
        assert all(0.0 <= p < 1.0 for p in pos)

    def test_rotation_beats_static_on_fairness(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import eval_rotation as ev
        static = ev.run("static")
        golden = ev.run("golden")
        assert golden["gini"] < static["gini"] / 10
        assert golden["starved_pct"] == 0.0
        assert golden["max_streak"] < static["max_streak"]


class TestImporter:
    """The onboarding path — 'bring your AI history home'. Exports get huge
    (multi-GB); they must stream in clean, idempotent, and crash-safe. A silent
    break here loses or corrupts a user's whole history."""

    def test_stream_array_handles_adversarial_content(self, tmp_path):
        from cairn.importer import _stream_array
        elems = [
            {"title": "has ] and , and { inside", "n": {"x": [1, 2, 3]}},
            {"s": "escaped \" quote, emoji 🎉", "list": ["a,b", "c]d"]},
            {"deep": [[[{"k": "v"}]]], "z": None},
        ]
        f = tmp_path / "arr.json"
        f.write_text(json.dumps(elems), encoding="utf-8")
        # tiny chunk forces element boundaries mid-token — must still reassemble
        assert list(_stream_array(f, chunk=5)) == elems
        assert list(_stream_array(f, chunk=1 << 20)) == elems

    def test_chatgpt_import_rich_content_and_noise(self, vault, tmp_path):
        from cairn.importer import import_export
        conv = {"title": "t", "create_time": 1700000000, "mapping": {
            "1": {"message": {"author": {"role": "user"}, "create_time": 1700000001,
                  "content": {"content_type": "text", "parts": ["how to sort in python?"]}}},
            "2": {"message": {"author": {"role": "assistant"}, "create_time": 1700000002,
                  "content": {"content_type": "code", "text": "print(sorted([3,1,2]))"}}},
            "3": {"message": {"author": {"role": "assistant"}, "create_time": 1700000003,
                  "content": {"content_type": "multimodal_text", "parts": ["chart",
                  {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-XYZ"}]}}},
            "4": {"message": {"author": {"role": "assistant"}, "create_time": 1700000004,
                  "content": {"content_type": "text",
                  "parts": ["``` This block is not supported on your current device"]}}},
            "5": {"message": {"author": {"role": "system"}, "create_time": 1700000005,
                  "content": {"content_type": "text", "parts": ["you are helpful"]}}},
        }}
        f = tmp_path / "chatgpt.json"
        f.write_text(json.dumps([conv]), encoding="utf-8")
        r = import_export(f, "chatgpt", vault=vault)
        assert (r["conversations"], r["turns"], r["dropped"]) == (1, 3, 1)
        rows = vault.conn.execute(
            "SELECT query, tags FROM nodes WHERE kind='conversation_turn'").fetchall()
        qs = [row["query"] for row in rows]
        assert any("sorted([3,1,2])" in q for q in qs)        # code captured as text
        assert any("[image: file-XYZ]" in q for q in qs)      # image → marker
        assert not any("not supported" in q for q in qs)      # export cruft dropped
        assert not any("you are helpful" in q for q in qs)    # system plumbing skipped
        assert any('"code"' in (row["tags"] or "") for row in rows)
        assert any('"image"' in (row["tags"] or "") for row in rows)

    def test_import_is_idempotent(self, vault, tmp_path):
        from cairn.importer import import_export
        conv = {"title": "t", "create_time": 1700000000, "mapping": {
            "1": {"message": {"author": {"role": "user"}, "create_time": 1700000001,
                  "content": {"content_type": "text", "parts": ["a real question here"]}}},
        }}
        f = tmp_path / "c.json"
        f.write_text(json.dumps([conv]), encoding="utf-8")
        first = import_export(f, "chatgpt", vault=vault)
        again = import_export(f, "chatgpt", vault=vault)
        assert first["conversations"] == 1
        assert again["conversations"] == 0 and again["skipped"] >= 1
        n = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation_turn'").fetchone()[0]
        assert n == 1   # re-run added nothing

    def test_import_redacts_secrets(self, vault, tmp_path):
        """A new user's backfill is the highest-risk leak path — people paste
        keys into chats. Import funnels through Vault.write, so the write-gate
        must scrub secrets out of imported turns. Also guards against a future
        raw-insert bypass that would skip the chokepoint."""
        from cairn.importer import import_export
        secret = "sk-ant-api03-" + "Z" * 20
        conv = {"title": "t", "create_time": 1700000000, "mapping": {
            "1": {"message": {"author": {"role": "user"}, "create_time": 1700000001,
                  "content": {"content_type": "text", "parts": [f"my key is {secret}"]}}},
            "2": {"message": {"author": {"role": "assistant"}, "create_time": 1700000002,
                  "content": {"content_type": "text", "parts": ["I won't store that."]}}},
        }}
        f = tmp_path / "leak.json"
        f.write_text(json.dumps([conv]), encoding="utf-8")
        import_export(f, "chatgpt", vault=vault)
        rows = vault.conn.execute(
            "SELECT query, output_preview, episodic_text FROM nodes").fetchall()
        blob = " ".join(((r["query"] or "") + " " + (r["output_preview"] or "")
                         + " " + (r["episodic_text"] or "")) for r in rows)
        assert secret not in blob, f"secret survived import: {blob!r}"
        assert "[REDACTED:" in blob
