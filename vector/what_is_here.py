#!/usr/bin/env python3
"""Which features of which Mapnik layers lie at a place (within a few metres), in drawing order.
Usage: what_is_here.py <style.xml> <plan.json> <lon> <lat> [radius m=5] [k=15]"""
import json, sys
import psycopg2
from layers import fill_tokens
from mapnik_model import MapnikMap, sql_ident
style, plan, lon, lat = sys.argv[1], json.load(open(sys.argv[2])), float(sys.argv[3]), float(sys.argv[4])
rad = float(sys.argv[5]) if len(sys.argv) > 5 else 5
k = int(sys.argv[6]) if len(sys.argv) > 6 else 15
conns = {}
for p in plan["layers"]:
    cur = conns.setdefault(p["db"], psycopg2.connect(dbname=p["db"])).cursor()
    g = sql_ident(p["geom"])
    pt = f"ST_Transform(ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326), 3857)"
    cols = ", ".join(sql_ident(c) for c in p["columns"]) or "1"
    try:
        cur.execute(f"SELECT {cols}, GeometryType({g}), round(ST_Area({g})) FROM {fill_tokens(p['table'], k, f'ST_Expand({pt}, 500)')} WHERE ST_DWithin({g}, {pt}, {rad})")
    except Exception as e:
        print(p["name"], "ERROR", str(e)[:100]); conns[p["db"]].rollback(); continue
    rows = cur.fetchall()
    for r in rows[:6]:
        d = {c: v for c, v in zip(list(p["columns"]) + ["_type", "_area"], r) if v not in (None, "")}
        print(f"{p['name']:28s} {d}")
