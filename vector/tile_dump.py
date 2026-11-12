#!/usr/bin/env python3
"""What is in a vector tile: features per layer, optionally grouped by an attribute.
Usage: tile_dump.py <file.mbtiles> z x y [layer [attribute]]"""
import collections, gzip, sqlite3, sys
import mapbox_vector_tile
f, z, x, y = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
row = sqlite3.connect(f).execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?", (z, x, 2 ** z - 1 - y)).fetchone()
t = mapbox_vector_tile.decode(gzip.decompress(row[0]))
if len(sys.argv) > 5:
    feats = t[sys.argv[5]]["features"]
    key = sys.argv[6] if len(sys.argv) > 6 else None
    c = collections.Counter((ft["geometry"]["type"], str(ft["properties"].get(key)) if key else "") for ft in feats)
    for k, v in c.most_common(30): print(v, k)
    print("properties of the first feature:", feats[0]["properties"] if feats else None)
else:
    for name, l in sorted(t.items()):
        print(f"{len(l['features']):6d} {name}")
