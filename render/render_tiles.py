#!/usr/bin/env python3
"""Render a Mapnik style into a raster MBTiles file.

Tiles are rendered at 256*scale pixels with Mapnik's scale_factor, i.e. scale=2 gives
"@2x" 512 px tiles that show the same area and the same map rules as normal 256 px tiles,
only with twice the resolution.

--magnify 2 draws everything twice as large on the same tiles (scale_factor = scale*magnify):
each zoom level then shows the content of the next lower level, sharp and enlarged.
"""
import argparse
import hashlib
import io
import math
import multiprocessing as mp
import os
import sqlite3
import sys
import time

import mapnik

R = 20037508.342789244  # half of the Web Mercator world width in metres

_map = None
_args = None


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(max(min(lat, 85.0511), -85.0511))
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(x, y, z):
    size = 2 * R / (2 ** z)
    minx = -R + x * size
    maxy = R - y * size
    return minx, maxy - size, minx + size, maxy


def register_fonts():
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/wine/fonts"):
        for dirpath, _, files in os.walk(root, followlinks=True):
            for f in files:
                if f.lower().endswith((".ttf", ".otf", ".ttc")):
                    mapnik.FontEngine.register_font(os.path.join(dirpath, f))


def init_worker(args):
    global _map, _args
    _args = args
    register_fonts()
    px = 256 * args.scale
    _map = mapnik.Map(px, px)
    mapnik.load_map(_map, args.style)
    # larger symbols and labels need a wider margin, otherwise they are cut at tile edges
    _map.buffer_size = int(max(_map.buffer_size, 128) * args.magnify)


def render(tile):
    z, x, y = tile
    minx, miny, maxx, maxy = tile_bounds(x, y, z)
    _map.zoom_to_box(mapnik.Box2d(minx, miny, maxx, maxy))
    im = mapnik.Image(_map.width, _map.height)
    try:
        mapnik.render(_map, im, float(_args.scale * _args.magnify))
    except Exception as e:  # keep going, report at the end
        return z, x, y, None, str(e)
    if _args.format.startswith("webp"):
        # WebP via Pillow (lossy, quality after the colon, e.g. "webp:85")
        from PIL import Image
        quality = int(_args.format.split(":")[1]) if ":" in _args.format else 85
        if _args.high_zoom_quality and z >= _args.high_zoom:
            quality = _args.high_zoom_quality
        png = Image.open(io.BytesIO(im.tostring("png32")))
        buf = io.BytesIO()
        if _args.overlay:
            # transparent layer of a few colours (lines, hatching, labels): reduced to 32 colours and stored
            # lossless; about a quarter of the size of lossy WebP with alpha, and sharper
            rgba = png.convert("RGBA")
            if rgba.getextrema()[3][1] == 0:     # completely empty
                rgba.save(buf, "WEBP", lossless=True)
            else:
                q = rgba.quantize(colors=32, method=Image.Quantize.FASTOCTREE).convert("RGBA")
                # method 4: about 15 times faster than 5 on full tiles, only about 2 % larger
                q.save(buf, "WEBP", lossless=True, quality=100, method=4)
        else:
            png.convert("RGB").save(buf, "WEBP", quality=quality, method=5)
        return z, x, y, buf.getvalue(), None
    return z, x, y, im.tostring(_args.format), None


def tiles_for(bbox, zmin, zmax):
    w, s, e, n = bbox
    for z in range(zmin, zmax + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def tiles_touching(table, z, bbox, cond):
    """Set of (x, y) tiles of zoom z in the bbox for which cond holds for a row t of the table (database gis);
    {env} in cond is the tile's rectangle. None if the query fails (then nothing is left out)."""
    import subprocess
    x0, y0 = lonlat_to_tile(bbox[0], bbox[3], z)
    x1, y1 = lonlat_to_tile(bbox[2], bbox[1], z)
    env = ("ST_MakeEnvelope(-20037508.342789244 + x * s, 20037508.342789244 - (y + 1) * s, "
           "-20037508.342789244 + (x + 1) * s, 20037508.342789244 - y * s, 3857)")
    sql = (f"SELECT x || ',' || y FROM generate_series({x0},{x1}) x, generate_series({y0},{y1}) y, "
           f"LATERAL (SELECT 40075016.68557849 / (1 << {z}) AS s) c "
           f"WHERE EXISTS (SELECT 1 FROM {table} t WHERE {cond.format(env=env)})")
    res = subprocess.run(["psql", "-d", "gis", "-At", "-c", sql], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"WARN: tile filter on {table} failed: " + res.stderr.strip(), flush=True)
        return None
    return {tuple(map(int, l.split(","))) for l in res.stdout.split()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--style", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--bbox", required=True, help="west,south,east,north")
    p.add_argument("--minzoom", type=int, default=8)
    p.add_argument("--maxzoom", type=int, default=16)
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--magnify", type=float, default=1.0, help="draw lines, symbols and labels this much larger")
    p.add_argument("--overlay", action="store_true", help="transparent overlay (MBTiles type=overlay, tiles with alpha)")
    p.add_argument("--format", default="png8:m=h")
    p.add_argument("--high-zoom-quality", type=int, default=0, help="WebP quality from zoom --high-zoom on "
                   "(the highest levels are most of the file and are looked at enlarged least); 0 = as --format")
    p.add_argument("--high-zoom", type=int, default=16)
    p.add_argument("--name", default="map")
    p.add_argument("--processes", type=int, default=os.cpu_count())
    p.add_argument("--shard", type=int, default=0, help="render only this part (0-based) ...")
    p.add_argument("--shards", type=int, default=1, help="... of this many parts (for parallel jobs)")
    p.add_argument("--land-table", default="", help="table in database gis with land polygons (column way): "
                   "tiles above --land-zoom are only rendered inside land tiles of that zoom (open sea is left out; "
                   "the app enlarges the sea tile of the lower zoom level there)")
    p.add_argument("--land-zoom", type=int, default=11)
    p.add_argument("--detail-table", default="", help="table in database gis with the areas that need all zoom levels "
                   "(column way, see wild.sh): tiles above --detail-zoom are only rendered where they touch it "
                   "(wilderness is left out; the app enlarges the tiles of --detail-zoom there)")
    p.add_argument("--detail-zoom", type=int, default=15)
    p.add_argument("--deadline", type=float, default=0, help="stop at this time (seconds since 1970) and keep "
                   "what is rendered so far (GitHub ends jobs after 6 hours and the whole result would be lost)")
    p.add_argument("--skip", type=int, default=0, help="leave out the first SKIP tiles of the (shard's) list: "
                   "fills in what a run stopped by --deadline left out (it logs after how many tiles it stopped)")
    p.add_argument("--limit", type=int, default=0, help="render at most this many tiles (after --skip)")
    args = p.parse_args()

    register_fonts()
    faces = set(mapnik.FontEngine.face_names())
    print(f"{len(faces)} font faces registered", flush=True)

    bbox = [float(v) for v in args.bbox.split(",")]
    tiles = list(tiles_for(bbox, args.minzoom, args.maxzoom))
    if args.land_table and args.maxzoom > args.land_zoom:
        lz = args.land_zoom
        land = tiles_touching(args.land_table, lz, bbox, "t.way && {env}")
        if land is not None:
            before = len(tiles)
            tiles = [t for t in tiles if t[0] <= lz or (t[1] >> (t[0] - lz), t[2] >> (t[0] - lz)) in land]
            print(f"land filter ({args.land_table}, zoom {lz}): {len(land)} land tiles, "
                  f"{before - len(tiles)} sea tiles left out", flush=True)
    if args.detail_table and args.maxzoom > args.detail_zoom:
        dz = args.detail_zoom
        detail = tiles_touching(args.detail_table, dz, bbox, "ST_Intersects(t.way, {env})")
        if detail is not None:
            before = len(tiles)
            above = sum(1 for t in tiles if t[0] > dz)
            tiles = [t for t in tiles if t[0] <= dz or (t[1] >> (t[0] - dz), t[2] >> (t[0] - dz)) in detail]
            print(f"wilderness filter ({args.detail_table}, zoom {dz}): {len(detail)} tiles of zoom {dz} with detail, "
                  f"{before - len(tiles)} of {above} tiles above zoom {dz} left out", flush=True)
    if args.shards > 1:
        # blocks of 8x8 tiles stay together (shared database pages and caches)
        tiles = [t for t in tiles if ((t[1] >> 3) * 7919 + (t[2] >> 3) * 104729 + t[0]) % args.shards == args.shard]
        print(f"shard {args.shard + 1}/{args.shards}", flush=True)
    if args.skip or args.limit:
        total = len(tiles)
        tiles = tiles[args.skip:args.skip + args.limit] if args.limit else tiles[args.skip:]
        print(f"fill: tiles {args.skip}-{args.skip + len(tiles)} of {total}", flush=True)
    print(f"{len(tiles)} tiles, zoom {args.minzoom}-{args.maxzoom}, {256 * args.scale} px, magnify {args.magnify}, {args.processes} processes", flush=True)

    if os.path.exists(args.out):
        os.remove(args.out)
    db = sqlite3.connect(args.out)
    # identical tiles (sea, lakes, uniform forest) are stored only once: "map" points to "images",
    # "tiles" is a view with the standard MBTiles columns, so readers need nothing special
    db.executescript("""
        PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
    """)
    w, s, e, n = bbox
    meta = {
        "name": args.name,
        "format": "webp" if args.format.startswith("webp") else "png",
        "type": "overlay" if args.overlay else "baselayer",
        "version": "1",
        "description": f"OpenTopoMap style, {256 * args.scale} px tiles, magnify {args.magnify}",
        "attribution": "Kartendaten: © OpenStreetMap-Mitwirkende, Copernicus DEM (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA)"
                       + (" | Vegetation: TASVEG 5.0 from www.theLIST.tas.gov.au ©State of Tasmania (CC BY 3.0 AU)" if os.environ.get("TASVEG") == "1" else "")
                       + " | Kartendarstellung: © OpenTopoMap (CC-BY-SA)",
        "bounds": f"{w},{s},{e},{n}",
        "center": f"{(w + e) / 2},{(s + n) / 2},{min(args.maxzoom, 12)}",
        "minzoom": str(args.minzoom),
        "maxzoom": str(args.maxzoom),
        "scale": str(args.scale),
        "tile_size": str(256 * args.scale),
        "magnify": str(args.magnify),
    }
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())

    start = time.time()
    done = errors = 0
    zoom_time = {}
    last = time.time()
    first_error = None
    # load the style once here: an error in the worker initializer would make the pool restart workers forever
    init_worker(args)
    with mp.Pool(args.processes, initializer=init_worker, initargs=(args,)) as pool:
        for z, x, y, data, err in pool.imap_unordered(render, tiles, chunksize=8):
            done += 1
            now = time.time()
            zoom_time[z] = zoom_time.get(z, 0) + (now - last)
            last = now
            if err:
                errors += 1
                first_error = first_error or f"{z}/{x}/{y}: {err}"
            else:
                tid = hashlib.md5(data).hexdigest()
                db.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (tid, sqlite3.Binary(data)))
                db.execute("INSERT INTO map VALUES (?, ?, ?, ?)", (z, x, (2 ** z - 1) - y, tid))
            if args.deadline and now > args.deadline:
                print(f"WARN: time limit reached, stopping after {done} of {len(tiles)} tiles (file incomplete)", flush=True)
                break
            if done % 500 == 0 or done == len(tiles):
                db.commit()
                el = time.time() - start
                print(f"  {done}/{len(tiles)} tiles, {el / 60:.1f} min, {done / el:.1f} tiles/s, {errors} errors", flush=True)
    db.commit()
    unique = db.execute("SELECT count(*) FROM images").fetchone()[0]
    print("time per zoom level (wall clock, all processes):", ", ".join(f"z{z}: {t / 60:.1f} min" for z, t in sorted(zoom_time.items())))
    print(f"{done - errors} tiles, {unique} unique images ({done - errors - unique} duplicates stored once)")
    db.execute("VACUUM")
    db.close()
    print(f"written {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")
    if errors:
        print(f"{errors} tiles failed, first error: {first_error}")
        if errors == len(tiles):
            sys.exit(1)


if __name__ == "__main__":
    main()
