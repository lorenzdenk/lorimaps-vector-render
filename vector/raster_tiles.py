#!/usr/bin/env python3
"""Raster tiles for the parts of the map that stay pictures: relief colours (low zooms) and hillshade.

Mapnik draws them from the same files and with the same resampling as in the raster map; only these layers
are rendered, on a transparent background, into 512 px tiles. A tile of zoom t is drawn like the style's
zoom level k = t + 1 (the same resolution as OpenTopoMap's 256 px tiles of level k).

Hillshade: Mapnik blends it with "grain-merge" (map colour + opacity * grey - 0.5), which lightens sunny slopes and
darkens shady ones. MapLibre can only lay a picture over the map. Therefore:
  * Flat ground has the grey value FLAT (180 of 255: sun 45 degrees high), so grain-merge makes everything
    on flat ground lighter by a fixed amount. make_style.py adds that amount to the colours below the
    hillshade, and the relief colours get it here: flat areas then have exactly the colours of the raster map
    and the hillshade tiles are transparent there.
  * For every pixel the tile holds the colour and transparency that, laid over the ground colour the vector
    map draws there, gives exactly what grain-merge gives. The ground colours (areas below the hillshade,
    without patterns and outlines) are rendered with Mapnik for this.
"""
import argparse
import hashlib
import io
import math
import multiprocessing as mp
import os
import re
import sqlite3
import sys
import time

import mapnik
import numpy as np
from PIL import Image

R = 20037508.342789244
FLAT = 180.5 / 255


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def prepare_xml(style_path, group):
    """the style with only the raster layers of the group (name starts with it), drawn plainly"""
    from xml.etree import ElementTree as ET
    tree = ET.parse(style_path)
    root = tree.getroot()
    root.attrib.pop("background-color", None)
    keep_styles, opacity = set(), {}
    for layer in list(root.findall("Layer")):
        ds = {p.attrib["name"]: p.text for p in layer.find("Datasource")}
        if ds.get("type") != "gdal" or not layer.attrib["name"].startswith(group):
            root.remove(layer)
        else:
            keep_styles |= {s.text for s in layer.findall("StyleName")}
    for st in list(root.findall("Style")):
        if st.attrib["name"] not in keep_styles:
            root.remove(st)
            continue
        for sym in st.iter("RasterSymbolizer"):
            if sym.attrib.get("comp-op") == "grain-merge":
                # the opacity is applied when the grey values are converted
                for rule in st.findall("Rule"):
                    mn, mx = rule.find("MinScaleDenominator"), rule.find("MaxScaleDenominator")
                    opacity[(float(mn.text) if mn is not None else 0.0, float(mx.text) if mx is not None else float("inf"))] = float(sym.attrib.get("opacity", 1))
                sym.attrib.pop("comp-op")
                sym.attrib["opacity"] = "1"
    return ET.tostring(root, encoding="unicode"), opacity


def ground_xml(style_path):
    """the style with only the plain area fills below the hillshade (what the vector map draws under it)"""
    from xml.etree import ElementTree as ET
    root = ET.parse(style_path).getroot()
    keep, below = set(), True
    for layer in list(root.findall("Layer")):
        ds = {p.attrib["name"]: p.text for p in layer.find("Datasource")}
        if ds.get("type") == "gdal" and layer.attrib["name"].startswith("hillshade"):
            below = False
        if below:
            keep |= {s.text for s in layer.findall("StyleName")}
        else:
            root.remove(layer)
    for st in list(root.findall("Style")):
        if st.attrib["name"] not in keep:
            root.remove(st)
            continue
        for rule in st.findall("Rule"):
            for sym in list(rule):
                if sym.tag.endswith("Symbolizer") and sym.tag not in ("PolygonSymbolizer", "RasterSymbolizer"):
                    rule.remove(sym)
    return ET.tostring(root, encoding="unicode")


def init(style_path, group, base_dir, fmt, flat_shift):
    global _map, _opacity, _fmt, _merge, _flat_shift, _ground
    _flat_shift = flat_shift
    xml, _opacity = prepare_xml(style_path, group)
    _merge = bool(_opacity)
    _map = mapnik.Map(512, 512)
    mapnik.load_map_from_string(_map, xml, False, base_dir + "/")
    _fmt = fmt
    _ground = None
    if _merge:
        _ground = mapnik.Map(512, 512)
        mapnik.load_map_from_string(_ground, ground_xml(style_path), False, base_dir + "/")


def image(m, box):
    m.zoom_to_box(box)
    im = mapnik.Image(512, 512)
    mapnik.render(m, im, 1.0)
    return np.frombuffer(im.tostring(), dtype=np.uint8).reshape(512, 512, 4).astype(np.float32) / 255.0


def render(tile):
    z, x, y = tile
    s = 2 * R / 2 ** z
    box = mapnik.Box2d(-R + x * s, R - (y + 1) * s, -R + (x + 1) * s, R - y * s)
    a = image(_map, box)
    alpha = a[:, :, 3]
    if alpha.max() == 0:
        return z, x, y, None
    if _merge:
        den = 559082264.028 / 2 ** (z + 1)
        op = next((v for (mn, mx), v in _opacity.items() if mn <= den < mx), 0.85)
        grey = np.where(alpha > 0, a[:, :, 0] / np.maximum(alpha, 1e-6), FLAT)   # un-premultiplied grey value 0..1
        # Mapnik's grain-merge with an opacity: colour + opacity * grey - 0.5 (checked against Mapnik's output)
        delta = ((op * grey - 0.5) * alpha)[:, :, None]
        ground = image(_ground, box)[:, :, 0:3]                 # opaque: the map has a background colour
        want = np.clip(ground + delta, 0, 1)                    # Mapnik: ground with the hillshade merged in
        have = np.clip(ground + (op * FLAT - 0.5), 0, 1)        # vector map: ground colours with the flat-ground amount
        diff = want - have
        # smallest transparency for which a valid colour exists in every channel
        need = np.where(diff < 0, -diff / np.maximum(have, 1e-3), diff / np.maximum(1 - have, 1e-3))
        al = np.clip(need.max(axis=2), 0, 1)
        al = np.where(al < 0.012, 0, al)
        col = np.where(al[:, :, None] > 0, (want - have * (1 - al[:, :, None])) / np.maximum(al[:, :, None], 1e-3), 0)
        out = np.dstack([np.clip(col, 0, 1), al])
        img = Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")
    else:
        rgb = np.where(alpha[:, :, None] > 0, a[:, :, 0:3] / np.maximum(alpha[:, :, None], 1e-6), 0)
        rgb = np.clip(rgb + _flat_shift, 0, 1)
        img = Image.fromarray((np.dstack([rgb, alpha]) * 255 + 0.5).astype(np.uint8), "RGBA")
    buf = io.BytesIO()
    if _fmt == "webp":
        img.save(buf, "WEBP", quality=82, method=5, alpha_quality=90)
    else:
        img.save(buf, "PNG", optimize=True)
    return z, x, y, buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", required=True)
    ap.add_argument("--base", required=True, help="directory the style's file paths are relative to")
    ap.add_argument("--group", required=True, help="raster layers whose name starts with this, e.g. hillshade or relief")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--maxzoom", type=int, default=12)
    ap.add_argument("--format", default="webp", choices=["webp", "png"])
    ap.add_argument("--flat-shift", type=float, default=0.0, help="added to the colours (0-1) of a group that lies below "
                    "the hillshade: what grain-merge adds on flat ground (hillshade opacity * FLAT - 0.5)")
    ap.add_argument("--processes", type=int, default=os.cpu_count())
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    w, s, e, n = map(float, args.bbox.split(","))
    tiles = []
    for z in range(args.minzoom, args.maxzoom + 1):
        ax, ay = lonlat_to_tile(w, n, z)
        bx, by = lonlat_to_tile(e, s, z)
        tiles += [(z, x, y) for x in range(ax, bx + 1) for y in range(ay, by + 1)]
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
    t0, count, size = time.time(), 0, 0
    with mp.Pool(args.processes, init, (args.style, args.group, args.base, args.format, args.flat_shift)) as pool:
        for z, x, y, data in pool.imap_unordered(render, tiles, chunksize=4):
            if data:
                tid = hashlib.md5(data).hexdigest()
                db.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (tid, data))
                db.execute("INSERT INTO map VALUES (?, ?, ?, ?)", (z, x, 2 ** z - 1 - y, tid))
                count += 1
                size += len(data)
    meta = {"name": args.group, "format": args.format, "type": "overlay", "version": "1", "minzoom": str(args.minzoom),
            "maxzoom": str(args.maxzoom), "bounds": f"{w},{s},{e},{n}", "tilesize": "512"}
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    db.execute("CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row)")
    db.commit()
    db.close()
    print(f"{args.group}: {count} of {len(tiles)} tiles, {size / 1e6:.1f} MB, {(time.time() - t0) / 60:.1f} min -> {args.out}")


if __name__ == "__main__":
    main()
