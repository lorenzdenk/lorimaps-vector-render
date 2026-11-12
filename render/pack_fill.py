#!/usr/bin/env python3
"""Adds the tiles of a fill run (fill.yml) to a published map in parts.

usage: pack_fill.py <dir with the map parts> <out dir> FILL.mbtiles [FILL.mbtiles ...]

The map parts "<name>-partKofM.mbtiles" are moved to the out dir as "<name>-partKofN.mbtiles", and the fill
files are packed into the new parts M+1 .. N (below 2 GB each). The app shows all parts of a map as one map
and takes each tile from the first part that has it, so tiles in both a map part and a fill part do no harm.
"""
import os
import re
import sqlite3
import sys

LIMIT = 1_900_000_000
src, out_dir, fills = sys.argv[1], sys.argv[2], sorted(sys.argv[3:])
os.makedirs(out_dir, exist_ok=True)

parts = []
for f in os.listdir(src):
    m = re.match(r"^(.*)-part(\d+)of(\d+)\.mbtiles$", f)
    if m:
        parts.append((int(m.group(2)), m.group(1), int(m.group(3)), f))
parts.sort()
if not parts:
    sys.exit("no map parts found in " + src)
base, m_old = parts[0][1], parts[0][2]
if len(parts) != m_old:
    sys.exit(f"only {len(parts)} of {m_old} parts of {base} found")

# greedy packs of fill files
packs, cur, size = [], [], 0
for f in fills:
    s = os.path.getsize(f)
    if cur and size + s > LIMIT:
        packs.append(cur)
        cur, size = [], 0
    cur.append(f)
    size += s
if cur:
    packs.append(cur)
n_new = m_old + len(packs)
print(f"{base}: {m_old} parts + {len(fills)} fill files in {len(packs)} new parts = {n_new} parts", flush=True)

for k, _, _, f in parts:
    target = os.path.join(out_dir, f"{base}-part{k}of{n_new}.mbtiles")
    os.replace(os.path.join(src, f), target)
    print(f"  {os.path.basename(target)} (was {f})", flush=True)

total = 0
for i, pack in enumerate(packs, m_old + 1):
    path = os.path.join(out_dir, f"{base}-part{i}of{n_new}.mbtiles")
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);""")
    # metadata of the map itself (name, bounds, zoom levels, ...)
    db.execute("ATTACH DATABASE ? AS part", (os.path.join(out_dir, f"{base}-part1of{n_new}.mbtiles"),))
    db.execute("INSERT INTO metadata SELECT * FROM part.metadata")
    db.commit()
    db.execute("DETACH DATABASE part")
    for f in pack:
        db.execute("ATTACH DATABASE ? AS part", (f,))
        db.execute("INSERT OR REPLACE INTO map SELECT * FROM part.map")
        db.execute("INSERT OR IGNORE INTO images SELECT * FROM part.images")
        db.commit()
        db.execute("DETACH DATABASE part")
    db.executescript("""
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;""")
    n = db.execute("SELECT count(*) FROM map").fetchone()[0]
    total += n
    db.commit()
    db.close()
    print(f"  {os.path.basename(path)}: {n} tiles, {os.path.getsize(path) / 1e9:.2f} GB", flush=True)
print(f"{total} filled tiles")
