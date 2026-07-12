"""
cairn/index.py — the self-built vector index. The 20k wall, demolished.

The old path decoded and dot-producted EVERY embedding in interpreted Python
on EVERY query: ~4.4M interpreter ops at 5.7k nodes (~1-2s), minutes at 100k.
That was the "migrate to LanceDB at 20k nodes" wall.

This index keeps the charter (no external deps — numpy is the embedder's
own dependency, already the fast path in edges.py) and removes the wall:

  - One contiguous, L2-normalized float32 matrix held in process.
  - Cosine for all N nodes = one matmul. 100k x 384 ≈ 10ms.
  - Recency + importance scored as vectorized arrays in the same pass.
  - Keyword (sparse) scoring runs only on the top-M candidates by partial
    score — M=1000, far past where the 0.25-max keyword term could change
    top-k membership on any realistic vault.

Staleness: the index signature is (embedded-active count, MAX(rowid)).
Writes, embeds, and voids all move it; the index rebuilds lazily on the
next query. Build cost ~50ms at 5.7k nodes, ~1s at 100k — paid once per
process, then every query is milliseconds.

Ceiling after this: brute-force exact to ~500k nodes. Past that, the same
class grows an IVF coarse quantizer (self-built k-means, probe top cells) —
the interface stays identical, nothing upstream changes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

DIM = 384


def _parse_epoch(ts: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(
            (ts or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class EmbeddingIndex:
    """Lazy, exact, in-process. ensure() returns False when numpy is
    unavailable — callers fall back to the pure-Python scan."""

    def __init__(self) -> None:
        self.ids:      list[str] = []
        self.sessions: list[str] = []
        self.mat  = None      # (N, DIM) float32, L2-normalized
        self.ts   = None      # (N,) float64 epoch seconds
        self.imp  = None      # (N,) float64 importance 0..1
        self._sig = None

    def _signature(self, conn):
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM nodes "
            "WHERE embedding IS NOT NULL AND status != 'void'").fetchone()
        return (r[0], r[1])

    def ensure(self, conn) -> bool:
        try:
            import numpy as np
        except ImportError:
            return False
        sig = self._signature(conn)
        if sig == self._sig and self.mat is not None:
            return True
        rows = conn.execute(
            "SELECT id, embedding, timestamp, importance, session FROM nodes "
            "WHERE embedding IS NOT NULL AND status != 'void'").fetchall()
        ids, vecs, ts, imp, sess = [], [], [], [], []
        skipped_dim = 0
        for r in rows:
            blob = r["embedding"]
            if blob is None:
                continue
            if len(blob) != DIM * 4:
                skipped_dim += 1          # wrong-dimension embedding (dim-contract)
                continue
            ids.append(r["id"])
            vecs.append(np.frombuffer(blob, dtype=np.float32))
            ts.append(_parse_epoch(r["timestamp"]))
            imp.append((r["importance"] or 5) / 10.0)
            sess.append(r["session"] or "")
        # dim-contract: a model change leaves wrong-size blobs that get skipped —
        # which silently empties search. Make that LOUD instead of invisible.
        if skipped_dim:
            import sys as _sys
            print(f"cairn: WARNING — {skipped_dim} embedding(s) have the wrong "
                  f"dimension (expected {DIM}) and are EXCLUDED from search. A "
                  f"model change requires re-embedding the whole vault.",
                  file=_sys.stderr)
        self._sig = sig
        if not ids:
            self.ids, self.sessions, self.mat = [], [], None
            return True
        mat = np.vstack(vecs).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        self.mat      = mat / norms
        self.ids      = ids
        self.sessions = sess
        self.ts       = np.asarray(ts,  dtype=np.float64)
        self.imp      = np.asarray(imp, dtype=np.float64)
        return True

    def partial_scores(self, q_blob: bytes, weights: tuple,
                       recency_lambda: float, now_ts: datetime,
                       session_id: Optional[str] = None):
        """
        Vectorized dense + recency + importance for every indexed node.
        Returns (partial ndarray, valid-mask ndarray) or (None, None) when
        the index is empty. Keyword (sparse) stays per-candidate upstream.
        """
        import numpy as np
        if self.mat is None or not self.ids:
            return None, None
        w_rel, w_rec, _w_kw, w_imp = weights

        q = np.frombuffer(q_blob, dtype=np.float32)
        if q.shape[0] != DIM:
            return None, None
        qn = float(np.linalg.norm(q))
        q = q / (qn if qn else 1.0)

        cos = self.mat @ q                                     # (N,)
        days = np.maximum(0.0, (now_ts.timestamp() - self.ts) / 86400.0)
        rec  = np.exp(-recency_lambda * days)
        partial = w_rel * cos + w_rec * rec + w_imp * self.imp

        if session_id:
            mask = np.fromiter((s == session_id for s in self.sessions),
                               dtype=bool, count=len(self.sessions))
        else:
            mask = np.ones(len(self.ids), dtype=bool)
        return partial, mask
