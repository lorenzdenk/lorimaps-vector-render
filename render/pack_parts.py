#!/usr/bin/env python3
"""Packs big shard files (staged as release assets, too big to pass between jobs as artifacts) into map files
below the 2 GB release file limit: "<name>-part1of5.mbtiles" ... The app shows all parts of a map as one map.

usage: pack_parts.py <stage release tag> <shard count> <out dir> <tmp dir>
Needs gh (GH_TOKEN, GITHUB_REPOSITORY). Every file is filled up to LIMIT bytes (a shard may continue in the next file), so there are as few files as possible;
identical tiles inside one file are stored once.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

# release files may be 2 GiB (2 147 483 648 bytes); a batch of tiles is at most about 100 MB
LIMIT = 2_020_000_000
BATCH = 400   # tiles per step
tag, shards, out_dir, tmp = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
repo = os.environ["GITHUB_REPOSITORY"]
os.makedirs(out_dir, exist_ok=True)
os.makedirs(tmp, exist_ok=True)

assets = json.loads(subprocess.run(["gh", "release", "view", tag, "-R", repo, "--json", "assets"],
                                   check=True, capture_output=True, text=True).stdout)["assets"]
groups = {}
for a in assets:
    m = re.match(r"^(.*)\.part(\d+)\.mbtiles$", a["name"])
    if m:
        groups.setdefault(m.group(1), []).append((int(m.group(2)), a["name"], a["size"]))


def new_db(path, first_part):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);""")
    db.execute("ATTACH DATABASE ? AS part", (first_part,))
    db.execute("INSERT INTO metadata SELECT * FROM part.metadata")
    db.commit()
    db.execute("DETACH DATABASE part")
    return db


def close_db(db, path):
    db.executescript("""
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;""")
    db.commit()
    db.close()


def est_size(path, rows):
    # file so far + the index on map that close_db() adds (about 30 bytes per tile)
    return os.path.getsize(path) + 30 * rows


for base, parts in sorted(groups.items()):
    parts.sort()
    name = base if len(parts) == shards else base + "-INCOMPLETE"
    if len(parts) != shards:
        print(f"WARN: {base}: only {len(parts)} of {shards} shards", flush=True)
    print(f"{base}: {len(parts)} shards, {sum(p[2] for p in parts) / 1e9:.2f} GB", flush=True)
    # every file is filled up to LIMIT (a shard may continue in the next file), so there are as few files as possible
    files, db, path, rows = [], None, None, 0
    for _, asset, _ in parts:
        local = os.path.join(tmp, asset)
        subprocess.run(["gh", "release", "download", tag, "-R", repo, "-p", asset, "-D", tmp, "--clobber"], check=True)
        src = sqlite3.connect(local)
        last = src.execute("SELECT max(rowid) FROM map").fetchone()[0] or 0
        src.close()
        a, attached = 0, False
        while a < last:
            if db is None:
                path = os.path.join(out_dir, f"{name}-part{len(files) + 1}.tmp")
                db = new_db(path, local)
                rows = 0
            if not attached:
                db.execute("ATTACH DATABASE ? AS part", (local,))
                attached = True
            b = a + BATCH
            db.execute("INSERT INTO map SELECT * FROM part.map WHERE rowid > ? AND rowid <= ?", (a, b))
            db.execute("INSERT OR IGNORE INTO images SELECT * FROM part.images WHERE tile_id IN "
                       "(SELECT tile_id FROM part.map WHERE rowid > ? AND rowid <= ?)", (a, b))
            db.commit()
            rows += BATCH
            a = b
            if est_size(path, rows) > LIMIT:
                db.execute("DETACH DATABASE part")
                attached = False
                close_db(db, path)
                files.append(path)
                db = None
        if attached:
            db.execute("DETACH DATABASE part")   # the next shard continues in this file
        os.remove(local)
    if db is not None:
        close_db(db, path)
        files.append(path)
    for k, f in enumerate(files, 1):
        final = os.path.join(out_dir, f"{name}-part{k}of{len(files)}.mbtiles" if len(files) > 1 else f"{name}.mbtiles")
        os.rename(f, final)
        print(f"  {os.path.basename(final)}: {os.path.getsize(final) / 1e9:.2f} GB", flush=True)
