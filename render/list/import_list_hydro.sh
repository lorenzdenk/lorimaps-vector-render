#!/usr/bin/env bash
# Rivers, streams, lakes from theLIST (LIST Hydrographic Lines / Areas, CC BY 3.0 AU) instead of OpenStreetMap.
# They are written into the OpenStreetMap tables with OSM-like tags, so the normal OpenTopoMap style draws and
# names them (waterway=river/stream/canal/ditch, natural=water). The OSM waterways and water areas of the
# area are deleted first. The sea still comes from the OSM coastline (water polygon shapefiles).
# Usage: import_list_hydro.sh <workdir> <west> <south> <east> <north>
set -euo pipefail
WORK="$1"; W="$2"; S="$3"; E="$4"; N="$5"
HERE="$(cd "$(dirname "$0")" && pwd)"
bash "$HERE/list_data.sh" "$WORK" "$W" "$S" "$E" "$N" hydro_lines 23 "HYDLNTY1,HYDLNTY2,HYD_CLASS,NAME" gis list_hydro_lines \
  -nlt PROMOTE_TO_MULTI -lco SPATIAL_INDEX=GIST
bash "$HERE/list_data.sh" "$WORK" "$W" "$S" "$E" "$N" hydro_areas 22 "HYDARTY1,HYDARTY2,HYD_CLASS,NAME,PERENNIAL" gis list_hydro_areas \
  -nlt PROMOTE_TO_MULTI -lco SPATIAL_INDEX=GIST -makevalid

ENV="ST_Transform(ST_MakeEnvelope($W,$S,$E,$N,4326),3857)"
psql -q -v ON_ERROR_STOP=1 -d gis <<SQL
-- OSM waterways and inland water of the area out
DELETE FROM planet_osm_line WHERE waterway IN ('river','stream','canal','ditch','drain','brook') AND way && $ENV;
DELETE FROM planet_osm_polygon WHERE ("natural" = 'water' OR waterway = 'riverbank' OR landuse IN ('reservoir','basin'))
  AND way && $ENV;

-- lines: natural and artificial watercourses (no connectors through lakes, pipelines, tunnels, tidal lines)
INSERT INTO planet_osm_line (osm_id, waterway, name, intermittent, z_order, way)
SELECT -(2000000000000 + row_number() OVER ()),
       CASE WHEN hydlnty1 = 'Artificial Watercourse' THEN
              CASE WHEN hydlnty2 IN ('Canal','Water channel','Spillway','Flume') THEN 'canal' ELSE 'ditch' END
            WHEN hyd_class IN ('Major River','River','Minor River') THEN 'river'
            -- indefinite / implied streams and the small streams get own, lighter styles (customize_style.py)
            WHEN hydlnty2 IN ('Indefinite','Implied') THEN 'list_indef'
            WHEN COALESCE(hyd_class, '') NOT IN ('Major Stream','Stream') THEN 'list_minor'
            ELSE 'stream' END,
       NULLIF(name, ''),
       CASE WHEN hydlnty2 IN ('Indefinite','Implied') THEN 'yes' END,
       0, (ST_Dump(way)).geom
  FROM list_hydro_lines
 WHERE (hydlnty1 = 'Watercourse'
        OR (hydlnty1 = 'Artificial Watercourse' AND hydlnty2 IN ('Canal','Water race','Water channel','Drain','Spillway','Flume','Sluice')));

-- areas: lakes, reservoirs, river areas, estuaries (wetlands stay with TASVEG/OSM, the sea with the coastline)
INSERT INTO planet_osm_polygon (osm_id, "natural", water, name, way_area, z_order, way)
SELECT -(2000000000000 + row_number() OVER ()), 'water',
       CASE WHEN hydarty1 = 'Watercourse' OR hydarty1 = 'Estuary' THEN 'river'
            WHEN hydarty2 IN ('Reservoir','Hydro Storage') THEN 'reservoir' ELSE 'lake' END,
       NULLIF(name, ''), ST_Area(g), 0, g
  FROM (SELECT hydarty1, hydarty2, name, (ST_Dump(way)).geom AS g FROM list_hydro_areas
         WHERE hydarty1 IN ('Water Body','Watercourse','Estuary','Dam')
           AND COALESCE(hydarty2, '') NOT IN ('Swimming pool','Water tank','Dam wall')) t;
ANALYZE planet_osm_line; ANALYZE planet_osm_polygon;
SQL
psql -d gis -At -c "SELECT 'LIST waterways: ' || string_agg(waterway || '=' || n, ', ') FROM (SELECT waterway, count(*) n FROM planet_osm_line WHERE osm_id < -2000000000000 AND waterway IS NOT NULL GROUP BY 1) t;"
psql -d gis -At -c "SELECT 'LIST water areas: ' || count(*) FROM planet_osm_polygon WHERE osm_id < -2000000000000;"
