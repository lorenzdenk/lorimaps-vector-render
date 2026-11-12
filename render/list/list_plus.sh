#!/usr/bin/env bash
# LIST_PLUS (on top of variant C): as many roads and tracks as possible from both sources, each drawn once.
#  1) Roads that LIST has are drawn with the LIST line (OSM road deleted). Road class for the look, route number
#     and one-way come from the OSM road along it; LIST adds its class (Access Road), access and surface.
#     Bridges and tunnels stay from OSM, the LIST road is cut there.
#  2) LIST roads that OSM does not have at all are added (access roads then drawn like a track); LIST roads where OSM only has a track or path are not
#     (the OSM track or path stays, as in variant C).
#  3) LIST vehicular tracks replace OSM roads on the same line (the LIST track is drawn).
#  4) LIST vehicular tracks along an OSM path/footway are not drawn (a footway stays a footway).
#  5) OSM tracks that LIST does not know at all are drawn as track grade 3.
# "Same line": within TOL Web Mercator metres (50 = about 36 m in Tasmania).
# Runs after import_list_transport.sh.
set -euo pipefail
psql -q -v ON_ERROR_STOP=1 -d gis <<'SQL'
\set TOL 50
-- sample points every 20 m along a (multi)line; "x % of a line lies within TOL of y" is the share of its points near y
-- (index lookups only; buffering unions of many segments was far too slow for the whole state)
CREATE OR REPLACE FUNCTION lp_pts(g geometry) RETURNS SETOF geometry LANGUAGE sql IMMUTABLE AS $$
  SELECT ST_LineInterpolatePoint(d.geom, (i + 0.5) / k.n)
    FROM ST_Dump(g) d, LATERAL (SELECT greatest(1, ceil(ST_Length(d.geom) / 20))::int AS n) k, generate_series(0, k.n - 1) i
   WHERE ST_Length(d.geom) > 0 AND GeometryType(d.geom) = 'LINESTRING' $$;
DROP TABLE IF EXISTS list_road, osm_road, list_road_ins;
CREATE TABLE list_road AS
  SELECT row_number() OVER () AS id, tran_class, user_type, surface_ty, pri_name, (ST_Dump(way)).geom AS way
    FROM list_transport WHERE trans_type = 'Road' AND tran_class NOT IN ('Not Applicable');
CREATE INDEX ON list_road USING gist (way);
ANALYZE list_road;
CREATE TABLE osm_road AS
  SELECT osm_id, highway, name, ref, oneway, way FROM planet_osm_line
   WHERE highway IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential','living_street',
                     'service','road','motorway_link','trunk_link','primary_link','secondary_link','tertiary_link')
     AND COALESCE(bridge, 'no') IN ('no', '') AND COALESCE(tunnel, 'no') IN ('no', '');
CREATE INDEX ON osm_road USING gist (way);
ANALYZE osm_road;

-- OSM roads that a LIST road (or LIST vehicular track) covers for more than half of their length: replaced
CREATE TEMP TABLE osm_road_dup AS
  SELECT o.osm_id FROM osm_road o
   WHERE ST_Length(o.way) > 0 AND EXISTS (SELECT 1 FROM list_road l WHERE ST_DWithin(l.way, o.way, :TOL))
     AND (SELECT avg((EXISTS (SELECT 1 FROM list_road l WHERE ST_DWithin(l.way, p, :TOL)))::int) FROM lp_pts(o.way) p) > 0.5;

-- LIST roads to draw: all except vehicular tracks (drawn by the LIST layer) and roads where OSM only has a
-- track or path (that one stays)
CREATE TABLE list_road_ins AS
  SELECT l.*,
         (SELECT o.highway FROM osm_road o WHERE ST_DWithin(o.way, ST_LineInterpolatePoint(l.way, 0.5), :TOL)
            ORDER BY ST_Distance(o.way, ST_LineInterpolatePoint(l.way, 0.5)) LIMIT 1) AS osm_highway,
         (SELECT o.ref FROM osm_road o WHERE ST_DWithin(o.way, ST_LineInterpolatePoint(l.way, 0.5), :TOL)
            ORDER BY ST_Distance(o.way, ST_LineInterpolatePoint(l.way, 0.5)) LIMIT 1) AS osm_ref,
         (SELECT o.name FROM osm_road o WHERE ST_DWithin(o.way, ST_LineInterpolatePoint(l.way, 0.5), :TOL)
            ORDER BY ST_Distance(o.way, ST_LineInterpolatePoint(l.way, 0.5)) LIMIT 1) AS osm_name
    FROM list_road l WHERE l.tran_class <> 'Vehicular Track';
DELETE FROM list_road_ins l
 WHERE l.osm_highway IS NULL
   AND (SELECT avg((EXISTS (SELECT 1 FROM planet_osm_line o
         WHERE o.highway IN ('track','path','footway','bridleway','steps','cycleway') AND ST_DWithin(o.way, p, :TOL)))::int)
          FROM lp_pts(l.way) p) > 0.5;
-- cut at OSM bridges and tunnels
UPDATE list_road_ins l SET way = ST_Difference(l.way, (
    SELECT ST_Union(ST_Buffer(o.way, 15)) FROM planet_osm_line o
     WHERE o.highway IS NOT NULL AND (COALESCE(o.bridge, 'no') NOT IN ('no', '') OR COALESCE(o.tunnel, 'no') NOT IN ('no', ''))
       AND ST_DWithin(o.way, l.way, 15)))
 WHERE EXISTS (SELECT 1 FROM planet_osm_line o
     WHERE o.highway IS NOT NULL AND (COALESCE(o.bridge, 'no') NOT IN ('no', '') OR COALESCE(o.tunnel, 'no') NOT IN ('no', ''))
       AND ST_DWithin(o.way, l.way, 15));

DELETE FROM planet_osm_line WHERE osm_id IN (SELECT osm_id FROM osm_road_dup);

ALTER TABLE planet_osm_line ADD COLUMN IF NOT EXISTS list_class text, ADD COLUMN IF NOT EXISTS list_user text;
-- access roads that OSM does not have at all (no road, no track): drawn like a track
INSERT INTO planet_osm_line (osm_id, highway, tracktype, name, ref, surface, list_class, list_user, z_order, way)
SELECT -(3000000000000 + row_number() OVER ()), hw, CASE WHEN hw = 'track' THEN 'grade2' END, COALESCE(NULLIF(pri_name, ''), osm_name), osm_ref,
       CASE surface_ty WHEN 'Sealed' THEN 'paved' WHEN 'Unsealed' THEN 'unpaved' WHEN '4WD required' THEN 'unpaved' END,
       tran_class, user_type,
       CASE hw WHEN 'track' THEN 110 WHEN 'motorway' THEN 380 WHEN 'trunk' THEN 370 WHEN 'primary' THEN 360 WHEN 'secondary' THEN 350
               WHEN 'tertiary' THEN 340 WHEN 'service' THEN 150 ELSE 330 END,
       (ST_Dump(way)).geom
  FROM (SELECT *, COALESCE(osm_highway, CASE WHEN tran_class = 'Access Road' THEN 'track' ELSE NULL END, CASE tran_class
          WHEN 'National/State Highway' THEN 'trunk' WHEN 'Arterial Road' THEN 'primary'
          WHEN 'Sub Arterial Road' THEN 'secondary' WHEN 'Collector Road' THEN 'tertiary'
          ELSE 'unclassified' END) AS hw FROM list_road_ins WHERE NOT ST_IsEmpty(way)) r;
ANALYZE planet_osm_line;

-- OSM tracks along a LIST road that is drawn anyway (matched to an OSM road elsewhere): not twice
DELETE FROM planet_osm_line o
 WHERE o.highway = 'track' AND o.osm_id > 0 AND ST_Length(o.way) > 0
   AND (SELECT avg((EXISTS (SELECT 1 FROM planet_osm_line l WHERE l.osm_id < -3000000000000 AND ST_DWithin(l.way, p, :TOL)))::int)
          FROM lp_pts(o.way) p) > 0.6;

-- OSM roads that stay (not in LIST): LIST class and access of a LIST road next to them, if any
UPDATE planet_osm_line o SET (list_class, list_user) = (
    SELECT l.tran_class, l.user_type FROM list_road l
     WHERE ST_DWithin(l.way, ST_LineInterpolatePoint(o.way, 0.5), :TOL) AND l.tran_class <> 'Vehicular Track'
     ORDER BY ST_Distance(l.way, ST_LineInterpolatePoint(o.way, 0.5)) LIMIT 1)
 WHERE o.osm_id > 0 AND o.highway IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential',
                     'living_street','service','road','motorway_link','trunk_link','primary_link',
                     'secondary_link','tertiary_link')
   AND GeometryType(o.way) = 'LINESTRING';

-- LIST vehicular tracks are drawn by the LIST layer, also where OSM had a road (deleted above)
UPDATE list_transport SET on_osm_road = false;

-- LIST vehicular tracks that OSM has as a path or footway: the footway wins
ALTER TABLE list_transport ADD COLUMN IF NOT EXISTS on_osm_path boolean DEFAULT false;
UPDATE list_transport l SET on_osm_path = true
  WHERE l.tran_class = 'Vehicular Track' AND ST_Length(l.way) > 0
    AND EXISTS (SELECT 1 FROM planet_osm_line o WHERE o.highway IN ('path','footway','bridleway','steps') AND ST_DWithin(o.way, l.way, :TOL))
    AND (SELECT avg((EXISTS (SELECT 1 FROM planet_osm_line o
          WHERE o.highway IN ('path','footway','bridleway','steps') AND ST_DWithin(o.way, p, :TOL)))::int) FROM lp_pts(l.way) p) > 0.6;

-- OSM tracks that LIST does not know at all (less than 30 % near any LIST segment): grade 3
UPDATE planet_osm_line o SET tracktype = 'grade3'
  WHERE o.highway = 'track' AND NOT COALESCE(o.list_dup, false) AND ST_Length(o.way) > 0
    AND COALESCE((SELECT avg((EXISTS (SELECT 1 FROM list_transport l WHERE ST_DWithin(l.way, p, :TOL)))::int)
                  FROM lp_pts(o.way) p), 0) < 0.3;
ANALYZE planet_osm_line; ANALYZE list_transport;
SQL
psql -d gis -At -c "SELECT 'roads drawn with the LIST line: ' || count(*) || ', ' || round(sum(ST_Length(way)) * 0.73 / 1000) || ' km, classes: ' || string_agg(DISTINCT list_class, ', ') FROM planet_osm_line WHERE osm_id < -3000000000000;"
psql -d gis -At -c "SELECT 'OSM roads kept (not in LIST): ' || count(*) FROM planet_osm_line WHERE osm_id > 0 AND highway IN ('primary','secondary','tertiary','unclassified','residential','service','road');"
psql -d gis -At -c "SELECT 'LIST vehicular tracks kept as OSM footway: ' || count(*) FROM list_transport WHERE on_osm_path;"
psql -d gis -At -c "SELECT 'OSM tracks drawn as grade 3: ' || count(*) FROM planet_osm_line WHERE highway = 'track' AND tracktype = 'grade3';"
