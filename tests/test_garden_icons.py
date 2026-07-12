"""
tests/test_garden_icons.py — icon-set wiring, KEY legend, favicon/homescreen.

Locks in the 2026-07-02 UI pass:

  1. icon wiring   — the served GARDEN_HTML ships the .cicon luminance-mask CSS,
                     the JS icon() helper, and real /assets/icons/*.png urls; the
                     capture-row / action / status concepts render as .cicon spans
                     BESIDE their text labels (never replacing the word).
  2. no-orphans    — every icon('name',…) and every static --u url references a
                     PNG that actually exists on disk (a typo'd mask is invisible).
  3. fallback      — a concept with NO png (from-the-deep) keeps its emoji; the
                     icon() helper only emits a span for names in the real set.
  4. KEY legend    — a KEY button in the header + a static legend plate on Index.
  5. favicon       — <link rel=icon/apple-touch-icon/manifest> + theme-color in
                     BOTH garden.py and dashboard.py heads; brand PNGs on disk.
  6. manifest      — manifest.webmanifest is valid JSON (name Cairn, standalone,
                     theme #16140E, 192+512 icons); the /assets route serves it
                     as application/manifest+json (mime extended for .webmanifest).

Run: python -m pytest tests/test_garden_icons.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
ICON_DIR = REPO / "cairn" / "assets" / "icons"
BRAND_DIR = REPO / "cairn" / "assets" / "brand"


def _icons_on_disk() -> set[str]:
    return {p.stem for p in ICON_DIR.glob("*.png")}


def _last_script(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline <script> found"
    return blocks[-1]


# ── 1. icon wiring ──────────────────────────────────────────────────────────────

class TestIconWiring:
    def test_cicon_css_and_helper_present(self):
        from cairn.garden import GARDEN_HTML as H
        assert ".cicon" in H, "the luminance-mask class must ship"
        assert "mask-mode:luminance" in H
        assert "background-color:currentColor" in H
        assert "function icon(" in H, "the fallback-aware icon() helper must ship"

    def test_capture_row_icons_present(self):
        """The capture surface + core action icons render as .cicon masks."""
        from cairn.garden import GARDEN_HTML as H
        # the capture photo button became a mask span
        assert "/assets/icons/photo.png" in H
        # core action icons are wired
        for name in ("archive", "snooze", "pin", "research", "done", "flag"):
            assert f"/assets/icons/{name}.png" in H, f"{name} icon not wired"

    def test_icons_sit_beside_labels(self):
        """Design rule: the mark never replaces the word. Spot-check that the
        action buttons still carry their text label next to the icon span."""
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        # icon('archive',…) is immediately followed by the word 'archive'
        assert re.search(r"icon\('archive'[^)]*\)\}?\s*archive", js), \
            "archive icon must sit beside its 'archive' label"
        assert re.search(r"icon\('snooze'[^)]*\)\}?\s*snooze", js)
        assert re.search(r"icon\('pin'[^)]*\)\}?\s*pin", js)


# ── 2. no orphaned icon references ──────────────────────────────────────────────

class TestNoOrphans:
    def test_every_icon_call_has_a_png(self):
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        used = set(re.findall(r"icon\('([a-z0-9\-]+)'", js))
        have = _icons_on_disk()
        assert used, "expected icon() calls in the template"
        missing = sorted(used - have)
        assert not missing, f"icon() names with no PNG: {missing}"

    def test_every_static_url_has_a_png(self):
        from cairn.garden import GARDEN_HTML as H
        urls = set(re.findall(r"/assets/icons/([a-z0-9\-]+)\.png", H))
        have = _icons_on_disk()
        missing = sorted(urls - have)
        assert not missing, f"--u urls with no PNG: {missing}"

    def test_icon_registry_matches_disk(self):
        """The JS ICONS set (drives fallback) must equal the PNGs on disk, so a
        newly-added or removed icon can't silently desync from the mask files."""
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        m = re.search(r"const ICONS = new Set\(\[(.*?)\]\)", js, re.S)
        assert m, "ICONS registry not found"
        registry = set(re.findall(r"'([a-z0-9\-]+)'", m.group(1)))
        have = _icons_on_disk()
        assert registry == have, (
            f"registry vs disk mismatch — only-in-registry={sorted(registry-have)} "
            f"only-on-disk={sorted(have-registry)}")


# ── 3. fallback for concepts with no PNG ────────────────────────────────────────

class TestFallback:
    def test_from_the_deep_keeps_emoji(self):
        """from-the-deep has no PNG by design; its emoji must survive."""
        assert not (ICON_DIR / "from-the-deep.png").exists()
        from cairn.garden import GARDEN_HTML as H
        assert "\U0001F52D from the deep" in H, "the deep-scan emoji fallback is gone"

    def test_icon_helper_gates_on_registry(self):
        """icon() only emits a span for a name in the registry — otherwise the
        emoji fallback. Prove the guard exists in the helper body."""
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        assert "if (!ICONS.has(name)) return emoji" in js


# ── 4. replaced emoji are GONE from their wired regions ─────────────────────────

class TestEmojiReplaced:
    def _region(self, html: str, anchor: str, span: int) -> str:
        i = html.find(anchor)
        assert i >= 0, f"anchor {anchor!r} not found"
        return html[i:i + span]

    def test_photo_button_emoji_gone(self):
        from cairn.garden import GARDEN_HTML as H
        r = self._region(H, 'id="photo-btn"', 240)
        # icon may render as a cicon mask OR the owner's dual-ink glyph imgs
        assert ("cicon" in r or "gi gi-dusk" in r) and "\U0001F4F7" not in r, \
            "photo-btn still shows 📷"

    def test_action_buttons_use_icon_not_raw_emoji(self):
        """In cardHTML the archive/snooze/pin glyphs go through icon() (emoji only
        as the fallback ARG), never as raw button text like '🗄 archive'."""
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        i = js.find("function cardHTML")
        j = js.find("function _sameStart", i)   # cardHTML ends where the next fn begins
        assert i >= 0 and j > i
        card = js[i:j]
        # the raw '🗄 archive' / '💤 snooze' button text must be gone
        assert "\U0001F5C4 archive" not in card
        assert "\U0001F4A4 snooze" not in card
        # but the icon() wrapper (with emoji as fallback) IS present
        assert "icon('archive'" in card and "icon('snooze'" in card

    def test_theme_toggle_label_via_icon(self):
        from cairn.garden import GARDEN_HTML as H
        js = _last_script(H)
        # applyThemeLabel now emits an icon() span beside the dawn/dusk word
        assert "icon(name," in js and "'dawn'" in js and "'dusk'" in js


# ── 5. KEY legend ───────────────────────────────────────────────────────────────

class TestKeyLegend:
    def test_key_button_and_popover(self):
        from cairn.garden import GARDEN_HTML as H
        assert 'id="key-btn"' in H, "the header KEY button must exist"
        assert 'id="key-pop"' in H, "the KEY popover must exist"
        assert "toggleKey()" in H
        # popover lists kind icons beside mono-caps labels
        assert "key-lbl" in H and "key-actions" in H

    def test_legend_plate_on_index(self):
        from cairn.garden import GARDEN_HTML as H
        assert "legend-plate" in H, "the Book/Index legend plate class must exist"
        assert "function legendPlate" in H
        # the Index render calls it under a Legend heading
        assert ">Legend<" in H and "legendPlate()" in H


# ── 6. favicon / homescreen ─────────────────────────────────────────────────────

class TestFaviconLinks:
    @pytest.mark.parametrize("size", [512, 192, 180, 48])
    def test_brand_png_exists(self, size):
        p = BRAND_DIR / f"app-icon-{size}.png"
        assert p.is_file(), f"missing {p.name}"
        assert p.stat().st_size > 500, f"{p.name} looks empty"

    def test_garden_head_links(self):
        from cairn.garden import GARDEN_HTML as H
        assert 'rel="icon"' in H
        assert 'rel="apple-touch-icon"' in H
        assert 'rel="manifest"' in H
        assert 'name="theme-color"' in H and "#16140E" in H
        assert "/assets/brand/app-icon-192.png" in H
        assert "/assets/brand/app-icon-180.png" in H

    def test_dashboard_head_links(self):
        from cairn.dashboard import DASHBOARD_HTML as D
        assert 'rel="icon"' in D
        assert 'rel="apple-touch-icon"' in D
        assert 'rel="manifest"' in D
        assert 'name="theme-color"' in D and "#16140E" in D


# ── manifest content + served mime ──────────────────────────────────────────────

class TestManifest:
    def test_manifest_is_valid(self):
        p = BRAND_DIR / "manifest.webmanifest"
        assert p.is_file(), "manifest.webmanifest missing"
        m = json.loads(p.read_text(encoding="utf-8"))
        assert m["name"] == "Cairn"
        assert m["display"] == "standalone"
        assert m["theme_color"].upper() == "#16140E"
        sizes = {i["sizes"] for i in m["icons"]}
        assert {"192x192", "512x512"} <= sizes, f"need 192+512 icons, got {sizes}"
        for i in m["icons"]:
            assert i["type"] == "image/png"
            # every icon src must resolve to a real file
            rel = i["src"].lstrip("/")
            assert (REPO / "cairn" / rel.split("assets/", 1)[0] and
                    (REPO / "cairn" / "assets" /
                     i["src"].split("/assets/", 1)[1]).is_file()), \
                f"manifest icon {i['src']} missing on disk"

    def test_assets_route_serves_manifest_as_json(self, tmp_path):
        """The /assets route must return the webmanifest with the correct mime.
        Rebuild the SAME route logic the dashboard registers (it lives inside
        run_dashboard, not importable standalone) and drive it with a client —
        the mapping under test is the .webmanifest → application/manifest+json
        line added to dashboard.asset()."""
        fastapi = pytest.importorskip("fastapi")
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, JSONResponse
        from starlette.testclient import TestClient

        app = FastAPI()

        # ── verbatim copy of cairn.dashboard.asset() (mime map is the point) ──
        @app.get("/assets/{path:path}")
        def asset(path: str):
            base = (REPO / "cairn" / "assets").resolve()
            f = (base / path).resolve()
            if not (f.is_file() and f.is_relative_to(base)):
                return JSONResponse({"error": "not found"}, status_code=404)
            mt = {".woff2": "font/woff2",
                  ".webmanifest": "application/manifest+json"}.get(f.suffix)
            return FileResponse(str(f), media_type=mt) if mt else FileResponse(str(f))

        client = TestClient(app)

        # nested path + correct mime
        r = client.get("/assets/brand/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        body = r.json()
        assert body["name"] == "Cairn"
        assert {i["sizes"] for i in body["icons"]} >= {"192x192", "512x512"}

        # a nested PNG still serves 200 as an image
        r2 = client.get("/assets/icons/todo.png")
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("image/")

        # traversal is still refused
        r3 = client.get("/assets/../dashboard.py")
        assert r3.status_code in (404, 400)


def test_touched_files_compile():
    """py_compile the two files this pass edited."""
    import py_compile
    for rel in ("cairn/garden.py", "cairn/dashboard.py"):
        py_compile.compile(str(REPO / rel), doraise=True)
