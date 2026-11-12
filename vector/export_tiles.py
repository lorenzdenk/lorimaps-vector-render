#!/usr/bin/env python3
"""Writes the data of a Mapnik style as vector tiles (MVT) into an MBTiles file.

Every database layer of the style is exported with its own SQL, so the tiles hold exactly what Mapnik
would have drawn. A tile of zoom t holds the features that the rules of the style need for the zoom
levels k = t+KMIN .. t+KMAX (the last tile zoom: all higher levels as well), because one vector tile is
shown over a range of map scales.
"""
import argparse
import gzip
import hashlib
import json
import math
import multiprocessing as mp
import os
import sqlite3
import sys
import time

import psycopg2

from layers import fill_tokens, placement, source_layer
from mapnik_model import MAX_K, MapnikMap, sql_ident, to_sql

EXTENT = 4096
R = 20037508.342789244


def tile_env(z, x, y):
    s = 2 * R / 2 ** z
    return -R + x * s, R - (y + 1) * s, -R + (x + 1) * s, R - y * s


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def layer_queries(m, plan, t, args):
    """SQL pieces for a tile of zoom t: {db: [sql with %(…)s placeholders]}"""
    k_lo = t + args.kmin
    k_hi = MAX_K if t >= args.maxzoom else t + args.kmax
    by_name = {l.name: l for l in m.layers}
    out = {}
    for p in plan["layers"]:
        l = by_name[p["name"]]
        types = p["columns"]
        for cls in p["classes"]:
            ks = [k for k in range(max(k_lo, cls[0]), min(k_hi, cls[1]) + 1)]
            if not ks:
                continue
            for kind in p["kinds"]:
                # rules that draw this kind at these zoom levels
                rules = [r for _, r in l.rules() if any(r.active(k) for k in ks)
                         and any((placement(s) if p["dims"] != [0] else "geom") == kind for s in r.symbolizers)]
                if not rules:
                    continue
                cols = sorted({c for r in rules for c in r.columns() if c in types})
                if any(r.filter is None for r in rules):
                    where = "true"
                else:
                    seen, parts = set(), []
                    for r in rules:
                        if r.filter_text not in seen:
                            seen.add(r.filter_text)
                            parts.append(to_sql(r.filter, types))
                    where = "(" + " OR ".join(parts) + ")"
                g = sql_ident(p["geom"])
                table = fill_tokens(l.table, max(ks), "ST_MakeEnvelope(%(bx0)s, %(by0)s, %(bx1)s, %(by1)s, 3857)")
                sel = []
                for c in cols:
                    ci = sql_ident(c)
                    sel.append(f"{ci}::float8 AS {ci}" if types[c] == "num" else (f"{ci}::int AS {ci}" if types[c] == "bool" else f"{ci}::text AS {ci}"))
                sel = (", " + ", ".join(sel)) if sel else ""
                name = source_layer(p["name"], kind, cls, len(p["classes"]))
                env = "ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 3857)"
                benv = "ST_MakeEnvelope(%(bx0)s, %(by0)s, %(bx1)s, %(by1)s, 3857)"
                if kind == "geom":
                    # lines that only carry labels or symbols along them (no drawn line): smoother, so that labels
                    # follow the general direction instead of every small bend
                    only_labels = all(s.type in ("Text", "Markers") for r in rules for s in r.symbolizers
                                      if (placement(s) if p["dims"] != [0] else "geom") == "geom")
                    tol = "%(tol_label)s" if only_labels else "%(tol)s"
                    geom = f"ST_AsMVTGeom(ST_Simplify({g}, {tol}, true), {env}, {EXTENT}, {args.buffer}, true)" \
                        if p["dims"] != [0] else f"ST_AsMVTGeom({g}, {env}, {EXTENT}, 0, false)"
                    box = benv if p["dims"] != [0] else env
                    q = (f"(SELECT COALESCE(ST_AsMVT(q, '{name}', {EXTENT}, 'geom'), ''::bytea) FROM ("
                         f"SELECT {geom} AS geom{sel} FROM {table} WHERE {g} && {box} AND {where}) q WHERE geom IS NOT NULL)")
                else:
                    # one point per feature, only in the tile that contains it
                    inner = "ST_PointOnSurface" if kind == "pi" else "ST_Centroid"
                    pt = (f"(CASE WHEN ST_Dimension({g}) = 1 THEN ST_LineInterpolatePoint(ST_GeometryN({g}, 1), 0.5) "
                          f"WHEN ST_Dimension({g}) = 2 THEN {inner}({g}) ELSE ST_GeometryN({g}, 1) END)")
                    q = (f"(SELECT COALESCE(ST_AsMVT(q, '{name}', {EXTENT}, 'geom'), ''::bytea) FROM ("
                         f"SELECT ST_AsMVTGeom(_pt, {env}, {EXTENT}, 0, false) AS geom{sel} FROM ("
                         f"SELECT *, {pt} AS _pt FROM {table} WHERE {g} && {env} AND {where}) s "
                         f"WHERE ST_X(_pt) >= %(x0)s AND ST_X(_pt) < %(x1)s AND ST_Y(_pt) > %(y0)s AND ST_Y(_pt) <= %(y1)s) q "
                         f"WHERE geom IS NOT NULL)")
                out.setdefault(p["db"], []).append((name, q))
    return out


def init_worker(style, plan_path, args):
    global _m, _plan, _args, _conns, _sql
    _m = MapnikMap(style)
    _plan = json.load(open(plan_path))
    _args = args
    _conns, _sql = {}, {}
    # file layers were loaded into the database by layers.py; use the tables from the plan
    for p in _plan["layers"]:
        for l in _m.layers:
            if l.name == p["name"]:
                l.type, l.table = "postgis", p["table"]


def render(tile):
    z, x, y = tile
    try:
        if z not in _sql:
            _sql[z] = layer_queries(_m, _plan, z, _args)
        x0, y0, x1, y1 = tile_env(z, x, y)
        w = x1 - x0
        b = w * _args.buffer / EXTENT
        prm = dict(x0=x0, y0=y0, x1=x1, y1=y1, bx0=x0 - b, by0=y0 - b, bx1=x1 + b, by1=y1 + b, tol=w / EXTENT * _args.simplify,
                   tol_label=w / EXTENT * _args.simplify_labels)
        data = b""
        for db, qs in _sql[z].items():
            if db not in _conns:
                _conns[db] = psycopg2.connect(dbname=db)
                _conns[db].autocommit = True
                _conns[db].cursor().execute("SET max_parallel_workers_per_gather = 0; SET jit = off")
            cur = _conns[db].cursor()
            # one statement per layer keeps the SQL short enough to plan quickly
            for name, q in qs:
                # the placeholders are filled in here (the layers' own SQL contains % signs: LIKE, modulo)
                for key, v in prm.items():
                    q = q.replace(f"%({key})s", repr(float(v)))
                cur.execute("SELECT " + q)
                part = cur.fetchone()[0]
                if part:
                    data += bytes(part)
        return z, x, y, (gzip.compress(data, 6, mtime=0) if data else None), None
    except Exception as e:
        return z, x, y, None, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", required=True, help="Mapnik XML written by mapnik.save_map")
    ap.add_argument("--plan", required=True, help="JSON written by layers.py")
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--maxzoom", type=int, default=14)
    ap.add_argument("--kmin", type=int, default=0, help="a tile of zoom t serves the style's zoom levels t+kmin ...")
    ap.add_argument("--kmax", type=int, default=2, help="... t+kmax")
    ap.add_argument("--buffer", type=int, default=128, help="margin around a tile for lines and areas, in 1/4096 of the tile")
    ap.add_argument("--simplify", type=float, default=1.0, help="simplification tolerance in tile units (1/4096 of the tile)")
    ap.add_argument("--simplify-labels", type=float, default=10.0, help="the same for lines that only carry labels")
    ap.add_argument("--processes", type=int, default=os.cpu_count())
    ap.add_argument("--name", default="map")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    w, s, e, n = map(float, args.bbox.split(","))
    tiles = []
    for z in range(args.minzoom, args.maxzoom + 1):
        ax, ay = lonlat_to_tile(w, n, z)
        bx, by = lonlat_to_tile(e, s, z)
        tiles += [(z, x, y) for x in range(ax, bx + 1) for y in range(ay, by + 1)]
    print(f"{len(tiles)} tiles, zoom {args.minzoom}-{args.maxzoom}", flush=True)

    if os.path.exists(args.out):
        os.remove(args.out)
    db = sqlite3.connect(args.out)
    db.executescript("""
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column, map.tile_row AS tile_row,
            images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
    """)
    t0, done, errors, size, per_zoom = time.time(), 0, [], 0, {}
    with mp.Pool(args.processes, init_worker, (args.style, args.plan, args)) as pool:
        for z, x, y, data, err in pool.imap_unordered(render, tiles, chunksize=4):
            done += 1
            if err:
                errors.append((z, x, y, err))
                if len(errors) <= 5:
                    print(f"ERROR tile {z}/{x}/{y}: {err}", flush=True)
            elif data:
                tid = hashlib.md5(data).hexdigest()
                db.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (tid, data))
                db.execute("INSERT INTO map VALUES (?, ?, ?, ?)", (z, x, 2 ** z - 1 - y, tid))
                size += len(data)
                c = per_zoom.setdefault(z, [0, 0])
                c[0] += 1
                c[1] += len(data)
            if done % 500 == 0:
                db.commit()
                el = time.time() - t0
                print(f"  {done}/{len(tiles)} tiles, {el / 60:.1f} min, {done / el:.1f} tiles/s, {len(errors)} errors", flush=True)
    m = MapnikMap(args.style)
    plan = json.load(open(args.plan))
    vector_layers = []
    for p in plan["layers"]:
        for cls in p["classes"]:
            for kind in p["kinds"]:
                vector_layers.append({"id": source_layer(p["name"], kind, cls, len(p["classes"])),
                                      "fields": {c: ("Number" if t != "text" else "String") for c, t in p["columns"].items()},
                                      "minzoom": args.minzoom, "maxzoom": args.maxzoom})
    meta = {
        "name": args.name, "format": "pbf", "type": "baselayer", "version": "1",
        "minzoom": str(args.minzoom), "maxzoom": str(args.maxzoom),
        "bounds": f"{w},{s},{e},{n}", "center": f"{(w + e) / 2},{(s + n) / 2},{min(args.maxzoom, 12)}",
        "attribution": "© OpenStreetMap contributors",
        "json": json.dumps({"vector_layers": vector_layers}),
    }
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    db.execute("CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row)")
    db.commit()
    db.close()
    el = time.time() - t0
    print(f"{done} tiles in {el / 60:.1f} min, {len(errors)} errors, {size / 1e6:.1f} MB compressed", flush=True)
    for z in sorted(per_zoom):
        c = per_zoom[z]
        print(f"  z{z}: {c[0]} tiles, {c[1] / 1e6:.2f} MB, {c[1] / max(1, c[0]) / 1024:.1f} KB per tile")
    print(f"written {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
