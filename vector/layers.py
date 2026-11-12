#!/usr/bin/env python3
"""What the vector tiles contain: for every data layer of the Mapnik style the vector tile layers made from it.

One Mapnik layer becomes up to three kinds of vector tile layers:
  <name>        the geometry itself (lines, areas, or the points of a point layer)
  <name>_pc     one point per line or area (its centre), for symbols and labels placed at a point
  <name>_pi     the same with a point that is always inside the area (Mapnik placement "interior")
and, for the few layers whose SQL depends on the map scale (!scale_denominator!), one set per class of
zoom levels with the same query result (<name>@<first k>).

The result is written as JSON ("plan") and used by export_tiles.py and make_style.py, so both agree.
"""
import json
import re
import sys

import psycopg2

from mapnik_model import MAX_K, MapnikMap, denom

NUM_OIDS = {20, 21, 23, 700, 701, 1700, 26}
GEOM_DRAW = {"Line", "LinePattern", "Polygon", "PolygonPattern"}


def placement(sym):
    """'geom' = drawn along or inside the geometry, 'pc' / 'pi' = placed at one point of it"""
    if sym.type in GEOM_DRAW:
        return "geom"
    p = sym.a.get("placement", "point")
    if sym.type in ("Text", "Markers") and p in ("line", "vertex-first", "vertex-last", "vertex"):
        return "geom"
    return "pi" if p == "interior" else "pc"


def fill_tokens(sql, k, env="NULL"):
    px = 40075016.686 / (256 * 2 ** k)
    return (sql.replace("!scale_denominator!", repr(denom(k))).replace("!pixel_width!", repr(px))
            .replace("!pixel_height!", repr(px)).replace("!bbox!", env))


def load_file_layers(m, work_sql):
    """Shapefile and CSV layers are loaded into the database "gis" (tables vt_*), so every layer is a query."""
    import subprocess
    for l in m.layers:
        tbl = "vt_" + re.sub(r"[^a-z0-9]+", "_", l.name.lower())
        if l.type == "shape":
            path = l.params["file"]
            if not path.endswith(".shp"):
                path += ".shp"
            subprocess.run(["ogr2ogr", "-f", "PostgreSQL", "PG:dbname=gis", path, "-nln", tbl, "-overwrite", "-a_srs", "EPSG:3857",
                            "-lco", "GEOMETRY_NAME=way", "-lco", "SPATIAL_INDEX=GIST", "-nlt", "PROMOTE_TO_MULTI",
                            "--config", "PG_USE_COPY", "YES"], check=True, cwd=work_sql)
            l.type, l.table, l.params["dbname"] = "postgis", f"(SELECT way FROM {tbl}) AS t", "gis"
        elif l.type == "csv":
            subprocess.run(["ogr2ogr", "-f", "PostgreSQL", "PG:dbname=gis", l.params["file"], "-nln", tbl, "-overwrite",
                            "-oo", "X_POSSIBLE_NAMES=lon,lng,x,longitude", "-oo", "Y_POSSIBLE_NAMES=lat,y,latitude", "-oo", "KEEP_GEOM_COLUMNS=NO",
                            "-s_srs", "EPSG:4326", "-t_srs", "EPSG:3857", "-lco", "GEOMETRY_NAME=way"], check=True, cwd=work_sql)
            l.type, l.table, l.params["dbname"] = "postgis", f"(SELECT * FROM {tbl}) AS t", "gis"


def make_plan(m):
    conns = {}
    plan = {"layers": []}
    for l in m.layers:
        if l.type != "postgis":
            continue
        db = l.params.get("dbname", "gis")
        if db not in conns:
            conns[db] = psycopg2.connect(dbname=db)
            conns[db].autocommit = True
        cur = conns[db].cursor()
        k0 = (l.zooms() or [14])[-1]
        table = fill_tokens(l.table, k0)
        cur.execute(f"SELECT * FROM {table} LIMIT 0")
        cols, geom = {}, l.params.get("geometry_field")
        for d in cur.description:
            cur2 = conns[db].cursor()
            cur2.execute("SELECT typname FROM pg_type WHERE oid = %s", (d.type_code,))
            tn = cur2.fetchone()[0]
            if tn == "geometry":
                geom = geom or d.name
                continue
            cols[d.name] = "num" if d.type_code in NUM_OIDS else ("bool" if tn == "bool" else "text")
        geom = geom or "way"
        # which kinds of geometry the layer has (0 points, 1 lines, 2 areas)
        cur.execute(f"SELECT array_agg(DISTINCT d) FROM (SELECT ST_Dimension({geom}) d FROM {table} LIMIT 50000) s")
        dims = sorted(cur.fetchone()[0] or [])
        used = l.columns()
        missing = sorted(c for c in used if c not in cols)
        if missing:
            print(f"WARN: layer {l.name}: columns used in the style but not in the query: {missing}", file=sys.stderr)
        kinds = set()
        for _, r in l.rules():
            for s in r.symbolizers:
                kinds.add(placement(s))
        if dims and max(dims) == 0:      # a pure point layer: everything is drawn at the points themselves
            kinds = {"geom"}
        plan["layers"].append({
            "name": l.name, "db": db, "table": l.table, "geom": geom, "dims": dims,
            "columns": {c: t for c, t in cols.items() if c in used},
            "kinds": sorted(kinds), "classes": l.scale_classes(), "zooms": l.zooms(),
            "styles": [s.name for s in l.styles],
        })
    return plan


def source_layer(name, kind, cls, n_classes):
    s = name
    if n_classes > 1:
        s += f"@{cls[0]}"
    if kind != "geom":
        s += "_" + kind
    return s


if __name__ == "__main__":
    style, out = sys.argv[1], sys.argv[2]
    m = MapnikMap(style)
    load_file_layers(m, sys.argv[3] if len(sys.argv) > 3 else ".")
    plan = make_plan(m)
    json.dump(plan, open(out, "w"), indent=1)
    for p in plan["layers"]:
        print(f"{p['name']:32s} dims {p['dims']} kinds {p['kinds']} k {p['zooms'][0] if p['zooms'] else '-'}..{p['zooms'][-1] if p['zooms'] else '-'} {len(p['columns'])} columns")
