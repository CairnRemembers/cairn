"""
eval_scale.py — proof the 20k-node wall is gone.

Builds a throwaway vault with N synthetic embedded nodes (direct INSERTs,
random unit vectors — worst case for any index) and times query_episodic on
the vectorized path, then times the pure-Python scan on a slice and
extrapolates what the old path would have cost at N.

Run: python -X utf8 eval_scale.py [N]      (default 50000)
"""
from __future__ import annotations

import random
import struct
import sys
import time
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cairn.vault import Vault
from cairn.index import EmbeddingIndex

DIM = 384
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
SLOW_SLICE = 2_000


class FakeEmbedder:
    dim = DIM
    def encode_one(self, text):
        rng = random.Random(hash(text))
        return struct.pack(f"{DIM}f", *(rng.gauss(0, 1) for _ in range(DIM)))


def build_vault(path: Path, n: int) -> Vault:
    v = Vault(db_path=path)
    rng = random.Random(42)
    base = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        vec = struct.pack(f"{DIM}f", *(rng.gauss(0, 1) for _ in range(DIM)))
        ts = (base - timedelta(days=rng.uniform(0, 365))).isoformat()
        rows.append((f"node{i:07d}", f"sess-{i % 200}", "conversation_turn",
                     f"synthetic fact number {i} about subject {i % 500}",
                     "active", ts, "[]", vec, 5))
    with v.conn:
        v.conn.executemany(
            "INSERT INTO nodes (id, session, kind, query, status, timestamp, "
            "tags, embedding, importance) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    v._embedder = FakeEmbedder()
    return v


def main() -> None:
    print(f"scale benchmark — {N:,} embedded nodes, {DIM}-dim, random vectors\n")
    with tempfile.TemporaryDirectory() as td:
        t0 = time.perf_counter()
        v = build_vault(Path(td) / "scale.db", N)
        print(f"vault built:        {time.perf_counter()-t0:6.1f}s ({N:,} inserts)")

        t0 = time.perf_counter()
        v.query_episodic("subject 137 facts", k=10)
        cold = time.perf_counter() - t0
        print(f"first query (cold): {cold*1000:8.1f}ms  (includes index build)")

        times = []
        for q in ("subject 42", "synthetic fact 999", "number about subject",
                  "fact 12345", "subject 250 details"):
            t0 = time.perf_counter()
            r = v.query_episodic(q, k=10)
            times.append(time.perf_counter() - t0)
            assert len(r) == 10
        warm = sum(times) / len(times)
        print(f"warm query (avg 5): {warm*1000:8.1f}ms  <- the new normal")

        # old path on a slice, extrapolated
        from cairn.index import EmbeddingIndex as _EI
        real_ensure = _EI.ensure
        _EI.ensure = lambda self, conn: False          # force pure-Python
        v._index = None
        v.conn.execute(f"DELETE FROM nodes WHERE rowid > {SLOW_SLICE}")
        v.conn.commit()
        t0 = time.perf_counter()
        v.query_episodic("subject 42", k=10)
        slow_slice = time.perf_counter() - t0
        _EI.ensure = real_ensure
        est = slow_slice * (N / SLOW_SLICE)
        print(f"old pure-Python:    {slow_slice*1000:8.1f}ms at {SLOW_SLICE:,} nodes "
              f"-> ~{est:6.1f}s extrapolated to {N:,}")
        print(f"\nspeedup at {N:,} nodes: ~{est/warm:,.0f}x  "
              f"(exact same ranking — see tests/test_cairn.py::TestIndex)")
        v.conn.close()    # Windows: temp dir can't delete an open DB


if __name__ == "__main__":
    main()
