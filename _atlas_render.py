"""Render the atlas exactly as the canvas would — so the model can SEE it,
diagnose layout flaws, and iterate. Usage: python _atlas_render.py out.png
Reads live map_x/map_y from the vault. Delete when layout iteration is done."""
import colorsys
import sys
from PIL import Image, ImageDraw

sys.path.insert(0, ".")
from cairn.vault import Vault

W = H = 1400
out = sys.argv[1] if len(sys.argv) > 1 else "_atlas.png"

v = Vault()
rows = v.conn.execute("""
    SELECT id, community, importance, map_x, map_y, tags FROM nodes
    WHERE status != 'void' AND map_x IS NOT NULL""").fetchall()

xs = [r["map_x"] for r in rows]; ys = [r["map_y"] for r in rows]
x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
k = min((W - 80) / (x1 - x0 + 1), (H - 80) / (y1 - y0 + 1))
ox, oy = W / 2 - k * (x0 + x1) / 2, H / 2 - k * (y0 + y1) / 2

img = Image.new("RGB", (W, H), (13, 17, 23))
d = ImageDraw.Draw(img)

agg = {}
for r in rows:
    cid, _, lbl = (r["community"] or "").partition("|")
    if cid:
        hue = ((sum(ord(ch) * (idx + 7) for idx, ch in enumerate(lbl or cid))
                * 137.508) % 360.0) / 360.0
        rgb = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.62, 0.58))
    else:
        rgb = (48, 54, 61)
    if cid and lbl:
        a = agg.setdefault(cid, [0, 0, 0, lbl])
        a[0] += r["map_x"]; a[1] += r["map_y"]; a[2] += 1
    px, py = r["map_x"] * k + ox, r["map_y"] * k + oy
    rad = max(1.0, (1.4 + (r["importance"] or 5) * 0.28) * k * 6)
    d.ellipse([px - rad, py - rad, px + rad, py + rad], fill=rgb)

for a in sorted(agg.values(), key=lambda a: -a[2])[:30]:
    d.text((a[0] / a[2] * k + ox, a[1] / a[2] * k + oy - 8), a[3][:22],
           fill=(140, 150, 160), anchor="mm")

img.save(out)
print(f"rendered {len(rows)} nodes -> {out} (scale {k:.4f})")
