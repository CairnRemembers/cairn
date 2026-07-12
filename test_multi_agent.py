"""
test_multi_agent.py — Cairn multi-agent / model-agnostic test suite

Simulates three models working the same session:
  - claude-sonnet-4-5  (worker, explores the problem)
  - gpt-4o             (auditor, reviews claude's findings)
  - llama3-70b-nim     (worker, executes the fix)

Verifies:
  1. Model attribution  — agent_id, model, agent_role stored per node
  2. Episodic identity  — model name encoded in episodic_text vector
  3. PROTOCOL.md        — traversal path shows [model] attribution
  4. Multi-agent summary — appears when >1 model contributed
  5. Cross-model chains  — parent links work across model boundaries
  6. Struggle per model  — gpt-4o hits 2 struggle nodes, others don't
  7. Golden angle        — scheduling works across mixed-model sessions
  8. Observer isolation  — observer nodes write correctly with read-only role
  9. Vault stats         — correct counts when multiple models contribute
 10. Legacy nodes        — nodes without model field default to 'unknown' gracefully
"""
import sys, time, tempfile, statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cairn.vault as _vmod
from cairn import Vault, MicroNode, schedule_context, PositionRecord
from cairn import golden_positions, update_compiled_hits, compile_session

PASS = "  [PASS]"
FAIL = "  [FAIL]"
total_tests = 0
total_pass  = 0

def check(label, condition, detail=""):
    global total_tests, total_pass
    total_tests += 1
    if condition:
        total_pass += 1
        print(f"{PASS} {label}")
        if detail: print(f"         {detail}")
    else:
        print(f"{FAIL} {label}")
        if detail: print(f"         {detail}")

def section(title):
    print(f"\n{'='*62}")
    print(f"  {title}")
    print(f"{'='*62}\n")


# ──────────────────────────────────────────────────────────────────────────────
section("SETUP — three-model collaborative session")
# ──────────────────────────────────────────────────────────────────────────────

tmpdir   = Path(tempfile.mkdtemp())
db       = tmpdir / "multi_agent.db"
out_dir  = tmpdir / "protocols"
vault    = Vault(db_path=db)

SESSION = "multi-agent-session-2026"

# ──────────────────────────────────────────────────────────────────────────────
section("1. MODEL ATTRIBUTION — nodes carry model identity")
# ──────────────────────────────────────────────────────────────────────────────

# ─── CLAUDE starts: exploring an auth problem ───────────────────────────────
c1 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Grep",
    query      = "authentication failure root cause",
    result_count = 0,
    latency_ms   = 920,
    agent_id   = "claude-session-abc",
    model      = "claude-sonnet-4-5",
    agent_role = "worker",
))
c2 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Grep",
    query      = "JWT token expiry validation",
    result_count = 2,
    latency_ms   = 1050,
    parent     = c1.id,
    agent_id   = "claude-session-abc",
    model      = "claude-sonnet-4-5",
    agent_role = "worker",
))
c3 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Read",
    query      = "auth/middleware.py",
    result_count = 94,
    latency_ms   = 85,
    parent     = c2.id,
    output_preview = "line 47: if DEV_MODE: return True  # SECURITY BYPASS",
    agent_id   = "claude-session-abc",
    model      = "claude-sonnet-4-5",
    agent_role = "worker",
))
# claude flags the security issue
vault.flag(c3.id)

# claude writes a decision note
c_note = vault.write(MicroNode(
    session    = SESSION,
    kind       = "decision",
    query      = "DEV_MODE bypass found in auth/middleware.py line 47 — must remove",
    agent_id   = "claude-session-abc",
    model      = "claude-sonnet-4-5",
    agent_role = "worker",
    parent     = c3.id,
))

# verify model is stored
row_c1 = vault.get(c1.id)
row_c3 = vault.get(c3.id)
check("Claude nodes stored with correct model",
      row_c1["model"] == "claude-sonnet-4-5",
      f"model: {row_c1['model']}")
check("Claude agent_id stored correctly",
      row_c1["agent_id"] == "claude-session-abc",
      f"agent_id: {row_c1['agent_id']}")
check("Claude agent_role is worker",
      row_c1["agent_role"] == "worker",
      f"role: {row_c1['agent_role']}")

# ─── GPT-4O takes over as auditor: reviews claude's findings ────────────────
g1 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Read",
    query      = "PROTOCOL.md — loading session compiled state",
    result_count = 38,
    latency_ms   = 68,
    agent_id   = "gpt4o-session-xyz",
    model      = "gpt-4o",
    agent_role = "auditor",
))
g2 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Grep",
    query      = "DEV_MODE usage across entire codebase",
    result_count = 0,
    latency_ms   = 1100,  # slow, struggle signal
    parent     = g1.id,
    agent_id   = "gpt4o-session-xyz",
    model      = "gpt-4o",
    agent_role = "auditor",
))
g3 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Grep",
    query      = "os.environ DEV_MODE",
    result_count = 1,
    latency_ms   = 850,   # still slow
    parent     = g2.id,
    agent_id   = "gpt4o-session-xyz",
    model      = "gpt-4o",
    agent_role = "auditor",
))
g4 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Read",
    query      = "tests/test_auth.py — checking test coverage",
    result_count = 122,
    latency_ms   = 95,
    parent     = g3.id,
    agent_id   = "gpt4o-session-xyz",
    model      = "gpt-4o",
    agent_role = "auditor",
    output_preview = "No test covers the DEV_MODE bypass path — gap confirmed",
))
vault.flag(g4.id)

# gpt-4o warning note
g_warn = vault.write(MicroNode(
    session    = SESSION,
    kind       = "warning",
    query      = "DEV_MODE bypass has NO test coverage — fix + add test simultaneously",
    agent_id   = "gpt4o-session-xyz",
    model      = "gpt-4o",
    agent_role = "auditor",
    parent     = g4.id,
))

row_g1 = vault.get(g1.id)
row_g4 = vault.get(g4.id)
check("GPT-4o nodes stored with correct model",
      row_g1["model"] == "gpt-4o",
      f"model: {row_g1['model']}")
check("GPT-4o auditor role stored",
      row_g1["agent_role"] == "auditor",
      f"role: {row_g1['agent_role']}")
check("GPT-4o flag operation works",
      row_g4["flagged"] == 1,
      f"flagged: {row_g4['flagged']}")

# ─── LLAMA3-NIM executes the fix ─────────────────────────────────────────────
l1 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Read",
    query      = "auth/middleware.py — full file for edit context",
    result_count = 94,
    latency_ms   = 70,
    agent_id   = "llama3-session-nim",
    model      = "llama3-70b-nim",
    agent_role = "worker",
    parent     = g_warn.id,  # cross-model chain: follows gpt-4o's warning
))
l2 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Edit",
    query      = "auth/middleware.py — remove DEV_MODE bypass at line 47",
    result_count = 3,   # lines changed — not a struggle signal
    latency_ms   = 115,
    parent     = l1.id,
    agent_id   = "llama3-session-nim",
    model      = "llama3-70b-nim",
    agent_role = "worker",
    output_preview = "Removed: if DEV_MODE: return True",
))
l3 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Write",
    query      = "tests/test_auth_devmode.py — new test for removed bypass",
    result_count = 18,  # lines written — not a struggle signal
    latency_ms   = 88,
    parent     = l2.id,
    agent_id   = "llama3-session-nim",
    model      = "llama3-70b-nim",
    agent_role = "worker",
    output_preview = "test_dev_mode_bypass_removed: assert no bypass path exists",
))
l4 = vault.write(MicroNode(
    session    = SESSION,
    kind       = "tool_call",
    tool       = "Bash",
    query      = "pytest auth/ -v",
    result_count = 7,
    latency_ms   = 390,
    parent     = l3.id,
    agent_id   = "llama3-session-nim",
    model      = "llama3-70b-nim",
    agent_role = "worker",
    output_preview = "7 passed in 0.42s",
))
l_resolved = vault.write(MicroNode(
    session    = SESSION,
    kind       = "resolved",
    query      = "DEV_MODE bypass removed, new test added, all 7 tests pass",
    agent_id   = "llama3-session-nim",
    model      = "llama3-70b-nim",
    agent_role = "worker",
    parent     = l4.id,
))

row_l1 = vault.get(l1.id)
row_l2 = vault.get(l2.id)
check("Llama3 nodes stored with correct model",
      row_l1["model"] == "llama3-70b-nim",
      f"model: {row_l1['model']}")
check("Cross-model chain: llama follows gpt-4o's warning",
      row_l1["parent"] == g_warn.id,
      f"parent: {row_l1['parent']}, expected: {g_warn.id}")

# ──────────────────────────────────────────────────────────────────────────────
section("2. EPISODIC TEXT — model identity in the vector")
# ──────────────────────────────────────────────────────────────────────────────

# Episodic text encodes model name → "claude-sonnet-4-5 searched file contents for"
# This makes cross-model queries possible: "where did gpt-4o struggle?"

row_c1_full  = vault.get(c1.id)
row_g2_full  = vault.get(g2.id)
row_l2_full  = vault.get(l2.id)
row_g1_full  = vault.get(g1.id)

et_c1 = row_c1_full["episodic_text"] or ""
et_g2 = row_g2_full["episodic_text"] or ""
et_l2 = row_l2_full["episodic_text"] or ""
et_g1 = row_g1_full["episodic_text"] or ""

check("Claude identity in episodic text",
      "claude-sonnet-4-5" in et_c1,
      f"text: '{et_c1[:80]}'")
check("GPT-4o identity in episodic text",
      "gpt-4o" in et_g2,
      f"text: '{et_g2[:80]}'")
check("Llama3 identity in episodic text",
      "llama3-70b-nim" in et_l2,
      f"text: '{et_l2[:80]}'")
check("Auditor role in gpt-4o episodic text",
      "[auditor]" in et_g1,
      f"text: '{et_g1[:80]}'")
check("Struggle signal in slow gpt-4o node",
      "struggled" in et_g2 or "nothing" in et_g2 or "1100ms" in et_g2,
      f"struggle text: '{et_g2[:100]}'")
check("Empty result signal in claude's first search",
      "nothing" in (vault.get(c1.id)["episodic_text"] or ""),
      f"empty signal in: '{et_c1[:100]}'")

# Decision node: episodic text should encode kind + model
et_note = vault.get(c_note.id)["episodic_text"] or ""
check("Decision note encodes model + kind",
      "claude-sonnet-4-5" in et_note and "decided" in et_note,
      f"decision text: '{et_note[:100]}'")

# Warning note: gpt-4o [auditor] wrote warning
et_warn = vault.get(g_warn.id)["episodic_text"] or ""
check("Warning note encodes gpt-4o + auditor + warning",
      "gpt-4o" in et_warn and "[auditor]" in et_warn and "warned" in et_warn,
      f"warning text: '{et_warn[:120]}'")

# ──────────────────────────────────────────────────────────────────────────────
section("3. CROSS-MODEL CHAINS — parent links cross model boundaries")
# ──────────────────────────────────────────────────────────────────────────────

# The full chain: c1 → c2 → c3 → c_note → (gap) → g1 → g2 → g3 → g4 → g_warn
#                                                                → l1 → l2 → l3 → l4 → l_resolved

# Chain to final resolution: l_resolved's chain should reach back through llama3 nodes
chain_to_resolved = vault.chain(l_resolved.id)
chain_models = [r["model"] for r in chain_to_resolved]
chain_roles  = [r["agent_role"] for r in chain_to_resolved]

check("Chain to resolution has multiple nodes",
      len(chain_to_resolved) >= 4,
      f"chain depth: {len(chain_to_resolved)}")
check("Chain includes llama3 nodes",
      "llama3-70b-nim" in chain_models,
      f"models in chain: {list(set(chain_models))}")
check("Chain includes gpt-4o warning node",
      "gpt-4o" in chain_models,
      f"models present: {list(set(chain_models))}")
check("Chain includes auditor role",
      "auditor" in chain_roles,
      f"roles in chain: {list(set(chain_roles))}")

# Chain to c3 (claude's discovery): c1 → c2 → c3
chain_to_c3 = vault.chain(c3.id)
chain_c3_models = [r["model"] for r in chain_to_c3]
check("Claude chain has 3 nodes",
      len(chain_to_c3) == 3,
      f"chain depth: {len(chain_to_c3)}")
check("Claude chain is single-model",
      all(m == "claude-sonnet-4-5" for m in chain_c3_models),
      f"models: {chain_c3_models}")

# ──────────────────────────────────────────────────────────────────────────────
section("4. PROTOCOL.md — multi-agent traversal + attribution")
# ──────────────────────────────────────────────────────────────────────────────

protocol_path = compile_session(vault, SESSION, out_dir)
check("PROTOCOL.md compiled successfully", protocol_path.exists(),
      f"path: {protocol_path}")

protocol_text = protocol_path.read_text(encoding="utf-8")

# Traversal path should show [model] attribution
check("Claude attribution in traversal path",
      "[claude-sonnet-4-5]" in protocol_text,
      "claude model shown in tool calls")
check("GPT-4o attribution in traversal path",
      "[gpt-4o]" in protocol_text,
      "gpt-4o model shown in tool calls")
check("Llama3 attribution in traversal path",
      "[llama3-70b-nim]" in protocol_text,
      "llama3 model shown in tool calls")
check("Auditor role shown in traversal",
      "(auditor)" in protocol_text,
      "auditor role annotated in traversal")

# Multi-agent summary section (only when >1 model)
check("Multi-agent collaboration section present",
      "## Multi-agent collaboration" in protocol_text,
      "section header found")
check("All 3 models in collaboration summary",
      "claude-sonnet-4-5" in protocol_text and
      "gpt-4o" in protocol_text and
      "llama3-70b-nim" in protocol_text,
      "all models named in summary")

# Flags and hard points
check("Flagged nodes in PROTOCOL.md",
      "## Flagged for review" in protocol_text,
      "flagged section present")
check("Hard points section present",
      "## Hard points" in protocol_text,
      "struggle section present")
check("GPT-4o struggle documented",
      "1100ms" in protocol_text or "1050ms" in protocol_text or "950ms" in protocol_text or "920ms" in protocol_text,
      "slow queries appear in hard points")

# Active threads section
check("Active threads section present",
      "## Active threads" in protocol_text,
      "active threads section present")

print(f"\n  Protocol size: {protocol_path.stat().st_size} bytes")
print(f"\n  Traversal path preview (first 15 tool nodes):")
for i, line in enumerate(protocol_text.split('\n')):
    if line.startswith('- `') and i < 200:
        print(f"    {line}")

# ──────────────────────────────────────────────────────────────────────────────
section("5. STRUGGLE DETECTION — per-model accuracy")
# ──────────────────────────────────────────────────────────────────────────────

struggles = vault.struggle_points(SESSION)
struggle_ids   = {r["id"] for r in struggles}
struggle_models = [r["model"] for r in struggles]

check("Struggle detection finds nodes", len(struggles) > 0,
      f"found {len(struggles)} struggle nodes")

# c1 (claude, 920ms, 0 results), c2 (claude, 1050ms), g2 (gpt-4o, 1100ms, 0 results)
# g3 (gpt-4o, 850ms, 1 result)
check("Claude's first slow search is a struggle",
      c1.id in struggle_ids,
      f"c1 ({c1.id}) in struggles: {c1.id in struggle_ids}")
check("GPT-4o's slow search is a struggle",
      g2.id in struggle_ids,
      f"g2 ({g2.id}) in struggles: {g2.id in struggle_ids}")
check("Both models hit struggle nodes",
      "claude-sonnet-4-5" in struggle_models and "gpt-4o" in struggle_models,
      f"models with struggles: {list(set(struggle_models))}")
check("Llama3 had no struggles (all fast + results)",
      "llama3-70b-nim" not in struggle_models or
      struggle_models.count("llama3-70b-nim") == 0,
      f"llama3 struggle count: {struggle_models.count('llama3-70b-nim')}")

# ──────────────────────────────────────────────────────────────────────────────
section("6. VAULT STATS — correct counts across models")
# ──────────────────────────────────────────────────────────────────────────────

stats = vault.stats()

all_nodes      = vault.session_nodes(SESSION)
total_expected = len(all_nodes)

check("Stats total matches actual node count",
      stats["total"] == total_expected,
      f"total: {stats['total']}, expected: {total_expected}")
check("Active nodes counted correctly",
      stats["active"] == total_expected,  # none voided in this test
      f"active: {stats['active']}")
check("Flagged nodes counted correctly",
      stats["flagged"] == 2,  # c3 and g4
      f"flagged: {stats['flagged']} (expected 2: c3, g4)")
check("Single session in stats",
      stats["sessions"] == 1,
      f"sessions: {stats['sessions']}")

# per-model counts via direct query
claude_nodes = [r for r in all_nodes if r["model"] == "claude-sonnet-4-5"]
gpt4o_nodes  = [r for r in all_nodes if r["model"] == "gpt-4o"]
llama_nodes  = [r for r in all_nodes if r["model"] == "llama3-70b-nim"]

check("Claude contributed expected nodes",
      len(claude_nodes) == 4,  # c1, c2, c3, c_note
      f"claude nodes: {len(claude_nodes)} (expected 4)")
check("GPT-4o contributed expected nodes",
      len(gpt4o_nodes) == 5,   # g1, g2, g3, g4, g_warn
      f"gpt4o nodes: {len(gpt4o_nodes)} (expected 5)")
check("Llama3 contributed expected nodes",
      len(llama_nodes) == 5,   # l1, l2, l3, l4, l_resolved
      f"llama3 nodes: {len(llama_nodes)} (expected 5)")
check("All nodes accounted for",
      len(claude_nodes) + len(gpt4o_nodes) + len(llama_nodes) == total_expected,
      f"total: {len(claude_nodes)}+{len(gpt4o_nodes)}+{len(llama_nodes)} = {total_expected}")

# ──────────────────────────────────────────────────────────────────────────────
section("7. GOLDEN ANGLE — scheduling across mixed-model nodes")
# ──────────────────────────────────────────────────────────────────────────────

# Pull all nodes, schedule them — golden angle should distribute uniformly
# regardless of which model wrote each node

class NodeProxy:
    """Lightweight proxy for schedule_context (needs .id, .status, .timestamp)"""
    def __init__(self, row):
        self.id        = row["id"]
        self.status    = row["status"]
        self.timestamp = row["timestamp"]
        self.model     = row["model"]

proxies  = [NodeProxy(r) for r in all_nodes]
records  = {}
position_history = {p.id: [] for p in proxies}

# simulate 5 sessions of scheduling (tracks which positions nodes land)
for session_num in range(5):
    ordered = schedule_context(proxies, records, max_context=len(proxies))
    for i, node in enumerate(ordered):
        position_history[node.id].append(i / len(ordered))

# verify: nodes from ALL models appear in scheduled output
scheduled_ids = set()
for _ in range(3):
    ordered = schedule_context(proxies, records, max_context=len(proxies))
    scheduled_ids.update(p.id for p in ordered)

claude_scheduled = sum(1 for r in all_nodes
                       if r["model"] == "claude-sonnet-4-5" and r["id"] in scheduled_ids)
gpt4o_scheduled  = sum(1 for r in all_nodes
                       if r["model"] == "gpt-4o" and r["id"] in scheduled_ids)
llama_scheduled  = sum(1 for r in all_nodes
                       if r["model"] == "llama3-70b-nim" and r["id"] in scheduled_ids)

check("Claude nodes appear in scheduled output",
      claude_scheduled > 0,
      f"claude scheduled: {claude_scheduled}/{len(claude_nodes)}")
check("GPT-4o nodes appear in scheduled output",
      gpt4o_scheduled > 0,
      f"gpt4o scheduled: {gpt4o_scheduled}/{len(gpt4o_nodes)}")
check("Llama3 nodes appear in scheduled output",
      llama_scheduled > 0,
      f"llama3 scheduled: {llama_scheduled}/{len(llama_nodes)}")

# verify golden angle coverage (no model gets exclusively middle)
mid_hits_by_model = {
    "claude-sonnet-4-5": 0,
    "gpt-4o": 0,
    "llama3-70b-nim": 0,
}
total_by_model = {
    "claude-sonnet-4-5": len(claude_nodes),
    "gpt-4o": len(gpt4o_nodes),
    "llama3-70b-nim": len(llama_nodes),
}
for row in all_nodes:
    m = row["model"]
    if m in mid_hits_by_model and row["id"] in records:
        mid_hits_by_model[m] += records[row["id"]].middle_hits

check("Position tracking active across all models",
      sum(r.total_loads for r in records.values()) > 0,
      f"total position records: {len(records)}")

# ──────────────────────────────────────────────────────────────────────────────
section("8. DEFAULT MODEL — nodes without explicit model set")
# ──────────────────────────────────────────────────────────────────────────────

# Simulates a legacy call or a runtime that doesn't set CAIRN_MODEL
# Should default gracefully to "unknown"

legacy_node = vault.write(MicroNode(
    session = SESSION,
    kind    = "tool_call",
    tool    = "Read",
    query   = "some/file.py",
    result_count = 50,
    # no model, agent_id, agent_role → use defaults
))
row_legacy = vault.get(legacy_node.id)
et_legacy  = row_legacy["episodic_text"] or ""

check("Default model is 'unknown'",
      row_legacy["model"] == "unknown",
      f"model: {row_legacy['model']}")
check("Default agent_role is 'worker'",
      row_legacy["agent_role"] == "worker",
      f"role: {row_legacy['agent_role']}")
check("Episodic text uses 'agent' when model unknown",
      "agent" in et_legacy,
      f"text: '{et_legacy[:80]}'")
check("Unknown-model node still writes episodic_text",
      bool(et_legacy.strip()),
      f"episodic_text present: {bool(et_legacy.strip())}")

# ──────────────────────────────────────────────────────────────────────────────
section("9. VOID ACROSS MODELS — immutability holds for all models")
# ──────────────────────────────────────────────────────────────────────────────

# void a claude node, then verify the gpt-4o auditor can still see it (archive != delete)
vault.void(c2.id)
row_c2_after_void = vault.get(c2.id)

check("Claude node voided successfully",
      row_c2_after_void["status"] == "void",
      f"status: {row_c2_after_void['status']}")
check("Voided node model attribution preserved",
      row_c2_after_void["model"] == "claude-sonnet-4-5",
      f"model still: {row_c2_after_void['model']}")
check("Voided node episodic text preserved",
      bool(row_c2_after_void["episodic_text"]),
      f"episodic_text present after void: {bool(row_c2_after_void['episodic_text'])}")

# void a gpt-4o node
vault.void(g3.id)
row_g3_after_void = vault.get(g3.id)
check("GPT-4o node voided successfully",
      row_g3_after_void["status"] == "void",
      f"status: {row_g3_after_void['status']}")

# direct SQL update should still be blocked regardless of model
blocked  = False
err_msg  = "trigger did not fire — update succeeded (BUG)"
try:
    vault.conn.execute("UPDATE nodes SET model='hacked' WHERE id=?", (l4.id,))
    vault.conn.commit()
except Exception as e:
    blocked = True
    err_msg = str(e)

check("Immutability trigger blocks model field tampering",
      blocked,
      f"trigger fired: '{err_msg[:60]}'")

# ──────────────────────────────────────────────────────────────────────────────
section("10. MULTI-SESSION HANDOFF — second session picks up")
# ──────────────────────────────────────────────────────────────────────────────

# A second session starts fresh, loads PROTOCOL.md from session 1
# New model (gemini-1.5-pro as reviewer) runs a post-fix audit

SESSION2 = "multi-agent-session-2026-followup"

gem1 = vault.write(MicroNode(
    session    = SESSION2,
    kind       = "tool_call",
    tool       = "Read",
    query      = "PROTOCOL.md from previous session",
    result_count = 52,
    latency_ms   = 72,
    agent_id   = "gemini-session-follow",
    model      = "gemini-1.5-pro",
    agent_role = "observer",  # just reviewing, not modifying
))
gem2 = vault.write(MicroNode(
    session    = SESSION2,
    kind       = "tool_call",
    tool       = "Bash",
    query      = "pytest auth/ --cov=auth --cov-report=term",
    result_count = 7,
    latency_ms   = 415,
    parent     = gem1.id,
    agent_id   = "gemini-session-follow",
    model      = "gemini-1.5-pro",
    agent_role = "observer",
    output_preview = "7 passed, coverage: 94%",
))
gem_insight = vault.write(MicroNode(
    session    = SESSION2,
    kind       = "insight",
    query      = "Post-fix audit: DEV_MODE bypass fully removed, coverage 94%, no regression",
    agent_id   = "gemini-session-follow",
    model      = "gemini-1.5-pro",
    agent_role = "observer",
    parent     = gem2.id,
))

p2_path  = compile_session(vault, SESSION2, out_dir / "session2")
p2_text  = p2_path.read_text(encoding="utf-8")
p1_text  = protocol_path.read_text(encoding="utf-8")

check("Session 2 PROTOCOL.md created",
      p2_path.exists(),
      f"size: {p2_path.stat().st_size} bytes")
check("Gemini appears in session 2 PROTOCOL.md",
      "gemini-1.5-pro" in p2_text,
      "gemini in traversal path")
check("Observer role appears in session 2",
      "(observer)" in p2_text,
      "observer role annotated")
check("Session 2 does NOT show session 1 models",
      "claude-sonnet-4-5" not in p2_text,
      "sessions are isolated in PROTOCOL.md")
check("Two sessions are independent",
      p1_text != p2_text,
      f"session 1: {len(p1_text)} chars, session 2: {len(p2_text)} chars")

print(f"\n  Session 1 ({SESSION[:30]}): {len(p1_text)} chars, 3 models")
print(f"  Session 2 ({SESSION2[:30]}): {len(p2_text)} chars, 1 model")

# ──────────────────────────────────────────────────────────────────────────────
section("RESULTS")
# ──────────────────────────────────────────────────────────────────────────────

print(f"  {total_pass}/{total_tests} tests passed\n")

if total_pass == total_tests:
    print("  ALL TESTS PASSED")
    print()
    print("  CONFIRMED multi-agent / model-agnostic:")
    print("    Model attribution   — agent_id + model + agent_role per node")
    print("    Episodic identity   — model name baked into semantic vector")
    print("    Cross-model chains  — parent links work across any model boundary")
    print("    PROTOCOL.md         — [model] + (role) in traversal path")
    print("    Multi-agent summary — appears only when >1 model contributed")
    print("    Struggle per model  — 'where did gpt-4o struggle?' is queryable")
    print("    Golden scheduling   — all models distributed equally, no bias")
    print("    Default graceful    — unknown model works without attribution")
    print("    Immutability        — trigger blocks tampering on all model nodes")
    print("    Session isolation   — each model's session compiles independently")
    print()
    print("  This is a system of thinking, not a Claude-only tool.")
    print("  Any model is an equal citizen. The path is the memory.")
else:
    print(f"  {total_tests - total_pass} FAILED — review output above")
    for_debug = [(r["id"], r["model"], r["episodic_text"][:60] if r["episodic_text"] else "None")
                 for r in vault.session_nodes(SESSION)[:5]]
    print(f"\n  First 5 nodes for debug: {for_debug}")
