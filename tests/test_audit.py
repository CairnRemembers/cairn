"""
tests/test_audit.py — the audit organ (cairn/audit.py).

Throwaway tmp_path vaults; no network, no embedder. Matches the style of
tests/test_cairn.py. The organ's contract: read-only sensing, ONE deduped
warning node per changed report, and sensor failures never raise.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.vault import Vault, MicroNode
from cairn.audit import audit, write_report


@pytest.fixture
def vault(tmp_path, monkeypatch):
    # keep check 6 (projects.json ghosts) off the machine's real config
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return Vault(db_path=tmp_path / "test.db")


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate(v, node_id: str, days: float) -> None:
    v.conn.execute("UPDATE nodes SET timestamp=? WHERE id=?",
                   (_iso_days_ago(days), node_id))
    v.conn.commit()


def test_clean_vault_is_clean(vault):
    vault.write(MicroNode(session="s1", kind="decision",
                          query="fresh decision", model="test"))
    assert audit(vault) == []


def test_stale_open_item_found(vault):
    n = vault.write(MicroNode(session="s1", kind="open_item",
                              query="file the provisional patent", model="test"))
    _backdate(vault, n.id, 30)
    findings = audit(vault)
    assert any("open item" in f and "21 days" in f for f in findings)


def test_import_sessions_exempt_from_staleness(vault):
    n = vault.write(MicroNode(session="import-chatgpt-2024", kind="open_item",
                              query="ancient imported todo", model="test"))
    _backdate(vault, n.id, 400)
    assert not any("open item" in f for f in audit(vault))


def test_impossible_dates_found(vault):
    n = vault.write(MicroNode(session="s1", kind="insight",
                              query="from the future", model="test"))
    vault.conn.execute("UPDATE nodes SET timestamp=? WHERE id=?",
                       ("2031-01-01T00:00:00+00:00", n.id))
    vault.conn.commit()
    assert any("impossible timestamps" in f for f in audit(vault))


def test_exposure_hoarding_found(vault):
    n = vault.write(MicroNode(session="s1", kind="decision",
                              query="the one note everyone hears", model="test"))
    now = datetime.now(timezone.utc).isoformat()
    vault.conn.executemany(
        "INSERT INTO attention_ledger (node_id, session, channel, position,"
        " trigger, shown_at) VALUES (?, 's1', 'hook', 0, 'heartbeat', ?)",
        [(n.id, now)] * 61)
    vault.conn.commit()
    assert any("hoarding" in f for f in audit(vault))


def test_voided_but_scheduled_found(vault):
    n = vault.write(MicroNode(session="s1", kind="decision",
                              query="to be voided", model="test"))
    vault.conn.execute(
        "UPDATE nodes SET status='void', memory_tier=1 WHERE id=?", (n.id,))
    vault.conn.commit()
    assert any("voided" in f for f in audit(vault))


def test_report_written_once_and_deduped(vault):
    findings = ["something is structurally wrong"]
    nid1 = write_report(vault, findings)
    assert nid1, "first report must write a node"
    row = vault.conn.execute(
        "SELECT kind, tags FROM nodes WHERE id=?", (nid1,)).fetchone()
    assert row["kind"] == "warning"
    assert "cairn-audit" in row["tags"]
    # identical findings the next night: say it once, write nothing
    assert write_report(vault, findings) is None
    # changed findings: a new node
    nid2 = write_report(vault, findings + ["a second problem"])
    assert nid2 and nid2 != nid1


def test_no_findings_no_node(vault):
    assert write_report(vault, []) is None
    n = vault.conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE tags LIKE '%cairn-audit%'"
    ).fetchone()[0]
    assert n == 0
