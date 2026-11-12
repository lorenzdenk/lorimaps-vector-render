#!/usr/bin/env python3
"""Measures which map layers make rendering slow.

Renders a few sample tiles, first with all layers, then each layer alone, and prints the time
per layer (summed over all sample tiles), slowest first. Usage (in the mapnik directory):

  render_profile.py --style opentopomap.xml --points points.txt --zoom 13 [--scale 2 --magnify 2]

points.txt: one "lat;lon" per line.
"""
import argparse
import math
import os
import time

import mapnik

from render_tiles import register_fonts, tile_bounds


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--style", required=True)
    p.add_argument("--points", required=True)
    p.add_argument("--zoom", type=int, default=13)
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--magnify", type=float, default=1.0)
    args = p.parse_args()

    register_fonts()
    px = 256 * args.scale
    m = mapnik.Map(px, px)
    mapnik.load_map(m, args.style)
    m.buffer_size = int(max(m.buffer_size, 128) * args.magnify)
    factor = float(args.scale * args.magnify)

    tiles = []
    for line in open(args.points):
        line = line.strip()
        if not line or ";" not in line:
            continue
        lat, lon = (float(v) for v in line.split(";")[:2])
        tiles.append(lonlat_to_tile(lon, lat, args.zoom))
    print(f"{len(tiles)} sample tiles at zoom {args.zoom}, {len(m.layers)} layers", flush=True)

    def render_all():
        t0 = time.time()
        for x, y in tiles:
            m.zoom_to_box(mapnik.Box2d(*tile_bounds(x, y, args.zoom)))
            im = mapnik.Image(px, px)
            mapnik.render(m, im, factor)
        return time.time() - t0

    render_all()  # warm-up: database and file caches
    total = render_all()
    print(f"all layers: {total:.1f} s ({total / len(tiles):.2f} s per tile)", flush=True)

    layers = list(m.layers)
    active = [l.active for l in layers]
    results = []
    for i, layer in enumerate(layers):
        if not active[i]:
            continue
        for l in layers:
            l.active = False
        layer.active = True
        t = render_all()
        try:
            typ = str(layer.datasource.params()["type"])
        except Exception:
            typ = "?"
        results.append((t, layer.name, typ))
        print(f"  {layer.name}: {t:.1f} s", flush=True)
    for l, a in zip(layers, active):
        l.active = a

    print("\nslowest layers (seconds for all sample tiles, share of the full render):")
    for t, name, typ in sorted(results, reverse=True)[:25]:
        print(f"  {t:7.1f} s  {100 * t / total:5.1f} %  {name}  [{typ}]")


if __name__ == "__main__":
    main()
