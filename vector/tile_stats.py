#!/usr/bin/env python3
"""Size of each layer in one tile (compressed), to see what makes tiles large. Usage: tile_stats.py style plan z x y"""
import gzip, json, sys, argparse
import psycopg2
import export_tiles as ex
from mapnik_model import MapnikMap
style, plan, z, x, y = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
args = argparse.Namespace(kmin=0, kmax=2, maxzoom=14, buffer=128, simplify=1.0)
ex.init_worker(style, plan, args)
qs = ex.layer_queries(ex._m, ex._plan, z, args)
x0, y0, x1, y1 = ex.tile_env(z, x, y); w = x1 - x0; b = w * args.buffer / ex.EXTENT
prm = dict(x0=x0, y0=y0, x1=x1, y1=y1, bx0=x0 - b, by0=y0 - b, bx1=x1 + b, by1=y1 + b, tol=w / ex.EXTENT * args.simplify)
rows = []
for db, lst in qs.items():
    cur = psycopg2.connect(dbname=db).cursor()
    for name, q in lst:
        for key, v in prm.items():
            q = q.replace(f"%({key})s", repr(float(v)))
        cur.execute("SELECT " + q); d = bytes(cur.fetchone()[0] or b"")
        rows.append((len(gzip.compress(d)), len(d), name))
tot = sum(r[0] for r in rows)
for gz, raw, name in sorted(rows, reverse=True)[:14]:
    print(f"{gz / 1024:8.1f} KB gz {raw / 1024:8.1f} KB raw  {name}")
print(f"{tot / 1024:8.1f} KB total, {len(rows)} layers")
