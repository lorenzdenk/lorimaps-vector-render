#!/usr/bin/env python3
"""Downloads ready-made map tiles (ArcGIS MapServer tile cache) into an MBTiles file, unchanged.

Usage: fetch_tiles.py <url template {z}/{y}/{x}> <minzoom> <maxzoom> <W> <S> <E> <N> <shard> <shards> <out.mbtiles>
                      [--threads 8] [--name ...] [--attribution ...] [--mask land.geojson]
The tiles are split into shards by row: a tile belongs to the shard that holds its top edge at maxzoom,
so all shards together are exactly the whole area. Missing tiles (404, sea) are skipped.
"""
import argparse
import math
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "LoriMaps-render (+https://github.com/lorenzdenk/lorimaps-render)"}


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url"); ap.add_argument("minzoom", type=int); ap.add_argument("maxzoom", type=int)
    ap.add_argument("W", type=float); ap.add_argument("S", type=float); ap.add_argument("E", type=float); ap.add_argument("N", type=float)
    ap.add_argument("shard", type=int); ap.add_argument("shards", type=int); ap.add_argument("out")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--name", default="satellite"); ap.add_argument("--attribution", default="")
    a = ap.parse_args()

    zmax = a.maxzoom
    _, ry0 = tile_xy(a.N, a.W, zmax)
    _, ry1 = tile_xy(a.S, a.E, zmax)
    rows = ry1 - ry0 + 1
    lo = ry0 + rows * a.shard // a.shards
    hi = ry0 + rows * (a.shard + 1) // a.shards  # exclusive
    print(f"shard {a.shard}/{a.shards}: rows {lo}..{hi - 1} of {ry0}..{ry1} at zoom {zmax}", flush=True)

    db = sqlite3.connect(a.out)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE IF NOT EXISTS metadata (name text, value text);
      CREATE TABLE IF NOT EXISTS tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);
      CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);""")
    meta = {"name": a.name, "format": "jpg", "type": "baselayer", "minzoom": str(a.minzoom), "maxzoom": str(zmax),
            "bounds": f"{a.W},{a.S},{a.E},{a.N}", "attribution": a.attribution,
            "center": f"{(a.W + a.E) / 2},{(a.S + a.N) / 2},{min(zmax, 10)}"}
    db.execute("DELETE FROM metadata")
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())

    def fetch(t):
        z, x, y = t
        for attempt in range(6):
            try:
                req = urllib.request.Request(a.url.format(z=z, x=x, y=y), headers=UA)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return t, r.read()
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    return t, None
                time.sleep(3 * (attempt + 1))
            except Exception:
                time.sleep(3 * (attempt + 1))
        return t, b"ERROR"

    total = got = failed = size = 0
    t0 = time.time()
    with ThreadPoolExecutor(a.threads) as pool:
        for z in range(a.minzoom, zmax + 1):
            x0, y0 = tile_xy(a.N, a.W, z)
            x1, y1 = tile_xy(a.S, a.E, z)
            k = zmax - z
            ys = [y for y in range(y0, y1 + 1) if lo <= max(y << k, ry0) < hi]
            for y in ys:
                tiles = [(z, x, y) for x in range(x0, x1 + 1)]
                batch = []
                for (tz, tx, ty), data in pool.map(fetch, tiles):
                    total += 1
                    if data == b"ERROR":
                        failed += 1
                    elif data:
                        got += 1; size += len(data)
                        batch.append((tz, tx, (1 << tz) - 1 - ty, data))
                db.executemany("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", batch)
                db.commit()
            print(f"  zoom {z}: {len(ys)} rows done; {got}/{total} tiles, {failed} failed, "
                  f"{size / 1e9:.2f} GB, {(time.time() - t0) / 60:.1f} min", flush=True)
    db.close()
    print(f"done: {got} tiles, {failed} failed, {size / 1e9:.2f} GB", flush=True)


if __name__ == "__main__":
    main()
