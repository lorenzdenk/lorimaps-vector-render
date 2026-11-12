#!/usr/bin/env python3
"""Merge deduplicated MBTiles parts (from parallel shards) into one file.
usage: merge_mbtiles.py OUT PART [PART ...]"""
import os
import sqlite3
import sys

out, parts = sys.argv[1], sys.argv[2:]
if os.path.exists(out):
    os.remove(out)
db = sqlite3.connect(out)
db.executescript("""
    PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
    CREATE TABLE images (tile_id TEXT, tile_data BLOB);
    CREATE UNIQUE INDEX images_id ON images (tile_id);
""")
for i, p in enumerate(sorted(parts)):
    db.execute("ATTACH DATABASE ? AS part", (p,))
    if i == 0:
        db.execute("INSERT INTO metadata SELECT * FROM part.metadata")
    db.execute("INSERT INTO map SELECT * FROM part.map")
    db.execute("INSERT OR IGNORE INTO images SELECT * FROM part.images")
    db.commit()
    db.execute("DETACH DATABASE part")
    print(f"merged {p}", flush=True)
db.executescript("""
    CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
    CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
        map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
""")
n_map = db.execute("SELECT count(*) FROM map").fetchone()[0]
n_img = db.execute("SELECT count(*) FROM images").fetchone()[0]
db.commit()
db.execute("VACUUM")
db.close()
print(f"{out}: {n_map} tiles, {n_img} unique images, {os.path.getsize(out) / 1e6:.1f} MB")
