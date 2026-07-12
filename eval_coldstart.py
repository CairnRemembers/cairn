"""
eval_coldstart.py — the cold-start quiz benchmark.

THE value claim: a fresh session with Cairn reaches correct context faster
than one without. This script makes that claim measurable and repeatable.

Design (Generative Agents eval pattern, adapted):
  - A bank of questions whose ground-truth answers live in the vault.
  - WITH Cairn:    query_episodic(question) → does top-k contain the answer?
  - WITHOUT Cairn: a fresh model has no vault — its only options are
                   re-deriving from the repo (slow, sometimes impossible:
                   decisions and WHY-knowledge aren't in the code) or
                   asking the user again. We score this arm by whether the
                   answer is derivable from code at all.

Two metrics:
  retrieval@1 / retrieval@3 — does Cairn surface the right node?
  irreplaceable%            — fraction of questions whose answers exist
                              NOWHERE but the vault (decisions, rationale,
                              failed-path knowledge). This is the moat number:
                              no amount of code-reading recovers them.

Add questions to QUIZ as the vault grows. Run after any scoring change:
  python -X utf8 eval_coldstart.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cairn.vault import Vault

# (question, must_contain_in_top_text, derivable_from_code_alone)
# derivable=False → the answer exists ONLY in the vault (decision/rationale/
# history). These are the irreplaceable memories.
QUIZ = [
    ("what threshold triggers the LanceDB migration",
     "lancedb", False),   # a decision — no code encodes it
    ("how should gunicorn workers be configured",
     "gunicorn", False),  # deployment decision, not in repo
    ("what golden angle constant does cairn use",
     "0.38", True),       # in schedule.py — code-derivable
    ("what happened with McAfee and Windows Defender",
     "mcafee", False),    # machine history — exists nowhere else
    ("why did the importance backfill fail",
     "trigger", False),   # debugging rationale — the WHY behind the fix
    ("what is the warm injection limit for token fill",
     "warm", True),       # in inject.py — code-derivable
    ("how are scorecards score types decoded from CSS",
     "css", False),       # scraping insight — pipeline.py shows HOW, not WHY
    ("what is the moat that makes cairn novel",
     "golden angle", False),  # strategic position — pure vault knowledge
    ("why does cairn stay on sqlite instead of a vector db",
     "sqlite", False),    # architecture rationale
    ("what kinds of nodes get importance 9",
     "warning", True),    # in vault.py KIND_IMPORTANCE
]


def run() -> dict:
    v = Vault()
    at1 = at3 = 0
    irreplaceable_total = irreplaceable_hit = 0
    rows = []

    for q, must, derivable in QUIZ:
        res = v.query_episodic(q, k=3)

        def text_of(d):
            return ((d.get("query") or "") + " "
                    + (d.get("episodic_text") or "")).lower()

        hit1 = bool(res) and must in text_of(res[0])
        hit3 = any(must in text_of(d) for d in res)
        at1 += hit1
        at3 += hit3
        if not derivable:
            irreplaceable_total += 1
            irreplaceable_hit += hit3
        rows.append((q, hit1, hit3, derivable))

    n = len(QUIZ)
    print("=" * 72)
    print("  CAIRN COLD-START BENCHMARK")
    print("=" * 72)
    for q, hit1, hit3, derivable in rows:
        mark = "@1" if hit1 else ("@3" if hit3 else "MISS")
        src  = "code-derivable" if derivable else "VAULT-ONLY"
        print(f"  [{mark:4}] [{src:14}] {q[:48]}")
    print("-" * 72)
    print(f"  retrieval@1: {at1}/{n} ({at1/n*100:.0f}%)   "
          f"retrieval@3: {at3}/{n} ({at3/n*100:.0f}%)")
    print(f"  irreplaceable knowledge recovered: "
          f"{irreplaceable_hit}/{irreplaceable_total} "
          f"({irreplaceable_hit/max(1,irreplaceable_total)*100:.0f}%)")
    print()
    print("  WITHOUT Cairn, a fresh session recovers the code-derivable answers")
    print(f"  by re-reading the repo — and ZERO of the {irreplaceable_total} vault-only answers.")
    print("  Decisions, rationale, and failure history are not in the code.")
    print("=" * 72)

    return {"at1": at1, "at3": at3, "n": n,
            "irreplaceable": irreplaceable_hit,
            "irreplaceable_total": irreplaceable_total}


if __name__ == "__main__":
    run()
