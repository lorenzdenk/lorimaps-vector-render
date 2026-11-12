#!/usr/bin/env python3
"""Side-by-side picture of the raster map (Mapnik tiles, left) and the vector map (MapLibre, right) for one
place, both as the app shows them: tile level n at 164 dp per 256 px tile.

Usage: compare.py <raster.mbtiles> <style dir> <out.png> <lon> <lat> <level> [size dp=450] name=file.mbtiles ...
"""
import io
import math
import os
import sqlite3
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ref, style_dir, out, lon, lat, level = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6])
rest = sys.argv[7:]
size = int(rest.pop(0)) if rest and "=" not in rest[0] else 450
RATIO, TILE_DP = 2, 164

# ---- raster: tiles of the level, scaled to 164 dp
db = sqlite3.connect(f"file:{ref}?mode=ro", uri=True)
fmt = dict(db.execute("SELECT name, value FROM metadata")).get("format", "png")
px = TILE_DP * RATIO
n = 2 ** level
cx = (lon + 180) / 360 * n * px
cy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n * px
w = size * RATIO
left, top = cx - w / 2, cy - w / 2
img = Image.new("RGB", (w, w), (255, 0, 255))
missing = 0
for tx in range(int(left // px), int((left + w) // px) + 1):
    for ty in range(int(top // px), int((top + w) // px) + 1):
        row = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?", (level, tx, n - 1 - ty)).fetchone()
        if not row:
            missing += 1
            continue
        t = Image.open(io.BytesIO(row[0])).convert("RGB").resize((px, px), Image.LANCZOS)
        img.paste(t, (round(tx * px - left), round(ty * px - top)))

# ---- vector
zoom = level + math.log2(TILE_DP / 512)
tmp = out + ".vector.png"
cmd = ["xvfb-run", "-a", "node", os.path.join(HERE, "node", "render.js"), style_dir, tmp, str(lon), str(lat), str(zoom), str(size), str(size), str(RATIO)] + rest
res = subprocess.run(cmd, capture_output=True, text=True)
msg = [l for l in (res.stdout + res.stderr).splitlines() if l and "Experimental" not in l and "trace-warnings" not in l and not l.startswith("written")]
if msg:
    print("\n".join(msg[:8]))
vec = Image.open(tmp).convert("RGB")
os.remove(tmp)

both = Image.new("RGB", (2 * w + 12, w + 26), "white")
both.paste(img, (0, 26))
both.paste(vec, (w + 12, 26))
d = ImageDraw.Draw(both)
d.text((6, 6), f"Raster (Mapnik), Stufe {level}" + (f", {missing} Kacheln fehlen" if missing else ""), fill="black")
d.text((w + 18, 6), f"Vektor (MapLibre), Zoom {zoom:.2f}", fill="black")
both.save(out)
print("written", out)
