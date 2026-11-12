#!/usr/bin/env bash
# WILD_MAXZOOM: table "wild_detail" (database gis) = everything that is NOT wilderness. Tiles above WILD_MAXZOOM
# are only rendered where they touch it; in the wilderness the app enlarges the tiles of zoom WILD_MAXZOOM.
# Not wilderness is:
#   - everything within WILD_ROAD_KM of a road (OSM and, if imported, LIST)
#   - everything within WILD_TRAIL_KM of a path or track that a day walk reaches: the piece of path lies
#     within WILD_HIKE_KM (straight line, one way) of a road; WILD_HIKE_KM=0: every path and track
# Usage: wild.sh <mid latitude of the map> ; distances from WILD_ROAD_KM, WILD_HIKE_KM, WILD_TRAIL_KM
set -euo pipefail
MIDLAT="$1"
# Web Mercator metres per real metre at this latitude (1.35 in Tasmania)
K=$(python3 -c "import math; print(1 / math.cos(math.radians($MIDLAT)))")
psql -q -v ON_ERROR_STOP=1 -d gis \
  -v road="$(echo "${WILD_ROAD_KM:-2} * 1000 * $K" | bc -l)" \
  -v hike="$(echo "${WILD_HIKE_KM:-10} * 1000 * $K" | bc -l)" \
  -v trail="$(echo "${WILD_TRAIL_KM:-1} * 1000 * $K" | bc -l)" <<'SQL'
DROP TABLE IF EXISTS wild_road, wild_trail, wild_detail;
CREATE TABLE wild_road AS SELECT way FROM planet_osm_line
  WHERE highway IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link','secondary','secondary_link',
                    'tertiary','tertiary_link','unclassified','residential','living_street','service','road');
-- pieces of about 500 m, so a long path counts piece by piece
CREATE OR REPLACE FUNCTION wild_pieces(g geometry) RETURNS SETOF geometry LANGUAGE sql IMMUTABLE AS $$
  SELECT ST_LineSubstring(d.geom, i::float / k.n, (i + 1)::float / k.n)
    FROM ST_Dump(g) d, LATERAL (SELECT greatest(1, ceil(ST_Length(d.geom) / 700))::int AS n) k, generate_series(0, k.n - 1) i
   WHERE GeometryType(d.geom) = 'LINESTRING' AND ST_Length(d.geom) > 0 $$;
CREATE TABLE wild_trail AS SELECT wild_pieces(way) AS way FROM planet_osm_line
  WHERE highway IN ('track','path','footway','bridleway','steps','cycleway');
-- LIST roads and tracks (LIST_PLUS removes OSM roads that LIST has, so both sources are needed)
SELECT to_regclass('list_transport') IS NOT NULL AS has_list \gset
\if :has_list
INSERT INTO wild_road SELECT (ST_Dump(way)).geom FROM list_transport
  WHERE trans_type = 'Road' AND tran_class NOT IN ('Vehicular Track', 'Not Applicable');
INSERT INTO wild_trail SELECT wild_pieces(way) FROM list_transport
  WHERE tran_class = 'Vehicular Track' OR trans_type = 'Track' OR (trans_type = 'Route' AND tran_class = 'Walking');
\endif
CREATE INDEX ON wild_road USING gist (way);
ANALYZE wild_road;
CREATE TABLE wild_detail AS
  SELECT ST_Buffer(ST_Simplify(way, 30), :road, 2) AS way FROM wild_road
  UNION ALL
  SELECT ST_Buffer(ST_Simplify(t.way, 30), :trail, 2) FROM wild_trail t
   WHERE :hike = 0 OR EXISTS (SELECT 1 FROM wild_road r WHERE ST_DWithin(r.way, t.way, :hike));
CREATE INDEX ON wild_detail USING gist (way);
ANALYZE wild_detail;
SELECT 'wild: ' || (SELECT count(*) FROM wild_road) || ' roads, ' || (SELECT count(*) FROM wild_trail) || ' pieces of path, '
       || (SELECT count(*) FROM wild_detail) || ' detail areas';
SQL
