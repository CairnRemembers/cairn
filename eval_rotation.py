"""
eval_rotation.py — the middle-rot benchmark.

Question under test: does golden-angle position rotation (cairn/schedule.py)
distribute model attention across memories better than static placement?

Attention model: the "lost in the middle" U-curve (Liu et al. 2023,
arXiv:2307.03172): models attend strongly to the start and end of context
and poorly to the middle. We model positional attention as

    attention(p) = max(exp(-p / TAU), exp(-(1 - p) / TAU)),  p in [0, 1]

so p=0 (front) and p=1 (back) get attention 1.0 and the dead zone around
p=0.5 gets ~exp(-0.5/TAU). This is a *simulation* with stated assumptions —
it measures placement fairness, not live model recall. A live A/B sits on
top of the attention ledger once enough receipts accumulate.

Strategies compared over S simulated sessions of N memories each:
  static  — importance order, same every session (what naive injection does:
            top memories always bright, the rest rot in the middle forever)
  random  — uniform shuffle per session (seeded; fair in expectation but
            unbounded worst-case streaks)
  golden  — the REAL cairn rotation: position_j(session_i) =
            ((j + i) * PHI_INV_SQ) mod 1, imported from cairn.schedule.
            Low-discrepancy: bounded gaps, no clumping, never periodic.

Metrics (cumulative attention per memory across all sessions):
  gini        — inequality of total attention (0 = perfectly fair)
  min/mean    — worst-served memory vs average (1.0 = nobody starves)
  starved %   — memories receiving < 50% of mean attention
  max streak  — longest consecutive run any memory spent in the dead zone
                (p in [0.30, 0.70]); the "how long can a memory rot" number

Deterministic: fixed seed, pure math, zero model calls, zero tokens.
Run: python -X utf8 eval_rotation.py
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cairn.schedule import PHI_INV_SQ  # the actual production constant

N_MEMORIES = 60
N_SESSIONS = 200
TAU        = 0.15
DEAD_LO, DEAD_HI = 0.30, 0.70
SEED       = 1337


def attention(p: float) -> float:
    return max(math.exp(-p / TAU), math.exp(-(1.0 - p) / TAU))


def positions_static(session: int, n: int) -> list[float]:
    return [j / n for j in range(n)]


def positions_random(session: int, n: int, rng: random.Random) -> list[float]:
    order = list(range(n))
    rng.shuffle(order)
    return [order[j] / n for j in range(n)]


def positions_golden(session: int, n: int) -> list[float]:
    return [((j + session) * PHI_INV_SQ) % 1.0 for j in range(n)]


def gini(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    total = sum(s)
    if total == 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(s, 1):
        cum += i * v
    return (2.0 * cum) / (n * total) - (n + 1.0) / n


def run(strategy: str) -> dict:
    rng = random.Random(SEED)
    cum_att  = [0.0] * N_MEMORIES
    streak   = [0] * N_MEMORIES
    max_strk = [0] * N_MEMORIES

    for s in range(N_SESSIONS):
        if strategy == "static":
            pos = positions_static(s, N_MEMORIES)
        elif strategy == "random":
            pos = positions_random(s, N_MEMORIES, rng)
        else:
            pos = positions_golden(s, N_MEMORIES)

        for j in range(N_MEMORIES):
            cum_att[j] += attention(pos[j])
            if DEAD_LO <= pos[j] <= DEAD_HI:
                streak[j] += 1
                max_strk[j] = max(max_strk[j], streak[j])
            else:
                streak[j] = 0

    mean = sum(cum_att) / len(cum_att)
    return {
        "gini":       gini(cum_att),
        "min_mean":   (min(cum_att) / mean) if mean else 0.0,
        "starved_pct": 100.0 * sum(1 for a in cum_att if a < 0.5 * mean) / N_MEMORIES,
        "max_streak": max(max_strk),
    }


def main() -> None:
    print(f"middle-rot benchmark — {N_MEMORIES} memories x {N_SESSIONS} sessions, "
          f"U-curve attention (tau={TAU}), dead zone [{DEAD_LO}, {DEAD_HI}]")
    print(f"golden constant: PHI_INV_SQ = {PHI_INV_SQ} (cairn.schedule)\n")

    header = f"{'strategy':<10} {'gini':>7} {'min/mean':>9} {'starved %':>10} {'max dead streak':>16}"
    print(header)
    print("-" * len(header))
    results = {}
    for strategy in ("static", "random", "golden"):
        r = run(strategy)
        results[strategy] = r
        print(f"{strategy:<10} {r['gini']:>7.4f} {r['min_mean']:>9.3f} "
              f"{r['starved_pct']:>9.1f}% {r['max_streak']:>13} sess")

    g, st = results["golden"], results["static"]
    print()
    if g["gini"] < st["gini"] and g["starved_pct"] < st["starved_pct"]:
        print("PASS: golden-angle rotation beats static placement on every metric.")
        print(f"  attention inequality (gini): {st['gini']:.4f} -> {g['gini']:.4f}")
        print(f"  starved memories:            {st['starved_pct']:.0f}% -> {g['starved_pct']:.0f}%")
        print(f"  worst dead-zone streak:      {st['max_streak']} -> {g['max_streak']} sessions")
        sys.exit(0)
    print("FAIL: golden rotation did not beat static placement — investigate.")
    sys.exit(1)


if __name__ == "__main__":
    main()
