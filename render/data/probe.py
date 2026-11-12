#!/usr/bin/env python3
"""Data probe on GitHub (the LIST and Parks servers are not reachable from the development machine).
Writes logs/gsw_pages.json, logs/gsw_list.json, logs/tile_sizes.txt."""
import csv, json, math, random, re, sys, time, urllib.parse, urllib.request

LOG = sys.argv[1]
UA = {"User-Agent": "LoriMaps-render (+https://github.com/lorenzdenk/lorimaps-render)"}


def get(url, binary=False, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return (r.status, data) if binary else (r.status, data.decode("utf-8", "replace"))


walks = list(csv.DictReader(open("render/data/gsw_walks.csv", encoding="utf-8"), delimiter=";"))

# 1) walk pages: coordinates, map links, start descriptions
pages = {}
for w in walks:
    try:
        st, html = get(w["url"])
    except Exception as e:
        pages[w["nr"]] = {"error": str(e)}
        continue
    coords = re.findall(r"(-4[0-4]\.\d{3,})\s*,\s*(14[4-8]\.\d{3,})", html)
    links = re.findall(r'href="([^"]*(?:maps\.google|google\.[a-z.]+/maps|goo\.gl|maps\.app|listmap|thelist)[^"]*)"', html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"(Getting there.{0,600})", text)
    g = re.search(r"(Grade\s*\d.{0,80})", text)
    pages[w["nr"]] = {"name": w["name"], "status": st, "coords": coords[:10], "links": links[:10],
                      "getting_there": m.group(1) if m else "", "grade": g.group(1) if g else ""}
    time.sleep(0.5)
json.dump(pages, open(f"{LOG}/gsw_pages.json", "w"), indent=1, ensure_ascii=False)

# 2) LIST transport segments whose name contains the walk name (or its main words)
Q = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/OpenDataWFS/MapServer/42/query"
STOP = {"and", "the", "walk", "walks", "circuit", "lookout", "summit", "historic", "site", "nature", "trail"}
out = {}
for w in walks:
    words = [x for x in re.findall(r"[A-Za-z]+", w["name"]) if x.lower() not in STOP]
    key = " ".join(words[:2]) if words else w["name"]
    where = f"UPPER(PRI_NAME) LIKE '%{key.upper().replace(chr(39), chr(39) * 2)}%'"
    q = urllib.parse.urlencode({"where": where, "outFields": "PRI_NAME,TRANS_TYPE,TRAN_CLASS,USER_TYPE",
                                "returnGeometry": "true", "outSR": 4326, "f": "geojson", "resultRecordCount": 200})
    try:
        _, js = get(Q + "?" + q, timeout=120)
        feats = json.loads(js).get("features", [])
    except Exception as e:
        out[w["nr"]] = {"key": key, "error": str(e)}
        continue
    names = {}
    for f in feats:
        p = f["properties"]
        k = f"{p['PRI_NAME']} | {p['TRANS_TYPE']} | {p['TRAN_CLASS']}"
        c = f["geometry"]["coordinates"] if f.get("geometry") else []
        while c and isinstance(c[0], list) and isinstance(c[0][0], list):
            c = c[0]
        pt = c[0] if c else None
        names.setdefault(k, [0, pt])[0] += 1
    out[w["nr"]] = {"name": w["name"], "key": key, "matches": {k: v for k, v in names.items()}}
json.dump(out, open(f"{LOG}/gsw_list.json", "w"), indent=1, ensure_ascii=False)


# 3) average tile sizes of LIST basemaps over land
def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


random.seed(1)
# land points: sample LIST transport segments (they are on land) spread over the island
q = urllib.parse.urlencode({"where": "1=1", "outFields": "OBJECTID", "returnGeometry": "true", "outSR": 4326,
                            "f": "geojson", "resultRecordCount": 2000, "resultOffset": 0})
pts = []
for off in (0, 60000, 120000, 180000, 240000):
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "OBJECTID", "returnGeometry": "true", "outSR": 4326,
                                "f": "geojson", "resultRecordCount": 400, "resultOffset": off, "orderByFields": "OBJECTID"})
    try:
        _, js = get(Q + "?" + q, timeout=120)
        for f in json.loads(js)["features"]:
            c = f["geometry"]["coordinates"]
            while isinstance(c[0], list):
                c = c[0]
            pts.append((c[1], c[0]))
    except Exception as e:
        print("points", e)
# plus random points in the bounding box (forest, mountains) to not only sample near roads
pts = random.sample(pts, min(60, len(pts))) + [(random.uniform(-43.6, -40.7), random.uniform(144.7, 148.3)) for _ in range(60)]
with open(f"{LOG}/tile_sizes.txt", "w") as fo:
    for svc, levels in (("HillshadeGrey", range(10, 19)), ("Orthophoto", range(12, 20)), ("AerialPhoto2026", range(14, 21))):
        for z in levels:
            sizes, missing, fmts = [], 0, {}
            for lat, lon in pts:
                x, y = deg2tile(lat, lon, z)
                url = f"https://services.thelist.tas.gov.au/arcgis/rest/services/Basemaps/{svc}/MapServer/tile/{z}/{y}/{x}"
                try:
                    st, data = get(url, binary=True, timeout=60)
                    sizes.append(len(data))
                    kind = "jpg" if data[:2] == b"\xff\xd8" else ("png" if data[:4] == b"\x89PNG" else "other")
                    fmts[kind] = fmts.get(kind, 0) + 1
                except Exception:
                    missing += 1
            avg = sum(sizes) / len(sizes) if sizes else 0
            line = f"{svc} z{z}: {len(sizes)} tiles, {missing} missing, avg {avg / 1024:.1f} KB, formats {fmts}"
            print(line, flush=True)
            fo.write(line + "\n")
