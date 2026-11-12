#!/usr/bin/env bash
# LIST Transport Segments (theLIST, ©State of Tasmania, CC BY 3.0 AU) for the map area into the database "gis":
#   list_transport   all segments (roads, vehicular tracks, walking tracks with AS2156 class, routes, ...)
# and marks OpenStreetMap paths that LIST already has (planet_osm_line.list_dup), so they are not drawn twice.
# Source: ArcGIS web service of theLIST (2000 features per request, paged), clipped to the area.
# Usage: import_list_transport.sh <workdir> <west> <south> <east> <north>
set -euo pipefail
WORK="$1"; W="$2"; S="$3"; E="$4"; N="$5"
mkdir -p "$WORK/list" && cd "$WORK/list"

# The whole state is downloaded once (about 290 000 segments) and kept for 14 days as release asset
# "osm-cache/list-transport-tas-YYYYMMDD.geojson.gz"; every render (and every shard) only clips it.
CACHED=""
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  export GH_TOKEN="$GITHUB_TOKEN"
  CACHED=$(gh release view osm-cache -R "$GITHUB_REPOSITORY" --json assets -q '.assets[].name' 2>/dev/null \
    | grep "^list-transport-tas-" | sort | tail -1 || true)
  if [ -n "$CACHED" ]; then
    day=$(echo "$CACHED" | sed -E 's/.*-([0-9]{8})\.geojson\.gz/\1/')
    age=$(( ( $(date -u +%s) - $(date -u -d "$day" +%s) ) / 86400 ))
    if [ "$age" -le 14 ] && gh release download osm-cache -R "$GITHUB_REPOSITORY" -p "$CACHED" -O tas.geojson.gz --clobber; then
      echo "using cached LIST data $CACHED ($age days old)"
      gunzip -f tas.geojson.gz
    else
      CACHED=""
    fi
  fi
fi
if [ -z "$CACHED" ]; then
  echo "downloading LIST transport segments for all of Tasmania"
python3 - <<'EOF'
import json, sys, time, urllib.parse, urllib.request
url = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/OpenDataWFS/MapServer/42/query"
fields = "TRANS_TYPE,TRAN_CLASS,USER_TYPE,SURFACE_TY,STATUS,PRI_NAME,SEC_NAME,TOUR_CLASS"
features, offset = [], 0
while True:
    q = urllib.parse.urlencode({
        "where": "1=1", "outFields": fields, "outSR": 4326,
        "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 2000, "f": "geojson"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url + "?" + q, timeout=120) as r:
                page = json.load(r)
            break
        except Exception as ex:
            print("retry", offset, ex, flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        sys.exit("ERROR: LIST web service not reachable")
    feats = page.get("features", [])
    features += feats
    print(f"  {len(features)} segments", flush=True)
    if len(feats) < 2000 and not page.get("exceededTransferLimit"):
        break
    offset += len(feats)
json.dump({"type": "FeatureCollection", "features": features}, open("tas.geojson", "w"))
EOF
  if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    name="list-transport-tas-$(date -u +%Y%m%d).geojson.gz"
    gzip -c tas.geojson > "$name"
    gh release upload osm-cache -R "$GITHUB_REPOSITORY" "$name" --clobber || echo "WARN: LIST cache upload failed"
    for old in $(gh release view osm-cache -R "$GITHUB_REPOSITORY" --json assets -q '.assets[].name' | grep "^list-transport-tas-" | grep -v "^$name$" || true); do
      gh release delete-asset osm-cache "$old" -R "$GITHUB_REPOSITORY" -y || true
    done
    rm -f "$name"
  fi
fi
ogr2ogr -f GeoJSON list_transport.geojson tas.geojson -spat "$W" "$S" "$E" "$N" -spat_srs EPSG:4326
rm -f tas.geojson

ogr2ogr -f PostgreSQL PG:dbname=gis list_transport.geojson -nln list_transport -overwrite \
  -t_srs EPSG:3857 -lco GEOMETRY_NAME=way -nlt PROMOTE_TO_MULTI -lco SPATIAL_INDEX=GIST

psql -q -v ON_ERROR_STOP=1 -d gis <<'SQL'
-- walking tracks of LIST (everything that is not a road, railway or ferry)
DROP TABLE IF EXISTS list_walk;
CREATE TABLE list_walk AS SELECT (ST_Dump(way)).geom AS way FROM list_transport
  WHERE trans_type = 'Track' OR (trans_type = 'Route' AND tran_class = 'Walking');
CREATE INDEX list_walk_way ON list_walk USING gist (way);
-- OSM paths that run along a LIST walking track for most of their length are duplicates
ALTER TABLE planet_osm_line ADD COLUMN IF NOT EXISTS list_dup boolean DEFAULT false;
UPDATE planet_osm_line o SET list_dup = true
  WHERE o.highway IN ('path','footway','bridleway','steps','cycleway')
    AND ST_Length(o.way) > 0
    AND (SELECT ST_Length(ST_Intersection(o.way, ST_Buffer(ST_Collect(l.way), 50)))
         FROM list_walk l WHERE ST_DWithin(l.way, o.way, 50)) > 0.6 * ST_Length(o.way);
-- OSM vehicle tracks that LIST has as a vehicular track or track (these LIST lines are drawn instead).
-- LIST roads are not drawn (roads come from OSM), so an OSM track that LIST has as a road stays.
DROP TABLE IF EXISTS list_all;
CREATE TABLE list_all AS SELECT (ST_Dump(way)).geom AS way FROM list_transport
  WHERE tran_class = 'Vehicular Track' OR trans_type = 'Track' OR (trans_type = 'Route' AND tran_class = 'Walking');
CREATE INDEX list_all_way ON list_all USING gist (way);
UPDATE planet_osm_line o SET list_dup = true
  WHERE o.highway = 'track'
    AND ST_Length(o.way) > 0
    AND (SELECT ST_Length(ST_Intersection(o.way, ST_Buffer(ST_Collect(l.way), 50)))
         FROM list_all l WHERE ST_DWithin(l.way, o.way, 50)) > 0.6 * ST_Length(o.way);
-- LIST roads that OSM only has as a (kept) track: no "unsealed" dots there (there is no white road to put them in)
-- LIST vehicular tracks that OSM already has as a road (e.g. Wallis Road): drawn by OSM as a road,
-- not a second time as a (grey, private) track line on top of it
ALTER TABLE list_transport ADD COLUMN IF NOT EXISTS on_osm_road boolean DEFAULT false;
UPDATE list_transport l SET on_osm_road = true
  WHERE l.tran_class = 'Vehicular Track' AND ST_Length(l.way) > 0
    AND (SELECT ST_Length(ST_Intersection(l.way, ST_Buffer(ST_Collect(o.way), 20)))
         FROM planet_osm_line o
         WHERE o.highway IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential',
                             'living_street','service','road','motorway_link','trunk_link','primary_link',
                             'secondary_link','tertiary_link')
           AND ST_DWithin(o.way, l.way, 20)) > 0.6 * ST_Length(l.way);
ALTER TABLE list_transport ADD COLUMN IF NOT EXISTS on_osm_track boolean DEFAULT false;
UPDATE list_transport l SET on_osm_track = true
  WHERE l.trans_type = 'Road' AND l.tran_class <> 'Vehicular Track'
    AND ST_Length(l.way) > 0
    AND (SELECT ST_Length(ST_Intersection(l.way, ST_Buffer(ST_Collect(o.way), 25)))
         FROM planet_osm_line o WHERE o.highway = 'track' AND NOT o.list_dup AND ST_DWithin(o.way, l.way, 25)) > 0.5 * ST_Length(l.way);
ANALYZE list_transport; ANALYZE planet_osm_line;
SQL
psql -d gis -At -c "SELECT 'LIST segments: ' || string_agg(trans_type || '/' || tran_class || '=' || n, ', ') FROM (SELECT trans_type, tran_class, count(*) n FROM list_transport GROUP BY 1, 2 ORDER BY 3 DESC) t;"
psql -d gis -At -c "SELECT 'LIST surfaces: ' || string_agg(COALESCE(surface_ty,'?') || '=' || n, ', ') FROM (SELECT surface_ty, count(*) n FROM list_transport GROUP BY 1 ORDER BY 2 DESC) t;"
psql -d gis -At -c "SELECT 'OSM paths duplicated by LIST: ' || count(*) FILTER (WHERE list_dup) || ' of ' || count(*) FROM planet_osm_line WHERE highway IN ('path','footway','bridleway','steps','cycleway');"
psql -d gis -At -c "SELECT 'OSM tracks duplicated by LIST: ' || count(*) FILTER (WHERE list_dup) || ' of ' || count(*) FROM planet_osm_line WHERE highway = 'track';"
