#!/usr/bin/env bash
# Variant D: roads from LIST as well. Runs after import_list_transport.sh.
#  - LIST road segments are added to planet_osm_line as OSM-like roads, so the normal OpenTopoMap road style draws
#    them (casing, colours, names). The road class comes from the OSM road that runs along it (keeps the familiar
#    look, ref numbers and one-way information), otherwise from the LIST class.
#  - OSM roads and tracks that LIST has are deleted. OSM bridges and tunnels stay (LIST has no bridge/tunnel
#    information): the LIST road is cut there and the OSM bridge or tunnel is drawn in its place.
#  - OSM roads that LIST does not have (and OSM tracks and paths, see import_list_transport.sh) stay.
set -euo pipefail
psql -q -v ON_ERROR_STOP=1 -d gis <<'SQL'
DROP TABLE IF EXISTS list_road;
CREATE TABLE list_road AS
  SELECT row_number() OVER () AS id, tran_class, pri_name, surface_ty, user_type, (ST_Dump(way)).geom AS way
    FROM list_transport WHERE trans_type = 'Road' AND tran_class <> 'Vehicular Track';
CREATE INDEX list_road_way ON list_road USING gist (way);
ANALYZE list_road;

-- OSM roads (incl. tracks) that run along a LIST road for most of their length
CREATE TEMP TABLE osm_road AS
  SELECT osm_id, highway, ref, oneway, bridge, tunnel, layer, way FROM planet_osm_line
   WHERE highway IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link','secondary',
                     'secondary_link','tertiary','tertiary_link','unclassified','residential','living_street',
                     'service','road','track');
CREATE INDEX ON osm_road USING gist (way);
CREATE TEMP TABLE osm_road_dup AS
  SELECT o.osm_id FROM osm_road o
   WHERE COALESCE(o.bridge, 'no') IN ('no', '') AND COALESCE(o.tunnel, 'no') IN ('no', '')
     AND ST_Length(o.way) > 0
     AND (SELECT ST_Length(ST_Intersection(o.way, ST_Buffer(ST_Collect(l.way), 20)))
            FROM list_road l WHERE ST_DWithin(l.way, o.way, 20)) > 0.6 * ST_Length(o.way);

-- attributes of the OSM road along each LIST road (the one with the longest common part)
ALTER TABLE list_road ADD COLUMN osm_highway text, ADD COLUMN osm_ref text, ADD COLUMN osm_oneway text;
UPDATE list_road l SET (osm_highway, osm_ref, osm_oneway) = (
  SELECT o.highway, o.ref, o.oneway FROM osm_road o
   WHERE ST_DWithin(o.way, ST_LineInterpolatePoint(l.way, 0.5), 20) AND o.highway <> 'track'
   ORDER BY ST_Distance(o.way, ST_LineInterpolatePoint(l.way, 0.5)) LIMIT 1);

-- cut the LIST roads at OSM bridges and tunnels
UPDATE list_road l SET way = ST_Difference(l.way, (
    SELECT ST_Union(ST_Buffer(o.way, 12)) FROM osm_road o
     WHERE (COALESCE(o.bridge, 'no') NOT IN ('no', '') OR COALESCE(o.tunnel, 'no') NOT IN ('no', ''))
       AND ST_DWithin(o.way, l.way, 12)))
  WHERE EXISTS (SELECT 1 FROM osm_road o
     WHERE (COALESCE(o.bridge, 'no') NOT IN ('no', '') OR COALESCE(o.tunnel, 'no') NOT IN ('no', ''))
       AND ST_DWithin(o.way, l.way, 12));
DELETE FROM list_road WHERE way IS NULL OR ST_IsEmpty(way);

DELETE FROM planet_osm_line WHERE osm_id IN (SELECT osm_id FROM osm_road_dup);

INSERT INTO planet_osm_line (osm_id, highway, name, ref, oneway, surface, access, z_order, way)
SELECT -(1000000000000 + id), hw, NULLIF(pri_name, ''), osm_ref, osm_oneway,
       CASE surface_ty WHEN 'Sealed' THEN 'paved' WHEN 'Unsealed' THEN 'unpaved' WHEN '4WD required' THEN 'unpaved' END,
       CASE WHEN user_type IN ('Restricted', 'Private') THEN 'private' END,
       CASE hw WHEN 'motorway' THEN 380 WHEN 'trunk' THEN 370 WHEN 'primary' THEN 360 WHEN 'secondary' THEN 350
               WHEN 'tertiary' THEN 340 WHEN 'service' THEN 150 ELSE 330 END,
       (ST_Dump(way)).geom
  FROM (SELECT *, COALESCE(osm_highway, CASE tran_class
          WHEN 'National/State Highway' THEN 'trunk' WHEN 'Arterial Road' THEN 'primary'
          WHEN 'Sub Arterial Road' THEN 'secondary' WHEN 'Collector Road' THEN 'tertiary'
          ELSE 'unclassified' END) AS hw FROM list_road) r;

-- the LIST roads are now drawn as roads: "unsealed" dots everywhere
UPDATE list_transport SET on_osm_track = false;
ANALYZE planet_osm_line;
SQL
psql -d gis -At -c "SELECT 'LIST roads: ' || count(*) || ' segments, classes: ' || string_agg(DISTINCT tran_class, ', ') FROM list_road;"
psql -d gis -At -c "SELECT 'LIST roads drawn as: ' || string_agg(highway || '=' || n, ', ') FROM (SELECT highway, count(*) n FROM planet_osm_line WHERE osm_id < -1000000000000 GROUP BY 1 ORDER BY 2 DESC) t;"
psql -d gis -At -c "SELECT 'OSM roads still drawn (not in LIST, or bridge/tunnel): ' || count(*) FROM planet_osm_line WHERE osm_id > -1000000000000 AND highway IN ('motorway','trunk','primary','secondary','tertiary','unclassified','residential','service','road');"
