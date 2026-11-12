#!/usr/bin/env python3
"""Cuts the LIST hillshade to the render area and adjusts its brightness/contrast to the OpenTopoMap
hillshade made from the 30 m DEM, so the style (grain-merge, opacity) looks the same, only sharper.

Usage: remap_hillshade.py <list.tif> <otm_hillshade.tif> <dem_3857.tif> <W> <S> <E> <N> <out.tif>
Brightness and contrast are matched on land only (DEM > 1 m), from a reduced copy of all three rasters.
"""
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
src, ref, dem, W, S, E, N, out = sys.argv[1], sys.argv[2], sys.argv[3], *map(float, sys.argv[4:8]), sys.argv[8]


def small(path):
    ds = gdal.Warp("", path, format="MEM", dstSRS="EPSG:3857", outputBounds=(W, S, E, N),
                   outputBoundsSRS="EPSG:4326", width=2000, height=0, resampleAlg="average")
    return ds.GetRasterBand(1).ReadAsArray().astype(np.float64)


a, b = small(src), small(ref)
h = small(dem)
land = (h > 1) & (a != 200)
if land.sum() < 1000:
    land = np.ones_like(a, bool)
ma, sa = a[land].mean(), a[land].std()
mb, sb = b[land].mean(), b[land].std()
print(f"LIST hillshade: mean {ma:.1f} sd {sa:.1f}; OTM hillshade: mean {mb:.1f} sd {sb:.1f}")
k = 2.5
gdal.Warp(out, src, dstSRS="EPSG:3857", outputBounds=(W, S, E, N), outputBoundsSRS="EPSG:4326",
          resampleAlg="near", creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
tmp = out + ".scaled.tif"
gdal.Translate(tmp, out, scaleParams=[[ma - k * sa, ma + k * sa, mb - k * sb, mb + k * sb]],
               outputType=gdal.GDT_Byte,
               creationOptions=["TILED=YES", "COMPRESS=JPEG", "JPEG_QUALITY=85", "BIGTIFF=IF_SAFER"])
import os
os.replace(tmp, out)
print("written", out)
