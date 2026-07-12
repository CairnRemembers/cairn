"""
tests/test_book_importer.py — coverage for cairn/book.py and cairn/importer.py.

All tests use throwaway tmp_path vaults; no network, no embedder, no live vault.
Matches the style of tests/test_cairn.py.
"""
from __future__ import annotations

import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault, MicroNode

DIM = 384


def _vec(seed: float) -> bytes:
    """Deterministic unit-ish embedding (same helper as test_cairn.py)."""
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


# ── synthetic vault fixture used by book tests ────────────────────────────────

@pytest.fixture()
def book_vault(vault):
    """~10 nodes of mixed kinds for book.py tests."""
    _write(vault, kind="open_item",  query="still need to: finish API auth",
           session="s1", tags=["cairn"])
    _write(vault, kind="open_item",  query="still need to: write deploy docs",
           session="s1", tags=["cairn", "doc"])
    _write(vault, kind="idea",       query="use golden-ratio rotation for fairness",
           session="s1", tags=["cairn"])
    _write(vault, kind="idea",       query="add fuzzy dedup on import",
           session="s2", tags=["cairn"])
    _write(vault, kind="warning",    query="embed_pending runs long if model not cached",
           session="s1", tags=["cairn"])
    _write(vault, kind="decision",   query="SQLite stays — LanceDB at 20k nodes",
           session="s1", tags=["cairn"])
    # artifact with structured tags
    _write(vault, kind="artifact",
           query="DOC: Architecture overview",
           session="s2",
           tags=["doc", "file:C:/x.md", "made:2026-01-01"])
    # term node — query must be 'term: definition'
    _write(vault, kind="term",
           query="moat: the defensible edge",
           session="s2", tags=[])
    # two nodes sharing a plain tag to satisfy count>=2 for index_data tags
    _write(vault, kind="decision",   query="keep vault append-only always",
           session="s3", tags=["architecture", "cairn"])
    _write(vault, kind="insight",    query="phyllotaxis beats round-robin",
           session="s3", tags=["architecture"])
    return vault


# ── hub_data ──────────────────────────────────────────────────────────────────

class TestHubData:
    def test_returns_documented_keys(self, book_vault):
        """hub_data returns all required top-level keys."""
        from cairn.book import hub_data
        d = hub_data(book_vault)
        for key in ("open_items", "fading", "ideas", "activity", "chat_week", "generated"):
            assert key in d, f"missing key: {key}"

    def test_open_items_contains_written_nodes(self, book_vault):
        """hub_data.open_items contains the two open_item nodes we wrote."""
        from cairn.book import hub_data
        d = hub_data(book_vault)
        assert len(d["open_items"]) == 2
        queries = " ".join(item["gist"] for item in d["open_items"])
        assert "API auth" in queries or "deploy docs" in queries

    def test_ideas_contains_written_nodes(self, book_vault):
        """hub_data.ideas contains the two idea nodes we wrote."""
        from cairn.book import hub_data
        d = hub_data(book_vault)
        assert len(d["ideas"]) == 2

    def test_activity_excludes_import_sessions(self, vault):
        """hub_data.activity excludes sessions whose id starts with 'import-'."""
        from cairn.book import hub_data
        _write(vault, kind="decision", query="real work", session="real-session")
        _write(vault, kind="decision", query="imported turn",
               session="import-claude-2024-01-01-test")
        d = hub_data(vault)
        sessions_in_activity = {row["session"] for row in d["activity"]}
        assert "real-session" in sessions_in_activity
        assert not any(s.startswith("import-") for s in sessions_in_activity)


# ── book_data ─────────────────────────────────────────────────────────────────

class TestBookData:
    def test_returns_documented_keys(self, book_vault):
        """book_data returns this_week, projects, volumes, generated."""
        from cairn.book import book_data
        d = book_data(book_vault)
        for key in ("this_week", "projects", "volumes", "generated"):
            assert key in d, f"missing key: {key}"

    def test_this_week_contains_meaning_nodes(self, book_vault):
        """book_data.this_week includes recently written meaning-kind nodes."""
        from cairn.book import book_data
        d = book_data(book_vault)
        # we wrote decision, warning, insight, idea, open_item — all meaning kinds
        assert len(d["this_week"]) >= 5

    def test_projects_list_has_required_keys(self, book_vault):
        """Each project entry has tag, name, desc, total keys."""
        from cairn.book import book_data
        d = book_data(book_vault)
        assert d["projects"]
        for p in d["projects"]:
            for key in ("tag", "name", "desc", "total"):
                assert key in p, f"project missing key: {key}"

    def test_volumes_empty_without_import_sessions(self, book_vault):
        """book_data.volumes is an empty list when no import-* sessions exist."""
        from cairn.book import book_data
        d = book_data(book_vault)
        assert d["volumes"] == []


# ── index_data ────────────────────────────────────────────────────────────────

class TestIndexData:
    def test_tags_excludes_structured_prefixes(self, book_vault):
        """index_data.tags strips file:, made:, member: etc. structured prefixes."""
        from cairn.book import index_data
        d = index_data(book_vault)
        for entry in d["tags"]:
            tag = entry["tag"]
            for prefix in ("file:", "mtime:", "made:", "member:", "lesson:",
                           "from:", "account:", "due:", "media:", "room:", "ext:"):
                assert not tag.startswith(prefix), (
                    f"structured prefix leaked into tags: {tag!r}")

    def test_tags_includes_plain_tags_with_count_ge_2(self, book_vault):
        """index_data.tags includes 'architecture' (appears on 2 nodes)."""
        from cairn.book import index_data
        d = index_data(book_vault)
        tag_names = {entry["tag"] for entry in d["tags"]}
        assert "architecture" in tag_names

    def test_docs_contains_artifact_with_parsed_fields(self, book_vault):
        """index_data.docs has our artifact with title, path, made parsed."""
        from cairn.book import index_data
        d = index_data(book_vault)
        assert d["docs"]
        doc = d["docs"][0]
        assert "Architecture overview" in doc["title"]
        assert doc["path"] == "C:/x.md"
        assert doc["made"] == "2026-01-01"

    def test_terms_parses_term_and_definition(self, book_vault):
        """index_data.terms splits 'moat: the defensible edge' correctly."""
        from cairn.book import index_data
        d = index_data(book_vault)
        assert d["terms"]
        term = d["terms"][0]
        assert term["term"] == "moat"
        assert "defensible" in term["definition"]


# ── page_one ──────────────────────────────────────────────────────────────────

class TestPageOne:
    def test_contains_required_landmarks(self, book_vault):
        """page_one() contains PAGE ONE, LAWS, and NAVIGATE strings."""
        from cairn.book import page_one
        text = page_one(book_vault)
        assert "PAGE ONE" in text
        assert "LAWS" in text
        assert "NAVIGATE" in text

    def test_within_34_lines(self, book_vault):
        """page_one() respects the <=34 line sizing law."""
        from cairn.book import page_one
        text = page_one(book_vault)
        assert isinstance(text, str)
        lines = text.splitlines()
        assert len(lines) <= 34, f"page_one exceeded 34 lines: {len(lines)}"


# ── write_book ────────────────────────────────────────────────────────────────

class TestWriteBook:
    def test_creates_both_files(self, book_vault, tmp_path):
        """write_book creates BOOK.md and PAGE_ONE.md in out_dir."""
        from cairn.book import write_book
        result = write_book(book_vault, out_dir=tmp_path)
        assert (tmp_path / "BOOK.md").exists()
        assert (tmp_path / "PAGE_ONE.md").exists()

    def test_book_md_within_150_lines(self, book_vault, tmp_path):
        """write_book produces BOOK.md that is <=150 lines (IFScale law)."""
        from cairn.book import write_book
        result = write_book(book_vault, out_dir=tmp_path)
        lines = (tmp_path / "BOOK.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 150, f"BOOK.md exceeded 150 lines: {len(lines)}"

    def test_returns_dict_with_paths(self, book_vault, tmp_path):
        """write_book returns a dict containing 'book' and 'page_one' keys."""
        from cairn.book import write_book
        result = write_book(book_vault, out_dir=tmp_path)
        assert "book" in result
        assert "page_one" in result
        assert result["book"].endswith("BOOK.md")
        assert result["page_one"].endswith("PAGE_ONE.md")


# ── helpers: build a minimal Claude export JSON ───────────────────────────────

def _make_claude_export(tmp_path: Path) -> Path:
    """Two conversations x 2-3 turns matching _extract_claude's expected shape."""
    data = [
        {
            "name": "Planning the vault schema",
            "created_at": "2025-03-01T10:00:00Z",
            "chat_messages": [
                {"sender": "human",     "text": "How should I design the vault schema?",
                 "created_at": "2025-03-01T10:00:00Z"},
                {"sender": "assistant", "text": "Use append-only with a status column.",
                 "created_at": "2025-03-01T10:01:00Z"},
                {"sender": "human",     "text": "What about embeddings?",
                 "created_at": "2025-03-01T10:02:00Z"},
            ],
        },
        {
            "name": "Importer design",
            "created_at": "2025-04-15T09:00:00Z",
            "chat_messages": [
                {"sender": "human",     "text": "How do I make import idempotent?",
                 "created_at": "2025-04-15T09:00:00Z"},
                {"sender": "assistant", "text": "Derive a deterministic session id and skip if exists.",
                 "created_at": "2025-04-15T09:01:00Z"},
            ],
        },
    ]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── import_export ─────────────────────────────────────────────────────────────

class TestImporter:
    def test_imports_both_conversations(self, vault, tmp_path):
        """import_export ingests both conversations; report counts are correct."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        report = import_export(path, "claude", vault=vault)
        assert report["conversations"] == 2
        assert report["skipped"] == 0
        assert report["turns"] == 5   # 3 + 2

    def test_turn_nodes_exist_with_speaker_and_model(self, vault, tmp_path):
        """Imported turn nodes have speaker and model attributes set correctly."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        import_export(path, "claude", vault=vault)
        turns = vault.conn.execute(
            "SELECT speaker, model FROM nodes WHERE kind='conversation_turn'"
        ).fetchall()
        assert len(turns) == 5
        speakers = {r["speaker"] for r in turns}
        assert "user" in speakers
        assert "agent" in speakers
        models = {r["model"] for r in turns}
        assert "human" in models
        assert "claude-imported" in models

    def test_original_timestamps_preserved(self, vault, tmp_path):
        """Imported nodes retain the original timestamps from the export."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        import_export(path, "claude", vault=vault)
        rows = vault.conn.execute(
            "SELECT timestamp FROM nodes WHERE kind='conversation_turn' ORDER BY timestamp"
        ).fetchall()
        assert rows[0]["timestamp"].startswith("2025-03-01")
        assert rows[-1]["timestamp"].startswith("2025-04-15")

    def test_idempotent_second_run_skips_all(self, vault, tmp_path):
        """Running import_export twice imports 0 new conversations on the second run."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        import_export(path, "claude", vault=vault)
        report2 = import_export(path, "claude", vault=vault)
        assert report2["conversations"] == 0
        assert report2["skipped"] == 2

    def test_account_tags_nodes(self, vault, tmp_path):
        """import_export with account= tags all turn nodes with 'account:<name>'."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        import_export(path, "claude", vault=vault, account="testacct")
        rows = vault.conn.execute(
            "SELECT tags FROM nodes WHERE kind='conversation_turn'"
        ).fetchall()
        assert rows
        for r in rows:
            tags = json.loads(r["tags"])
            assert "account:testacct" in tags, (
                f"account tag missing from: {tags}")

    def test_account_sets_sessions_table(self, vault, tmp_path):
        """import_export with account= writes account column on sessions rows."""
        from cairn.importer import import_export
        path = _make_claude_export(tmp_path)
        import_export(path, "claude", vault=vault, account="testacct")
        rows = vault.conn.execute(
            "SELECT account FROM sessions WHERE id LIKE 'import-%'"
        ).fetchall()
        assert rows
        for r in rows:
            assert r["account"] == "testacct", (
                f"sessions.account not set correctly: {r['account']!r}")


# ── importer v2: full-text preservation + continued-conversation tail import ──

from cairn.importer import TRUNC_QUERY, TRUNC_PREVIEW


def _claude_export(tmp_path: Path, convs: list, name: str = "conversations.json") -> Path:
    """Write a Claude-shaped export. `convs` is a list of
    (title, created_at, [(sender, text, created_at), ...])."""
    data = [
        {"name": title, "created_at": created,
         "chat_messages": [{"sender": s, "text": t, "created_at": ts}
                           for (s, t, ts) in msgs]}
        for (title, created, msgs) in convs
    ]
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _chatgpt_msg_node(role: str, ts: int, text: str) -> dict:
    """One ChatGPT mapping node (plain-text message)."""
    return {"message": {"author": {"role": role}, "create_time": ts,
                        "content": {"content_type": "text", "parts": [text]}}}


def _chatgpt_zip(tmp_path: Path, title: str, created: int, turns: list,
                 name: str = "export.zip", sharded: bool = False) -> Path:
    """Build a minimal modern ChatGPT export .zip. `turns` is a list of
    (role, ts, text). Single conversation. sharded → conversations-000.json."""
    import zipfile
    mapping = {str(i): _chatgpt_msg_node(role, ts, text)
               for i, (role, ts, text) in enumerate(turns)}
    conv = {"title": title, "create_time": created,
            "default_model_slug": "gpt-4o", "mapping": mapping}
    member = "conversations-000.json" if sharded else "conversations.json"
    zp = tmp_path / name
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(member, json.dumps([conv]))
    return zp


class TestImporterV2FullText:
    """FIX 1: the complete turn text is preserved in episodic_text when it
    overflows the output_preview display cap; the display fields stay capped;
    short turns carry no redundant episodic_full."""

    def test_long_turn_full_text_in_episodic_beyond_caps(self, vault, tmp_path):
        big = "PARAGRAPH " + ("x" * (TRUNC_PREVIEW + 4000)) + " TAIL_SENTINEL"
        path = _claude_export(tmp_path, [
            ("Long chat", "2025-05-01T10:00:00Z",
             [("human", big, "2025-05-01T10:00:00Z"),
              ("assistant", "ok", "2025-05-01T10:01:00Z")]),
        ])
        from cairn.importer import import_export
        import_export(path, "claude", vault=vault)
        row = vault.conn.execute(
            "SELECT query, output_preview, episodic_text FROM nodes "
            "WHERE kind='conversation_turn' AND speaker='user'").fetchone()
        # display fields stay capped exactly as before
        assert len(row["query"]) == TRUNC_QUERY
        assert len(row["output_preview"]) == TRUNC_PREVIEW
        # full text — including the tail past the cap — lands in episodic_text
        assert "TAIL_SENTINEL" in row["episodic_text"]
        assert "TAIL_SENTINEL" not in row["output_preview"]
        assert len(row["episodic_text"]) > TRUNC_PREVIEW
        assert row["episodic_text"].startswith("user said:")

    def test_short_turn_no_redundant_episodic_full(self, vault, tmp_path):
        """A turn under the cap keeps the normal derived (capped) episodic_text —
        it does NOT carry a second full copy (matches capture.py's convention)."""
        path = _claude_export(tmp_path, [
            ("Short", "2025-05-02T10:00:00Z",
             [("human", "just a short line", "2025-05-02T10:00:00Z")]),
        ])
        from cairn.importer import import_export
        import_export(path, "claude", vault=vault)
        row = vault.conn.execute(
            "SELECT output_preview, episodic_text FROM nodes "
            "WHERE kind='conversation_turn'").fetchone()
        # derived form, same as live capture: "<speaker> said: <capped body>"
        assert row["episodic_text"] == "user said: just a short line"

    def test_full_text_still_redacted(self, vault, tmp_path):
        """A secret inside an over-cap turn must not survive into episodic_text —
        episodic_full goes through the write-gate scrub like every other field."""
        secret = "sk-ant-api03-" + "Z" * 20
        big = secret + " " + ("y" * (TRUNC_PREVIEW + 100))
        path = _claude_export(tmp_path, [
            ("Leak", "2025-05-03T10:00:00Z",
             [("human", big, "2025-05-03T10:00:00Z")]),
        ])
        from cairn.importer import import_export
        import_export(path, "claude", vault=vault)
        row = vault.conn.execute(
            "SELECT episodic_text FROM nodes WHERE kind='conversation_turn'").fetchone()
        assert secret not in (row["episodic_text"] or "")
        assert "[REDACTED:" in (row["episodic_text"] or "")

    def test_chatgpt_zip_full_text_preserved(self, vault, tmp_path):
        big = "ZIP " + ("z" * (TRUNC_PREVIEW + 500)) + " ZIP_TAIL"
        zp = _chatgpt_zip(tmp_path, "Zip long", 1700000000,
                          [("user", 1700000001, big),
                           ("assistant", 1700000002, "reply")])
        from cairn.importer import import_export
        import_export(zp, "chatgpt", vault=vault)
        row = vault.conn.execute(
            "SELECT output_preview, episodic_text FROM nodes "
            "WHERE kind='conversation_turn' AND speaker='user'").fetchone()
        assert len(row["output_preview"]) == TRUNC_PREVIEW
        assert "ZIP_TAIL" in row["episodic_text"]
        assert "ZIP_TAIL" not in row["output_preview"]


class TestImporterV2TailImport:
    """FIX 2: a conversation continued since the last import imports ONLY its new
    tail, chained to the prior last node; re-run is a no-op; a shrunk export is
    skipped without touching anything."""

    def _turns(self, vault, session):
        return vault.conn.execute(
            "SELECT id, parent, speaker, query FROM nodes "
            "WHERE session=? AND kind='conversation_turn' "
            "ORDER BY timestamp ASC, rowid ASC", (session,)).fetchall()

    def test_continued_conversation_imports_only_tail_chained(self, vault, tmp_path):
        title, created = "Ongoing thread", "2025-06-01T10:00:00Z"
        # first export: 2 turns
        p1 = _claude_export(tmp_path, [
            (title, created,
             [("human", "turn one", "2025-06-01T10:00:00Z"),
              ("assistant", "turn two", "2025-06-01T10:01:00Z")]),
        ], name="v1.json")
        from cairn.importer import import_export
        r1 = import_export(p1, "claude", vault=vault)
        assert r1["conversations"] == 1 and r1["turns"] == 2
        session = r1["sessions"][0]
        before = self._turns(vault, session)
        last_before = before[-1]["id"]

        # second export: SAME conversation, now 4 turns (2 new tail turns)
        p2 = _claude_export(tmp_path, [
            (title, created,
             [("human", "turn one", "2025-06-01T10:00:00Z"),
              ("assistant", "turn two", "2025-06-01T10:01:00Z"),
              ("human", "turn three NEW", "2025-06-01T10:02:00Z"),
              ("assistant", "turn four NEW", "2025-06-01T10:03:00Z")]),
        ], name="v2.json")
        r2 = import_export(p2, "claude", vault=vault)

        # only the tail imported; reported as resumed, not a fresh conversation
        assert r2["conversations"] == 0
        assert r2["resumed"] == 1
        assert r2["turns"] == 2
        assert r2["skipped"] == 0 and r2["shrunk"] == 0

        after = self._turns(vault, session)
        assert len(after) == 4
        # the two originals are untouched (append-only)
        assert [n["id"] for n in after[:2]] == [n["id"] for n in before]
        # the first new turn chains to the session's prior last node
        assert after[2]["parent"] == last_before
        # and the tail chains internally
        assert after[3]["parent"] == after[2]["id"]
        assert "turn three NEW" in after[2]["query"]

    def test_rerun_same_export_is_noop(self, vault, tmp_path):
        p = _claude_export(tmp_path, [
            ("Thread", "2025-06-02T10:00:00Z",
             [("human", "a", "2025-06-02T10:00:00Z"),
              ("assistant", "b", "2025-06-02T10:01:00Z"),
              ("human", "c", "2025-06-02T10:02:00Z")]),
        ])
        from cairn.importer import import_export
        import_export(p, "claude", vault=vault)
        n_after_first = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation_turn'").fetchone()[0]
        r2 = import_export(p, "claude", vault=vault)
        assert r2["conversations"] == 0
        assert r2["resumed"] == 0
        assert r2["skipped"] == 1
        assert r2["turns"] == 0
        n_after_second = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation_turn'").fetchone()[0]
        assert n_after_second == n_after_first

    def test_shrunk_export_skips_without_touching(self, vault, tmp_path):
        title, created = "Thread", "2025-06-03T10:00:00Z"
        p1 = _claude_export(tmp_path, [
            (title, created,
             [("human", "one", "2025-06-03T10:00:00Z"),
              ("assistant", "two", "2025-06-03T10:01:00Z"),
              ("human", "three", "2025-06-03T10:02:00Z")]),
        ], name="full.json")
        from cairn.importer import import_export
        import_export(p1, "claude", vault=vault)
        ids_before = [r["id"] for r in vault.conn.execute(
            "SELECT id FROM nodes ORDER BY rowid").fetchall()]

        # weird re-export with FEWER turns than stored
        p2 = _claude_export(tmp_path, [
            (title, created,
             [("human", "one", "2025-06-03T10:00:00Z")]),
        ], name="short.json")
        r2 = import_export(p2, "claude", vault=vault)
        assert r2["shrunk"] == 1
        assert r2["turns"] == 0
        assert r2["conversations"] == 0 and r2["resumed"] == 0
        # nothing added, nothing changed
        ids_after = [r["id"] for r in vault.conn.execute(
            "SELECT id FROM nodes ORDER BY rowid").fetchall()]
        assert ids_after == ids_before

    def test_chatgpt_zip_tail_import_and_rerun(self, vault, tmp_path):
        # v1 zip: 2 turns
        z1 = _chatgpt_zip(tmp_path, "Zip thread", 1700000000,
                          [("user", 1700000001, "q one"),
                           ("assistant", 1700000002, "a one")],
                          name="z1.zip", sharded=True)
        from cairn.importer import import_export
        r1 = import_export(z1, "chatgpt", vault=vault)
        assert r1["conversations"] == 1 and r1["turns"] == 2
        session = r1["sessions"][0]
        last_before = vault.conn.execute(
            "SELECT id FROM nodes WHERE session=? AND kind='conversation_turn' "
            "ORDER BY timestamp ASC, rowid ASC", (session,)).fetchall()[-1]["id"]

        # v2 zip: same conversation, 3 turns (1 new)
        z2 = _chatgpt_zip(tmp_path, "Zip thread", 1700000000,
                          [("user", 1700000001, "q one"),
                           ("assistant", 1700000002, "a one"),
                           ("user", 1700000003, "q two NEW")],
                          name="z2.zip", sharded=True)
        r2 = import_export(z2, "chatgpt", vault=vault)
        assert r2["resumed"] == 1 and r2["turns"] == 1 and r2["conversations"] == 0
        rows = vault.conn.execute(
            "SELECT id, parent, query FROM nodes WHERE session=? "
            "AND kind='conversation_turn' ORDER BY timestamp ASC, rowid ASC",
            (session,)).fetchall()
        assert len(rows) == 3
        assert rows[2]["parent"] == last_before
        assert "q two NEW" in rows[2]["query"]

        # re-run v2 → no-op
        r3 = import_export(z2, "chatgpt", vault=vault)
        assert r3["resumed"] == 0 and r3["turns"] == 0 and r3["skipped"] == 1
        n = vault.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation_turn'").fetchone()[0]
        assert n == 3
