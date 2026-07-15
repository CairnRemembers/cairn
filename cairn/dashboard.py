"""
cairn/dashboard.py
Local real-time dashboard — http://localhost:7331

Three panels:
  LEFT   — live node feed (streams as nodes arrive, color-coded by kind/model)
  CENTER — D3.js force graph (nodes + edges, Obsidian-style, interactive)
  RIGHT  — node detail (click any node → full episodic text, chain, metadata)

Bottom bar:
  Session stats | Context budget | Vault total | Embed coverage

Run:
  python -m cairn dashboard
  python -m cairn dashboard --port=8080
  python -m cairn dashboard --session=2026-06-08

Requires: pip install fastapi uvicorn
D3.js: loaded from CDN — no npm, no build step.
"""
from __future__ import annotations
import json, os, time, asyncio, threading
from pathlib import Path
from datetime import datetime, timezone

from cairn.accounts import ACCOUNTS

# ── FastAPI (optional dep — don't import at module level for hook safety) ────

_ATLAS_PALETTE = ["#C9A227", "#4A9EDB", "#E3B341", "#AB47BC", "#FF7043",
                  "#26A69A", "#7E57C2", "#EF5350", "#66BB6A", "#5C6BC0"]


def _atlas_project_tags() -> list:
    """Atlas project groups = the user's OWN declared tags from
    ~/.cairn/projects.json. Empty for a fresh vault (no file) — a new user's
    atlas groups by account until they declare projects. NOTHING personal is
    ever baked into shipped code."""
    import json
    from pathlib import Path
    try:
        f = Path.home() / ".cairn" / "projects.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            return [k for k, v in data.items()
                    if k and isinstance(v, (list, tuple))]
    except Exception:
        pass
    return []


def _atlas_project_colors(tags: list) -> dict:
    """Stable palette color per declared project tag + the generic system groups
    (import / _none). No personal project is ever hardcoded here."""
    colors = {t: _ATLAS_PALETTE[i % len(_ATLAS_PALETTE)]
              for i, t in enumerate(tags)}
    colors["import"] = "#6E7681"
    colors["_none"] = "#484F58"
    return colors


def run_dashboard(port: int = 7331, session_id: str | None = None,
                  open_browser: bool = True):
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
        import uvicorn
    except ImportError:
        print("cairn: dashboard requires fastapi and uvicorn")
        print("       pip install fastapi uvicorn")
        return

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from cairn.vault import Vault, VAULT_ROOT

    app   = FastAPI(title="Cairn Dashboard")
    vault = Vault()

    @app.middleware("http")
    async def _origin_guard(request, call_next):
        # Anti DNS-rebinding: the server binds 127.0.0.1 only, so a legitimate
        # browser or CLI caller always connects with a localhost Host. Reject any
        # other Host — a rebinding attack (evil.com -> 127.0.0.1) carries the
        # attacker's own Host and is refused here before it can read or mutate the
        # vault. Empty Host (some CLI/hook callers) is allowed, matching the Origin
        # rule below. A future --lan/phone mode would extend this allowlist.
        _host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if _host and _host not in ("localhost", "127.0.0.1", "::1"):
            return JSONResponse({"error": "invalid host"}, status_code=403)
        # CSRF defense (same-origin): refuse a state-changing request whose Origin
        # host doesn't match the host the browser actually connected to. Same-
        # origin UI fetches match (allowed); CLI/hook callers send no Origin
        # (allowed); a cross-site drive-by sends a foreign or null Origin (blocked).
        # Matching on the request's own Host means this works whether the server
        # is bound to localhost OR a LAN IP — so the future --lan/phone feature
        # won't collide with it. No CORS added; Origin is never required.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlparse
                o_host   = urlparse(origin).hostname
                srv_host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
                if o_host != srv_host:
                    return JSONResponse(
                        {"error": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    # housekeeping: clear tool-buffers left behind by sessions that ended uncleanly
    # (a crash/kill never reaches drain()) — else the live feed replays them as "live".
    try:
        from cairn import pending as _pending_boot
        _swept = _pending_boot.sweep_stale(24.0)
        if _swept:
            print(f"cairn: swept {_swept} stale tool-buffer(s)")
    except Exception:
        pass



    # media files (phone photos) — ~/.cairn/media/
    @app.get("/media/{fname}")
    def media(fname: str):
        from fastapi.responses import FileResponse
        safe = Path(fname).name  # no traversal
        f = Path.home() / ".cairn" / "media" / safe
        if not f.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(f))

    # bundled UI assets (facelift: fonts, textures, wordmark) — cairn/assets/
    @app.get("/assets/{path:path}")
    def asset(path: str):
        from fastapi.responses import FileResponse
        base = (Path(__file__).parent / "assets").resolve()
        f = (base / path).resolve()
        if not (f.is_file() and f.is_relative_to(base)):   # no traversal
            return JSONResponse({"error": "not found"}, status_code=404)
        # explicit mimes where FileResponse's guess is wrong/absent
        mt = {".woff2": "font/woff2",
              ".webmanifest": "application/manifest+json"}.get(f.suffix)
        return FileResponse(str(f), media_type=mt) if mt else FileResponse(str(f))

    # The Garden — the human face, same vault, same port, /garden
    try:
        from cairn.garden import register_garden
        register_garden(app, vault, _current_session)
    except Exception as e:
        print(f"cairn: garden failed to register — {e}")

    # ── API ──────────────────────────────────────────────────────────────────

    @app.get("/api/status")
    def api_status():
        stats = vault.stats()
        sess  = session_id or _current_session()
        nodes = vault.session_nodes(sess)
        struggles = vault.struggle_points(sess)

        total_chars = sum(len(r["episodic_text"] or "") for r in nodes)
        token_est   = total_chars // 4
        embedded    = sum(1 for r in nodes if r["embedding"])
        # session start = the first node's timestamp — an ADDITIVE field for the
        # humanized status bar (every existing metric is unchanged).
        _stamps = sorted(t for t in ((r["timestamp"] or "") for r in nodes) if t)
        session_started = _stamps[0] if _stamps else ""

        # vault-wide tier counts — what the inject engine actually draws from
        # (per-session counts showed H:0 when no compress events, which is misleading)
        t = vault.conn.execute(
            "SELECT memory_tier, COUNT(*) FROM nodes WHERE status='active' GROUP BY memory_tier"
        ).fetchall()
        tier_map  = {row[0]: row[1] for row in t}
        tier_hot  = tier_map.get(0, 0)
        tier_warm = tier_map.get(1, 0)
        tier_cold = tier_map.get(2, 0)

        # model-aware context window (reads CAIRN_MODEL env var)
        _MODEL_CTX = {
            "claude-fable-5": 1_000_000, "claude-mythos-5": 1_000_000,
            "claude-opus-4":    200_000, "claude-sonnet-4":   200_000,
            "gpt-4o":           128_000, "gemini-1.5-pro":  1_000_000,
        }
        model_env = (os.environ.get("CAIRN_MODEL") or
                     os.environ.get("CLAUDE_MODEL") or "").lower()
        ctx_window = next((v for k, v in _MODEL_CTX.items() if k in model_env), 200_000)

        return {
            "session":        sess,
            "session_started": session_started,
            "session_nodes":  len(nodes),
            "struggles":      len(struggles),
            "token_est":      token_est,
            "embedded":       embedded,
            "vault_total":    stats["total"],
            "vault_sessions": stats["sessions"],
            "vault_flagged":  stats["flagged"],
            "vault_voided":   stats["voided"],
            "tier_hot":       tier_hot,
            "tier_warm":      tier_warm,
            "tier_cold":      tier_cold,
            "context_window": ctx_window,   # model-aware — JS uses this for the % bar
            "attention":      vault.attention_efficiency(),  # the EEG line
        }

    @app.get("/api/inject_state")
    def api_inject_state():
        """Return current inject gate state from inject_state.json."""
        WARM_LIMIT = 40
        state_file = Path.home() / ".cairn" / "inject_state.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        warm_sent  = int(state.get("warm_blocks_sent", 0))
        call_count = int(state.get("call_counter", 0))
        warm_pct   = min(100, round(warm_sent / WARM_LIMIT * 100))
        return {
            "call_counter":      call_count,
            "warm_blocks_sent":  warm_sent,
            "warm_blocks_limit": WARM_LIMIT,
            "warm_pct":          warm_pct,
        }

    # Shared cross-view node selection — knowledge layer first, then importance,
    # then recency, capped. ONE definition so /api/graph, /api/semantic_edges
    # and /api/edges always describe the same node set.
    CROSS_VIEW_ORDER = """
        ORDER BY CASE WHEN kind IN
            ('decision','warning','insight','idea','open_item','procedure',
             'resolved','hypothesis','question','blocker','context_stamp')
            THEN 0 ELSE 1 END,
            importance DESC, timestamp DESC
        LIMIT 900
    """

    @app.get("/api/edges")
    def api_edges(sess: str | None = None, cross: bool = False,
                  account: str | None = None):
        """Typed, tiered edges from the precomputed edges table (cairn edges
        build) filtered to the same node selection the graph endpoint returns.
        Visual-overlay data — the dashboard adds NO force from these."""
        if account:
            id_rows = vault.conn.execute("""
                SELECT n.id FROM nodes n
                JOIN sessions s ON n.session = s.id
                WHERE s.account = ? AND n.status != 'void'
                ORDER BY n.timestamp ASC LIMIT 1000
            """, (account,)).fetchall()
        elif cross:
            id_rows = vault.conn.execute(
                "SELECT id FROM nodes WHERE status != 'void' " + CROSS_VIEW_ORDER
            ).fetchall()
        else:
            target = sess or session_id or _current_session()
            id_rows = vault.conn.execute(
                "SELECT id FROM nodes WHERE status != 'void' AND session = ?",
                (target,)).fetchall()
        ids = {r["id"] for r in id_rows}
        edges = []
        for r in vault.conn.execute(
                "SELECT src, dst, type, tier, weight FROM edges"):
            if r["src"] in ids and r["dst"] in ids:
                edges.append({"source": r["src"], "target": r["dst"],
                              "type": r["type"], "tier": r["tier"],
                              "weight": r["weight"]})
        counts: dict = {}
        for e in edges:
            key = e["tier"] or e["type"]
            counts[key] = counts.get(key, 0) + 1
        return {"edges": edges, "counts": counts}

    def _ts_ms(ts) -> int:
        from datetime import datetime as _dt
        try:
            return int(_dt.fromisoformat(
                (ts or "").replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return 0

    @app.get("/api/atlas")
    def api_atlas():
        """The full-vault map: EVERY active node at its precomputed stable
        position (fractal phyllotaxis, cairn edges build). Canvas-rendered
        client-side — no physics, no node cap, no re-scramble."""
        import math as _math
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        rows = vault.conn.execute("""
            SELECT n.id, n.kind, n.community, n.importance, n.map_x, n.map_y,
                   n.gist, n.query, n.tags, n.timestamp, n.memory_tier,
                   n.model, s.account
            FROM nodes n LEFT JOIN sessions s ON n.session = s.id
            WHERE n.status != 'void' AND n.map_x IS NOT NULL
        """).fetchall()
        # FIX 1a — live-fill: also pull recent active nodes that don't yet have a
        # map position (no edge rebuild since they were created). They get a
        # PROVISIONAL position client-visible within seconds instead of after a
        # compute_atlas run.
        _now = _dt.now(_tz.utc)
        _cut2d = (_now - _td(days=2)).isoformat()
        fresh_rows = vault.conn.execute("""
            SELECT n.id, n.kind, n.community, n.importance, n.map_x, n.map_y,
                   n.gist, n.query, n.tags, n.timestamp, n.memory_tier,
                   n.model, s.account
            FROM nodes n LEFT JOIN sessions s ON n.session = s.id
            WHERE n.status != 'void' AND n.map_x IS NULL
              AND n.timestamp >= ?
        """, (_cut2d,)).fetchall()
        # community centroids from the MAPPED nodes — provisional placement anchor
        _cc = {}
        for r in rows:
            _cid0, _, _ = (r["community"] or "").partition("|")
            if _cid0 and r["map_x"] is not None:
                a = _cc.setdefault(_cid0, [0.0, 0.0, 0])
                a[0] += r["map_x"]; a[1] += r["map_y"]; a[2] += 1
        _GA = _math.pi * (3.0 - _math.sqrt(5.0))   # golden angle
        # PER-GALAXY anchor — community-less fresh nodes land in THEIR OWN galaxy's
        # orphan halo, NOT a ring around the whole field. Each account's centre +
        # body radius come from its MAPPED nodes: centroid pass, then max-distance
        # pass. (sqlite3.Row -> index with r["account"], never .get())
        _gsum = {}
        for r in rows:
            if r["map_x"] is None: continue
            ga = r["account"] or "?"
            g = _gsum.setdefault(ga, [0.0, 0.0, 0])
            g[0] += r["map_x"]; g[1] += r["map_y"]; g[2] += 1
        _gcen = {ga: (g[0] / g[2], g[1] / g[2]) for ga, g in _gsum.items() if g[2]}
        _gbody = {ga: 80.0 for ga in _gcen}
        for r in rows:
            if r["map_x"] is None: continue
            ga = r["account"] or "?"
            cx, cy = _gcen.get(ga, (0.0, 0.0))
            _gbody[ga] = max(_gbody[ga], _math.hypot(r["map_x"] - cx, r["map_y"] - cy))
        _prov = []
        _orphan_i = 0
        for i, r in enumerate(fresh_rows):
            _cid0, _, _ = (r["community"] or "").partition("|")
            c = _cc.get(_cid0)
            if c and c[2]:
                # has a community — land near its centroid
                bx, by = c[0] / c[2], c[1] / c[2]
                off = 18.0 + 6.0 * i
                ang = _GA * (i + 1)
            else:
                # no community yet -> scatter DIFFUSE into THIS galaxy's orphan halo,
                # like real orphan dust: per-id hash -> area-even radius + free angle.
                # NOT a fixed-radius golden-angle band (that lines them up as a ring).
                # Stays inside its own galaxy, never the whole field.
                ga = r["account"] or "?"
                cx, cy = _gcen.get(ga, (0.0, 0.0))
                br = _gbody.get(ga, 200.0)
                _h = 2166136261
                for _ch in (r["id"] or ""):
                    _h = ((_h ^ ord(_ch)) * 16777619) & 0xFFFFFFFF
                _fr = (_h & 0xFFFFF) / 1048575.0          # 0..1 -> area-even radius
                _fa = ((_h >> 20) & 0xFFF) / 4095.0        # 0..1 -> free angle
                rr = br * _math.sqrt(1.1 + 1.8 * _fr)      # ~1.05..1.70x body, diffuse
                ang = _fa * 6.2831853
                bx, by = cx + rr * _math.cos(ang), cy + rr * _math.sin(ang)
                off = 0.0
            _prov.append((r, bx + off * _math.cos(ang), by + off * _math.sin(ang)))
        _cut24h = (_now - _td(days=1)).isoformat()
        # iterable of (row, provisional_x_or_None, provisional_y_or_None, is_fresh)
        _all = [(r, None, None) for r in rows] + [(r, px, py) for (r, px, py) in _prov]
        nodes, agg = [], {}
        for r, _px, _py in _all:
            _fresh = (_px is not None) or ((r["timestamp"] or "") >= _cut24h)
            cid, _, lbl = (r["community"] or "").partition("|")
            if cid:
                # hue from the LABEL TEXT hash — stable across rebuilds and
                # decorrelated from position (index-based hue rainbows the map)
                hue = (sum(ord(ch) * (idx + 7) for idx, ch in enumerate(lbl or cid))
                       * 137.508) % 360.0
                color = f"hsl({hue:.0f},58%,62%)"
            else:
                color = "#3A3528"
            nx = r["map_x"] if _px is None else _px
            ny = r["map_y"] if _py is None else _py
            nd = {
                "id": r["id"],
                "x": round(nx, 1), "y": round(ny, 1),
                "r": round(1.4 + (r["importance"] or 5) * 0.28, 2),
                "c": color,
                "dim": 1 if '"import"' in (r["tags"] or "") else 0,
                "g": (r["gist"] or (r["query"] or ""))[:70],
                "k": r["kind"],
                "i": r["importance"] or 5,
                "a": (r["account"] or "").replace("claude backfill-", ""),
                "ts": _ts_ms(r["timestamp"]),
                "cid": cid,
                "tr": r["memory_tier"] if r["memory_tier"] is not None else 1,
                "m": (r["model"] or "")[:20],
            }
            if _fresh:
                nd["fresh"] = 1
            nodes.append(nd)
            # only MAPPED nodes contribute to community label centroids
            if cid and lbl and r["map_x"] is not None:
                a = agg.setdefault(cid, [0.0, 0.0, 0, lbl])
                a[0] += r["map_x"]; a[1] += r["map_y"]; a[2] += 1
        labels = [{"x": round(a[0] / a[2], 1), "y": round(a[1] / a[2], 1),
                   "t": a[3], "n": a[2], "cid": cid_}
                  for cid_, a in sorted(agg.items(), key=lambda kv: -kv[1][2])[:80]]
        # ALL edge types ship to the client — [src, dst, type-code].
        # semantic: s/m/w  |  chain: c  |  dendrite: d
        # The Links checkboxes decide what's visible; the data is just there.
        edges = [[r["src"], r["dst"], (r["tier"] or "s")[0]]
                 for r in vault.conn.execute(
                     "SELECT src, dst, tier FROM edges WHERE type='semantic'")]
        edges += [[r["src"], r["dst"], "c"]
                  for r in vault.conn.execute(
                      "SELECT src, dst FROM edges WHERE type='chain'")]
        edges += [[r["src"], r["dst"], "d"]
                  for r in vault.conn.execute(
                      "SELECT src, dst FROM edges WHERE type='dendrite'")]
        # atlas revision — lets an open dashboard notice a coordinate-only rebuild
        # (same node/edge counts, new map_x/map_y) instead of skipping the refresh.
        try:
            _revrow = vault.conn.execute(
                "SELECT v FROM atlas_meta WHERE k='rev'").fetchone()
            _rev = _revrow["v"] if _revrow else 0
        except Exception:
            _rev = 0
        return {"nodes": nodes, "labels": labels, "edges": edges, "rev": _rev}

    @app.get("/api/graph_search")
    def api_graph_search(q: str = "", k: int = 12):
        """Hybrid search hits for the graph spotlight. Returns ids + scores
        only — the client lights them up IN PLACE and rings them with their
        tiered neighbors. Search never moves a node."""
        q = (q or "").strip()[:500]
        if not q:
            return {"hits": []}
        try:
            rows = vault.query_episodic(q, k=max(1, min(40, int(k))))
        except Exception:
            return {"hits": [], "error": "search unavailable (embedder?)"}
        return {"hits": [
            {"id":    r.get("id"),
             "score": round(float(r.get("score", 0)), 3),
             "gist":  (r.get("gist") or (r.get("query") or "")[:80])}
            for r in rows if r.get("id")
        ]}

    @app.get("/api/semantic_edges")
    def api_semantic_edges(sess: str | None = None,
                           threshold: float = 0.62,
                           cross: bool = False):
        """
        Pairwise cosine similarity between embedded nodes.
        Returns edges above `threshold` — the GraphRAG layer.

        Same embedding format as vault.query_episodic:
        struct.pack(f"{DIM}f", *vector)
        """
        import struct
        try:
            import numpy as np
            HAS_NP = True
        except ImportError:
            HAS_NP = False

        DIM = 384  # all-MiniLM-L6-v2

        target = sess or session_id or _current_session()

        if cross:
            # all sessions with nodes — same selection as /api/graph cross view
            rows = vault.conn.execute(
                "SELECT id, session, embedding FROM nodes "
                "WHERE embedding IS NOT NULL AND status != 'void' "
                + CROSS_VIEW_ORDER).fetchall()
        else:
            rows = vault.conn.execute("""
                SELECT id, session, embedding
                FROM nodes
                WHERE embedding IS NOT NULL
                  AND status != 'void'
                  AND session = ?
            """, (target,)).fetchall()

        if len(rows) < 2:
            return {"edges": [], "node_count": len(rows)}

        # decode embeddings
        valid = []
        for r in rows:
            try:
                blob = r["embedding"]
                emb  = struct.unpack(f"{DIM}f", blob)
                valid.append({"id": r["id"], "session": r["session"], "emb": emb})
            except struct.error:
                continue

        if len(valid) < 2:
            return {"edges": [], "node_count": len(valid)}

        if HAS_NP:
            import numpy as np
            mat   = np.array([v["emb"] for v in valid], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            mat  = mat / norms
            sims = mat @ mat.T   # (n × n) cosine similarity matrix
            n    = len(valid)
            edges = []
            for i in range(n):
                for j in range(i + 1, n):
                    s = float(sims[i, j])
                    if s >= threshold:
                        edges.append({
                            "source":       valid[i]["id"],
                            "target":       valid[j]["id"],
                            "similarity":   round(s, 3),
                            "cross_session": valid[i]["session"] != valid[j]["session"],
                        })
        else:
            # pure-Python fallback (slower)
            def _cosine(a, b):
                dot = sum(x * y for x, y in zip(a, b))
                ma  = sum(x * x for x in a) ** 0.5
                mb  = sum(x * x for x in b) ** 0.5
                return dot / (ma * mb) if ma * mb else 0.0

            edges = []
            for i, ni in enumerate(valid):
                for j in range(i + 1, len(valid)):
                    nj = valid[j]
                    s  = _cosine(ni["emb"], nj["emb"])
                    if s >= threshold:
                        edges.append({
                            "source":       ni["id"],
                            "target":       nj["id"],
                            "similarity":   round(s, 3),
                            "cross_session": ni["session"] != nj["session"],
                        })

        edges.sort(key=lambda e: -e["similarity"])
        return {
            "edges":      edges[:200],
            "node_count": len(valid),
            "threshold":  threshold,
        }

    @app.get("/api/graph")
    def api_graph(sess: str | None = None, cross: bool = False,
                  account: str | None = None):
        """Returns D3 force graph data: nodes + links.
        cross=true returns the knowledge-first multi-session view.
        account=<label> loads an imported archive as its own view —
        conversation chains, chronological, capped for SVG sanity."""
        if account:
            rows = vault.conn.execute("""
                SELECT n.* FROM nodes n
                JOIN sessions s ON n.session = s.id
                WHERE s.account = ? AND n.status != 'void'
                ORDER BY n.timestamp ASC LIMIT 1000
            """, (account,)).fetchall()
        elif cross:
            # all sessions — knowledge layer first so imported episodic sediment
            # can't crowd out the vault; same selection as /api/edges and
            # /api/semantic_edges (CROSS_VIEW_ORDER keeps the three in lockstep)
            rows = vault.conn.execute(
                "SELECT n.*, s.account AS account FROM nodes n "
                "LEFT JOIN sessions s ON n.session = s.id "
                "WHERE n.status != 'void' "
                + CROSS_VIEW_ORDER).fetchall()
        else:
            target = sess or session_id or _current_session()
            rows   = vault.session_nodes(target)

        MODEL_COLORS = {
            "claude-sonnet-4-5": "#4A9EDB",
            "claude":            "#4A9EDB",
            "gpt-4o":            "#10A37F",
            "gpt":               "#10A37F",
            "llama":             "#F97316",
            "gemini":            "#EA4335",
            "unknown":           "#94A3B8",
        }
        KIND_SHAPES = {
            "tool_call":        "circle",
            "decision":         "diamond",
            "hypothesis":       "triangle",
            "warning":          "star",
            "insight":          "diamond",
            "question":         "triangle",
            "open_item":        "square",
            "blocker":          "star",
            "resolved":         "circle",
            "context_stamp":    "pentagon",
            "conversation_turn":"circle",
            "interrupt":        "square",
            "note":             "circle",
        }

        def model_color(model: str) -> str:
            model = (model or "unknown").lower()
            for prefix, color in MODEL_COLORS.items():
                if model.startswith(prefix):
                    return color
            return MODEL_COLORS["unknown"]

        def row_get(r, key, default=None):
            """sqlite3.Row doesn't have .get() — safe fallback."""
            try:
                return r[key]
            except (IndexError, KeyError):
                return default

        # Injection pulse window: nodes injected in the last 24h glow
        from datetime import datetime, timezone, timedelta
        pulse_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        graph_nodes = []
        consolidation_links = []   # dendrites: consolidated node → its members
        for r in rows:
            color  = model_color(row_get(r, "model", "unknown"))
            if row_get(r, "flagged", 0):
                color = "#EF4444"
            if row_get(r, "status", "active") == "void":
                color = "#475569"

            lat       = row_get(r, "latency_ms") or 0
            rc        = row_get(r, "result_count")
            tool_name = row_get(r, "tool") or ""
            HIGH_VOL  = {"Grep", "Glob", "WebSearch"}
            struggle  = (lat > 2000
                         or (rc == 0)
                         or (rc is not None and rc <= 2 and tool_name in HIGH_VOL))

            # Consolidated synthesis node? (REM-pass output — the neocortex layer)
            tags_raw = row_get(r, "tags") or "[]"
            consolidated = "consolidated" in tags_raw
            if consolidated:
                try:
                    for tag in json.loads(tags_raw):
                        if isinstance(tag, str) and tag.startswith("member:"):
                            consolidation_links.append({
                                "source": row_get(r, "id"),
                                "target": tag.split(":", 1)[1],
                                "type":   "dendrite",
                            })
                except Exception:
                    pass

            last_inj = row_get(r, "last_injected")
            recently_injected = bool(last_inj and last_inj >= pulse_cutoff)

            graph_nodes.append({
                "id":           row_get(r, "id"),
                "kind":         row_get(r, "kind", "note"),
                "tool":         row_get(r, "tool"),
                "model":        row_get(r, "model") or "unknown",
                "status":       row_get(r, "status", "active"),
                "flagged":      bool(int(row_get(r, "flagged", 0) or 0)),
                "struggle":     struggle,
                "color":        color,
                "shape":        KIND_SHAPES.get(row_get(r, "kind", "note"), "circle"),
                "label":        (row_get(r, "tool") or row_get(r, "kind") or "")[:20],
                "query":        (row_get(r, "query") or "")[:80],
                "episodic":     (row_get(r, "episodic_text") or "")[:200],
                "gist":         row_get(r, "gist") or "",
                "timestamp":    row_get(r, "timestamp"),
                "latency_ms":   int(row_get(r, "latency_ms") or 0) or None,
                "result_count": row_get(r, "result_count"),
                "memory_tier":  int(row_get(r, "memory_tier") or 1),
                "importance":   int(row_get(r, "importance") or 5),
                "stability":    float(row_get(r, "stability_days") or 1.0),
                "pulse":        recently_injected,
                "consolidated": consolidated,
                "session":      row_get(r, "session", ""),
                "community":    (row_get(r, "community") or "").partition("|")[0],
                "topic":        (row_get(r, "community") or "").partition("|")[2],
            })

        # Only include links where BOTH endpoints exist in the node set.
        # Voided nodes are excluded from graph_nodes but their children may still
        # reference them as parents — a dangling link crashes D3's forceLink silently.
        node_id_set = {n["id"] for n in graph_nodes}
        links = [
            {"source": r["parent"], "target": r["id"]}
            for r in rows
            if r["parent"]
            and r["parent"] in node_id_set
            and r["id"] in node_id_set
        ]
        # Dendrites: consolidation lineage rendered as distinct link type.
        # Only when both ends are visible in this view.
        links += [
            l for l in consolidation_links
            if l["source"] in node_id_set and l["target"] in node_id_set
        ]

        # degree (backlink count) — the Obsidian "size = connectedness" signal.
        # Counts parent-chain + dendrite links touching each node.
        degree: dict[str, int] = {}
        for l in links:
            degree[l["source"]] = degree.get(l["source"], 0) + 1
            degree[l["target"]] = degree.get(l["target"], 0) + 1

        # dominant project tag per node → grouping + group color
        # Projects come from the USER'S declared tags (~/.cairn/projects.json)
        # — nothing personal is baked in; a fresh vault groups by account.
        PROJECT_TAGS = _atlas_project_tags()
        PROJECT_COLORS = _atlas_project_colors(PROJECT_TAGS)
        # account colors from ~/.cairn/accounts.json (personal handles never ship)
        for _acct, _cfg in ACCOUNTS.items():
            if _cfg.get("color"):
                PROJECT_COLORS.setdefault(_acct, _cfg["color"])
        # origin per node — imported accounts (and someday team streams) are
        # first-class categories: their own hulls, ring sectors, cluster
        # centers, instead of an undifferentiated grey 'import' blob
        acct_map = {r["id"]: (row_get(r, "account") or "") for r in rows}
        for n in graph_nodes:
            n["degree"] = degree.get(n["id"], 0)
            n["orphan"] = n["degree"] == 0
            tags = []
            try:
                tags = json.loads(
                    next((r["tags"] for r in rows if r["id"] == n["id"]), "[]") or "[]")
            except Exception:
                pass
            grp = next((t for t in PROJECT_TAGS if t in tags), None)
            if not grp:
                # untagged node — a session NAMED after a declared project groups
                # there (generic; no personal hints baked in)
                sess_name = (n["session"] or "").lower()
                grp = next((t for t in PROJECT_TAGS if t in sess_name), None)
            if not grp:
                acct = acct_map.get(n["id"]) or ""
                if acct:
                    grp = acct.replace("claude backfill-", "")
            if not grp:
                grp = "import" if "import" in tags else "_none"
            n["group"] = grp
            n["group_color"] = PROJECT_COLORS.get(grp, "#484F58")

        # session color map for cross-session view (stable order)
        sessions_seen = list(dict.fromkeys(
            row_get(r, "session", "") for r in rows
        ))
        SESSION_PALETTE = [
            "#4A9EDB","#C9A227","#D29922","#A371F7",
            "#F85149","#7CA38C","#FF7043","#AB47BC",
        ]
        session_colors = {
            sid: SESSION_PALETTE[i % len(SESSION_PALETTE)]
            for i, sid in enumerate(sessions_seen)
        }

        return {
            "nodes": graph_nodes,
            "links": links,
            "cross": cross,
            "session_colors": session_colors,
            "project_colors": PROJECT_COLORS,
        }

    def _row_to_dict(row) -> dict:
        """Convert sqlite3.Row to plain dict safely.
        Strips the embedding BLOB — raw bytes break JSON serialization
        (this 500'd the node detail panel for every embedded node)."""
        try:
            d = {k: row[k] for k in row.keys()}
        except Exception:
            d = dict(row)
        if "embedding" in d:
            d["has_embedding"] = d["embedding"] is not None
            del d["embedding"]
        return d

    @app.get("/api/node/{node_id}")
    def api_node(node_id: str):
        row = vault.get(node_id)
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        chain = vault.chain(node_id)

        # connections — the backlinks panel: every typed edge touching this
        # node, strongest first, with enough gist to decide whether to hop
        conns = []
        for e in vault.conn.execute(
                "SELECT src, dst, type, tier, weight FROM edges "
                "WHERE src = ? OR dst = ? "
                "ORDER BY CASE tier WHEN 'strong' THEN 0 WHEN 'medium' THEN 1 "
                "WHEN 'weak' THEN 2 ELSE 3 END, weight DESC LIMIT 40",
                (node_id, node_id)):
            other = e["dst"] if e["src"] == node_id else e["src"]
            nb = vault.conn.execute(
                "SELECT id, kind, gist, query, community FROM nodes "
                "WHERE id = ? AND status = 'active'", (other,)).fetchone()
            if nb:
                conns.append({
                    "id":     nb["id"],
                    "kind":   nb["kind"],
                    "gist":   nb["gist"] or (nb["query"] or "")[:80],
                    "topic":  (nb["community"] or "").partition("|")[2],
                    "type":   e["type"],
                    "tier":   e["tier"],
                    "weight": e["weight"],
                })

        # thread — for conversation turns the unit of meaning is the
        # conversation, not the utterance: previous/next turns, clickable
        def _mini(r):
            if not r:
                return None
            return {"id": r["id"], "kind": r["kind"],
                    "speaker": r["speaker"],
                    "gist": r["gist"] or (r["query"] or "")[:100]}
        prev_row = vault.get(row["parent"]) if row["parent"] else None
        nxt = vault.conn.execute(
            "SELECT * FROM nodes WHERE parent = ? AND status = 'active' "
            "LIMIT 3", (node_id,)).fetchall()

        # receipts — has any model actually been shown / used this memory?
        rc = vault.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cited), 0) FROM attention_ledger "
            "WHERE node_id = ?", (node_id,)).fetchone()

        try:
            topic = (row["community"] or "").partition("|")[2]
        except (KeyError, IndexError):
            topic = ""

        # notes — annotations attached to this node (kind=note, tag 'annotation')
        notes = [{"id": r["id"],
                  "gist": r["gist"] or (r["query"] or "")[:120],
                  "memory": ('"memory"' in (r["tags"] or ""))}
                 for r in vault.conn.execute(
                     "SELECT id, gist, query, tags FROM nodes WHERE parent = ? "
                     "AND status = 'active' AND tags LIKE '%annotation%' "
                     "ORDER BY timestamp ASC LIMIT 12", (node_id,)).fetchall()]

        return {
            "node":        _row_to_dict(row),
            "chain":       [_row_to_dict(r) for r in chain],
            "connections": conns,
            "notes":       notes,
            "thread":      {"prev": _mini(prev_row),
                            "next": [_mini(r) for r in nxt]},
            "receipts":    {"shown": rc[0] or 0, "cited": rc[1] or 0},
            "topic":       topic,
        }

    @app.post("/api/rebuild")
    def api_rebuild():
        """Recompute embeddings + communities + atlas coordinates so freshly
        captured nodes settle into their real clusters. Runs cairn as isolated
        subprocesses; the vault's WAL + busy_timeout keep concurrent captures
        safe — a hook write just waits for the lock, never lost."""
        import subprocess, sys as _sys
        root = str(Path(__file__).resolve().parent.parent)
        for sub in ("embed", "edges"):
            try:
                r = subprocess.run(
                    [_sys.executable, "-X", "utf8", "-m", "cairn", sub],
                    cwd=root, capture_output=True, text=True, timeout=600)
            except Exception as e:
                return JSONResponse({"ok": False, "step": sub, "error": str(e)},
                                    status_code=500)
            if r.returncode != 0:
                return JSONResponse(
                    {"ok": False, "step": sub,
                     "error": (r.stderr or r.stdout or "")[-400:]},
                    status_code=500)
        return {"ok": True}

    @app.get("/api/sessions")
    def api_sessions():
        # Count ACTIVE nodes only and hide fully-voided sessions — voided
        # test/seed sessions stay archived in the vault but shouldn't
        # clutter the session picker.
        # Imported history is an ARCHIVE, not sessions: 226 backfilled
        # conversations would drown the ~15 real working sessions. The picker
        # gets live sessions; imports roll up to one summary line per account
        # (reachable through search, topics, and the graph — not the picker).
        rows = vault.conn.execute("""
            SELECT s.id, s.started_at,
                   COUNT(n.id) AS node_count
            FROM   sessions s
            JOIN   nodes n ON n.session = s.id AND n.status != 'void'
            WHERE  s.id NOT LIKE 'import-%'
            GROUP  BY s.id
            HAVING node_count > 0
            ORDER  BY s.started_at DESC
            LIMIT  50
        """).fetchall()
        archive = vault.conn.execute("""
            SELECT COALESCE(s.account, 'unlabeled import') AS account,
                   COUNT(DISTINCT s.id) AS sessions,
                   COUNT(n.id)          AS node_count,
                   MIN(substr(s.started_at, 1, 10)) AS first_day,
                   MAX(substr(s.started_at, 1, 10)) AS last_day
            FROM   sessions s
            JOIN   nodes n ON n.session = s.id AND n.status != 'void'
            WHERE  s.id LIKE 'import-%'
            GROUP  BY COALESCE(s.account, 'unlabeled import')
            ORDER  BY node_count DESC
        """).fetchall()
        return {"sessions": [_row_to_dict(r) for r in rows],
                "archive":  [_row_to_dict(r) for r in archive]}

    @app.get("/api/stream")
    async def api_stream(sess: str | None = None):
        """Server-Sent Events — pushes new nodes as they arrive."""
        async def generate():
            # Start from "now" so we stream only NEW nodes, not 64k of history.
            row0 = vault.conn.execute("SELECT MAX(timestamp) m FROM nodes").fetchone()
            last_ts = (row0["m"] if row0 else "") or ""
            # 5a: tool calls no longer become nodes, so the live "tools as they
            # fire" stream comes from the pending_tools buffers. Track how many
            # records we've emitted per buffer file; when a file shrinks (drained
            # at Stop) reset its offset. These events carry live:true and a
            # synthetic id so the client paints them ephemerally, not as graph nodes.
            from cairn import pending as _pending
            tool_offsets: dict = {}
            while True:
                # Follow ALL live activity by timestamp, not one fixed session. The
                # old code locked onto the session current when the stream OPENED, so
                # when the session rolled over (new chat / post-compaction session id)
                # the feed silently stopped and you had to refresh. ?sess= still
                # scopes to a single session if explicitly asked.
                if sess:
                    rows = vault.conn.execute(
                        "SELECT * FROM nodes WHERE session=? AND timestamp>? "
                        "ORDER BY timestamp ASC LIMIT 20", (sess, last_ts)).fetchall()
                else:
                    rows = vault.conn.execute(
                        "SELECT * FROM nodes WHERE timestamp>? "
                        "ORDER BY timestamp ASC LIMIT 20", (last_ts,)).fetchall()
                for row in rows:
                    last_ts = row["timestamp"]
                    data = json.dumps({
                        "id":       row["id"],
                        "kind":     row["kind"],
                        "tool":     row["tool"],
                        "model":    row["model"] or "unknown",
                        "query":    (row["query"] or "")[:80],
                        "status":   row["status"],
                        "flagged":  bool(row["flagged"]),
                        "latency":  row["latency_ms"],
                        "results":  row["result_count"],
                        "tokens_out":        row["tokens_out"],
                        "tokens_cache_read": row["tokens_cache_read"],
                        "ts":       row["timestamp"],
                    })
                    yield f"data: {data}\n\n"

                # ── 5a: live tool stream from the pending_tools buffers ──────
                try:
                    import time as _t
                    pdir = _pending.PENDING_DIR
                    files = sorted(pdir.glob("*.jsonl")) if pdir.exists() else []
                    # Only follow buffers touched in the last 15 min. A session that
                    # ended WITHOUT draining leaves a stale buffer; without this guard
                    # the stream REPLAYS it as "live" on every dashboard load — old
                    # tool calls surfacing as if they're firing now (the confusing case).
                    files = [f for f in files
                             if (_t.time() - f.stat().st_mtime) < 900]
                    live_sessions = ([sess] if sess else
                                     [f.stem for f in files])
                    for sname in live_sessions:
                        recs = _pending.read(sname)
                        if sname not in tool_offsets:
                            # first sight of this buffer: skip its history, stream
                            # only tools that fire AFTER you open the dashboard
                            # (mirrors the node stream's "start from now").
                            tool_offsets[sname] = len(recs)
                            continue
                        seen = tool_offsets[sname]
                        if len(recs) < seen:
                            seen = 0          # buffer drained/rotated — resync
                        for i in range(seen, len(recs)):
                            tc = recs[i]
                            yield "data: " + json.dumps({
                                "id":      f"live:{sname}:{i}",
                                "kind":    "tool_call",
                                "tool":    tc.get("tool"),
                                "model":   "live",
                                "query":   (tc.get("query") or "")[:80],
                                "status":  "active",
                                "flagged": False,
                                "latency": tc.get("latency_ms"),
                                "results": tc.get("result_count"),
                                "tokens_out": None, "tokens_cache_read": None,
                                "ts":      tc.get("ts") or "",
                                "live":    True,
                            }) + "\n\n"
                        tool_offsets[sname] = len(recs)
                except Exception:
                    pass  # the live tool stream is a nicety — never break the SSE
                await asyncio.sleep(1.0)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/feed")
    def api_feed(limit: int = 40):
        """Recent feed history so the live feed isn't blank on (re)load — the client
        paints these (oldest-first), then streams new ones on top via SSE."""
        n = max(1, min(limit, 120))
        rows = vault.conn.execute(
            "SELECT * FROM nodes WHERE status != 'void' ORDER BY timestamp DESC LIMIT ?",
            (n,)).fetchall()
        out = [{
            "id": r["id"], "kind": r["kind"], "tool": r["tool"],
            "model": r["model"] or "unknown", "query": (r["query"] or "")[:80],
            "status": r["status"], "flagged": bool(r["flagged"]),
            "latency": r["latency_ms"], "results": r["result_count"],
            "tokens_out": r["tokens_out"], "tokens_cache_read": r["tokens_cache_read"],
            "ts": r["timestamp"],
        } for r in reversed(rows)]   # oldest first → client stacks newest on top
        return JSONResponse(out)

    @app.get("/", response_class=HTMLResponse)
    def index():
        # no-store: the dashboard iterates fast; a cached page makes every
        # shipped fix look broken ("did it change?") — never cache the shell
        # inject the account config (personal handles never ship in source);
        # the .replace('</','<\\/') closes the <script> breakout hole
        html = DASHBOARD_HTML.replace(
            "/*__CAIRN_ACCOUNTS__*/{}",
            json.dumps(ACCOUNTS, ensure_ascii=True).replace("</", "<\\/"))
        return HTMLResponse(html,
                            headers={"Cache-Control": "no-store"})

    # ── launch ───────────────────────────────────────────────────────────────
    if open_browser:
        def _open():
            time.sleep(1.2)
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        threading.Thread(target=_open, daemon=True).start()

    host = "127.0.0.1"  # localhost only - LAN/phone exposure removed
    print(f"cairn dashboard → http://localhost:{port}")

    uvicorn.run(app, host=host, port=port, log_level="warning")


def _current_session() -> str:
    sid = (os.environ.get("CAIRN_SESSION") or
           os.environ.get("CLAUDE_SESSION_ID"))
    if sid:
        return sid
    p = Path.home() / ".cairn" / "last_session.txt"
    if p.exists():
        return p.read_text().strip() or _date_session()
    return _date_session()


def _date_session() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Dashboard HTML — single file, no build step ───────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cairn Remembers — Episodic Agent Memory</title>
<meta name="theme-color" content="#16140E">
<link rel="icon" type="image/png" sizes="192x192" href="/assets/brand/app-icon-192.png">
<link rel="icon" type="image/png" sizes="48x48" href="/assets/brand/favicon-48.png">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/assets/brand/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/app-icon-180.png">
<link rel="manifest" href="/assets/brand/manifest.webmanifest">
<script src="/assets/d3.v7.min.js"></script>
<script>window.CAIRN_ACCOUNTS = /*__CAIRN_ACCOUNTS__*/{};</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, monospace;
    background: #16140E; color: #E8E2D2;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  #header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 20px; background: #1A1811; border-bottom: 1px solid #2C2A21;
    flex-shrink: 0;
  }
  #header h1 { font-size: 14px; font-weight: 600; color: #7CA38C; letter-spacing: 0.05em; }
  #header .subtitle { font-size: 11px; color: #6E7681; margin-left: 12px; }
  #main { display: flex; flex: 1; overflow: hidden; }

  /* LEFT: live feed */
  #feed {
    width: 280px; flex-shrink: 0;
    background: #16140E; border-right: 1px solid #2C2A21;
    overflow-y: auto; padding: 8px;
  }
  #feed h2 { font-size: 11px; color: #6E7681; padding: 6px 4px; text-transform: uppercase; letter-spacing: 0.08em; }
  .feed-item {
    padding: 6px 8px; margin: 2px 0; border-radius: 4px;
    border-left: 3px solid #2C2A21; cursor: pointer;
    transition: background 0.15s;
  }
  .feed-item:hover { background: #1A1811; }
  .feed-item .kind { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
  .feed-item .query { font-size: 11px; color: #968F7D; margin-top: 2px; line-height: 1.3; }
  .feed-item .meta { font-size: 10px; color: #484F58; margin-top: 2px; }
  .feed-item.struggle { border-left-color: #F85149; }
  .feed-item.flagged  { border-left-color: #D29922; }
  .feed-item.stamp    { border-left-color: #A371F7; }
  .feed-item.turn     { border-left-color: #C9A227; }

  /* CENTER: graph */
  #graph-panel {
    flex: 1; position: relative; overflow: hidden;
    background: radial-gradient(ellipse at center, #1A1812 0%, #16140E 100%);
  }
  #graph-svg { width: 100%; height: 100%; }
  #graph-controls {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    display: flex; flex-direction: column; gap: 6px; align-items: center; width: max-content; max-width: 94vw;
    background: rgba(25,23,17,.9); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    border: 1px solid #2C2A21; border-radius: 8px; padding: 8px 12px;
  }
  .ctrl-row { display: flex; gap: 6px; align-items: center; flex-wrap: nowrap; justify-content: center; }
  #graph-controls button {
    background: #1A1811; border: 1px solid #2C2A21; color: #968F7D;
    padding: 5px 11px; border-radius: 6px; font-size: 11px; cursor: pointer;
  }
  #graph-controls button:hover { color: #E8E2D2; border-color: #3A3528; }
  .ctrl-lbl { font-size: 9.5px; color: #6E6757; display: flex; align-items: center; gap: 5px;
    text-transform: uppercase; letter-spacing: .12em; font-family: ui-monospace,'SF Mono',Menlo,Consolas,monospace; }
  .ctrl-lbl select, .session-select {
    appearance: none; -webkit-appearance: none;
    background: #1A1811 url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%2210%22 height=%2210%22><path d=%22M2 3.5l3 3 3-3%22 fill=%22none%22 stroke=%23968F7D stroke-width=%221.3%22/></svg>') no-repeat right 8px center;
    border: 1px solid #2C2A21; color: #E8E2D2; padding: 4px 22px 4px 9px;
    border-radius: 6px; font-size: 11px; cursor: pointer;
  }

  /* RIGHT: detail */
  #detail {
    width: 300px; flex-shrink: 0;
    background: #16140E; border-left: 1px solid #2C2A21;
    overflow-y: auto; padding: 12px;
  }
  #detail h2 { font-size: 11px; color: #6E7681; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px; }
  #detail .empty { color: #484F58; font-size: 12px; }
  .detail-field { margin-bottom: 8px; }
  .detail-label { font-size: 10px; color: #6E7681; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }
  .detail-value { font-size: 12px; color: #E8E2D2; line-height: 1.4; word-break: break-all; }
  .detail-badge {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 600; margin-right: 4px;
  }
  .chain-item {
    padding: 4px 8px; margin: 2px 0; background: #1A1811;
    border-radius: 3px; font-size: 11px; color: #968F7D;
    border-left: 2px solid #3A3528;
  }
  .chain-item .chain-tool { color: #7CA38C; }

  /* BOTTOM: stats bar */
  #statusbar {
    display: flex; align-items: center; gap: 20px;
    padding: 6px 16px; background: #1A1811; border-top: 1px solid #2C2A21;
    flex-shrink: 0; font-size: 11px; color: #6E7681;
  }
  #statusbar .stat { display: flex; align-items: center; gap: 6px; }
  #statusbar .stat-val { color: #E8E2D2; font-weight: 600; }
  #statusbar .context-bar {
    flex: 1; max-width: 200px; height: 4px;
    background: #2C2A21; border-radius: 2px; overflow: hidden;
  }
  #statusbar .context-fill {
    height: 100%; background: #7CA38C; border-radius: 2px;
    transition: width 0.5s;
  }
  #statusbar .inject-bar {
    flex: 1; max-width: 120px; height: 4px;
    background: #2C2A21; border-radius: 2px; overflow: hidden;
  }
  #statusbar .inject-fill {
    height: 100%; background: #C9A227; border-radius: 2px;
    transition: width 0.5s;
  }
  .tier-badge {
    font-size: 10px; padding: 1px 5px; border-radius: 3px;
    font-weight: 600; letter-spacing: 0.3px;
  }

  /* injection pulse — recently surfaced memory glows like cortical activation */
  @keyframes cairn-pulse {
    0%   { stroke-opacity: 0.9; stroke-width: 2px; }
    50%  { stroke-opacity: 0.15; stroke-width: 7px; }
    100% { stroke-opacity: 0.9; stroke-width: 2px; }
  }
  .pulse-halo {
    fill: none; stroke: #7CA38C;
    animation: cairn-pulse 2.4s ease-in-out infinite;
  }
  /* dendrites — consolidation lineage (insight → absorbed episodes) */
  .link.dendrite {
    stroke: #E3B341; stroke-dasharray: 4 3;
    stroke-opacity: 0.55; stroke-width: 1.2px;
  }

  /* tooltip */
  .tooltip {
    position: absolute; background: #1A1811; border: 1px solid #3A3528;
    padding: 6px 10px; border-radius: 4px; font-size: 11px; color: #E8E2D2;
    pointer-events: none; max-width: 200px; line-height: 1.4; z-index: 100;
  }
  .node circle, .node rect, .node polygon { stroke-width: 1.5; cursor: pointer; }
  .link { stroke: #2C2A21; stroke-opacity: 0.8; stroke-width: 1; }
  .link.cross-model { stroke: #C9A227; stroke-opacity: 0.6; stroke-dasharray: 4,2; }
  .node text { font-size: 8px; fill: #6E7681; pointer-events: none; }

  /* ── GraphRAG semantic edges ── */
  /* opacity controlled via D3 attr (not CSS transition) so focus-dimming works per-edge */
  .sem-link { stroke-dasharray: 5,3; stroke-opacity: 0; pointer-events: none; }
  .sem-link.same  { stroke: #D29922; }
  .sem-link.cross { stroke: #7CA38C; }
  /* .session-ring — tag added in renderGraph so applySessionFocus can skip it */
  .btn-rag  { background: rgba(201,162,39,.14) !important; color: #C9A227 !important;
              border-color: rgba(201,162,39,.5) !important; }
  .btn-xsess{ background: rgba(124,163,140,.16) !important; color: #7CA38C !important;
              border-color: rgba(124,163,140,.55) !important; }
  .rag-badge {
    position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%);
    background: rgba(22,27,34,.85); border: 1px solid #3A3528;
    border-radius: 20px; padding: 4px 14px; font-size: 10px; color: #6E7681;
    pointer-events: none; white-space: nowrap; backdrop-filter: blur(4px);
    transition: opacity .3s;
  }
</style>
</head>
<body>

<div id="header">
  <div style="display:flex;align-items:center">
    <h1 style="display:flex;align-items:center;gap:9px;margin:0">
      <img src="/assets/mark-bone.png" alt="" style="width:26px;height:26px;opacity:.96">
      <img src="/assets/wordmark.png" alt="Cairn" style="height:24px;width:auto;opacity:.96">
      <span style="font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;font-size:13px;letter-spacing:.22em;color:#7CA38C;margin-left:2px">Remembers</span>
    </h1>
    <span class="subtitle">Episodic Agent Memory</span>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <select class="session-select" id="session-sel" onchange="loadSession(this.value)">
      <option value="__all__" selected>All sessions</option>
      <option value="">current session</option>
    </select>
    <span style="font-size:11px;color:#484F58" id="last-update">—</span>
    <a href="/garden" title="the human side — your memory, readable"
       style="display:inline-flex;align-items:center">
      <img src="/assets/btn-garden.png" alt="the garden →" style="height:30px;width:auto;display:block"></a>
  </div>
</div>

<div id="main">
  <div id="feed">
    <h2>Live Feed</h2>
    <div id="feed-items"></div>
  </div>

  <div id="graph-panel">
    <svg id="graph-svg"></svg>
    <canvas id="atlas-canvas" style="display:none;position:absolute;top:0;left:0;z-index:30;cursor:grab"></canvas>
    <div id="graph-controls">
      <div class="ctrl-row">
      <input id="graph-q" placeholder="search the vault…" spellcheck="false"
             style="background:#16140E;border:1px solid #3A3528;color:#C9C2B2;border-radius:6px;padding:3px 9px;font-size:11px;width:138px;outline:none"
             oninput="if(!this.value.trim())clearGraphSearch()"
             onkeydown="if(event.key==='Enter')graphSearch();if(event.key==='Escape')clearGraphSearch()">
      <button id="btn-atlas" onclick="toggleAtlas()" title="The Galaxy — every memory in the vault, mapped">Galaxy</button>
      <button id="btn-galaxies" onclick="toggleGalaxyPanel()" title="Each source (GPT / your Claude accounts / live) is its own solar system — show or hide any">Solar Systems ▾</button>
      <label class="ctrl-lbl">Color
        <select id="sel-color" onchange="recolor()">
          <option value="topic" selected>by topic</option>
          <option value="kind">by kind</option>
          <option value="source">by source</option>
          <option value="model">by model</option>
        </select></label>
      <label class="ctrl-lbl">Layout
        <select id="sel-layout" onchange="relayout()">
          <!-- rings (time) + free are the pre-galaxy whole-vault lenses,
               restored 2026-07-08. They can read a bit off next to the galaxy
               until they earn galaxy-awareness — the reason they were parked. -->
          <option value="cluster" selected>cluster</option>
          <option value="rings">rings (time)</option>
          <option value="free">free</option>
        </select></label>
      </div>
      <div class="ctrl-row">
      <button id="btn-recent" onclick="toggleRecent()" title="Cluster view: lift today + this week out to orbit rings — what you're working on lately. Toggle off and they fall home into their clusters.">◎ recent</button>
      <button id="btn-ambient" onclick="toggleAmbient()" title="Ambient: off / twinkle / ripple. Leave the atlas idle (~12s) and it falls asleep — the brain dreams in soft neural firing along your real connections. Nodes never move, so clicking stays exact. Saved to your profile.">✦ off</button>
      <label class="ctrl-lbl">Show
        <select id="sel-filter" onchange="refilter()">
          <option value="all">everything</option>
          <option value="meaning">ideas & decisions</option>
          <option value="open_item">open items</option>
          <option value="idea">ideas</option>
          <option value="conversation_turn">conversations</option>
        </select></label>
      <label class="ctrl-lbl">Time
        <select id="sel-time" onchange="toggleTimeRange();refilter()">
          <option value="all">all time</option>
          <option value="1h">last 1h</option>
          <option value="6h">last 6h</option>
          <option value="12h">last 12h</option>
          <option value="today">today</option>
          <option value="week">this week</option>
          <option value="lastweek">last week</option>
          <option value="month">this month</option>
          <option value="quarter">last 3 months</option>
          <option value="custom">custom range…</option>
        </select></label>
      <span id="time-range" style="display:none;align-items:center;gap:4px">
        <input type="date" id="time-from" onchange="refilter()" title="from"
               style="background:#16140E;border:1px solid #3A3528;color:#C9C2B2;border-radius:5px;font-size:11px;padding:2px 5px">
        <span style="color:#6E7681">→</span>
        <input type="date" id="time-to" onchange="refilter()" title="to"
               style="background:#16140E;border:1px solid #3A3528;color:#C9C2B2;border-radius:5px;font-size:11px;padding:2px 5px">
      </span>
      <span style="position:relative;display:inline-block">
        <button id="btn-links" onclick="toggleLinksPanel()">Links ▾</button>
        <div id="links-panel" style="display:none;position:absolute;top:28px;left:0;background:#1A1811EE;border:1px solid #3A3528;border-radius:8px;padding:10px 14px;font-size:11px;color:#C9C2B2;line-height:2.1;z-index:70;white-space:nowrap">
          <label style="display:block"><input type="checkbox" id="lk-strong" checked onchange="applyLinkLayers()"> <span style="color:#38BDF8">━</span> semantic strong ≥ .78</label>
          <label style="display:block"><input type="checkbox" id="lk-medium" onchange="applyLinkLayers()"> <span style="color:#D877C9">╌</span> semantic medium ≥ .70</label>
          <label style="display:block"><input type="checkbox" id="lk-weak" onchange="applyLinkLayers()"> <span style="color:#968F7D">┈</span> semantic weak ≥ .62</label>
          <label style="display:block"><input type="checkbox" id="lk-chain" onchange="applyLinkLayers()"> <span style="color:#F0B942">━</span> chain — reasoning path</label>
          <label style="display:block"><input type="checkbox" id="lk-dendrite" onchange="applyLinkLayers()"> <span style="color:#50DC64">┄</span> dendrite — consolidation</label>
          <div style="color:#6E7681;margin-top:6px;line-height:1.4;font-style:italic;max-width:190px;white-space:normal">checked types light up when you hover a node — never shown constantly</div>
          <div id="links-counts" style="color:#968F7D;margin-top:4px;line-height:1.4"></div>
        </div>
      </span>
      <button onclick="toggleLabels()">Labels</button>
      <button id="btn-legend" onclick="toggleLegend()" title="What the shapes and colors mean">?</button>
      </div>
    </div>
    <div class="tooltip" id="tooltip" style="display:none"></div>
    <div class="rag-badge" id="rag-badge" style="opacity:0"></div>
    <div id="search-results" style="display:none;position:absolute;top:54px;left:20px;max-width:340px;max-height:55%;overflow-y:auto;background:#1A1811EE;border:1px solid #3A3528;border-radius:8px;padding:8px 10px;font-size:11px;color:#C9C2B2;z-index:200;line-height:1.5;pointer-events:auto"></div>
    <div id="legend" style="display:none;position:absolute;bottom:14px;left:14px;background:#1A1811EE;border:1px solid #3A3528;border-radius:8px;padding:12px 16px;font-size:11px;color:#968F7D;line-height:1.9;z-index:60">
      <div style="color:#E8E2D2;font-weight:600;margin-bottom:4px">Reading the Galaxy</div>
      <div><b>Solar Systems</b> = your sources — each account / model (GPT, your Claude accounts, live work) is its own system</div>
      <div><b>Color</b> = by topic (default): hue groups related ideas. The Color menu re-keys it (kind / source / model)</div>
      <div><b>size</b> = backlinks (connected hubs grow) · faded = few or no links yet</div>
      <div><span style="color:#7CA38C">◌</span> pulsing halo = surfaced into a model's context &lt;24h &nbsp;<span style="color:#F85149">○</span> red ring = struggle &nbsp;<span style="color:#D29922">◌</span> dashed = flagged</div>
      <div><b>Layout</b> arranges · <b>Show</b> filters · <b>Links</b> = hover a node to light its strongest ties</div>
      <div><b>double-click</b> = spotlight neighborhood · click background to clear</div>
    </div>
  </div>

  <div id="detail">
    <h2>Node Detail</h2>
    <div id="detail-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
</div>

<!-- Status bar — humanized (P3): every metric KEPT, presentation only. Plain-
     language labels + a one-sentence title tooltip on every segment; session
     shows a short id + start time (full id in its tooltip); the Embedded
     pending count carries the ○ freshness marker (same vocabulary as the
     Garden: ○ = captured, weaves in at tonight's sleep). -->
<div id="statusbar">
  <div class="stat" id="stat-sess-wrap" title="The conversation this dashboard is watching — memories being written right now.">Session <span class="stat-val" id="stat-sess">—</span></div>
  <div class="stat" title="Memories captured in this session so far.">
    Nodes <span class="stat-val" id="stat-nodes">—</span>
    &nbsp;<span class="tier-badge" id="stat-hot"  title="Hot — pinned front-of-mind memories (whole vault); always offered to the model first." style="background:#4A2B1A;color:#F97316">hot 0</span>
    <span class="tier-badge" id="stat-warm" title="Warm — the working set (whole vault); pushed into the model's context when relevant." style="background:#1A3020;color:#C9A227">warm 0</span>
    <span class="tier-badge" id="stat-cold" title="Cold — the deep archive (whole vault); still searchable, rarely pushed." style="background:#1A1F2E;color:#6E7681">cold 0</span>
  </div>
  <div class="stat" title="Moments this session where the model visibly struggled — slow or near-empty results worth a second look.">Struggles <span class="stat-val" id="stat-str">—</span></div>
  <div class="stat" title="Rough size of this session's captured text, in model tokens (chars ÷ 4).">~Tokens <span class="stat-val" id="stat-tok">—</span></div>
  <div class="stat" style="flex:1" title="How much of the model's context window this session's memory would fill.">
    Context
    <div class="context-bar"><div class="context-fill" id="ctx-bar" style="width:0%"></div></div>
    <span id="ctx-pct" style="color:#E8E2D2">0%</span>
  </div>
  <div class="stat" title="Memories pushed into the model's context this session / the gate's cap.">
    Inject gate
    <div class="inject-bar"><div class="inject-fill" id="inj-bar" style="width:0%"></div></div>
    <span id="inj-pct" style="color:#E8E2D2">0/40</span>
  </div>
  <div class="stat" title="Of memories shown to the model, the share it actually used; underattended = shown but never used.">
    Attention <span class="stat-val" id="stat-attn">—</span>
  </div>
  <div class="stat" title="Everything the vault holds, across every session and model — nothing here is ever deleted.">Vault <span class="stat-val" id="stat-total">—</span> memories · <span class="stat-val" id="stat-sessions">—</span> sessions</div>
  <div class="stat" title="New memories not yet woven into search — the nightly sleep embeds them.">Embedded <span class="stat-val" id="stat-emb">—</span><span id="stat-emb-pend" style="color:#C9A227;display:none"></span></div>
</div>

<script>
const API = '';
let simulation, svg, g, showLabels = true;
let currentGraph = {nodes: [], links: []};
let currentSession = '';
// ── GraphRAG state ────────────────────────────────────────────────────────────
let showSemantic     = false;
let showCrossSession = false;
let semanticEdges    = [];
let semLinkSel       = null;   // D3 selection — updated every tick
let semSnapshot      = null;   // node positions before the sem force moved them
let layerEdges       = null;   // typed/tiered edges from /api/edges (lazy)
let currentAccount   = '';     // non-empty = viewing an imported archive
let layerSel         = null;   // rendered tier lines — force-free overlay
let topicPalette     = null;   // community id -> color (top-N in current view)
let topicLabels      = {};     // community id -> human label
let sessionColorMap  = {};     // session_id → colour (for cross-session mode)
// focusedSession: when GraphRAG is active, the dropdown acts as a *focus lens*
// (dims other sessions' nodes) rather than a full graph reload.
// Empty string = no focus / show all sessions equally.
let focusedSession   = '';

// ── Status bar ────────────────────────────────────────────────────────────────
async function updateStatus() {
  try {
    const [s, inj] = await Promise.all([
      fetch(API + '/api/status').then(r => r.json()),
      fetch(API + '/api/inject_state').then(r => r.json()).catch(() => ({})),
    ]);
    // Session — short id + start time; the FULL id lives in the tooltip.
    // (Humanized P3: a human reads "when did this start", not a uuid tail.)
    const sessShort = (s.session || '').slice(0, 8);
    let sessStart = '';
    if (s.session_started) {
      try {
        const sd = new Date(s.session_started);
        sessStart = ' · ' + (new Date().toDateString() === sd.toDateString()
          ? 'started ' + sd.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})
          : 'started ' + sd.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}));
      } catch(e) {}
    }
    document.getElementById('stat-sess').textContent = sessShort + sessStart;
    const sessWrap = document.getElementById('stat-sess-wrap');
    if (sessWrap) sessWrap.title =
      'The conversation this dashboard is watching — memories being written right now. Full id: ' + s.session;
    document.getElementById('stat-nodes').textContent = s.session_nodes;
    document.getElementById('stat-str').textContent   = s.struggles;
    document.getElementById('stat-tok').textContent   = s.token_est.toLocaleString();
    document.getElementById('stat-total').textContent = s.vault_total.toLocaleString();
    document.getElementById('stat-sessions').textContent = s.vault_sessions;
    document.getElementById('stat-emb').textContent   = s.embedded + '/' + s.session_nodes;
    // ○ = captured but not yet woven in (same freshness vocabulary as the
    // Garden) — shows only while something is pending; the nightly sleep clears it.
    const pend = Math.max(0, (s.session_nodes || 0) - (s.embedded || 0));
    const pendEl = document.getElementById('stat-emb-pend');
    if (pendEl) {
      pendEl.style.display = pend > 0 ? '' : 'none';
      pendEl.textContent = pend > 0 ? ' ○ ' + pend + ' pending' : '';
    }

    // Tier count badges — plain words, not initials (humanized P3)
    document.getElementById('stat-hot').textContent  = 'hot '  + (s.tier_hot  || 0).toLocaleString();
    document.getElementById('stat-warm').textContent = 'warm ' + (s.tier_warm || 0).toLocaleString();
    document.getElementById('stat-cold').textContent = 'cold ' + (s.tier_cold || 0).toLocaleString();

    // Attention efficiency — the EEG line (golden-angle feedback loop)
    const at = s.attention || {};
    const attnEl = document.getElementById('stat-attn');
    if (at.total_loads > 0) {
      const pctEff = Math.round(at.efficiency * 100);
      attnEl.textContent = pctEff + '%' +
        (at.underattended > 0 ? ' (' + at.underattended + ' underattended)' : '');
      attnEl.style.color = pctEff >= 40 ? '#C9A227' : pctEff >= 15 ? '#D29922' : '#F85149';
    } else {
      attnEl.textContent = 'warming up';
    }

    // Context budget: vault memory footprint vs model's actual context window
    const ctxWindow = s.context_window || 200000;
    const ctxLabel  = ctxWindow >= 1000000 ? '1M' : Math.round(ctxWindow/1000) + 'k';
    const pct = Math.min(100, Math.round(s.token_est / ctxWindow * 100));
    document.getElementById('ctx-bar').style.width = pct + '%';
    document.getElementById('ctx-pct').textContent = pct + '% of ' + ctxLabel;
    document.getElementById('ctx-bar').style.background =
      pct > 80 ? '#F85149' : pct > 50 ? '#D29922' : '#7CA38C';

    // Inject gate gauge (warm blocks sent vs WARM_INJECT_LIMIT)
    const warmSent  = inj.warm_blocks_sent  || 0;
    const warmLimit = inj.warm_blocks_limit || 40;
    const warmPct   = Math.min(100, Math.round(warmSent / warmLimit * 100));
    document.getElementById('inj-bar').style.width = warmPct + '%';
    document.getElementById('inj-pct').textContent = warmSent + '/' + warmLimit;
    document.getElementById('inj-bar').style.background =
      warmPct > 90 ? '#F85149' : warmPct > 65 ? '#D29922' : '#C9A227';

    document.getElementById('last-update').textContent =
      'updated ' + new Date().toLocaleTimeString();
  } catch(e) {}
}

// ── Importance heat coloring ──────────────────────────────────────────────────

function importanceColor(imp) {
  // 1-4: cool blue | 5-6: amber | 7-8: orange | 9-10: red
  const i = imp || 5;
  if (i >= 9) return '#F85149';   // critical — red
  if (i >= 7) return '#FF7B29';   // high — orange
  if (i >= 5) return '#D29922';   // medium — amber
  return '#4A9EDB';                // low — blue
}

// ── Unified color system — ONE function owns every fill ──────────────────────
// Fixes the "everything green" bug: conversation_turns no longer hardcode a
// color. The Color dropdown picks the axis; nothing else writes fill.
const KIND_COLORS = {
  decision:'#4A9EDB', warning:'#F85149', open_item:'#D29922', insight:'#E3B341',
  procedure:'#2EA043', idea:'#A371F7', hypothesis:'#7C5CBF', question:'#5E6B78',
  resolved:'#C9A227', conversation_turn:'#39506B', context_stamp:'#A371F7',
  blocker:'#F85149', tool_call:'#484F58', interrupt:'#3A4048',
};
const MODEL_COLORS = {
  'claude':'#4A9EDB', 'gpt':'#10A37F', 'llama':'#F97316',
  'gemini':'#EA4335', 'human':'#E3B341', 'cairn':'#A371F7',
};
function nodeFill(d) {
  if (d.consolidated) return '#E3B341';
  const mode = document.getElementById('sel-color').value;
  if (mode === 'kind')       return KIND_COLORS[d.kind] || '#6E7681';
  if (mode === 'topic')      return topicColorFor(d);
  if (mode === 'project')    return d.group === '_none' ? '#3A4048' : d.group_color;
  if (mode === 'tier')       return ['#F97316','#4A9EDB','#3A4048'][d.memory_tier ?? 1];
  if (mode === 'importance') return importanceColor(d.importance);
  if (mode === 'degree') {
    const dg = d.degree || 0;
    return dg === 0 ? '#3A4048' : dg >= 4 ? '#E3B341' : dg >= 2 ? '#C9A227' : '#4A9EDB';
  }
  if (mode === 'model') {
    const m = (d.model || '').toLowerCase();
    for (const k in MODEL_COLORS) if (m.includes(k)) return MODEL_COLORS[k];
    return '#6E7681';
  }
  return d.color;
}
function visualColor(d) { return nodeFill(d); }  // back-compat alias

// ── Time lens — today / this week / last week / this month / quarter ─────────
// One clock for both worlds: the SVG graph filters by it in applyView, the
// atlas skips out-of-range nodes in draw. Humans think in "when", not tiers.
function timeOk(ts) {
  const mode = document.getElementById('sel-time');
  const m = mode ? mode.value : 'all';
  if (m === 'all') return true;
  if (!ts) return false;
  const t = (typeof ts === 'number') ? ts : new Date(ts).getTime();
  if (!isFinite(t)) return false;
  const now = Date.now(), DAY = 86400000, HR = 3600000;
  if (m === '1h')  return t >= now - HR;
  if (m === '6h')  return t >= now - 6 * HR;
  if (m === '12h') return t >= now - 12 * HR;
  if (m === 'today') {
    const mid = new Date(); mid.setHours(0, 0, 0, 0);
    return t >= mid.getTime();
  }
  if (m === 'week')     return t >= now - 7  * DAY;
  if (m === 'lastweek') return t >= now - 14 * DAY && t < now - 7 * DAY;
  if (m === 'month')    return t >= now - 30 * DAY;
  if (m === 'quarter')  return t >= now - 90 * DAY;
  if (m === 'custom') {
    const f = document.getElementById('time-from'), to = document.getElementById('time-to');
    const fv = (f && f.value) ? new Date(f.value + 'T00:00:00').getTime() : -Infinity;
    const tv = (to && to.value) ? new Date(to.value + 'T23:59:59').getTime() : Infinity;
    return t >= fv && t <= tv;
  }
  return true;
}
function toggleTimeRange() {   // show the from→to inputs only for the 'custom' range
  const el = document.getElementById('time-range'); if (!el) return;
  el.style.display = (document.getElementById('sel-time').value === 'custom') ? 'inline-flex' : 'none';
}

function recolor() {
  saveProfile();
  if (atlasOn) { drawAtlas(); return; }   // dropdown repaints the atlas live
  applyView();
}
function relayout() {
  saveProfile();
  // in the atlas, Layout switches GEOMETRY — same full vault, new shape.
  // cluster = galaxy, rings = tier orbits, free = one whole-vault sunflower
  if (atlasOn) {
    atlasGridMode = null;
    atlasFit(document.getElementById('atlas-canvas'));
    atlasReveal(850);              // re-fill artistically into the new shape
    const m = document.getElementById('sel-layout').value;
    setBadge(m === 'rings'
      ? 'tier orbits — topic sectors inside each band · every node'
      : m === 'stack'
      ? 'the stack — hot disc on top, warm mid, cold wide below · drag + zoom'
      : m === 'free'
      ? 'one sunflower — the whole vault on a single spiral, importance inward'
      : 'the galaxy — topics on the golden spiral');
    return;
  }
  applyView();
}
function refilter() {
  saveProfile();
  if (atlasOn) { drawAtlas(); return; }   // time lens repaints the atlas too
  applyView();
}

// ── ONE orchestrator — color, layout, filter always reconcile together ───────
// Every dropdown calls this. Order is fixed: filter (what's visible) →
// color (fill the visible) → layout (arrange) → hulls (last, once). No
// combination can glitch because nothing runs in isolation.
const MEANING_KINDS_SET = new Set(['decision','warning','open_item','insight',
  'idea','hypothesis','procedure','resolved','question','blocker']);

function applyView() {
  if (!g || !simulation) return;
  const colorMode  = document.getElementById('sel-color').value;
  const layoutMode = document.getElementById('sel-layout').value;
  const filterMode = document.getElementById('sel-filter').value;

  const pass = d => timeOk(d.timestamp) && (
    filterMode === 'all'     ? true :
    filterMode === 'meaning' ? MEANING_KINDS_SET.has(d.kind) :
    d.kind === filterMode);

  // 1. filter — visibility
  g.selectAll('.node').style('display', d => pass(d) ? null : 'none');
  g.selectAll('.link').style('display', function(l) {
    const s = l.source, t = l.target;
    const sd = (typeof s === 'object') ? s : null;
    const td = (typeof t === 'object') ? t : null;
    return (sd && td && pass(sd) && pass(td)) ? null : 'none';
  });

  // 2. color — fill every node (hidden ones too; cheap, keeps state clean)
  g.selectAll('.node').each(function(d) {
    d3.select(this).selectAll('circle,rect,polygon').attr('fill', nodeFill(d));
  });

  // 3. layout — positional forces, mutually exclusive
  const panel = document.getElementById('graph-panel');
  const w = panel.clientWidth, h = panel.clientHeight, base = Math.min(w, h);
  simulation.force('radial', null).force('gx', null).force('gy', null);
  if (layoutMode === 'rings') {
    simulation.force('radial', d3.forceRadial(
      d => [base*0.08, base*0.26, base*0.42][d.memory_tier ?? 1], w/2, h/2).strength(0.8));
  } else if (layoutMode === 'stack') {
    // SVG twin of the atlas stack: tier bands — hot floats high, cold sinks
    const Y = [h * 0.20, h * 0.50, h * 0.80];
    simulation.force('gy', d3.forceY(
        d => Y[Math.min(2, Math.max(0, d.memory_tier ?? 1))]).strength(0.45))
              .force('gx', d3.forceX(w / 2).strength(0.04));
  } else if (layoutMode === 'cluster') {
    // Phyllotaxis anchoring: group centers sit on a golden-angle spiral —
    // the same PHI_INV_SQ that schedules context positions. Sunflower
    // packing: maximal spread, no overlap, and deterministic (sorted keys),
    // so the map's geography stays stable instead of re-scrambling.
    const PHI_INV_SQ = 0.3819660112501051;
    const gkey = clusterKey();
    const groups = [...new Set(currentGraph.nodes.map(gkey))].sort();
    const centers = {};
    const maxI = Math.max(1, groups.length - 1);
    groups.forEach((grp, i) => {
      const a = i * 2 * Math.PI * PHI_INV_SQ;
      const r = 0.10 + 0.26 * Math.sqrt(i / maxI);
      centers[grp] = [w/2 + Math.cos(a) * w * r, h/2 + Math.sin(a) * h * r];
    });
    simulation.force('gx', d3.forceX(d => (centers[gkey(d)] || [w/2, h/2])[0]).strength(0.16))
              .force('gy', d3.forceY(d => (centers[gkey(d)] || [w/2, h/2])[1]).strength(0.16));
  }
  simulation.alpha(0.6).restart();

  // 4. hulls — last, once
  updateHulls();
  setTimeout(updateHulls, 500);
}

// ── Cluster hulls — the Obsidian/Graphify "group blob" look ───────────────────
let hullG = null;
function updateHulls() {
  if (!g) return;
  if (!hullG) hullG = g.insert('g', ':first-child').attr('class', 'hulls');
  const colorMode = document.getElementById('sel-color').value;
  const showHulls = document.getElementById('sel-layout').value === 'cluster'
                 && (colorMode === 'project' || colorMode === 'topic');
  if (!showHulls) { hullG.selectAll('*').remove(); return; }
  const topicMode = colorMode === 'topic';
  const gkey = clusterKey();
  const hullColor = grp => topicMode
    ? (topicPalette || {})[grp] || '#484F58'
    : (currentGraph.projectColors || {})[grp] || '#484F58';
  const byGroup = {};
  g.selectAll('.node').each(function(d) {
    if (this.style.display === 'none') return;
    const k = gkey(d);
    if (k === '_none' || k === '_misc') return;
    (byGroup[k] = byGroup[k] || []).push([d.x, d.y]);
  });
  const data = Object.entries(byGroup).filter(([_, pts]) => pts.length >= 3);
  const sel = hullG.selectAll('path').data(data, d => d[0]);
  sel.exit().remove();
  sel.enter().append('path').merge(sel)
    .attr('d', ([grp, pts]) => {
      const hull = d3.polygonHull(pts);
      return hull ? 'M' + hull.join('L') + 'Z' : '';
    })
    .attr('fill', ([grp]) => hullColor(grp))
    .attr('fill-opacity', 0.07)
    .attr('stroke', ([grp]) => hullColor(grp))
    .attr('stroke-opacity', 0.25).attr('stroke-width', 1.5)
    .attr('stroke-linejoin', 'round');
}

// ── Topic communities (cairn edges build) — palette + cluster keys ────────────
const TOPIC_PALETTE = ['#4A9EDB','#C9A227','#E3B341','#A371F7','#F97316','#7CA38C',
                       '#EC6CB9','#9CCC65','#FF7043','#AB47BC','#26A69A','#D4A72C'];
function ensureTopicPalette() {
  if (topicPalette) return;
  const counts = {};
  (currentGraph.nodes || []).forEach(n => {
    if (n.community) counts[n.community] = (counts[n.community] || 0) + 1;
  });
  topicPalette = {}; topicLabels = {};
  Object.keys(counts).sort((a, b) => counts[b] - counts[a])
    .slice(0, TOPIC_PALETTE.length)
    .forEach((c, i) => topicPalette[c] = TOPIC_PALETTE[i]);
  (currentGraph.nodes || []).forEach(n => {
    if (n.community && n.topic) topicLabels[n.community] = n.topic;
  });
}
function topicColorFor(d) {
  ensureTopicPalette();
  return (d.community && topicPalette[d.community]) || '#3A4048';
}
function clusterKey() {
  if (document.getElementById('sel-color').value !== 'topic') return d => d.group;
  ensureTopicPalette();
  return d => (d.community && topicPalette[d.community]) ? d.community : '_misc';
}

// ── Link layers — typed/tiered edge overlays, ZERO force ─────────────────────
// Checkboxes pick which connective tissue is visible. Nothing here moves a
// node: space is stable, lenses change.
const TIER_STYLE = {
  strong: {stroke: '#7CA38C', width: 1.6, dash: null,  op: 0.55},
  medium: {stroke: '#968F7D', width: 1.1, dash: '6,4', op: 0.45},
  weak:   {stroke: '#6E7681', width: 0.8, dash: '2,4', op: 0.30},
};

// ── Popover close-on-outside-click — shared by the Links + Solar Systems panels
// (both used to stay open when you clicked away). One-shot, self-removing. ──
let _outsideClosers = {};
function closeOnOutside(panelId, triggerId) {
  if (_outsideClosers[panelId]) { document.removeEventListener('mousedown', _outsideClosers[panelId]); }
  const h = (e) => {
    const p = document.getElementById(panelId);
    if (p && p.contains(e.target)) return;                        // inside the panel — keep open
    if (triggerId && e.target.closest && e.target.closest('#' + triggerId)) {
      document.removeEventListener('mousedown', h); delete _outsideClosers[panelId]; return;  // trigger toggles itself
    }
    if (p) p.style.display = 'none';
    document.removeEventListener('mousedown', h); delete _outsideClosers[panelId];
  };
  _outsideClosers[panelId] = h;
  setTimeout(() => document.addEventListener('mousedown', h), 0);
}
function toggleLinksPanel() {
  const p = document.getElementById('links-panel');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  if (open) closeOnOutside('links-panel', 'btn-links');
}

async function loadLayerEdges() {
  const url = currentAccount
    ? API + '/api/edges?account=' + encodeURIComponent(currentAccount)
    : showCrossSession
    ? API + '/api/edges?cross=true'
    : (currentSession ? API + '/api/edges?sess=' + encodeURIComponent(currentSession)
                      : API + '/api/edges');
  try {
    const data = await fetch(url).then(r => r.json());
    layerEdges = data.edges || [];
    const c = data.counts || {};
    document.getElementById('links-counts').textContent =
      `in view: ${c.chain||0} chain · ${c.strong||0}/${c.medium||0}/${c.weak||0} s/m/w`;
  } catch (e) { layerEdges = []; }
}

// ── Hover focus — when many line layers are on, hovering a node makes ITS
// edges pop and fades the rest. The cure for spaghetti: you read the graph
// one neighborhood at a time, by pointing at it.
function _touches(id) {
  return l => (l.source.id || l.source) === id || (l.target.id || l.target) === id;
}

function highlightEdges(id) {
  if (!g) return;
  const t = _touches(id);
  g.selectAll('.link').style('opacity', l => t(l) ? 1 : 0.05);
  if (layerSel) layerSel
    .attr('stroke-opacity', d => t(d) ? 0.95 : 0.04)
    .attr('stroke-width',   d => TIER_STYLE[d.tier].width + (t(d) ? 0.9 : 0));
  if (semLinkSel && semLinkSel.size() > 0)
    semLinkSel.attr('stroke-opacity', d => t(d) ? 0.9 : 0.04);
}

function unhighlightEdges() {
  if (!g) return;
  g.selectAll('.link').style('opacity', searchActive ? 0.12 : null);
  if (layerSel) layerSel
    .attr('stroke-opacity', d => TIER_STYLE[d.tier].op)
    .attr('stroke-width',   d => TIER_STYLE[d.tier].width);
  if (semLinkSel && semLinkSel.size() > 0) {
    semLinkSel.attr('stroke-opacity', d => {
      if (!focusedSession) return 0.5;
      const srcIn = d.source.session === focusedSession;
      const tgtIn = d.target.session === focusedSession;
      return (srcIn && tgtIn) ? 0.70 : (srcIn || tgtIn) ? 0.55 : 0.03;
    });
  }
}

// ── Atlas — the full-vault map ────────────────────────────────────────────────
// Every node, one canvas, zero physics. Positions are precomputed server-side
// (fractal phyllotaxis) and STABLE across sessions — unlike force layouts,
// the map never re-scrambles, so you can learn your vault's geography.
let atlasOn = false, atlasData = null, atlasIdx = null, atlasGrid = null;
let atlasDeg = {};   // strong-edge degree per node — powers "by backlinks"
let atlasLabelsOn = false;   // Labels button toggles topic names in the atlas (default OFF)
let atlasPosCache = {};    // layout mode -> {id: [x,y]} — geometry, no physics
let atlasGridMode = null;  // which mode the hover grid was built for
let recencyOn = false, recencyAnim = 0, recencyRAF = null;   // ◎ recent lens (cluster)
let ambientMode = 'off', ambientT = 0, ambientRAF = null, ambientLast = 0;   // ✦ ambient motion (profile)
let neuralPulses = [], neuralFlares = [], neuralLastIg = 0, atlasAdj = null;  // ✦ neural firing
const NEURAL_CAP = 90;
let idleTimer = null, asleep = false; const IDLE_MS = 12000;   // walk away ~12s → the brain dreams (neural)
let rippleDrops = [], rippleLastDrop = 0, rippleSysIdx = 0;     // ripple = expanding rings, one per solar system (round-robin)
const NEURAL_HUE = {c: [44, 88], d: [131, 64], s: [199, 90], m: [308, 55], w: [214, 12]};  // pulse color = link type (Links legend)

// ── Atlas layouts — same 7,400 nodes, different geometry, zero physics ───────
// cluster = the galaxy (server phyllotaxis) · rings = tier orbits (hot core,
// warm belt, cold rim) · free = one whole-vault sunflower, importance inward
// ── Galaxy separation: lay each source's nodes around its OWN center so the
// galaxies read as distinct worlds (per-account, GPT, …), with cross-source
// edges spanning between them as bridges. Wraps whatever the layout mode makes
// (cluster/rings/…) — just translates each galaxy into place. Cached per
// (mode + which galaxies are shown); recomputed only when those change.
let atlasGalaxySeparate = false;    // separation is BAKED into map_x/map_y (compute_atlas, server-side); client galaxify off
let atlasGalaxyMeta = [];           // [{key,x,y,r,n}] — galaxy label anchors

function atlasGalaxify(raw) {
  const groups = {};
  for (const n of atlasData.nodes) {
    const k = atlasGalaxyKey(n);
    if (atlasGalaxiesOff.has(k)) continue;
    const b = raw[n.id]; if (!b) continue;
    (groups[k] || (groups[k] = [])).push(n.id);
  }
  const keys = Object.keys(groups).sort();
  const P = {}; atlasGalaxyMeta = [];
  if (keys.length === 0) return P;
  const meta = {};
  for (const k of keys) {
    const ids = groups[k]; let sx = 0, sy = 0;
    for (const id of ids) { const b = raw[id]; sx += b[0]; sy += b[1]; }
    const cx = sx / ids.length, cy = sy / ids.length;
    let r = 1; for (const id of ids) { const b = raw[id]; const d = Math.hypot(b[0]-cx, b[1]-cy); if (d > r) r = d; }
    meta[k] = { cx, cy, r };
  }
  if (keys.length === 1) {                       // one galaxy → just re-center it
    const k = keys[0], m = meta[k];
    for (const id of groups[k]) { const b = raw[id]; P[id] = [b[0]-m.cx, b[1]-m.cy]; }
    atlasGalaxyMeta = [{ key: k, x: 0, y: 0, r: m.r, n: groups[k].length }];
    return P;
  }
  const maxR = Math.max(...keys.map(k => meta[k].r));
  const ringR = maxR * Math.max(2.0, keys.length * 0.8);   // generous so planets clearly separate
  keys.forEach((k, i) => {
    const ang = (i / keys.length) * 2 * Math.PI - Math.PI / 2;
    const gx = ringR * Math.cos(ang), gy = ringR * Math.sin(ang), m = meta[k];
    for (const id of groups[k]) { const b = raw[id]; P[id] = [b[0]-m.cx+gx, b[1]-m.cy+gy]; }
    atlasGalaxyMeta.push({ key: k, x: gx, y: gy, r: m.r, n: groups[k].length });
  });
  return P;
}

function atlasPositions() {
  const raw = atlasPositionsRaw();
  if (!atlasGalaxySeparate) return raw;
  const mode = document.getElementById('sel-layout').value;
  const sig = mode + '|' + [...atlasGalaxiesOff].sort().join(',') + '|' + (recencyOn ? 'r' : '');
  if (atlasPosCache._gxy && atlasPosCache._gxySig === sig) return atlasPosCache._gxy;
  const P = atlasGalaxify(raw);
  atlasPosCache._gxy = P; atlasPosCache._gxySig = sig;
  return P;
}

function atlasPositionsRaw() {
  const mode = document.getElementById('sel-layout').value;
  if (mode !== 'cluster' && atlasPosCache[mode]) return atlasPosCache[mode];
  const GA = 2 * Math.PI * 0.3819660112501051;
  const P = {};
  if (mode === 'rings') {
    // TREE RINGS — memory the way it actually grew. Radius = time (oldest
    // at the core, newest at the rim, like a tree). Angle = each topic's
    // PERMANENT compass bearing (hashed from its id). A long-lived topic
    // becomes a radial streak core-to-rim; an abandoned idea is a short arc
    // near the center; the current obsession lights only the outer edge.
    let t0 = Infinity, t1 = -Infinity;
    for (const n of atlasData.nodes) {
      if (n.ts) { if (n.ts < t0) t0 = n.ts; if (n.ts > t1) t1 = n.ts; }
    }
    const span = Math.max(1, t1 - t0);
    // GROW with the vault: the outer radius scales with sqrt(node count) so the
    // ring band physically expands as the brain grows (incl. bulk GPT import)
    // — density stays ~constant instead of crushing older data into the core.
    // ~2700 at today's ~7.5k nodes, then grows; matches the galaxy's sqrt area.
    const R0 = 260, R1 = Math.max(2700, 30 * Math.sqrt(atlasData.nodes.length));
    atlasPosCache._ringsMeta = {t0: t0, t1: t1, R0: R0, R1: R1};
    const bearing = {};
    for (const n of atlasData.nodes) {
      const cid = n.cid || n.id;
      if (!(cid in bearing)) {
        let h = 0;
        for (let i = 0; i < cid.length; i++) h = (h * 31 + cid.charCodeAt(i)) >>> 0;
        bearing[cid] = (h % 3600) / 3600 * 2 * Math.PI;
      }
      const u = n.ts ? Math.min(1, Math.max(0, (n.ts - t0) / span)) : 0;
      const r = R0 + (R1 - R0) * u;     // linear: the newest gets the most room
      let hj = 0;
      for (let i = 0; i < n.id.length; i++) hj = (hj * 33 + n.id.charCodeAt(i)) >>> 0;
      const a = bearing[cid] + ((hj % 1000) / 1000 - 0.5) * 0.45;
      P[n.id] = [r * Math.cos(a), r * Math.sin(a)];
    }
  } else if (mode === 'stack') {
    // 2.5D: the three tiers as tilted phyllotaxis discs stacked in space —
    // hot floats on top, cold spreads wide at the bottom
    const cnt = [0, 0, 0], idx = [0, 0, 0];
    for (const n of atlasData.nodes)
      cnt[Math.min(2, Math.max(0, n.tr ?? 1))]++;
    // GROW with the vault: scale the tier discs + their vertical spacing by
    // sqrt(N/8000) so the stack expands as the brain grows (1x at ~today).
    const gf = Math.max(1, Math.sqrt(atlasData.nodes.length / 8000));
    const RAD = [950*gf, 1600*gf, 2700*gf], ZOFF = [-1500*gf, 0, 1600*gf], TILT = 0.42;
    for (const n of atlasData.nodes) {
      const t = Math.min(2, Math.max(0, n.tr ?? 1));
      const k = idx[t]++;
      const r = RAD[t] * Math.sqrt((k + 0.5) / Math.max(1, cnt[t]));
      P[n.id] = [r * Math.cos(k * GA),
                 r * Math.sin(k * GA) * TILT + ZOFF[t]];
    }
  } else if (mode === 'free') {
    const order = [...atlasData.nodes]
      .sort((a, b) => (b.i - a.i) || (a.id < b.id ? -1 : 1));
    order.forEach((n, k) => {
      const r = 26 * Math.sqrt(k + 0.5);
      P[n.id] = [r * Math.cos(k * GA), r * Math.sin(k * GA)];
    });
  } else {
    // CLUSTER — the topic galaxy. Space = topic (stable). Two refinements:
    //  · un-clustered nodes (no community) scatter as a faint STARFIELD across
    //    the whole field instead of a hard rim halo — deterministic, so the sky
    //    never re-scrambles. A node leaves the sky the instant it earns a
    //    community (it falls into its cluster on the next layout pass).
    //  · the "◎ recent" lens lifts today + this week out to two orbit rings just
    //    outside the galaxy; toggling it animates them home (recencyAnim 0→1).
    let base = atlasPosCache._clusterBase;
    if (!base) {
      base = {};
      // Positions are baked PER-GALAXY by the server (compute_atlas): every source
      // is its OWN phyllotaxis atlas — communities as colored blobs, orphans as a
      // dust halo hugging that galaxy's body, the whole thing offset onto the hub
      // ring. Trust them verbatim so orphan stars surround THEIR creator galaxy.
      // (The old global starfield/satellite re-layout is gone — it flung orphans
      // across the whole field instead of around the maker that authored them.)
      let bodyR = 1;
      for (const n of atlasData.nodes) {
        base[n.id] = [n.x, n.y];
        n._star = !n.cid;                       // un-clustered → faint star in its galaxy's halo
        const d = Math.hypot(n.x, n.y); if (d > bodyR) bodyR = d;
      }
      atlasPosCache._bodyR = bodyR;
      atlasPosCache._clusterBase = base;
    }
    if (!recencyOn && recencyAnim === 0) return base;   // pure topic galaxy — cheap path
    const orbit = atlasRecencyOrbit();                  // rings hug each node's OWN galaxy now
    const a = recencyEase(recencyAnim);
    for (const id in base) {
      const o = orbit[id], b = base[id];
      P[id] = o ? [b[0] + (o[0] - b[0]) * a, b[1] + (o[1] - b[1]) * a] : b;   // recent → its galaxy's ring; else holds
    }
    return P;
  }
  atlasPosCache[mode] = P;
  return P;
}

// deterministic star — un-clustered nodes scatter area-evenly across the field,
// hashed from the id so the sky is STABLE across loads (never re-scrambles),
// honoring "build spatial memory of your own vault"
function atlasStarPos(id, R) {
  let h1 = 2166136261, h2 = 5381;
  for (let i = 0; i < id.length; i++) {
    h1 = ((h1 ^ id.charCodeAt(i)) * 16777619) >>> 0;
    h2 = (((h2 * 33) >>> 0) ^ id.charCodeAt(i)) >>> 0;
  }
  const r = R * Math.sqrt((h1 % 100000) / 100000);   // sqrt → even areal density
  const a = (h2 % 100000) / 100000 * 2 * Math.PI;
  return [r * Math.cos(a), r * Math.sin(a)];
}

// the recency lens — today on the OUTER orbit, this week on the INNER, packed by
// golden angle into thin annulus bands just outside the galaxy body
function atlasRecencyOrbit() {
  // Recent nodes lift into rings around THEIR OWN galaxy (centre + radius from
  // atlasGalaxyCentres) — this-week inner, today outer — so "what's fresh" haloes
  // the maker that authored it. A galaxy with no recent work simply shows no ring.
  const now = Date.now(), DAY = 86400000, GA = 2 * Math.PI * 0.3819660112501051;
  const cen = {}; for (const g of atlasGalaxyCentres()) cen[g.key] = g;
  const today = {}, week = {};                 // per-galaxy buckets
  for (const n of atlasData.nodes) {
    if (!n.ts) continue;
    const gk = atlasGalaxyKey(n);
    if (n.ts >= now - DAY)          (today[gk] || (today[gk] = [])).push(n.id);
    else if (n.ts >= now - 7 * DAY) (week[gk]  || (week[gk]  = [])).push(n.id);
  }
  const O = {};
  const band = (ids, g, fin, fout) => {
    if (!g || !ids) return;
    ids.sort();                                // deterministic angular order
    const rin = g.cr * fin, rout = g.cr * fout, n = ids.length || 1;
    for (let k = 0; k < ids.length; k++) {
      const r = Math.sqrt(rin * rin + (rout * rout - rin * rin) * ((k + 0.5) / n)), aa = k * GA;
      O[ids[k]] = [g.x + r * Math.cos(aa), g.y + r * Math.sin(aa)];
    }
  };
  for (const gk in week)  band(week[gk],  cen[gk], 1.12, 1.19);   // inner band = this week (clear GAP off the body)
  for (const gk in today) band(today[gk], cen[gk], 1.21, 1.28);   // outer band = today
  return O;
}

// easeInOutCubic — the fly-out / fall-home tween for the recency lens
function recencyEase(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function atlasTierOn(t) {
  const el = document.getElementById(
    {s: 'lk-strong', m: 'lk-medium', w: 'lk-weak', c: 'lk-chain', d: 'lk-dendrite'}[t]);
  return el ? el.checked : false;
}
let atlasT = {x: 0, y: 0, k: 1}, atlasDrag = null, atlasHover = null;
let atlasFocus = null;   // {hits:Set, ring:Set, q} — search spotlight on the atlas
let atlasIntro = null;   // 0..1 reveal progress during the grow-in, null when idle

// the artistic fill — every view (cluster/rings/free/stack) grows in from the
// core outward, dots scaling + fading at the leading edge. Recomputes _maxR for
// whatever layout is active so the reveal matches the shape.
function atlasReveal(ms) {
  if (atlasData) atlasData._maxR = 0;   // recompute for the current layout
  atlasIntro = 0;
  const dur = ms || 1100;
  let _t0 = null;
  const _grow = ts => {
    if (!atlasOn) { atlasIntro = null; return; }
    if (_t0 === null) _t0 = ts;
    atlasIntro = Math.min(1, (ts - _t0) / dur);
    drawAtlas();
    if (atlasIntro < 1) requestAnimationFrame(_grow);
    else { atlasIntro = null; drawAtlas(); }   // settle → normal draw (edges return)
  };
  requestAnimationFrame(_grow);
}
const ATLAS_CELL = 60;

function exitAtlas(quiet) {
  if (!atlasOn) return;
  atlasOn = false;
  atlasStopPolling();
  ambientStop();
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; } asleep = false;
  atlasFocus = null;
  document.getElementById('btn-atlas').classList.remove('btn-xsess');
  document.getElementById('atlas-canvas').style.display = 'none';
  const rag = document.getElementById('btn-rag');
  if (rag) rag.style.display = '';
  hideTooltip(); setBadge('');
  // landing back on a near-empty session view reads as "broken" —
  // return to the knowledge overview instead (unless the caller is about
  // to load a specific view itself)
  if (!quiet && (currentGraph.nodes || []).length < 10 && !showCrossSession) {
    toggleCrossSession();
  }
}

// (re)build the lookup index, hover grid and strong-degree map from atlasData
function atlasReindex() {
  atlasIdx = {}; atlasGrid = {}; atlasDeg = {};
  (atlasData.nodes || []).forEach(n => {
    atlasIdx[n.id] = n;
    const key = Math.floor(n.x / ATLAS_CELL) + ':' + Math.floor(n.y / ATLAS_CELL);
    (atlasGrid[key] = atlasGrid[key] || []).push(n);
  });
  atlasGridMode = null;   // force the hover grid to rebuild from atlasPositions()
                          // (cluster orphans now live in the starfield, not x/y)
  (atlasData.edges || []).forEach(e => {
    if (e[2] && e[2] !== 's') return;     // degree counts strong ties only
    atlasDeg[e[0]] = (atlasDeg[e[0]] || 0) + 1;
    atlasDeg[e[1]] = (atlasDeg[e[1]] || 0) + 1;
  });
}

// FIX 1b — while the atlas is open, poll for new nodes so a just-made thought
// lands in the sky within seconds (provisional positions, server-side). Bust
// the geometry cache so rings/free reflow with the widened time span.
let atlasPollTimer = null;
function atlasStartPolling() {
  if (atlasPollTimer) return;
  atlasPollTimer = setInterval(async () => {
    if (!atlasOn) { atlasStopPolling(); return; }
    let fresh;
    try { fresh = await fetch(API + '/api/atlas').then(r => r.json()); }
    catch (e) { return; }                 // poll failed — keep old data
    if (!fresh || !Array.isArray(fresh.nodes)) return;
    if (fresh.nodes.length === atlasData.nodes.length &&
        (fresh.edges || []).length === (atlasData.edges || []).length &&
        (fresh.rev == null || fresh.rev === atlasData.rev)) return;  // nothing new (incl. coord-only rebuilds)
    atlasData = fresh;
    atlasPosCache = {};                    // rings/free recompute t0/t1 + reflow
    atlasAdj = null;                       // neural adjacency rebuilds from the new edges
    atlasReindex();
    drawAtlas();
  }, 25000);
}
function atlasStopPolling() {
  if (atlasPollTimer) { clearInterval(atlasPollTimer); atlasPollTimer = null; }
}

async function toggleAtlas() {
  const c = document.getElementById('atlas-canvas');
  // The Galaxy is HOME, not a mode (owner ruling: "make galaxy always on" —
  // the whole-vault legacy layouts it used to exit into are parked). When
  // already home, the button just re-fits the view. Scoped drill-downs
  // (picking a session) still exit on purpose via exitAtlas(true).
  if (atlasOn) { atlasFit(c); atlasReveal(600); return; }
  atlasOn = true;
  document.getElementById('btn-atlas').classList.add('btn-xsess');
  const ctrl = document.getElementById('graph-controls');
  if (ctrl) ctrl.style.zIndex = 50;   // keep controls above the canvas —
                                      // never touch position: CSS owns it
  // Semantic is an SVG-graph mode; hide it here rather than have a button
  // that ejects you from the view you're in
  const rag = document.getElementById('btn-rag');
  if (rag) rag.style.display = 'none';
  c.style.display = 'block';
  c.style.opacity = '1';
  const firstLoad = !atlasData;
  if (firstLoad) {
    setBadge('loading atlas…');
    atlasData = await fetch(API + '/api/atlas').then(r => r.json());
    atlasReindex();
    atlasInitEvents(c);
  }
  atlasResize(c);
  atlasFit(c);
  atlasReveal();                  // radial grow-in from the core outward
  // first-load drift fix (owner-observed): the fit above races page layout —
  // the feed column and fonts settle after first paint and reflow the panel.
  // Refit once right after paint so the correction lands before the eye sees it.
  requestAnimationFrame(() => requestAnimationFrame(() => { atlasResize(c); atlasFit(c); }));
  if (document.fonts && document.fonts.ready)
    document.fonts.ready.then(() => { atlasResize(c); atlasFit(c); });
  atlasStartPolling();
  asleep = false; if (idleTimer) clearTimeout(idleTimer); idleTimer = setTimeout(goAsleep, IDLE_MS);   // idle → dream
  if (ambientMode !== 'off') ambientStart();
  setBadge(`atlas — ${atlasData.nodes.length.toLocaleString()} nodes · the whole vault · ` +
           `search box works here · Color dropdown repaints the galaxy · Esc exits`);
}

// ◎ recent — the recency LENS on the cluster galaxy. ON: today + this week lift
// out to two orbit rings (what you're working on lately). OFF: they fall home
// into their topic clusters. A toggle IS a lens — space stays topic underneath.
function toggleRecent() {
  if (!atlasOn) return;
  const sel = document.getElementById('sel-layout');
  if (sel.value !== 'cluster') { sel.value = 'cluster'; relayout(); }   // the lens only means something on the galaxy
  recencyOn = !recencyOn;
  const btn = document.getElementById('btn-recent');
  if (btn) btn.classList.toggle('btn-xsess', recencyOn);
  atlasGridMode = null;                          // hover grid must follow the move
  saveProfile();
  const from = recencyAnim, to = recencyOn ? 1 : 0, dur = 900;
  let t0 = null;
  if (recencyRAF) cancelAnimationFrame(recencyRAF);
  const step = ts => {
    if (!atlasOn) { recencyRAF = null; return; }
    if (t0 === null) t0 = ts;
    const u = Math.min(1, (ts - t0) / dur);
    recencyAnim = from + (to - from) * u;
    drawAtlas();
    if (u < 1) { recencyRAF = requestAnimationFrame(step); }
    else { recencyRAF = null; recencyAnim = to; atlasGridMode = null; drawAtlas(); }
  };
  recencyRAF = requestAnimationFrame(step);
  setBadge(recencyOn
    ? 'recency lens ON — today on the outer ring, this week on the inner · ◎ recent to fall home'
    : 'recency lens off — back to the topic galaxy');
}

// ── Profile — a tiny localStorage "home": your layout + lenses + ambient, so the
// dashboard lands you where you left it (sticky). Local only, never leaves the
// machine. New users get defaults; it kicks in once you change a view.
function saveProfile() {
  try {
    localStorage.setItem('cairnProfile', JSON.stringify({
      layout:  document.getElementById('sel-layout').value,
      color:   document.getElementById('sel-color').value,
      time:    document.getElementById('sel-time').value,
      recency: !!recencyOn,
      ambient: ambientMode,
      links:   linkState(),
    }));
  } catch (e) {}
}
function linkState() {   // which link layers are toggled on (persisted in the profile)
  const chk = id => { const el = document.getElementById(id); return el ? el.checked : false; };
  return { chain: chk('lk-chain'), dendrite: chk('lk-dendrite'), strong: chk('lk-strong'), medium: chk('lk-medium'), weak: chk('lk-weak') };
}
function applyProfile() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem('cairnProfile') || '{}'); } catch (e) { p = {}; }
  const set = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
  set('sel-layout', p.layout); set('sel-color', p.color); set('sel-time', p.time);
  ambientMode = p.ambient || 'off';
  if (p.links) {   // restore which link layers are on
    const ck = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    ck('lk-chain', p.links.chain); ck('lk-dendrite', p.links.dendrite);
    ck('lk-strong', p.links.strong); ck('lk-medium', p.links.medium); ck('lk-weak', p.links.weak);
  }
  return p;
}
function applyProfilePost(p) {
  toggleTimeRange();   // reflect a restored 'custom' time selection
  if (p && p.recency && document.getElementById('sel-layout').value === 'cluster') {
    recencyOn = true; recencyAnim = 1; atlasGridMode = null;
    const rb = document.getElementById('btn-recent'); if (rb) rb.classList.add('btn-xsess');
  }
  const ab = document.getElementById('btn-ambient');
  if (ab) { ab.textContent = '✦ ' + ambientMode; ab.classList.toggle('btn-xsess', ambientMode !== 'off'); }
  if (ambientMode !== 'off') ambientStart(); else drawAtlas();
}

// ── ✦ Ambient — subtle motion that NEVER moves a node (positions are fixed, so
// click/hover stay pixel-exact). twinkle = per-node brightness breathe; ripple =
// a slow brightness wave sweeping the field. Throttled ~22fps, paused when the
// tab is hidden. Opt-in, saved to the profile.
// scattered ripple "drops" — [xFrac, yFrac, speed, phase], positions as fractions
// of the body radius. Concentric rings from each interfere = an organic pond
// ripple, not parallel stripes.
const AMBIENT_DROPS = [[-0.62, -0.34, 1.00, 0.0], [0.55, 0.46, 0.82, 2.1], [0.12, -0.72, 1.18, 4.0], [0.80, -0.06, 0.70, 1.2]];

// returns {a: alpha mult, sz: SIZE mult}. The size pop is what reads as a
// twinkle/ripple — a point that flares bigger+brighter then snaps back. Center
// never moves (hover keys off center + a fixed pixel radius), so still clickable.
function ambientFX(n, p) {
  let a = 1, sz = 1, glow = 0;
  if (ambientMode === 'twinkle') {
    let ph = n._tw, sp = n._tws;
    if (ph === undefined) {
      let h = 0; for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) >>> 0;
      ph = n._tw = (h % 1000) / 1000 * 6.2832;
      sp = n._tws = 0.9 + (h % 500) / 500 * 0.5;        // 0.9..1.4 — narrow, consistent rate
    }
    const v = 0.5 + 0.5 * Math.sin(ambientT * sp + ph);
    a *= 0.82 + 0.18 * v;                                // gentle even brightness base
    const pk = Math.max(0, (v - 0.72) / 0.28);
    glow = pk * pk * 7;                                  // soft sparkle HALO at the peak — size-INDEPENDENT, so it reads evenly across ALL nodes
  }
  if (ambientMode === 'ripple') {
    // expanding rings from "drops" — but CONTAINED to each solar system: a node
    // only feels drops from its OWN system, and the ring front + band scale to
    // that system's radius, so small systems ripple just as clearly as big ones.
    let gk = n._gk; if (gk === undefined) gk = n._gk = atlasGalaxyKey(n);
    const now2 = ambientT * 1000; let lit = 0;
    for (let i = 0; i < rippleDrops.length; i++) {
      const dp = rippleDrops[i]; if (dp.sys !== gk) continue;            // stay inside this system
      const age = now2 - dp.t0, fade = 1 - age / 4200; if (fade <= 0) continue;
      const front = (age / 4200) * dp.R * 1.15;                          // ring reaches the system edge over its lifetime
      const band = Math.max(40, dp.R * 0.16);                            // ring thickness ∝ system size
      const d = Math.hypot(p[0] - dp.x, p[1] - dp.y) - front;
      const val = Math.exp(-(d * d) / (band * band)) * fade; if (val > lit) lit = val;
    }
    a  *= 1 + 0.55 * lit;
    sz *= 1 + 0.45 * lit;
  }
  return { a, sz, glow };
}
// effective mode: when the atlas is left idle it falls ASLEEP and dreams in
// neural firing, whatever the button says; any interaction wakes it.
function ambientEff() { return asleep ? 'neural' : ambientMode; }
function ambientStart() {
  if (ambientRAF || ambientEff() === 'off' || !atlasOn) return;
  const loop = ts => {
    if (!atlasOn || ambientEff() === 'off' || document.hidden) { ambientRAF = null; return; }
    if (ts - ambientLast >= 33) { ambientLast = ts; ambientT = ts * 0.001; const _m = ambientEff(); if (_m === 'neural') neuralStep(ts); else if (_m === 'ripple') rippleStep(ts); drawAtlas(); }
    ambientRAF = requestAnimationFrame(loop);
  };
  ambientRAF = requestAnimationFrame(loop);
}
// idle → dream → wake
function goAsleep() {
  if (!atlasOn || asleep) return;
  asleep = true; neuralPulses = []; neuralFlares = []; neuralLastIg = 0;
  ambientStart();                                        // the loop now renders neural
}
function noteInteraction() {
  if (!atlasOn) return;
  if (asleep) { asleep = false; neuralPulses = []; neuralFlares = []; if (ambientMode === 'off') { ambientStop(); drawAtlas(); } }
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(goAsleep, IDLE_MS);
}
function ambientStop() { if (ambientRAF) { cancelAnimationFrame(ambientRAF); ambientRAF = null; } neuralPulses = []; neuralFlares = []; rippleDrops = []; }
// ripple = discrete "rock drops" — each sends ONE ring expanding outward, then fades
function rippleStep(now) {
  for (let i = rippleDrops.length - 1; i >= 0; i--) if (now - rippleDrops[i].t0 > 4200) rippleDrops.splice(i, 1);
  if (now - rippleLastDrop > 1100) {
    rippleLastDrop = now;
    const vis = atlasGalaxyCentres().filter(g => !atlasGalaxiesOff.has(g.key));   // visible solar systems (gc is an array)
    if (!vis.length) return;
    const g = vis[(rippleSysIdx++) % vis.length];                                // next system in the rotation
    const ang = Math.random() * 6.2832, rr = Math.sqrt(Math.random()) * Math.max(60, g.r) * 0.5;
    rippleDrops.push({ x: g.x + rr * Math.cos(ang), y: g.y + rr * Math.sin(ang), t0: now, sys: g.key, R: Math.max(60, g.r) });
  }
}
const AMBIENT_CYCLE = ['off', 'twinkle', 'ripple'];
function toggleAmbient() {
  if (!atlasOn) return;
  asleep = false;                                        // a click wakes it
  ambientMode = AMBIENT_CYCLE[(AMBIENT_CYCLE.indexOf(ambientMode) + 1) % AMBIENT_CYCLE.length];
  const btn = document.getElementById('btn-ambient');
  if (btn) { btn.textContent = '✦ ' + ambientMode; btn.classList.toggle('btn-xsess', ambientMode !== 'off'); }
  saveProfile();
  setBadge(ambientMode === 'off' ? 'ambient off · leave it idle and the brain dreams (neural)'
                                 : ambientMode + ' · idle → it drifts into neural firing');
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(goAsleep, IDLE_MS);
  if (ambientMode === 'off') { ambientStop(); drawAtlas(); } else ambientStart();
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && ambientEff() !== 'off' && atlasOn) ambientStart(); });

// adjacency over the REAL graph — every node to its edge-neighbours. Built once
// from atlasData.edges (busted when new nodes arrive).
function neuralAdj() {
  if (atlasAdj) return atlasAdj;
  atlasAdj = {};
  for (const e of (atlasData.edges || [])) {
    const t = e[2] || 's';
    (atlasAdj[e[0]] = atlasAdj[e[0]] || []).push([e[1], t]);
    (atlasAdj[e[1]] = atlasAdj[e[1]] || []).push([e[0], t]);
  }
  return atlasAdj;
}

// ✦ NEURAL FIRING — a thought ignites at a node and CASCADES along the synapses:
// each arrival flares the node and re-fires to its neighbours (decaying per hop),
// like a memory being recalled. Pulses travel between FIXED node centers — nothing
// moves, so clicking stays exact. Capped + paused-when-hidden for cheapness.
function neuralStep(now) {
  const adj = neuralAdj();
  for (let i = neuralPulses.length - 1; i >= 0; i--) {
    const p = neuralPulses[i];
    p.t += 0.055;
    if (p.t >= 1) {
      neuralFlares.push({id: p.b, t0: now, h: p.h, s: p.s, inten: p.inten});
      if (p.inten > 0.42 && neuralPulses.length < NEURAL_CAP) {
        const nb = adj[p.b] || []; let spawned = 0;
        for (let k = 0; k < nb.length && spawned < 5; k++) {
          const e = nb[k];
          if (e[0] !== p.a && Math.random() < 0.62 && neuralPulses.length < NEURAL_CAP) {
            const c = NEURAL_HUE[e[1]] || NEURAL_HUE.s;
            neuralPulses.push({a: p.b, b: e[0], t: 0, h: c[0], s: c[1], inten: p.inten * 0.62}); spawned++;
          }
        }
      }
      neuralPulses.splice(i, 1);
    }
  }
  for (let i = neuralFlares.length - 1; i >= 0; i--) if (now - neuralFlares[i].t0 > 820) neuralFlares.splice(i, 1);
  if (now - neuralLastIg > 1100 && neuralPulses.length < NEURAL_CAP - 6) {
    neuralLastIg = now;
    const nodes = atlasData.nodes, n0 = nodes[(Math.random() * nodes.length) | 0];
    if (n0 && adj[n0.id]) {
      const nb = adj[n0.id], c0 = NEURAL_HUE[nb[0][1]] || NEURAL_HUE.s;
      neuralFlares.push({id: n0.id, t0: now, h: c0[0], s: c0[1], inten: 1});
      for (let k = 0; k < nb.length && k < 6; k++) {
        const c = NEURAL_HUE[nb[k][1]] || NEURAL_HUE.s;
        neuralPulses.push({a: n0.id, b: nb[k][0], t: 0, h: c[0], s: c[1], inten: 1});
      }
    }
  }
}

function atlasResize(c) {
  const p = document.getElementById('graph-panel');
  c.width  = p.clientWidth  * devicePixelRatio;
  c.height = p.clientHeight * devicePixelRatio;
  c.style.width  = p.clientWidth  + 'px';
  c.style.height = p.clientHeight + 'px';
}

function atlasFit(c) {
  const P = atlasPositions();
  let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
  for (const id in P) {
    const p = P[id];
    if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
    if (p[1] < y0) y0 = p[1]; if (p[1] > y1) y1 = p[1];
  }
  const w = c.width / devicePixelRatio, h = c.height / devicePixelRatio;
  const k = Math.min(w / (x1 - x0 + 200), h / (y1 - y0 + 200));
  atlasT = {k: k, x: w / 2 - k * (x0 + x1) / 2, y: h / 2 - k * (y0 + y1) / 2};
}

// frame just the search hits, so results are never stranded off-screen at the
// rim (they scatter by topic, so a search may land anywhere on the map)
function atlasFitToHits(hitSet) {
  const c = document.getElementById('atlas-canvas');
  const P = atlasPositions();
  let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9, n = 0;
  hitSet.forEach(id => {
    const p = P[id]; if (!p) return; n++;
    if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
    if (p[1] < y0) y0 = p[1]; if (p[1] > y1) y1 = p[1];
  });
  if (!n) return;
  const w = c.width / devicePixelRatio, h = c.height / devicePixelRatio, pad = 280;
  const k = Math.min(6, w / (x1 - x0 + pad), h / (y1 - y0 + pad));  // cap zoom-in
  atlasT = {k: k, x: w / 2 - k * (x0 + x1) / 2, y: h / 2 - k * (y0 + y1) / 2};
}

function drawAtlas() {
  const c = document.getElementById('atlas-canvas');
  const ctx = c.getContext('2d'), dpr = devicePixelRatio;
  const T = atlasT;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#16140E';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.setTransform(T.k * dpr, 0, 0, T.k * dpr, T.x * dpr, T.y * dpr);

  const P = atlasPositions();   // geometry for the current Layout mode

  // tree-ring time guides — faint circles at year boundaries so the rings
  // read as chunks of time, with the year etched on each
  const lmode = document.getElementById('sel-layout').value;
  if (lmode === 'rings' && atlasPosCache._ringsMeta) {
    const M = atlasPosCache._ringsMeta;
    ctx.strokeStyle = 'rgba(110,118,129,0.18)';
    ctx.lineWidth = 1 / T.k;
    ctx.fillStyle = 'rgba(139,148,158,0.65)';
    ctx.font = `${13 / T.k}px monospace`;
    ctx.textAlign = 'center';
    const y0 = new Date(M.t0).getFullYear(), y1 = new Date(M.t1).getFullYear();
    for (let y = y0; y <= y1; y++) {
      const t = new Date(y, 0, 1).getTime();
      if (t < M.t0 || t > M.t1) continue;
      const r = M.R0 + (M.R1 - M.R0) * (t - M.t0) / Math.max(1, M.t1 - M.t0);
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, 6.2832);
      ctx.stroke();
      ctx.fillText(String(y), 0, -r - 10 / T.k);
    }
  }

  // connections: NEVER ambient. The Links checkboxes decide WHICH edge types
  // are included in the reveal; the reveal itself only fires for the node you
  // hover (or search hits). Check more types → more colors light up at once on
  // that same hover. Default: chain (orange) on, so a hover shows the path.
  const F = atlasFocus;
  if (atlasIntro === null && atlasData.edges && (F || atlasHover)) {
    // bright color per type — shown only for the active node's ties.
    // strong/medium pop like the orange; weak stays a faint grey.
    const HOT = {s: 'rgba(56,189,248,0.95)',   // bright cyan
                 m: 'rgba(216,119,201,0.92)',   // bright magenta
                 w: 'rgba(139,148,158,0.50)',   // faint grey
                 c: 'rgba(240,185,66,0.95)',    // bright orange (chain)
                 d: 'rgba(80,220,100,0.92)'};   // bright green (dendrite)
    for (const e of atlasData.edges) {
      const t = e[2] || 's';
      if (!atlasTierOn(t)) continue;   // checkbox = include this type in the reveal
      const touchesHover = atlasHover && (e[0] === atlasHover.id || e[1] === atlasHover.id);
      const touchesHit   = F && (F.hits.has(e[0]) || F.hits.has(e[1]));
      if (!touchesHover && !touchesHit) continue;   // only the active node's ties
      const a = atlasIdx[e[0]], b = atlasIdx[e[1]];
      if (!a || !b) continue;
      if (!timeOk(a.ts) || !timeOk(b.ts) ||
          !atlasKindOk(a) || !atlasKindOk(b) ||
          !atlasGalaxyOk(a) || !atlasGalaxyOk(b)) continue;   // no edges to ghosts
      ctx.strokeStyle = HOT[t] || HOT.s;
      ctx.lineWidth = Math.max(0.3, (touchesHover ? 1.2 : 1.0) / T.k);
      const pa = P[e[0]], pb = P[e[1]];
      ctx.beginPath();
      ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]);
      ctx.stroke();
    }
  }

  const boost = Math.min(3.5, Math.max(0.8, 0.5 / T.k));
  // intro reveal — the galaxy grows outward from the core, a soft leading
  // edge where dots fade + scale up into place ("filling into the shape")
  let introR = Infinity, introBand = 1;
  if (atlasIntro !== null) {
    if (!atlasData._maxR) {
      let mr = 1;
      for (const n of atlasData.nodes) {
        const q = P[n.id]; if (!q) continue;
        const d = Math.hypot(q[0], q[1]); if (d > mr) mr = d;
      }
      atlasData._maxR = mr;
    }
    // ease-out so it rushes out then settles
    const e = 1 - Math.pow(1 - atlasIntro, 3);
    introR   = e * atlasData._maxR * 1.08;
    introBand = atlasData._maxR * 0.22;
  }
  const visCnt = {};   // visible nodes per community — labels follow the dots
  const _ambEff = ambientEff();   // twinkle/ripple shimmer applies in this loop; neural is the overlay below
  // perf: viewport culling + zoom-based LOD keep 60k+ nodes smooth. Off-screen
  // dots are skipped (big win when zoomed in); when zoomed far out (dots are
  // sub-pixel anyway) we thin the field on large vaults — which also kills the
  // moiré that the regular phyllotaxis grid throws at low zoom.
  const _wW = c.width / dpr, _wH = c.height / dpr, _mg = 60 / T.k;
  const _minX = -T.x / T.k - _mg, _maxX = (_wW - T.x) / T.k + _mg;
  const _minY = -T.y / T.k - _mg, _maxY = (_wH - T.y) / T.k + _mg;
  const _big = atlasData.nodes.length > 12000;
  const _lodStep = (_big && T.k < 0.10) ? 4 : (_big && T.k < 0.20) ? 2 : 1;
  const _jit = 2.0 / T.k;   // screen-constant sub-pixel jitter — breaks the regular
                            // phyllotaxis lattice so it can't beat against the pixel
                            // grid (that's the mid-zoom "squares"/moiré). Stable per node.
  // EXPERIMENT (zoom-out only): snap each dot's centre to the DEVICE-pixel grid.
  // The phyllotaxis lattice carries a near-constant sub-pixel phase across
  // neighbours; that phase beating against the pixel grid is the moiré/"squares".
  // Quantising centres to whole pixels removes the phase → no beat. Transform is
  // scale=T.k*dpr, offset=T.{x,y}*dpr (setTransform above), so round in device
  // space and map back to world. Gated <0.25 so zoom-in panning stays smooth.
  const _snap = T.k < 0.25, _S = T.k * dpr, _Ox = T.x * dpr, _Oy = T.y * dpr;
  let _di = 0;
  const recencyLit = recencyOn || recencyAnim > 0;   // recent lens active → recent nodes glow in signature color
  const _recentCut = Date.now() - 604800000;         // 7-day window
  for (const n of atlasData.nodes) {
    if (!timeOk(n.ts) || !atlasKindOk(n) || !atlasGalaxyOk(n)) continue;   // time + kind + galaxy lenses
    const p = P[n.id];
    if (!p) continue;
    if (n._jx === undefined) {
      let h = 5381; const s = n.id || '';
      for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
      // CIRCULAR jitter (polar). Independent x/y was a SQUARE distribution, so a
      // dense cluster's dots filled a little BOX — the "squares" at the densest
      // cluster centers when zoomed out. A disk gives round footprints instead.
      const _ja = (h & 1023) / 1023 * 6.2831853;
      const _jr = Math.sqrt(((h >>> 10) & 1023) / 1023) * 0.5;   // sqrt → uniform disk
      n._jx = _jr * Math.cos(_ja); n._jy = _jr * Math.sin(_ja);
      n._dh = (h >>> 20) & 255;   // decimation hash — bits 20-27, decoupled from the jitter
    }
    let px = p[0] + n._jx * _jit, py = p[1] + n._jy * _jit;
    if (_snap) {                                  // align centre to whole device pixels
      px = (Math.round(px * _S + _Ox) - _Ox) / _S;
      py = (Math.round(py * _S + _Oy) - _Oy) / _S;
    }
    if (px < _minX || px > _maxX || py < _minY || py > _maxY) continue;  // off-screen → skip
    if (_lodStep > 1 && !F && n.cid && (n._dh % _lodStep)) continue;  // LOD — thin DENSE clusters only; keep EVERY orphan star
    let grow = 1, aMul = 1;
    if (atlasIntro !== null) {
      const d = Math.hypot(p[0], p[1]);
      if (d > introR) continue;            // not yet reached by the reveal
      const edge = (introR - d) / introBand;
      if (edge < 1) { grow = 0.25 + 0.75 * edge; aMul = Math.max(0, edge); }
    }
    if (n.cid) visCnt[n.cid] = (visCnt[n.cid] || 0) + 1;
    const star = !n.cid && n._star && !n.fresh && lmode === 'cluster';   // true loner — faint background star
    const sat  = !n.cid && !n._star && !n.fresh && lmode === 'cluster';  // satellite — peripheral, hugs its cluster
    const recRing = recencyLit && n.ts && n.ts >= _recentCut && lmode === 'cluster';  // recent → glows in its galaxy's signature hue
    const col = atlasNodeColor(n);
    ctx.fillStyle = col;
    let baseA = (F
      ? (F.hits.has(n.id) ? 1.0 : F.ring.has(n.id) ? 0.65 : 0.05)
      : (star ? 0.80 : sat ? 0.84 : 1.0)) * aMul * ((_ambEff === 'twinkle' || _ambEff === 'ripple') ? 0.72 : 1);   // full brightness when ambient is OFF; dimmed to 0.72 under twinkle/ripple so the effect has headroom to brighten back up (import no longer dims — it's ~97% of the vault)
    if (recRing) baseA = aMul;   // recent reads full-bright
    let amSz = 1;
    if (_ambEff === 'ripple') { const fx = ambientFX(n, p); baseA *= fx.a; amSz = fx.sz; }   // ripple grows the dot (visible); twinkle is the screen-space pass below
    ctx.globalAlpha = baseA;
    if (n.fresh || recRing) {
      ctx.shadowColor = recRing ? col : 'rgba(63,185,80,0.75)';
      ctx.shadowBlur  = recRing ? 6 : 8;
    }
    ctx.beginPath();
    const _wr = (recRing ? n.r : star ? Math.min(n.r, 1.2) : sat ? Math.min(n.r, 1.7) : n.r) * boost * grow * amSz;
    ctx.arc(px, py, Math.max(_wr, 0.7 / T.k), 0, 6.2832);   // floor ~0.7px on screen so dots never go sub-pixel when zoomed out (the "dim only when zoomed out" cause)
    ctx.fill();
    if (n.fresh || recRing) {
      ctx.shadowColor = 'transparent';
      ctx.shadowBlur  = 0;
    }
  }
  ctx.globalAlpha = 1;

  // labels + hover ring in screen space (constant size at any zoom).
  // Topic labels belong to the galaxy's geography — other layouts scatter
  // communities, so the names would float over nothing.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = '11px monospace';
  ctx.textAlign = 'center';
  const mode = document.getElementById('sel-layout').value;
  if (atlasLabelsOn && mode === 'cluster') {
    // galaxy NAME labels — clean, from the baked per-source centroids
    const gc = atlasGalaxyCentres();
    if (gc.length > 1) {
      ctx.font = 'bold 13px monospace';
      for (const G of gc) {
        if (atlasGalaxiesOff.has(G.key)) continue;
        ctx.fillStyle = (GALAXY_COLORS[G.key] || '#A371F7') + 'dd';
        ctx.fillText(G.key, G.x * T.k + T.x, (G.y - G.r) * T.k + T.y - 14);
      }
      ctx.font = '11px monospace';
    }
    // topic labels — now sit inside their own galaxy (positions are per-galaxy)
    for (const L of atlasData.labels) {
      if ((visCnt[L.cid] || 0) < 3) continue;
      if (T.k < 0.18 && L.n < 60) continue;
      ctx.fillStyle = 'rgba(139,148,158,0.8)';
      ctx.fillText(L.t, L.x * T.k + T.x, L.y * T.k + T.y - 6);
    }
  }

  // ✦ twinkle — SCREEN-SPACE sparkles. Nodes are sub-pixel at most zooms, so a
  // brightness/glow on the dot is invisible; instead draw a FIXED-pixel sparkle at
  // each peaking node — same size for every node, so it's visible AND perfectly even.
  if (ambientEff() === 'twinkle') {
    for (const n of atlasData.nodes) {
      if (!timeOk(n.ts) || !atlasKindOk(n) || !atlasGalaxyOk(n)) continue;
      let ph = n._tw, sp = n._tws;
      if (ph === undefined) { let h = 0; for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) >>> 0; ph = n._tw = (h % 1000) / 1000 * 6.2832; sp = n._tws = 0.9 + (h % 500) / 500 * 0.5; n._twb = 0.85 + ((h >>> 7) % 100) / 100 * 0.75; }
      const v = 0.5 + 0.5 * Math.sin(ambientT * sp + ph);
      if (v < 0.95) continue;                            // only the very peak sparkles → ~15% lit at any moment
      const pp = P[n.id]; if (!pp) continue;
      const pk = (v - 0.95) / 0.05, b = n._twb, col = atlasNodeColor(n);
      ctx.globalAlpha = Math.min(1, pk * b);             // BRIGHTNESS flicker; b varies per node so SOME twinkle brighter (real-star variety)
      ctx.shadowColor = col; ctx.shadowBlur = 3 + 2 * (b - 0.85);   // the bright ones glow a touch more (size still fixed-ish)
      ctx.fillStyle = col;
      const _sr = Math.max(0.9, (n.r || 2) * boost * T.k * 1.3);   // scale sparkle to the node's on-screen size so twinkle shows at ALL zooms (was fixed 0.7px → invisible on big zoomed-in dots)
      ctx.beginPath(); ctx.arc(pp[0] * T.k + T.x, pp[1] * T.k + T.y, _sr, 0, 6.2832); ctx.fill();
    }
    ctx.shadowBlur = 0; ctx.globalAlpha = 1;
  }

  // ✦ neural firing overlay — pulses race the synapses, nodes flare as a thought
  // arrives. Screen space so glows are crisp at any zoom; positions never move.
  if (ambientEff() === 'neural') {                       // the dream — kept soft & dim
    for (const pl of neuralPulses) {
      const pa = P[pl.a], pb = P[pl.b]; if (!pa || !pb) continue;
      const ax = pa[0] * T.k + T.x, ay = pa[1] * T.k + T.y, bx = pb[0] * T.k + T.x, by = pb[1] * T.k + T.y;
      const hx = ax + (bx - ax) * pl.t, hy = ay + (by - ay) * pl.t;
      const tt = Math.max(0, pl.t - 0.45), qx = ax + (bx - ax) * tt, qy = ay + (by - ay) * tt;
      ctx.strokeStyle = `hsla(${pl.h},${pl.s}%,66%,${(0.55 * pl.inten + 0.18).toFixed(3)})`;
      ctx.lineWidth = 1.9; ctx.beginPath(); ctx.moveTo(qx, qy); ctx.lineTo(hx, hy); ctx.stroke();
      ctx.shadowColor = `hsla(${pl.h},${pl.s}%,64%,0.62)`; ctx.shadowBlur = 7;
      ctx.fillStyle = `hsla(${pl.h},${pl.s}%,64%,0.92)`; ctx.beginPath(); ctx.arc(hx, hy, 1.7 * pl.inten + 0.7, 0, 6.2832); ctx.fill();
      ctx.shadowBlur = 0;
    }
    const tnow = ambientT * 1000;
    for (const fl of neuralFlares) {
      const pp = P[fl.id]; if (!pp) continue;
      const k = 1 - (tnow - fl.t0) / 800; if (k <= 0) continue;
      ctx.shadowColor = `hsla(${fl.h},${fl.s}%,62%,${(0.6 * k).toFixed(3)})`; ctx.shadowBlur = 9 * k + 1;
      ctx.fillStyle = `hsla(${fl.h},${fl.s}%,${(56 + 12 * k) | 0}%,${(0.9 * k + 0.1).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(pp[0] * T.k + T.x, pp[1] * T.k + T.y, 1.6 + 3.0 * k * fl.inten, 0, 6.2832); ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  if (atlasHover && P[atlasHover.id]) {
    const hp = P[atlasHover.id];
    ctx.strokeStyle = '#7CA38C';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.arc(hp[0] * T.k + T.x, hp[1] * T.k + T.y, 9, 0, 6.2832);
    ctx.stroke();
  }
  if (F) {
    ctx.strokeStyle = '#7CA38C';
    ctx.lineWidth = 1.4;
    for (const id of F.hits) {
      const p = P[id];
      if (!p) continue;
      ctx.beginPath();
      ctx.arc(p[0] * T.k + T.x, p[1] * T.k + T.y, 8, 0, 6.2832);
      ctx.stroke();
    }
  }
}

// atlas color views — the Color dropdown repaints the whole galaxy:
//   by topic (default) · by kind · by source · by model
//   (which life a memory came from: per-account imports / live work)
// ── Galaxies: each source (account / agent) is its own galaxy. The galaxy KEY
// groups by maker (per-account, GPT, …); each node still keeps its own model.
// Deselecting a galaxy hides it (and, with the spatial layout, re-packs the rest).
const CAIRN_ACCOUNTS = window.CAIRN_ACCOUNTS || {};
// Build label lookup + color map from injected config. 'Claude-Code' (live,
// no account) keeps its built-in green — it is not a personal handle.
const GALAXY_COLORS = {'Claude-Code': '#C9A227'};
for (const k in CAIRN_ACCOUNTS) {
  const c = CAIRN_ACCOUNTS[k];
  if (c && c.label && c.color) GALAXY_COLORS[c.label] = c.color;
}
function atlasGalaxyKey(n) {
  const a = (n.a || '').toLowerCase();
  if (!a) return 'Claude-Code';
  const c = CAIRN_ACCOUNTS[a];
  if (c && c.label) return c.label;
  return a.charAt(0).toUpperCase() + a.slice(1);   // future agents: Hermes, OpenClaw, …
}
function atlasGalaxyColor(n) { return GALAXY_COLORS[atlasGalaxyKey(n)] || '#A371F7'; }
let atlasGalaxiesOff = new Set();                   // galaxy keys the user deselected
function atlasGalaxyOk(n) { return !atlasGalaxiesOff.has(atlasGalaxyKey(n)); }

function toggleGalaxyPanel() {
  let pan = document.getElementById('galaxy-panel');
  if (pan && pan.style.display !== 'none') { pan.style.display = 'none'; return; }
  const host = document.getElementById('graph-panel');
  if (!pan) {
    pan = document.createElement('div'); pan.id = 'galaxy-panel';
    pan.style.cssText = 'position:absolute;z-index:60;background:#1A1811;border:1px solid #3A3528;'
      + 'border-radius:8px;padding:8px 11px;font-size:11px;color:#C9C2B2;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.45);max-height:320px;overflow:auto;min-width:172px';
    host.appendChild(pan);
  }
  const b = document.getElementById('btn-galaxies'), br = b.getBoundingClientRect(), hr = host.getBoundingClientRect();
  pan.style.left = (br.left - hr.left) + 'px'; pan.style.top = (br.bottom - hr.top + 4) + 'px';
  const counts = {};
  for (const n of atlasData.nodes) { const k = atlasGalaxyKey(n); counts[k] = (counts[k] || 0) + 1; }
  const keys = Object.keys(counts).sort();
  pan.innerHTML = '<div style="font-weight:600;margin-bottom:6px">Solar Systems</div>' + keys.map(k => {
    const on = !atlasGalaxiesOff.has(k), col = GALAXY_COLORS[k] || '#A371F7';
    return '<label style="display:flex;align-items:center;gap:7px;padding:3px 0;cursor:pointer">'
      + '<input type="checkbox" ' + (on ? 'checked' : '') + ' data-k="' + esc(k) + '" onchange="toggleGalaxy(this.dataset.k)">'
      + '<span style="width:9px;height:9px;border-radius:50%;background:' + col + ';display:inline-block"></span>'
      + '<span>' + esc(k) + '</span><span style="color:#6E7681;margin-left:auto">' + counts[k] + '</span></label>';
  }).join('') + '<div style="margin-top:7px;display:flex;gap:10px">'
    + '<a style="cursor:pointer;color:#7CA38C" onclick="galaxyAll(true)">all</a>'
    + '<a style="cursor:pointer;color:#7CA38C" onclick="galaxyAll(false)">none</a></div>';
  pan.style.display = 'block';
  closeOnOutside('galaxy-panel', 'btn-galaxies');
}
function toggleGalaxy(k) {
  if (atlasGalaxiesOff.has(k)) atlasGalaxiesOff.delete(k); else atlasGalaxiesOff.add(k);
  atlasPosCache = {}; atlasPosCache._clusterBase = null;   // re-pack the visible galaxies
  drawAtlas();
}
function galaxyAll(on) {
  const ks = new Set(); for (const n of atlasData.nodes) ks.add(atlasGalaxyKey(n));
  atlasGalaxiesOff = on ? new Set() : ks;
  atlasPosCache = {}; atlasPosCache._clusterBase = null;
  const pan = document.getElementById('galaxy-panel'); if (pan) pan.style.display = 'none';
  drawAtlas();
}
function toggleGalaxySeparate() {
  atlasGalaxySeparate = !atlasGalaxySeparate;
  atlasPosCache._gxy = null;       // recompute (or skip) the galaxy offsets
  drawAtlas();
}
// galaxy centres + radii from the BAKED positions (server separated the
// galaxies in compute_atlas) — for galaxy-name labels. Cached on atlasData.
function atlasGalaxyCentres() {
  if (atlasData._gc) return atlasData._gc;
  // CENTRE from CLUSTERED members only. Un-homed newborns get provisional
  // positions flung to the whole-field edge; averaging them in dragged the
  // centre toward field-centre and inflated cr (p98 distance FROM that centre),
  // so the recency rings ballooned, went off-centre, and swallowed neighbouring
  // galaxies. The clustered body defines where a galaxy is and how big it is.
  const acc = {};
  for (const n of atlasData.nodes) {
    const k = atlasGalaxyKey(n), a = acc[k] || (acc[k] = { sx: 0, sy: 0, n: 0, csx: 0, csy: 0, cn: 0 });
    a.sx += n.x; a.sy += n.y; a.n++;
    if (n.cid) { a.csx += n.x; a.csy += n.y; a.cn++; }
  }
  const out = {};
  for (const k in acc) {
    const a = acc[k], C = a.cn > 0;   // prefer the clustered body; fall back to all if a galaxy has none clustered
    out[k] = { key: k, x: C ? a.csx / a.cn : a.sx / a.n, y: C ? a.csy / a.cn : a.sy / a.n, r: 1, cr: 1, n: a.n, _d: [], _cd: [] };
  }
  for (const n of atlasData.nodes) {
    const g = out[atlasGalaxyKey(n)], d = Math.hypot(n.x - g.x, n.y - g.y);
    g._d.push(d); if (n.cid) g._cd.push(d);   // _cd = CLUSTERED only (no orphan halo)
  }
  // ROBUST radii — percentiles, NOT the max (a few stray nodes flung to the far
  // edge would otherwise blow the galaxy's "edge" out ~10x and make the recency
  // rings circle the whole field).
  //   r  = p96 of ALL members  -> full galaxy extent (incl. orphan halo)
  //   cr = p92 of CLUSTERED    -> the cluster BODY, so recent rings hug it tightly
  for (const k in out) {
    const d = out[k]._d.sort((a, b) => a - b), cd = out[k]._cd.sort((a, b) => a - b);
    out[k].r  = Math.max(1, d[Math.floor(d.length * 0.96)] || d[d.length - 1] || 1);
    out[k].cr = Math.max(1, cd.length ? (cd[Math.floor(cd.length * 0.98)] || cd[cd.length - 1]) : out[k].r);
    delete out[k]._d; delete out[k]._cd;
  }
  atlasData._gc = Object.values(out); return atlasData._gc;
}
// cross-galaxy edges — the bridges between worlds. Cached.
function atlasBridges() {
  if (atlasData._br) return atlasData._br;
  const br = [];
  for (const e of (atlasData.edges || [])) {
    const a = atlasIdx[e[0]], b = atlasIdx[e[1]];
    if (a && b && atlasGalaxyKey(a) !== atlasGalaxyKey(b)) br.push(e);
  }
  atlasData._br = br; return br;
}

// Each galaxy's signature hue (its naming color). Used ONLY for the two "edge"
// classes in the topic view: recent-ring nodes wear the BRIGHT version, orphan
// stars the DIM version — so a galaxy is identifiable by its halo/rings without
// ever flattening the topic-rainbow body. Unknown makers get a stable hashed hue.
const GALAXY_HUE = {'Claude-Code': 136};
for (const k in CAIRN_ACCOUNTS) {
  const c = CAIRN_ACCOUNTS[k];
  if (c && c.label && c.hue != null) GALAXY_HUE[c.label] = c.hue;
}
function atlasGalaxySignature(n, bright) {
  const k = atlasGalaxyKey(n);
  let h = GALAXY_HUE[k];
  if (h === undefined) { h = 0; for (let i = 0; i < k.length; i++) h = (h * 31 + k.charCodeAt(i)) >>> 0; h %= 360; }
  return bright ? `hsl(${h},95%,68%)` : `hsl(${h},58%,62%)`;   // bright recent ring · calmer-but-visible orphan
}
const ATLAS_RECENT_MS = 604800000;   // 7 days — the recency-lens window

function atlasNodeColor(n) {
  const mode = document.getElementById('sel-color').value;
  if (mode === 'kind')   return KIND_COLORS[n.k] || '#6E7681';
  if (mode === 'source') return atlasGalaxyColor(n);
  if (mode === 'model') {
    const m = (n.m || '').toLowerCase();
    for (const k in MODEL_COLORS) if (m.includes(k)) return MODEL_COLORS[k];
    return '#6E7681';
  }
  // topic view: the body is the topic rainbow, but the two edge classes wear the
  // galaxy's signature color — recent → BRIGHT (only while the lens is on),
  // orphan → DIM. Recent falls home to its topic hue when the lens is off.
  if ((recencyOn || recencyAnim > 0) && n.ts && n.ts >= Date.now() - ATLAS_RECENT_MS)
    return atlasGalaxySignature(n, true);
  if (!n.cid) return atlasGalaxySignature(n, false);
  return n.c;   // topic hue — the default
}

// the Show filter, atlas-side — same semantics as the SVG graph's pass()
function atlasKindOk(n) {
  const sel = document.getElementById('sel-filter');
  const m = sel ? sel.value : 'all';
  if (m === 'all') return true;
  if (m === 'meaning') return MEANING_KINDS_SET.has(n.k);
  return n.k === m;
}

// search ON the atlas — the whole vault answers, not the 300-node viewport
async function atlasSearch(q) {
  const panel = document.getElementById('search-results');
  if (!q) {
    atlasFocus = null;
    if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
    drawAtlas();
    setBadge('');
    return;
  }
  panel.innerHTML = '<div style="color:#968F7D">⋯ searching the whole vault…</div>';
  panel.style.display = 'block';
  setBadge('searching the whole vault…');
  let data;
  try {
    data = await fetch(API + '/api/graph_search?q=' + encodeURIComponent(q) +
                       '&k=40').then(r => r.json());
  } catch (e) {
    panel.innerHTML = '<div style="color:#F85149">search failed</div>';
    setBadge('search failed'); return;
  }
  const hits = new Set((data.hits || []).map(h => h.id).filter(id => atlasIdx[id]));
  const ring = new Set();
  (atlasData.edges || []).forEach(e => {
    if (hits.has(e[0]) && atlasIdx[e[1]] && !hits.has(e[1])) ring.add(e[1]);
    if (hits.has(e[1]) && atlasIdx[e[0]] && !hits.has(e[0])) ring.add(e[0]);
  });
  atlasFocus = {hits: hits, ring: ring, q: q};
  if (hits.size) atlasFitToHits(hits);   // frame the hits — never stranded at the rim
  drawAtlas();
  // results list — click a row to open the node + light its connections,
  // exactly like clicking the node on the canvas (straight quotes! curly
  // quotes here silently broke data-id matching and killed list clicks)
  const hits_list = (data.hits || []).filter(h => atlasIdx[h.id]);
  panel.innerHTML = `<div style="color:#968F7D;margin-bottom:4px">hits for "${esc(q)}" — click to open</div>` +
    hits_list.map(h =>
      `<div data-id="${esc(h.id)}" style="cursor:pointer;padding:3px 4px;border-radius:5px;display:flex;gap:8px">` +
      `<span style="color:#7CA38C;min-width:34px">${h.score.toFixed(2)}</span>` +
      `<span>${esc((h.gist || '').slice(0, 64))}</span></div>`
    ).join('');
  panel.style.display = hits_list.length ? 'block' : 'none';
  panel.onclick = e => {
    e.stopPropagation();
    const row = e.target.closest('[data-id]');
    if (row) atlasGoToNode(row.dataset.id);
  };
  panel.onmouseover = e => { const r = e.target.closest('[data-id]'); if (r) r.style.background = '#2C2A21'; };
  panel.onmouseout  = e => { const r = e.target.closest('[data-id]'); if (r) r.style.background = ''; };
  setBadge(`atlas "${q}" — ${hits.size} hits across the whole vault · ` +
           `${ring.size} strongly wired neighbors · Esc clears`);
}

// open a node from the list exactly like clicking it on the canvas: light its
// connections (hover-reveal keys off atlasHover), center it, open the detail
function atlasGoToNode(id) {
  const n = atlasIdx[id];
  if (n) {
    atlasHover = n;
    const P = atlasPositions(), p = P[id];
    const c = document.getElementById('atlas-canvas');
    if (p && c) {
      atlasT.x = c.clientWidth / 2 - p[0] * atlasT.k;
      atlasT.y = c.clientHeight / 2 - p[1] * atlasT.k;
    }
    drawAtlas();
  }
  showDetail(id);
}

function atlasNodeAt(mx, my) {
  const mode = document.getElementById('sel-layout').value;
  const P = atlasPositions();
  if (atlasGridMode !== mode) {       // hover grid follows the geometry
    atlasGrid = {};
    for (const n of atlasData.nodes) {
      const p = P[n.id];
      const key = Math.floor(p[0] / ATLAS_CELL) + ':' +
                  Math.floor(p[1] / ATLAS_CELL);
      (atlasGrid[key] = atlasGrid[key] || []).push(n);
    }
    atlasGridMode = mode;
  }
  const wx = (mx - atlasT.x) / atlasT.k, wy = (my - atlasT.y) / atlasT.k;
  const cx = Math.floor(wx / ATLAS_CELL), cy = Math.floor(wy / ATLAS_CELL);
  let best = null, bd = 1e9;
  for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
    const cell = atlasGrid[(cx + dx) + ':' + (cy + dy)];
    if (!cell) continue;
    for (const n of cell) {
      if (!timeOk(n.ts) || !atlasKindOk(n) || !atlasGalaxyOk(n)) continue;   // never hover a ghost
      const p = P[n.id];
      const d = (p[0] - wx) ** 2 + (p[1] - wy) ** 2;
      if (d < bd) { bd = d; best = n; }
    }
  }
  return (best && Math.sqrt(bd) * atlasT.k < 10) ? best : null;
}

function atlasInitEvents(c) {
  ['mousemove', 'mousedown', 'wheel', 'keydown'].forEach(ev =>
    window.addEventListener(ev, () => { if (atlasOn) noteInteraction(); }, {passive: true}));   // reset the idle→dream timer
  c.addEventListener('wheel', e => {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const wx = (mx - atlasT.x) / atlasT.k, wy = (my - atlasT.y) / atlasT.k;
    atlasT.k = Math.min(20, Math.max(0.008, atlasT.k * Math.pow(1.15, -e.deltaY / 100)));
    atlasT.x = mx - wx * atlasT.k;
    atlasT.y = my - wy * atlasT.k;
    requestAnimationFrame(drawAtlas);
  }, {passive: false});
  c.addEventListener('mousedown', e => {
    atlasDrag = {x: e.clientX, y: e.clientY, tx: atlasT.x, ty: atlasT.y, moved: false};
    c.style.cursor = 'grabbing';
  });
  window.addEventListener('mousemove', e => {
    if (!atlasOn) return;
    if (atlasDrag) {
      atlasT.x = atlasDrag.tx + (e.clientX - atlasDrag.x);
      atlasT.y = atlasDrag.ty + (e.clientY - atlasDrag.y);
      if (Math.abs(e.clientX - atlasDrag.x) + Math.abs(e.clientY - atlasDrag.y) > 4)
        atlasDrag.moved = true;
      requestAnimationFrame(drawAtlas);
      return;
    }
    const r = c.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right ||
        e.clientY < r.top || e.clientY > r.bottom) return;
    const hit = atlasNodeAt(e.clientX - r.left, e.clientY - r.top);
    if (hit !== atlasHover) {
      atlasHover = hit;
      if (hit) {
        tip.style.display = 'block';
        tip.style.left = (e.clientX - r.left + 14) + 'px';
        tip.style.top  = (e.clientY - r.top - 10) + 'px';
        const w = fmtWhen(hit.timestamp || hit.ts);
        tip.innerHTML = `<b>${esc(hit.k)}</b><br><span style="color:#968F7D">${esc(hit.g)}</span>` +
          (w ? `<br><span style="color:#6E7681">${w}</span>` : '');
      } else {
        tip.style.display = 'none';
      }
      requestAnimationFrame(drawAtlas);
    }
  });
  window.addEventListener('mouseup', () => {
    if (atlasDrag && !atlasDrag.moved && atlasHover) showDetail(atlasHover.id);
    atlasDrag = null;
    c.style.cursor = 'grab';
  });
  window.addEventListener('keydown', e => {
    if (!atlasOn || e.key !== 'Escape') return;
    if (atlasFocus) {
      atlasFocus = null;
      const inp = document.getElementById('graph-q');
      if (inp) inp.value = '';
      drawAtlas();
      setBadge('');
    } else {
      toggleAtlas();
    }
  });
}

// ── Graph search — the layered spotlight ──────────────────────────────────────
// Hybrid search hits glow IN PLACE; their tiered neighbors light as rings:
// ring1 = strong + chain neighbors, ring2 = medium, ring3 = weak. Everything
// else dims. Search NEVER moves a node — a spotlight over stable geography.
// The ring shape is a diagnosis: one tight cluster = coherent topic; faint
// dots scattered across hulls = fragmented thinking, a consolidation target.
let searchActive = false;

async function graphSearch() {
  const q = document.getElementById('graph-q').value.trim();
  if (atlasOn) { return atlasSearch(q); }
  if (!q) { clearGraphSearch(); return; }
  if (!g || !simulation) return;
  setBadge('searching… (first search loads the embedder, ~15s)');
  let data;
  try {
    data = await fetch(API + '/api/graph_search?q=' + encodeURIComponent(q))
      .then(r => r.json());
  } catch (e) { setBadge('search failed'); return; }
  if (layerEdges === null) await loadLayerEdges();

  const nodeMap = {};
  simulation.nodes().forEach(n => nodeMap[n.id] = n);
  const hits = new Set((data.hits || []).map(h => h.id).filter(id => nodeMap[id]));
  const offview = (data.hits || []).length - hits.size;

  // tier adjacency over in-view typed edges + chain adjacency from sim links
  const adj = {strong: {}, medium: {}, weak: {}};
  (layerEdges || []).forEach(e => {
    if (e.type !== 'semantic') return;
    (adj[e.tier][e.source] = adj[e.tier][e.source] || []).push(e.target);
    (adj[e.tier][e.target] = adj[e.tier][e.target] || []).push(e.source);
  });
  const chainAdj = {};
  (currentGraph.links || []).forEach(l => {
    const s = l.source.id || l.source, t = l.target.id || l.target;
    (chainAdj[s] = chainAdj[s] || []).push(t);
    (chainAdj[t] = chainAdj[t] || []).push(s);
  });
  const expand = (seeds, table) => {
    const out = new Set();
    seeds.forEach(id => (table[id] || []).forEach(nb => {
      if (!seeds.has(nb)) out.add(nb);
    }));
    return out;
  };

  const ring1 = expand(hits, adj.strong);
  expand(hits, chainAdj).forEach(id => ring1.add(id));
  const lit1  = new Set([...hits, ...ring1]);
  const ring2 = new Set([...expand(lit1, adj.medium)].filter(id => !lit1.has(id)));
  const lit2  = new Set([...lit1, ...ring2]);
  const ring3 = new Set([...expand(lit2, adj.weak)].filter(id => !lit2.has(id)));

  searchActive = true;
  g.selectAll('.search-halo').remove();
  g.selectAll('.node').each(function(d) {
    const op = hits.has(d.id)  ? 1.0  :
               ring1.has(d.id) ? 0.85 :
               ring2.has(d.id) ? 0.50 :
               ring3.has(d.id) ? 0.30 : 0.06;
    d3.select(this).selectAll('circle:not(.session-ring), rect, polygon')
      .transition().duration(250).attr('opacity', op);
    d3.select(this).selectAll('text')
      .transition().duration(250)
      .style('opacity', hits.has(d.id) ? 1 : ring1.has(d.id) ? 0.5 : 0.05)
      .style('display', hits.has(d.id) ? 'block' : null);
    if (hits.has(d.id)) {
      d3.select(this).append('circle')
        .attr('class', 'search-halo').attr('r', 17)
        .attr('fill', 'none').attr('stroke', '#7CA38C')
        .attr('stroke-width', 2).attr('stroke-opacity', 0.9)
        .attr('pointer-events', 'none');
    }
  });
  g.selectAll('.link').transition().duration(250).style('opacity', 0.12);

  // results panel — the answer to "ok but WHICH ones?"
  const panel = document.getElementById('search-results');
  const rows = (data.hits || []).map(h => {
    const inView = hits.has(h.id);
    return `<div onclick="jumpToHit('${jesc(h.id)}', ${inView})" ` +
      `style="cursor:pointer;padding:3px 4px;border-radius:5px;display:flex;gap:8px" ` +
      `onmouseover="this.style.background='#2C2A21';highlightEdges('${jesc(h.id)}')" ` +
      `onmouseout="this.style.background='';unhighlightEdges()">` +
      `<span style="color:#7CA38C;min-width:34px">${h.score.toFixed(2)}</span>` +
      `<span style="${inView ? '' : 'color:#6E7681'}">${esc((h.gist || '').slice(0, 64))}` +
      `${inView ? '' : ' <i>(off-view)</i>'}</span></div>`;
  }).join('');
  panel.innerHTML =
    `<div style="color:#968F7D;margin-bottom:4px">hits for “${esc(q)}” — click to jump</div>` + rows;
  panel.style.display = rows ? 'block' : 'none';

  setBadge(`“${q}” — ${hits.size} hits in view` +
           (offview > 0 ? ` (+${offview} off-view)` : '') +
           ` · rings ${ring1.size}/${ring2.size}/${ring3.size} · Esc clears`);
}

function jumpToHit(id, inView) {
  showDetail(id);
  if (!inView || !simulation) return;
  const n = simulation.nodes().find(nd => nd.id === id);
  if (!n || !isFinite(n.x)) return;
  const panel = document.getElementById('graph-panel');
  const w = panel.clientWidth, h = panel.clientHeight, scale = 1.5;
  svg.transition().duration(500).call(window._zoom.transform,
    d3.zoomIdentity.translate(w/2 - scale*n.x, h/2 - scale*n.y).scale(scale));
}

function clearGraphSearch() {
  const inp = document.getElementById('graph-q');
  if (inp) inp.value = '';
  const panel = document.getElementById('search-results');
  if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
  // atlas spotlight clears too (atlas uses atlasFocus, not searchActive)
  if (atlasOn) { atlasFocus = null; drawAtlas(); setBadge(''); }
  if (!searchActive) return;
  searchActive = false;
  if (g) {
    g.selectAll('.search-halo').remove();
    g.selectAll('.link').transition().duration(250).style('opacity', null);
  }
  applySessionFocus('');
  setBadge('');
}

// ── Pin ego — click a node, see its actual connections, color-coded by
// category. Respects the Links checkboxes for semantic tiers; if none are
// checked, shows ALL tiers for the pinned node (a pin that shows nothing
// would feel broken). Background click clears. Chains green, dendrites gold,
// strong solid blue, medium dashed, weak dotted.
let pinnedEgo = null;
const EGO_STYLE = {
  chain:    {stroke: '#C9A227', width: 1.6, dash: null,  op: 0.85},
  dendrite: {stroke: '#E3B341', width: 1.4, dash: '4,3', op: 0.85},
  strong:   {stroke: '#7CA38C', width: 1.8, dash: null,  op: 0.90},
  medium:   {stroke: '#968F7D', width: 1.3, dash: '6,4', op: 0.75},
  weak:     {stroke: '#6E7681', width: 0.9, dash: '2,4', op: 0.55},
};

async function pinEgo(id) {
  if (atlasOn || !g || !simulation) return;
  pinnedEgo = id;
  if (layerEdges === null) await loadLayerEdges();
  const nodeMap = {};
  simulation.nodes().forEach(n => nodeMap[n.id] = n);
  const tierBox = {strong: 'lk-strong', medium: 'lk-medium', weak: 'lk-weak'};
  const anyChecked = Object.values(tierBox).some(i => {
    const el = document.getElementById(i);
    return el && el.checked;
  });
  const mine = (layerEdges || []).filter(e => {
    if (e.source !== id && e.target !== id) return false;
    if (!nodeMap[e.source] || !nodeMap[e.target]) return false;
    if (e.type === 'semantic' && anyChecked) {
      const el = document.getElementById(tierBox[e.tier]);
      if (!el || !el.checked) return false;
    }
    return true;
  }).map(e => ({...e, source: nodeMap[e.source], target: nodeMap[e.target]}));

  layerSel = g.select('.tier-group').selectAll('line')
    .data(mine, d => d.source.id + '→' + d.target.id + (d.tier || d.type))
    .join('line')
    .attr('stroke',           d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).stroke)
    .attr('stroke-width',     d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).width)
    .attr('stroke-dasharray', d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).dash)
    .attr('stroke-opacity',   d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).op)
    .attr('pointer-events', 'none')
    .attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
    .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);

  const nbr = new Set([id]);
  mine.forEach(e => { nbr.add(e.source.id); nbr.add(e.target.id); });
  g.selectAll('.node').each(function(d) {
    d3.select(this).selectAll('circle:not(.session-ring), rect, polygon')
      .transition().duration(200)
      .attr('opacity', nbr.has(d.id) ? 1 : 0.15);
  });
  const counts = {};
  mine.forEach(e => { const k = e.tier || e.type; counts[k] = (counts[k] || 0) + 1; });
  setBadge(mine.length
    ? 'connections: ' + Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(' · ') +
      ' — background click clears'
    : 'no edges for this node yet (cairn edges builds them nightly)');
}

function unpinEgo() {
  if (!pinnedEgo) return;
  pinnedEgo = null;
  applySessionFocus(focusedSession || '');
  applyLinkLayers();
  setBadge('');
}

// hover = the atlas behavior on SVG: a node's connections appear the moment
// you point at it, color-coded by category, gone when you leave. Pin (click)
// outranks hover.
function hoverEgo(id) {
  if (pinnedEgo || !g || !simulation || layerEdges === null) return;
  const nodeMap = {};
  simulation.nodes().forEach(n => nodeMap[n.id] = n);
  const mine = (layerEdges || [])
    .filter(e => (e.source === id || e.target === id)
              && nodeMap[e.source] && nodeMap[e.target])
    .map(e => ({...e, source: nodeMap[e.source], target: nodeMap[e.target]}));
  g.select('.hover-ego').selectAll('line')
    .data(mine, d => d.source.id + '→' + d.target.id + (d.tier || d.type))
    .join('line')
    .attr('stroke',           d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).stroke)
    .attr('stroke-width',     d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).width)
    .attr('stroke-dasharray', d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).dash)
    .attr('stroke-opacity',   d => (EGO_STYLE[d.tier || d.type] || EGO_STYLE.chain).op * 0.8)
    .attr('pointer-events', 'none')
    .attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
    .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);
}

function clearHoverEgo() {
  if (g) g.select('.hover-ego').selectAll('line').remove();
}

async function applyLinkLayers() {
  saveProfile();                          // persist the link-layer choices
  if (atlasOn) { drawAtlas(); return; }   // checkboxes repaint the atlas live
  if (!g || !simulation) return;
  const st = {
    chain:    document.getElementById('lk-chain').checked,
    dendrite: document.getElementById('lk-dendrite').checked,
    strong:   document.getElementById('lk-strong').checked,
    medium:   document.getElementById('lk-medium').checked,
    weak:     document.getElementById('lk-weak').checked,
  };
  // chains + dendrites are the structural links already in the simulation —
  // visibility (not display) so the kind-filter in applyView still composes
  g.selectAll('.link').style('visibility', function() {
    const isDen = this.classList.contains('dendrite');
    return (isDen ? st.dendrite : st.chain) ? null : 'hidden';
  });

  const wanted = ['strong', 'medium', 'weak'].filter(t => st[t]);
  if (wanted.length && layerEdges === null) await loadLayerEdges();

  const nodeMap = {};
  simulation.nodes().forEach(n => nodeMap[n.id] = n);
  const data = (layerEdges || [])
    .filter(e => e.type === 'semantic' && wanted.includes(e.tier)
              && nodeMap[e.source] && nodeMap[e.target])
    .map(e => ({...e, source: nodeMap[e.source], target: nodeMap[e.target]}));

  layerSel = g.select('.tier-group').selectAll('line')
    .data(data, d => d.source.id + '→' + d.target.id)
    .join('line')
    .attr('stroke', d => TIER_STYLE[d.tier].stroke)
    .attr('stroke-width', d => TIER_STYLE[d.tier].width)
    .attr('stroke-dasharray', d => TIER_STYLE[d.tier].dash)
    .attr('stroke-opacity', d => TIER_STYLE[d.tier].op)
    .attr('pointer-events', 'none')
    .attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
    .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);
}

// ── Spotlight: Juggl-style local ego-graph (double-click a node) ──────────────
let spotlightId = null;
function spotlightEgo(centerId) {
  spotlightId = centerId;
  // 2-hop neighborhood over parent links + dendrites
  const adj = {};
  currentGraph.links.forEach(l => {
    const s = l.source.id || l.source, t = l.target.id || l.target;
    (adj[s] = adj[s] || []).push(t);
    (adj[t] = adj[t] || []).push(s);
  });
  const lit = new Set([centerId]);
  (adj[centerId] || []).forEach(n1 => {
    lit.add(n1);
    (adj[n1] || []).forEach(n2 => lit.add(n2));
  });
  g.selectAll('.node').attr('opacity', d => lit.has(d.id) ? 1 : 0.06);
  g.selectAll('.link').attr('stroke-opacity', l => {
    const s = l.source.id || l.source, t = l.target.id || l.target;
    return (lit.has(s) && lit.has(t)) ? 0.85 : 0.03;
  });
  if (semLinkSel) semLinkSel.attr('stroke-opacity', l => {
    const s = l.source.id || l.source, t = l.target.id || l.target;
    return (lit.has(s) && lit.has(t)) ? 0.5 : 0.02;
  });
}
function clearSpotlight() {
  if (!spotlightId) return;
  spotlightId = null;
  g.selectAll('.node').attr('opacity', d => d.orphan ? 0.5 : 1);
  g.selectAll('.link').attr('stroke-opacity', null);
  if (semLinkSel) semLinkSel.attr('stroke-opacity', null);
}

function toggleLegend() {
  const el = document.getElementById('legend');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ── Session list ─────────────────────────────────────────────────────────────
async function loadSessions() {
  const data = await fetch(API + '/api/sessions').then(r => r.json());
  const sessions = data.sessions || data;   // back-compat with array shape
  const sel = document.getElementById('session-sel');
  sessions.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.id.slice(-30) + ' (' + s.node_count + ')';
    sel.appendChild(opt);
  });
  // imported archive — provenance summary, not selectable sessions.
  // The archive lives in the graph, topics, and search; the picker is
  // for sessions you actually worked.
  const archive = data.archive || [];
  if (archive.length) {
    const og = document.createElement('optgroup');
    og.label = '— imported archive —';
    archive.forEach(a => {
      const opt = document.createElement('option');
      opt.value = '__account__:' + a.account;
      opt.textContent = `${a.account}: ${a.sessions} convos · ` +
                        `${a.node_count} turns · ${a.first_day} → ${a.last_day}`;
      og.appendChild(opt);
    });
    sel.appendChild(og);
  }
}

async function loadSession(sessId) {
  exitAtlas(true);   // picking any view drills DOWN out of the atlas
  // "All sessions" → the cross-session knowledge graph (single scope control,
  // replaces the old separate All Sessions button)
  if (sessId === '__all__') {
    currentAccount = '';
    currentSession = '';
    focusedSession = '';
    if (showSemantic) { applySessionFocus(''); return; }
    showCrossSession = true;
    const data = await fetch(API + '/api/graph?cross=true').then(r => r.json());
    currentGraph = data;
    renderGraph(data);
    setBadge(`all sessions — ${data.nodes.length} highest-signal nodes ` +
             `(knowledge first, then origins) · the Atlas holds everything`);
    return;
  }
  // archive account selected → load that import as its own view:
  // chronological conversation chains, capped at 1000 nodes for legibility
  if (sessId && sessId.startsWith('__account__:')) {
    currentAccount = sessId.slice(12);
    currentSession = '';
    focusedSession = '';
    showCrossSession = false;
    const data = await fetch(API + '/api/graph?account=' +
      encodeURIComponent(currentAccount)).then(r => r.json());
    currentGraph = data;
    renderGraph(data);
    setBadge(`archive “${currentAccount}” — showing ${data.nodes.length} nodes ` +
             `(chains = conversations; full archive via search/topics)`);
    return;
  }
  currentAccount = '';
  currentSession = sessId;
  showCrossSession = false;
  if (showSemantic) {
    // ── GraphRAG mode: dropdown = focus lens, not a full reload ──────────────
    // The cross-session graph stays intact. We just dim/highlight.
    focusedSession = sessId;
    applySessionFocus(sessId);
    // status bar still reflects vault totals, not per-session; no need to reload
  } else {
    // ── Normal mode: load only this session (existing behaviour) ─────────────
    focusedSession = '';
    await loadGraph(sessId);
    await updateStatus();
  }
}

// ── D3 force graph ────────────────────────────────────────────────────────────
function initGraph() {
  const panel = document.getElementById('graph-panel');
  const w = panel.clientWidth, h = panel.clientHeight;

  svg = d3.select('#graph-svg')
    .attr('width', w).attr('height', h);

  const zoom = d3.zoom().scaleExtent([0.1, 8])
    .on('zoom', e => g.attr('transform', e.transform));
  svg.call(zoom);
  window._zoom = zoom;

  g = svg.append('g');

  // gradient background grid
  const defs = svg.append('defs');
  const grid = defs.append('pattern')
    .attr('id', 'grid').attr('width', 40).attr('height', 40)
    .attr('patternUnits', 'userSpaceOnUse');
  grid.append('path').attr('d', 'M 40 0 L 0 0 0 40')
    .attr('fill', 'none').attr('stroke', '#1A1811').attr('stroke-width', '0.5');
  svg.insert('rect', 'g')
    .attr('width', w).attr('height', h).attr('fill', 'url(#grid)');
}

async function loadGraph(sessId) {
  const url = sessId ? API + '/api/graph?sess=' + encodeURIComponent(sessId) : API + '/api/graph';
  const data = await fetch(url).then(r => r.json());
  currentGraph = data;
  renderGraph(data);
}

function renderGraph(data) {
  g.selectAll('*').remove();
  semanticEdges = [];
  semLinkSel    = null;
  semSnapshot   = null;   // new simulation, new node objects — old positions are meaningless
  layerEdges    = null;   // view changed — typed edges refetch lazily
  layerSel      = null;
  topicPalette  = null;   // topic colors recompute for the new node set
  const panel = document.getElementById('graph-panel');
  const w = panel.clientWidth, h = panel.clientHeight;

  sessionColorMap = data.session_colors || {};
  currentGraph.projectColors = data.project_colors || {};
  hullG = null;

  const nodeMap = {};
  data.nodes.forEach(n => nodeMap[n.id] = n);

  // ── Pre-spread nodes so they don't pile up at center on init ─────────────────
  // Group by session first so same-session nodes start near each other
  const sessions = [...new Set(data.nodes.map(n => n.session))];
  const sessCount = sessions.length || 1;
  data.nodes.forEach((d, i) => {
    if (d.x === undefined || d.x === null) {
      // place each session in its own sector of a ring
      const si   = sessions.indexOf(d.session);
      const angle = (si / sessCount) * 2 * Math.PI + (Math.random() - 0.5) * 0.8;
      const r     = (w < h ? w : h) * 0.28 + Math.random() * 80;
      d.x = w/2 + Math.cos(angle) * r;
      d.y = h/2 + Math.sin(angle) * r;
    }
  });

  // ── Semantic link layer — created BEFORE parent links so it renders beneath
  const semG = g.append('g').attr('class', 'sem-group');
  semLinkSel  = semG.selectAll('line');  // empty — populated by renderSemanticEdges()
  // typed/tiered overlay layer (Links checkboxes) — also beneath parent links
  g.append('g').attr('class', 'tier-group');
  g.append('g').attr('class', 'hover-ego');   // transient hover connections
  loadLayerEdges();   // prefetch so hover connections appear instantly

  // charge scales with node count so clusters stay legible at any size
  const chargeStr = data.nodes.length > 100 ? -200 : -120;

  window._autoFitDone = false;   // fresh graph → allow exactly one auto-fit
  simulation = d3.forceSimulation(data.nodes)
    .velocityDecay(0.5)          // settle firmly (d3 default 0.4 stayed jittery)
    .alphaMin(0.02)              // come to rest sooner, then stay put → grabbable
    .force('link', d3.forceLink(data.links)
      .id(d => d.id).distance(60).strength(0.4))
    .force('charge', d3.forceManyBody().strength(chargeStr))
    .force('center', d3.forceCenter(w/2, h/2).strength(0.04))
    .force('collision', d3.forceCollide(18));

  // links
  const link = g.append('g').selectAll('line')
    .data(data.links).join('line')
    .attr('class', d => {
      // In cross-session mode, color edge by whether it crosses sessions
      if (d.type === 'dendrite') return 'link dendrite';
      const src = nodeMap[d.source.id || d.source];
      const tgt = nodeMap[d.target.id || d.target];
      const crossModel = src && tgt && src.model !== tgt.model;
      return 'link' + (crossModel ? ' cross-model' : '');
    });

  // nodes
  const node = g.append('g').selectAll('.node')
    .data(data.nodes).join('g')
    .attr('class', 'node')
    .call(d3.drag()
      .on('start', (e, d) => { clearHoverEgo(); if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag',  (e, d) => { d.fx=e.x; d.fy=e.y; })
      .on('end',   (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx=null; d.fy=null; }))
    .on('click',  (e, d) => { showDetail(d.id); pinEgo(d.id); })
    .on('dblclick', (e, d) => { e.stopPropagation(); spotlightEgo(d.id); })
    .on('mouseover', (e, d) => { showTooltip(e, d); highlightEdges(d.id); hoverEgo(d.id); })
    .on('mouseout', () => { hideTooltip(); unhighlightEdges(); clearHoverEgo(); });

  // click empty background → clear spotlight + pinned ego
  svg.on('click.spotlight', (e) => {
    if (e.target.tagName === 'svg') { clearSpotlight(); unpinEgo(); }
  });
  // hover lines must never outlive the hover — drag and canvas-exit both
  // swallow mouseout, so clear on leave too
  svg.on('mouseleave.hego', clearHoverEgo);

  // shape by kind
  node.each(function(d) {
    const el = d3.select(this);
    // Obsidian-style: size grows with backlink count (connectedness).
    // Base by tier/kind, then add a log-scaled degree boost.
    let r = d.memory_tier === 0 ? 9 : d.kind === 'tool_call' ? 6 : 7;
    r += Math.min(11, Math.log2(1 + (d.degree || 0)) * 2.4);
    // orphans (no backlinks) dim back — connected hubs should dominate the eye
    const opacity = d.status === 'void' ? 0.35 : d.orphan ? 0.5 : 1.0;
    const stroke = d.struggle ? '#F85149' : d.orphan ? '#2C2A21' : '#3A3528';

    // Consolidated synthesis nodes — the neocortex layer. Render as larger
    // gold cortical nodes; their dashed dendrites reach absorbed episodes.
    if (d.consolidated) {
      r = 13;
      el.append('circle')
        .attr('r', r).attr('fill', '#E3B341').attr('stroke', '#F0CC60')
        .attr('stroke-width', 2).attr('opacity', opacity);
      el.append('circle')
        .attr('r', r + 4).attr('fill', 'none')
        .attr('stroke', '#E3B341').attr('stroke-width', 1)
        .attr('stroke-opacity', 0.4);
      if (d.pulse) {
        el.append('circle').attr('class', 'pulse-halo').attr('r', r + 7);
      }
      return;
    }

    // fill always comes from nodeFill() so the Color dropdown owns it
    const fill = nodeFill(d);
    if (d.shape === 'diamond') {
      el.append('rect')
        .attr('width', r*1.6).attr('height', r*1.6)
        .attr('transform', `rotate(45) translate(${-r*0.8},${-r*0.8})`)
        .attr('fill', fill).attr('stroke', stroke)
        .attr('opacity', opacity);
    } else if (d.shape === 'star' || d.kind === 'warning' || d.kind === 'blocker') {
      el.append('circle')
        .attr('r', r).attr('fill', fill).attr('stroke', '#F85149')
        .attr('stroke-width', 2).attr('opacity', opacity);
    } else {
      el.append('circle')
        .attr('r', r).attr('fill', fill).attr('stroke', stroke)
        .attr('opacity', opacity);
    }

    // ring for flagged
    if (d.flagged) {
      el.append('circle').attr('r', r+3)
        .attr('fill', 'none').attr('stroke', '#D29922')
        .attr('stroke-width', 1.5).attr('stroke-dasharray', '3,2');
    }

    // injection pulse — this memory surfaced into a model's context recently
    if (d.pulse) {
      el.append('circle').attr('class', 'pulse-halo').attr('r', r + 6);
    }
  });

  // labels
  const label = node.append('text')
    .attr('dy', 16).attr('text-anchor', 'middle')
    .text(d => d.label)
    .style('display', showLabels ? 'block' : 'none');

  // In cross-session mode, ring each node with a session-colour halo.
  // class="session-ring" keeps applySessionFocus from dimming these —
  // they're identity markers and should stay visible even when a session is focused.
  if (data.cross) {
    node.each(function(d) {
      const col = sessionColorMap[d.session] || '#6E7681';
      d3.select(this).append('circle')
        .attr('class', 'session-ring')
        .attr('r', 14).attr('fill', 'none')
        .attr('stroke', col).attr('stroke-width', 2)
        .attr('stroke-opacity', 0.35)
        .attr('stroke-dasharray', '3,2')
        .attr('pointer-events', 'none');
    });
  }

  let tickN = 0;
  simulation.on('tick', () => {
    link
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
    if (semLinkSel) {
      semLinkSel
        .attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
        .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);
    }
    if (layerSel) {
      layerSel
        .attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
        .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);
    }
    const hego = g.select('.hover-ego').selectAll('line');
    if (!hego.empty()) {
      hego.attr('x1', d => d.source.x || 0).attr('y1', d => d.source.y || 0)
          .attr('x2', d => d.target.x || 0).attr('y2', d => d.target.y || 0);
    }
    if (hullG && ++tickN % 4 === 0) updateHulls();
  });

  // Auto-fit only the FIRST time a fresh graph cools — not on every reheat
  // (relayout/drag reheat the sim; re-fitting each time made the view zoom on
  // its own). The Fit button calls fitView() directly anytime.
  simulation.on('end', () => {
    if (!window._autoFitDone) { fitView(); window._autoFitDone = true; }
    updateHulls();
  });
  // apply the current dropdown selections to the fresh graph
  relayout();
  applyLinkLayers();
}

function fitView() {
  if (atlasOn) { atlasFit(document.getElementById('atlas-canvas')); return; }  // Galaxy view: fit the atlas, not the (inactive) SVG graph
  if (!g || !simulation) return;
  const nodes = simulation.nodes();
  if (!nodes.length) return;

  const panel = document.getElementById('graph-panel');
  const w = panel.clientWidth, h = panel.clientHeight;
  const PAD = 60;

  const xs = nodes.map(n => n.x).filter(x => isFinite(x));
  const ys = nodes.map(n => n.y).filter(y => isFinite(y));
  if (!xs.length) return;

  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const bw = x1 - x0 || 1, bh = y1 - y0 || 1;

  const scale = Math.min(0.95, Math.min((w - PAD*2) / bw, (h - PAD*2) / bh));
  const tx = w/2 - scale * (x0 + bw/2);
  const ty = h/2 - scale * (y0 + bh/2);

  svg.transition().duration(600)
    .call(window._zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(scale));
}

function resetZoom() {
  svg.transition().duration(400).call(window._zoom.transform, d3.zoomIdentity);
}

function toggleLabels() {
  if (atlasOn) {                 // in the atlas, Labels = topic names
    atlasLabelsOn = !atlasLabelsOn;
    drawAtlas();
    return;
  }
  showLabels = !showLabels;
  g.selectAll('.node text').style('display', showLabels ? 'block' : 'none');
}

// ── Tooltip ───────────────────────────────────────────────────────────────────
const tip = document.getElementById('tooltip');
// compact "Jun 11, 3:42 PM" from an ISO string or ms epoch; '' if unparseable
function fmtWhen(v) {
  if (v === null || v === undefined || v === '') return '';
  const dt = new Date(v);
  if (isNaN(dt.getTime())) return '';
  return dt.toLocaleString(undefined,
    {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
}
function showTooltip(event, d) {
  tip.style.display = 'block';
  tip.style.left = (event.offsetX + 12) + 'px';
  tip.style.top  = (event.offsetY - 10) + 'px';
  const struggle = d.struggle ? ' ⚠ slow/empty' : '';
  const topic = d.topic ? `<br><span style="color:#6E7681">topic: ${esc(d.topic)}</span>` : '';
  const when = fmtWhen(d.timestamp || d.ts);
  const whenLine = when ? `<br><span style="color:#6E7681">${when}</span>` : '';
  tip.innerHTML = `<b>${esc(d.tool || d.kind)}</b> [${esc(d.model)}]${struggle}<br>
    <span style="color:#968F7D">${esc(d.query || '')}</span>${topic}${whenLine}`;
}
function hideTooltip() { tip.style.display = 'none'; }

// ── Node detail panel ─────────────────────────────────────────────────────────
function fmtTok(t) {
  if (t == null) return '0';
  return t >= 1000 ? (t/1000).toFixed(1) + 'k' : String(t);
}

async function rebuildLayout() {
  const btn = document.getElementById('btn-rebuild');
  const label = btn ? btn.textContent : '';
  if (btn) { btn.textContent = '↻ rebuilding…'; btn.disabled = true; }
  try {
    const r = await fetch(API + '/api/rebuild', {method: 'POST'}).then(r => r.json());
    if (r.ok) { location.reload(); return; }   // redraw atlas with fresh coords
    console.warn('rebuild failed:', r.error || r.step);
  } catch (e) { console.warn('rebuild error', e); }
  if (btn) { btn.textContent = label; btn.disabled = false; }
}

// ── Tag fold: human chips + machine strata behind "provenance N ▸" ──────────
// Mirrors garden.py's _is_machine_tag prefix denylist. DISPLAY only — every
// tag stays untouched in the vault and in retrieval. Declared BEFORE its
// callers (consts don't hoist; house declaration-order rule).
const MACHINE_TAG_PREFIXES = ['kw:','entity:','prov:','by:','stance:',
  'account:','turn:','member:','due:'];
function isMachineTag(t) {
  return typeof t === 'string' && MACHINE_TAG_PREFIXES.some(p => t.startsWith(p));
}
function tagFoldHTML(tagsRaw) {
  let tags = tagsRaw;
  if (typeof tags === 'string') { try { tags = JSON.parse(tags); } catch(e) { tags = []; } }
  if (!Array.isArray(tags)) tags = [];
  tags = tags.filter(t => typeof t === 'string' && t);
  if (!tags.length) return '';
  const human   = tags.filter(t => !isMachineTag(t));
  const machine = tags.filter(isMachineTag);
  const chip  = t => `<span class="detail-badge" style="background:#26231A;color:#C9BFA9">${esc(t)}</span>`;
  const mchip = t => `<span class="detail-badge" style="background:#1C2128;color:#6E7681;font-family:monospace">${esc(t)}</span>`;
  const fold = machine.length
    ? `<span onclick="toggleProvFold(event,this)" title="machine strata — retrieval plumbing the AI uses; folded away, never deleted" ` +
      `style="cursor:pointer;color:#6E7681;font-size:10px;user-select:none;white-space:nowrap">` +
      `provenance ${machine.length} <span class="prov-caret">▸</span></span>` +
      `<span style="display:none">${machine.map(mchip).join('')}</span>`
    : '';
  if (!human.length && !fold) return '';
  return `<div class="detail-field"><div style="display:flex;flex-wrap:wrap;align-items:center;gap:3px">` +
    `${human.map(chip).join('')}${fold}</div></div>`;
}
function toggleProvFold(e, el) {
  e.stopPropagation();
  const zone = el.nextElementSibling;
  const caret = el.querySelector('.prov-caret');
  if (!zone) return;
  const open = zone.style.display === 'none';
  zone.style.display = open ? 'inline' : 'none';
  if (caret) caret.textContent = open ? '▾' : '▸';
}

async function showDetail(nodeId) {
  const data = await fetch(API + '/api/node/' + encodeURIComponent(nodeId)).then(r => r.json());
  const n = data.node;
  const chain = data.chain;

  const MODEL_BADGE_COLORS = {
    'claude': '#1F6FEB', 'gpt': '#10A37F',
    'llama': '#F97316', 'gemini': '#EA4335',
  };
  function modelBadge(model) {
    const m = (model || 'unknown').toLowerCase();
    let bg = '#484F58';
    for (const [k,v] of Object.entries(MODEL_BADGE_COLORS))
      if (m.startsWith(k)) bg = v;
    return `<span class="detail-badge" style="background:${bg}">${esc(model || 'unknown')}</span>`;
  }

  const struggle = (n.latency_ms > 800 || (n.result_count !== null && n.result_count <= 1))
    ? '<span class="detail-badge" style="background:#7D1F1F;color:#F85149">STRUGGLE</span>' : '';
  const flagged = n.flagged
    ? '<span class="detail-badge" style="background:#5A3E00;color:#D29922">FLAGGED</span>' : '';
  const voided = n.status === 'void'
    ? '<span class="detail-badge" style="background:#1C2128;color:#6E7681">VOID</span>' : '';
  const tier = ['HOT', 'WARM', 'COLD'][n.memory_tier || 1];
  const tierColors = ['#A371F7','#7CA38C','#484F58'];
  const tierBadge = `<span class="detail-badge" style="background:${tierColors[n.memory_tier||1]}20;color:${tierColors[n.memory_tier||1]};border:1px solid ${tierColors[n.memory_tier||1]}40">${tier}</span>`;

  const chainHtml = chain.map((r, i) =>
    `<div class="chain-item" style="margin-left:${Math.min(i,6)*8}px">
      <span class="chain-tool">${esc(r.tool || r.kind)}</span>
      <span style="color:#484F58"> [${esc(r.model || '?')}]</span>
      <span style="color:#6E7681"> ${esc((r.query||'').slice(0,50))}</span>
    </div>`
  ).join('');

  // thread — read the conversation around this turn, not just the turn
  const th = data.thread || {};
  const threadHtml = (th.prev || (th.next || []).length) ? `
    <div class="detail-field">
      <div class="detail-label">Thread — conversation flow</div>
      ${th.prev ? `<div class="chain-item" onclick="showDetail('${jesc(th.prev.id)}')"
        style="cursor:pointer"><span style="color:#7CA38C">◀</span>
        <span style="color:#484F58">${th.prev.speaker === 'user' ? 'user' : 'ai'}</span>
        ${esc((th.prev.gist || '').slice(0, 70))}</div>` : ''}
      <div class="chain-item" style="color:#E8E2D2">● this node</div>
      ${(th.next || []).map(x => `<div class="chain-item"
        onclick="showDetail('${jesc(x.id)}')" style="cursor:pointer">
        <span style="color:#7CA38C">▶</span>
        <span style="color:#484F58">${x.speaker === 'user' ? 'user' : 'ai'}</span>
        ${esc((x.gist || '').slice(0, 70))}</div>`).join('')}
    </div>` : '';

  // connections — the backlinks panel: hop the graph without searching
  const TIER_DOT = {strong: '#7CA38C', medium: '#968F7D', weak: '#6E7681'};
  const conns = data.connections || [];
  const connHtml = conns.length ? `
    <div class="detail-field">
      <div class="detail-label">Connections (${conns.length})</div>
      ${conns.map(c => `<div class="chain-item" onclick="showDetail('${jesc(c.id)}')"
        style="cursor:pointer">
        <span style="color:${TIER_DOT[c.tier] || '#C9A227'}">●</span>
        <span style="color:#484F58">${esc(c.tier || c.type)}</span>
        ${esc((c.gist || '').slice(0, 58))}
        ${c.topic ? `<span style="color:#484F58"> · ${esc(c.topic)}</span>` : ''}
      </div>`).join('')}
    </div>` : '';

  // notes — human annotations attached to this node (quiet vs kept-as-memory)
  const notes = data.notes || [];
  const notesHtml = notes.length ? `
    <div class="detail-field">
      <div class="detail-label">📝 Notes (${notes.length})</div>
      ${notes.map(nt => `<div class="chain-item" onclick="showDetail('${jesc(nt.id)}')"
        style="cursor:pointer">
        <span style="color:${nt.memory ? '#A371F7' : '#6E7681'}">●</span>
        <span style="color:#484F58">${nt.memory ? 'kept' : 'quiet'}</span>
        ${esc((nt.gist || '').slice(0, 64))}
      </div>`).join('')}
    </div>` : '';

  const rcpt = data.receipts || {shown: 0, cited: 0};
  const receiptsHtml = `
    <div class="detail-field">
      <div class="detail-label">Attention receipts</div>
      <div class="detail-value" style="color:#968F7D">${rcpt.shown
        ? `surfaced to a model ${rcpt.shown}x · actually used ${rcpt.cited}x`
        : 'never surfaced to a model yet'}
        ${data.topic ? ` · topic: <span style="color:#E3B341">${esc(data.topic)}</span>` : ''}</div>
    </div>`;

  // FIX 5 — human-first: lead with the sentence a person actually reads, then
  // a friendly one-line meta, THEN the machine internals lower down.
  const headline = n.gist || n.query || n.episodic_text || '(no text)';
  const _sameStart = (a, b) => { a = (a||'').trim(); b = (b||'').trim(); const m = Math.min(a.length, b.length, 80); return m >= 20 && a.slice(0,m) === b.slice(0,m); };   // render-once: one field is a truncation of another
  const whenFull = n.timestamp ? new Date(n.timestamp).toLocaleString() : '';
  const metaBits = [
    n.kind + (n.tool ? ' → ' + n.tool : ''),
    (n.model || 'unknown'),
    (n.session || ''),
    whenFull,
  ].filter(Boolean);

  document.getElementById('detail-content').innerHTML = `
    <div class="detail-field">
      <div class="detail-value" style="font-family:Georgia,'Times New Roman',serif;font-size:19px;font-style:italic;color:#E8E2D2;line-height:1.3">${esc(headline)}</div>
    </div>
    <div class="detail-field">
      <div class="detail-value" style="color:#968F7D;font-size:11px">${metaBits.map(esc).join(' · ')}</div>
    </div>
    ${tagFoldHTML(n.tags)}
    ${n.query && n.query !== headline ? `
    <div class="detail-field">
      <div class="detail-label">Query (verbatim)</div>
      <div class="detail-value">${esc(n.query)}</div>
    </div>` : ''}
    ${receiptsHtml}
    ${threadHtml}
    ${notesHtml}
    ${connHtml}
    ${chain.length > 1 ? `
    <div class="detail-field">
      <div class="detail-label">Reasoning Chain (${chain.length} hops)</div>
      ${chainHtml}
    </div>` : ''}
    ${(() => {
      // 5a: tool calls baked into this turn — the work done UNDER the exchange,
      // grouped here instead of scattered as a node per call.
      let tcs = n.tool_calls;
      if (!tcs) return '';
      try { if (typeof tcs === 'string') tcs = JSON.parse(tcs); } catch (e) { return ''; }
      if (!Array.isArray(tcs) || !tcs.length) return '';
      const rows = tcs.map(t => {
        const meta = [t.latency_ms ? t.latency_ms + 'ms' : '',
                      (t.result_count != null) ? t.result_count + ' res' : '']
                     .filter(Boolean).join(' · ');
        const q = esc(t.query || '');
        return `<div style="display:flex;gap:6px;padding:3px 0;border-bottom:1px solid #1A1811">
          <span style="color:#7CA38C;font-family:monospace;font-size:11px;flex-shrink:0">${esc(t.tool || '?')}</span>
          <span style="color:#968F7D;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${q}</span>
          ${meta ? `<span style="color:#6E7681;font-size:10px;flex-shrink:0">${meta}</span>` : ''}
        </div>`;
      }).join('');
      return `<div class="detail-field">
        <div class="detail-label">Tool calls this turn (${tcs.length})</div>
        <div style="margin-top:4px">${rows}</div>
      </div>`;
    })()}
    <!-- machine internals, muted, below the human-readable story -->
    <div class="detail-field" style="margin-top:14px;border-top:1px solid #2C2A21;padding-top:10px">
      <div style="display:flex;flex-wrap:wrap;gap:3px;margin-bottom:8px">
        ${modelBadge(n.model)} ${struggle} ${flagged} ${voided} ${tierBadge}
      </div>
      <div class="detail-label">Node ID</div>
      <div class="detail-value" style="font-family:monospace;color:#7CA38C;font-size:11px">${esc(n.id)}</div>
    </div>
    ${n.stability_days && n.stability_days > 1.0 ? `
    <div class="detail-field">
      <div class="detail-label">FSRS Stability</div>
      <div class="detail-value" style="color:#968F7D;font-size:11px">${Number(n.stability_days).toFixed(1)} days
        ${n.last_injected ? ' · last surfaced ' + new Date(n.last_injected).toLocaleString() : ''}</div>
    </div>` : ''}
    ${n.latency_ms || n.result_count !== null ? `
    <div class="detail-field">
      <div class="detail-label">Performance</div>
      <div class="detail-value" style="color:#968F7D;font-size:11px">
        ${n.latency_ms ? n.latency_ms + 'ms' : ''}
        ${n.result_count !== null ? ' · ' + n.result_count + ' results' : ''}
      </div>
    </div>` : ''}
    ${n.tokens_out != null ? `
    <div class="detail-field">
      <div class="detail-label">Tokens (this turn)</div>
      <div class="detail-value" style="color:#968F7D;font-size:11px">
        ${fmtTok(n.tokens_out)} out · ${fmtTok(n.tokens_in)} in ·
        ${fmtTok(n.tokens_cache_read)} cache-read · ${fmtTok(n.tokens_cache_write)} cache-write
      </div>
    </div>` : ''}
    ${(n.episodic_text && !_sameStart(n.episodic_text, headline) && !_sameStart(n.episodic_text, n.query)) ? `
    <div class="detail-field">
      <div class="detail-label">Episodic Text (embedded vector)</div>
      <div class="detail-value" style="color:#968F7D;font-size:11px">${esc(n.episodic_text)}</div>
    </div>` : ''}
    ${(n.output_preview && !_sameStart(n.output_preview, headline) && !_sameStart(n.output_preview, n.query) && !_sameStart(n.output_preview, n.episodic_text)) ? `
    <div class="detail-field">
      <div class="detail-label">Output Preview</div>
      <div class="detail-value" style="font-size:11px;color:#6E7681">${esc((n.output_preview||'').slice(0,300))}</div>
    </div>` : ''}
  `;
}

// ── Live feed ─────────────────────────────────────────────────────────────────
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
// JS-string-context escape — for a value interpolated INTO an onclick="..." handler
// (a single-quoted JS string inside a double-quoted HTML attribute). HEX-escape the
// quotes/backslash/'<' so no raw quote reaches the HTML stream (a literal " would close
// the attribute, a literal ' the JS string; HTML ignores backslash escapes). Mirrors
// garden.py jesc(). Use for node ids/keys placed in onclick single-quoted args.
function jesc(s){return String(s==null?'':s).replace(/\\\\/g,'\\\\\\\\').replace(/'/g,'\\\\x27').replace(/"/g,'\\\\x22').replace(/</g,'\\\\x3c').replace(/\\r?\\n/g,' ');}

function addFeedItem(node) {
  const container = document.getElementById('feed-items');
  const div = document.createElement('div');
  const isStruggle = node.latency > 800 || (node.results !== null && node.results <= 1);
  const isFlagged  = node.flagged;
  const isStamp    = node.kind === 'context_stamp';
  const isTurn     = node.kind === 'conversation_turn';
  const isLive = node.live === true;   // 5a: ephemeral tool, not yet a node
  div.className = 'feed-item' +
    (isStruggle ? ' struggle' : '') +
    (isFlagged  ? ' flagged'  : '') +
    (isStamp    ? ' stamp'    : '') +
    (isTurn     ? ' turn'     : '') +
    (isLive     ? ' live'     : '');
  // Live tool events have no node yet (they bake into the turn at Stop), so
  // there's nothing to open — only real nodes are clickable.
  if (isLive) { div.style.opacity = '0.7'; }
  else        { div.onclick = () => showDetail(node.id); }

  const MODEL_COLORS = {
    'claude':'#4A9EDB', 'gpt':'#10A37F', 'llama':'#F97316', 'gemini':'#EA4335'
  };
  let mc = '#6E7681';
  for (const [k,v] of Object.entries(MODEL_COLORS))
    if ((node.model||'').toLowerCase().startsWith(k)) mc = v;

  div.innerHTML = `
    <div class="kind" style="color:${mc}">${esc(node.tool || node.kind)}</div>
    <div class="query">${esc(node.query || '')}</div>
    <div class="meta">
      ${node.model !== 'unknown' ? esc(node.model) + ' · ' : ''}
      ${node.latency ? node.latency + 'ms · ' : ''}
      ${node.results !== null && node.results !== undefined ? node.results + ' results' : ''}
      ${node.tokens_out ? ' · ' + (node.tokens_out >= 1000 ? (node.tokens_out/1000).toFixed(1)+'k' : node.tokens_out) + ' tok' : ''}
      ${isFlagged ? '🚩' : ''}
    </div>
  `;
  container.insertBefore(div, container.firstChild);
  if (container.children.length > 100)
    container.removeChild(container.lastChild);
}

// ── SSE stream ────────────────────────────────────────────────────────────────
function startStream() {
  const es = new EventSource(API + '/api/stream');
  es.onmessage = async (e) => {
    const node = JSON.parse(e.data);
    addFeedItem(node);
    // While the atlas is open it owns the canvas and live-fills itself via
    // its own poll — never render the hidden SVG graph underneath (that was
    // the "random graphs flashing" on load).
    if (atlasOn) return;
    // The live FEED updates above (addFeedItem). The force-graph deliberately
    // does NOT rebuild per streamed node: re-fetching + renderGraph on every
    // node re-scattered and re-zoomed the whole layout, which is exactly what
    // made the non-atlas view drift and jump uncontrollably. New nodes appear
    // in the graph on the next explicit refresh — switch view · Fit · Rebuild.
    // (The atlas, the default view, still live-fills via its own poll, so live
    // spatial growth is unaffected there.)
  };
  es.onerror = () => { setTimeout(startStream, 3000); es.close(); };
}

// ── GraphRAG semantic edges ────────────────────────────────────────────────────
async function toggleSemantic() {
  // GraphRAG mode is an SVG-graph experience — step down from the atlas
  // QUIETLY (no auto cross-load: the ON branch below does its own, and two
  // racing loads leave stale edge state behind)
  if (atlasOn) exitAtlas(true);
  showSemantic = !showSemantic;
  document.getElementById('btn-rag').classList.toggle('btn-rag', showSemantic);

  if (showSemantic) {
    // GraphRAG always needs cross-session context to be meaningful.
    // If we're still in single-session view, auto-expand to all sessions now.
    if (!showCrossSession) {
      showCrossSession = true;
      const data = await fetch(API + '/api/graph?cross=true').then(r => r.json());
      currentGraph = data;
      renderGraph(data);
    }
    await loadSemanticEdges();
    // If the user already had a session selected, apply focus immediately
    if (currentSession) {
      focusedSession = currentSession;
      applySessionFocus(focusedSession);
    }
  } else {
    // ── Semantic OFF ─────────────────────────────────────────────────────────
    focusedSession = '';
    // Fade out semantic edges
    if (semLinkSel) semLinkSel.transition().duration(300).attr('stroke-opacity', 0);
    setBadge('');
    if (simulation) simulation.force('sem', null);
    // Put every node back where it sat before the sem force moved it —
    // toggling a lens must never permanently rearrange the map.
    if (simulation && semSnapshot) {
      simulation.nodes().forEach(n => {
        const p = semSnapshot[n.id];
        if (p) { n.x = p[0]; n.y = p[1]; n.vx = 0; n.vy = 0; }
      });
      simulation.alpha(0.12).restart();
    }
    semSnapshot = null;
    // Remove all node dimming (restore normal opacity)
    applySessionFocus('');
    // re-assert the Links checkboxes — semantic mode is gone, the link
    // layers go back to exactly what the user had chosen
    applyLinkLayers();
    // Cross-session graph stays loaded — user can dismiss it with the Knowledge button
  }
}

async function toggleCrossSession() {
  if (showSemantic) {
    // ── In GraphRAG mode, "All Sessions" means "clear focus / show all equal" ─
    // We do NOT reload the graph (it's already cross-session because semantic is on).
    // We just clear the focused session so all nodes render at full opacity.
    focusedSession = '';
    document.getElementById('session-sel').value = '';
    applySessionFocus('');
    // Restore badge to summary view
    if (semLinkSel && semLinkSel.size() > 0) {
      const edgeData = semLinkSel.data();
      const crossCount = edgeData.filter(e => e.source.session !== e.target.session).length;
      setBadge(`${edgeData.length} semantic connections · ${crossCount} cross-session`);
    }
    return;
  }

  // ── Normal mode: toggle the full cross-session graph load ──────────────────
  exitAtlas(true);
  currentAccount = '';
  showCrossSession = !showCrossSession;

  const url = showCrossSession
    ? API + '/api/graph?cross=true'
    : (currentSession ? API + '/api/graph?sess=' + encodeURIComponent(currentSession) : API + '/api/graph');
  const data = await fetch(url).then(r => r.json());
  currentGraph = data;
  renderGraph(data);
  if (showCrossSession) {
    setBadge(`all sessions — ${data.nodes.length} highest-signal nodes ` +
             `(knowledge first, then origins) · the Atlas holds everything`);
  }
}

async function loadSemanticEdges() {
  // GraphRAG always fetches cross-session — that's where the value lives.
  // Consistent 0.62 threshold (74 edges at this level is well-bounded).
  const threshold = 0.62;
  const url = `${API}/api/semantic_edges?threshold=${threshold}&cross=true`;

  setBadge('Computing semantic edges…');
  try {
    const data = await fetch(url).then(r => r.json());
    semanticEdges = data.edges || [];
    renderSemanticEdges();
    // Re-apply focus AFTER edges are in the DOM so applySessionFocus can address them
    if (focusedSession) {
      applySessionFocus(focusedSession);
    } else {
      const crossCount = semanticEdges.filter(e => e.cross_session).length;
      setBadge(
        `${semanticEdges.length} semantic connections` +
        (crossCount ? ` · ${crossCount} cross-session` : '') +
        ` · threshold ${threshold}`
      );
    }
  } catch(e) {
    setBadge('Semantic edges failed — run cairn embed first');
  }
}

function renderSemanticEdges() {
  if (!semLinkSel || !simulation) return;

  // Build node lookup from live simulation nodes
  const liveNodes = simulation.nodes();
  const nodeMap   = {};
  liveNodes.forEach(n => nodeMap[n.id] = n);

  // Only render edges where both endpoints exist in the current graph.
  // (In cross-session mode this is nearly all of them.)
  const valid = semanticEdges.filter(e => nodeMap[e.source] && nodeMap[e.target]);

  // Replace string IDs with actual node objects (required for D3 tick positions)
  const edgesForSim = valid.map(e => ({
    ...e,
    source: nodeMap[e.source],
    target: nodeMap[e.target],
  }));

  // Join edges — opacity set via attr (not CSS .show class) so applySessionFocus
  // can override individual edge opacity independently.
  semLinkSel = g.select('.sem-group')
    .selectAll('line')
    .data(edgesForSim, d => d.source.id + '→' + d.target.id)
    .join(
      enter => enter.append('line')
        .attr('class', d => `sem-link ${d.cross_session ? 'cross' : 'same'}`)
        .attr('stroke-width', d => Math.max(0.5, (d.similarity - 0.55) * 6))
        .attr('stroke-opacity', 0)
        .call(s => s.transition().duration(400).attr('stroke-opacity', 0.5)),
      update => update
        .attr('class', d => `sem-link ${d.cross_session ? 'cross' : 'same'}`)
        .attr('stroke-width', d => Math.max(0.5, (d.similarity - 0.55) * 6))
        .attr('stroke-opacity', 0.5),
      exit => exit.transition().duration(300).attr('stroke-opacity', 0).remove()
    );

  // Gentle pull force — semantically related nodes drift together without
  // distorting the parent-chain structural layout
  if (!semSnapshot) {
    semSnapshot = {};
    liveNodes.forEach(n => semSnapshot[n.id] = [n.x, n.y]);
  }
  simulation
    .force('sem', d3.forceLink(edgesForSim)
      .id(d => d.id)
      .distance(d => (1 - d.similarity) * 180)   // sim=0.90 → 18px, sim=0.62 → 68px
      .strength(d => (d.similarity - 0.58) * 0.5) // stronger pull, cluster-forming
    )
    .alpha(0.15)
    .restart();
}

function setBadge(text) {
  const el = document.getElementById('rag-badge');
  el.textContent = text;
  el.style.opacity = text ? '1' : '0';
}

// ── GraphRAG focus lens ───────────────────────────────────────────────────────
// applySessionFocus(sessId)
//   sessId = '' → clear focus, all nodes/edges show at full opacity
//   sessId = 'demo-live-...' → dim every OTHER session's nodes; emphasise
//   semantic edges that touch the focused session.
//
// Intentionally does NOT dim .session-ring circles — they're identity markers
// and help you see which session a dim node belongs to.
function applySessionFocus(sessId) {
  if (!g) return;
  const DIM_NODE = 0.10;   // non-focused node shape opacity
  const DIM_TEXT = 0.07;   // non-focused label opacity
  const DIM_EDGE = 0.03;   // semantic edge not touching focused session

  g.selectAll('.node').each(function(d) {
    const focused = !sessId || d.session === sessId;
    const baseOpacity = d.status === 'void' ? 0.35 : 1.0;
    // dim shapes but NOT session-ring (class filter)
    d3.select(this)
      .selectAll('circle:not(.session-ring), rect, polygon')
      .transition().duration(300)
      .attr('opacity', focused ? baseOpacity : DIM_NODE);
    d3.select(this).selectAll('text')
      .transition().duration(300)
      .style('opacity', focused ? 1 : DIM_TEXT);
  });

  // Semantic edges: highlight those touching the focused session
  if (semLinkSel && semLinkSel.size() > 0) {
    semLinkSel.transition().duration(300)
      .attr('stroke-opacity', d => {
        if (!sessId) return 0.5;  // No focus: all equal
        const srcIn = d.source.session === sessId;
        const tgtIn = d.target.session === sessId;
        if (srcIn && tgtIn) return 0.70;  // Both ends in focused session
        if (srcIn || tgtIn) return 0.55;  // Cross-session edge — these are the gems
        return DIM_EDGE;                  // Neither end — fade to near-invisible
      });
  }

  // Update badge with focus-aware stats
  if (!sessId) {
    // Clearing focus: show global summary
    if (semLinkSel && semLinkSel.size() > 0) {
      const edgeData = semLinkSel.data();
      const crossCount = edgeData.filter(e => e.source.session !== e.target.session).length;
      setBadge(`${edgeData.length} semantic connections · ${crossCount} cross-session`);
    }
    return;
  }
  // Focused summary
  if (semLinkSel && semLinkSel.size() > 0) {
    const edgeData  = semLinkSel.data();
    const myEdges   = edgeData.filter(e => e.source.session === sessId || e.target.session === sessId);
    const crossEdges = myEdges.filter(e => e.source.session !== e.target.session);
    const shortName = sessId.replace(/^\\d{4}-\\d{2}-\\d{2}[-_]?/, '').slice(0, 18) || sessId.slice(-18);
    setBadge(`${shortName} · ${myEdges.length} edges · ${crossEdges.length} cross-session`);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
(async () => {
  initGraph();
  await loadSessions();
  await updateStatus();
  try {                                   // paint recent history so the feed persists across reloads (no more blank-on-refresh)
    const _hist = await (await fetch(API + '/api/feed?limit=40')).json();
    for (const _n of _hist) addFeedItem(_n);
  } catch (e) {}
  startStream();
  setInterval(updateStatus, 5000);
  // go straight to atlas — skip loadGraph() on init so the force graph
  // never flashes before the canvas takes over
  let _prof = {}; try { _prof = applyProfile(); } catch (e) {}   // sticky home: restore saved layout + lenses
  await toggleAtlas();
  try { applyProfilePost(_prof); } catch (e) {}                  // recency + ambient, after the canvas is live
})();
</script>
</body>
</html>
"""


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    port    = 7331
    sess    = None
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg.startswith("--session="):
            sess = arg.split("=")[1]
    run_dashboard(port=port, session_id=sess)
