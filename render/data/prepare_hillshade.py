#!/usr/bin/env python3
"""Downloads theLIST "HillshadeGrey" basemap (CC BY 3.0 AU, ©State of Tasmania) for an area and one zoom level
and writes it as a single grey GeoTIFF in Web Mercator (tiled, JPEG compressed, with overviews).
Sea / no-data tiles are filled with a light flat grey (the sea is drawn over the hillshade anyway).

Usage: prepare_hillshade.py <level> <west> <south> <east> <north> <out.tif> [threads]
"""
import io
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from osgeo import gdal
from PIL import Image

URL = "https://services.thelist.tas.gov.au/arcgis/rest/services/Basemaps/HillshadeGrey/MapServer/tile/{z}/{y}/{x}"
UA = {"User-Agent": "LoriMaps-render (+https://github.com/lorenzdenk/lorimaps-render)"}
R = 20037508.342789244


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def fetch(args):
    z, x, y = args
    for attempt in range(6):
        try:
            req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            img = Image.open(io.BytesIO(data)).convert("L")
            a = np.asarray(img, dtype=np.uint8)
            return a
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def main():
    z = int(sys.argv[1])
    w, s, e, n = (float(v) for v in sys.argv[2:6])
    out = sys.argv[6]
    threads = int(sys.argv[7]) if len(sys.argv) > 7 else 12
    x0, y0 = tile_xy(n, w, z)
    x1, y1 = tile_xy(s, e, z)
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    size = 2 * R / 2 ** z
    print(f"level {z}: {cols} x {rows} tiles = {cols * rows}, {cols * 256} x {rows * 256} px", flush=True)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out, cols * 256, rows * 256, 1, gdal.GDT_Byte,
                    ["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", "COMPRESS=JPEG", "JPEG_QUALITY=80",
                     "BIGTIFF=YES", "PHOTOMETRIC=MINISBLACK"])
    ds.SetGeoTransform((-R + x0 * size, size / 256, 0, R - y0 * size, 0, -size / 256))
    ds.SetProjection("EPSG:3857")
    band = ds.GetRasterBand(1)
    empty = np.full((256, 256), 200, np.uint8)
    t0, got = time.time(), 0
    with ThreadPoolExecutor(threads) as pool:
        for r in range(rows):
            tiles = list(pool.map(fetch, [(z, x0 + c, y0 + r) for c in range(cols)]))
            strip = np.hstack([t if t is not None else empty for t in tiles])
            band.WriteArray(strip, 0, r * 256)
            got += sum(t is not None for t in tiles)
            if r % 10 == 0 or r == rows - 1:
                el = time.time() - t0
                print(f"  row {r + 1}/{rows}, {got} tiles with data, {el / 60:.1f} min", flush=True)
    ds.FlushCache()
    ds = None
    print("building overviews", flush=True)
    ds = gdal.Open(out, gdal.GA_Update)
    gdal.SetConfigOption("COMPRESS_OVERVIEW", "JPEG")
    gdal.SetConfigOption("JPEG_QUALITY_OVERVIEW", "80")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32, 64])
    ds = None
    print("done", flush=True)


if __name__ == "__main__":
    main()
