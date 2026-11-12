#!/usr/bin/env python3
"""Downloads one layer of theLIST OpenDataWFS map service (CC BY 3.0 AU) for an area as GeoJSON (WGS84).
Usage: fetch_layer.py <layer id> <W> <S> <E> <N> <out.geojson> [fields, comma separated]
The service returns at most 2000 features per request: paged by OBJECTID."""
import json
import sys
import time
import urllib.parse
import urllib.request

layer, w, s, e, n, out = sys.argv[1:7]
fields = sys.argv[7] if len(sys.argv) > 7 else "*"
url = f"https://services.thelist.tas.gov.au/arcgis/rest/services/Public/OpenDataWFS/MapServer/{layer}/query"
UA = {"User-Agent": "LoriMaps-render (+https://github.com/lorenzdenk/lorimaps-render)"}
features, offset = [], 0
while True:
    q = urllib.parse.urlencode({
        "where": "1=1", "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "outFields": fields, "outSR": 4326, "orderByFields": "OBJECTID",
        "resultOffset": offset, "resultRecordCount": 2000, "f": "geojson"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "?" + q, headers=UA), timeout=180) as r:
                page = json.load(r)
            break
        except Exception as ex:
            print("retry", offset, ex, flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        sys.exit(f"ERROR: LIST layer {layer} not reachable")
    feats = page.get("features", [])
    features += feats
    if len(feats) < 2000 and not page.get("exceededTransferLimit"):
        break
    offset += len(feats)
print(f"LIST layer {layer}: {len(features)} features", flush=True)
json.dump({"type": "FeatureCollection", "features": features}, open(out, "w"))
