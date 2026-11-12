#!/usr/bin/env bash
# Own walk data for the "walks" overlay into the database "gis":
#   bigtree_hikes  render/pois/big_tree_hikes.geojson (tracks from bigtreestate.com and others, car routes removed)
#   gsw_tracks     60 Great Short Walks of Tasmania (number, name, level, time, distance from gsw_walks.csv).
#                  The booklet has no coordinates: the track lines are found by name (gsw_spec.csv: regular
#                  expression, approximate position and search radius) in the LIST transport segments
#                  (walking tracks and vehicular tracks), and in OpenStreetMap paths where LIST has nothing or
#                  much less than the length in the booklet. Pattern "@walks": all walking tracks in the
#                  (small) radius, for walks without a track name. Lines are cut to the search circle.
#                  gsw_manual.geojson: hand-made lines (e.g. beach walks without any track).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ogr2ogr -f PostgreSQL PG:dbname=gis "$HERE/../pois/big_tree_hikes.geojson" -nln bigtree_hikes -overwrite \
  -t_srs EPSG:3857 -lco GEOMETRY_NAME=way -nlt PROMOTE_TO_MULTI

HAS_LIST=$(psql -d gis -At -c "SELECT to_regclass('list_transport') IS NOT NULL")
if [ "$HAS_LIST" = t ]; then
  LIST_PART="SELECT s.nr, (ST_Dump(ST_Intersection(l.way, s.circle))).geom AS way, 'LIST' AS src FROM gsw_spec s JOIN list_transport l
      ON ST_DWithin(l.way, s.centre, s.radius)
     AND ((s.pattern <> '@walks' AND (l.trans_type = 'Track' OR l.tran_class IN ('Vehicular Track', 'Walking'))
           AND l.pri_name ~* s.pattern)
       OR (s.pattern = '@walks' AND l.trans_type = 'Track'
           AND (l.tran_class LIKE 'AS2156%' OR l.tran_class IN ('Walking', 'Shared Use Bike/Walking', 'Shared Use Track'))))"
else
  LIST_PART="SELECT NULL::text AS nr, NULL::geometry AS way, NULL::text AS src WHERE false"
fi

psql -q -v ON_ERROR_STOP=1 -d gis <<SQL
DROP TABLE IF EXISTS gsw_walks, gsw_spec, gsw_tracks, gsw_manual;
CREATE TABLE gsw_walks (nr text, name text, level text, time text, distance text, search text);
\copy gsw_walks FROM '$HERE/gsw_walks.csv' WITH (FORMAT csv, DELIMITER ';', HEADER true)
-- length of the walk in metres (first number: "7,4 km hin+zurück", "600 m", "2,4 km hin+zurück / 4 km Rundweg" -> 2400)
ALTER TABLE gsw_walks ADD COLUMN metres float;
UPDATE gsw_walks SET metres = (SELECT replace(m[1], ',', '.')::float * CASE WHEN m[2] = 'km' THEN 1000 ELSE 1 END
                                 FROM regexp_match(distance, '([0-9]+(?:,[0-9]+)?)[^0-9]*?\s*(km|m)\M') m);
-- "hin+zurück" (there and back): the track itself is half as long
UPDATE gsw_walks SET metres = metres / 2 WHERE distance ~ 'hin\+zurück' OR (distance !~ 'Rundweg' AND time ~ '^[^/]*hin\+zurück');
CREATE TABLE gsw_spec (nr text, pattern text, lon float, lat float, radius_km float);
\copy gsw_spec FROM '$HERE/gsw_spec.csv' WITH (FORMAT csv, DELIMITER ';', HEADER true)
ALTER TABLE gsw_spec ADD COLUMN centre geometry, ADD COLUMN radius float, ADD COLUMN circle geometry;
-- Web Mercator metres are 1/cos(latitude) times longer than real metres
UPDATE gsw_spec SET centre = ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 3857),
                    radius = radius_km * 1000 / cos(radians(lat));
UPDATE gsw_spec SET circle = ST_Buffer(centre, radius, 32);
DROP TABLE IF EXISTS gsw_list, gsw_osm;
CREATE TABLE gsw_list AS $LIST_PART;
DELETE FROM gsw_list WHERE way IS NULL OR GeometryType(way) <> 'LINESTRING';
-- OSM where LIST has nothing, or less than 40 % of the length in the booklet
CREATE TABLE gsw_osm AS
  SELECT s.nr, (ST_Dump(ST_Intersection(o.way, s.circle))).geom AS way, 'OSM' AS src FROM gsw_spec s
    JOIN planet_osm_line o
      ON o.highway IN ('path', 'footway', 'track', 'steps', 'bridleway', 'cycleway')
     AND ST_DWithin(o.way, s.centre, s.radius)
     AND ((s.pattern <> '@walks' AND o.name ~* s.pattern) OR (s.pattern = '@walks' AND o.highway IN ('path', 'footway', 'steps')))
    JOIN gsw_walks w ON w.nr = s.nr
   WHERE COALESCE((SELECT sum(ST_Length(l.way) * cos(radians(s.lat))) FROM gsw_list l WHERE l.nr = s.nr), 0)
         < 0.4 * COALESCE(w.metres, 0) + 1;
DELETE FROM gsw_osm WHERE way IS NULL OR GeometryType(way) <> 'LINESTRING';
-- OSM lines along a LIST line of the same walk: not twice
DELETE FROM gsw_osm o
 WHERE (SELECT ST_Length(ST_Intersection(o.way, ST_Buffer(ST_Collect(l.way), 30)))
          FROM gsw_list l WHERE l.nr = o.nr AND ST_DWithin(l.way, o.way, 30)) > 0.6 * ST_Length(o.way);
SQL
if [ -f "$HERE/gsw_manual.geojson" ]; then
  ogr2ogr -f PostgreSQL PG:dbname=gis "$HERE/gsw_manual.geojson" -nln gsw_manual -overwrite -t_srs EPSG:3857 \
    -lco GEOMETRY_NAME=way -nlt LINESTRING
else
  psql -q -d gis -c "CREATE TABLE gsw_manual (nr text, way geometry);"
fi
psql -q -v ON_ERROR_STOP=1 -d gis <<SQL
DROP TABLE IF EXISTS gsw_tracks;
CREATE TABLE gsw_tracks AS
  SELECT w.nr, w.name, w.level, w.time, w.distance, string_agg(DISTINCT t.src, '+') AS src,
         ST_Multi(ST_LineMerge(ST_Collect(t.way))) AS way
    FROM (SELECT nr, way, src FROM gsw_list UNION ALL SELECT nr, way, src FROM gsw_osm
          UNION ALL SELECT nr::text, way, 'manual' FROM gsw_manual) t
    JOIN gsw_walks w ON w.nr = t.nr
   GROUP BY 1, 2, 3, 4, 5;
CREATE INDEX gsw_tracks_way ON gsw_tracks USING gist (way);
SQL
psql -d gis -At -c "SELECT 'big tree hikes: ' || count(*) FROM bigtree_hikes;"
psql -d gis -At -c "SELECT 'great short walks found: ' || count(*) || ' (' || string_agg(nr || ' ' || src || ' ' || round(ST_Length(way) * cos(radians(ST_Y(ST_Transform(ST_Centroid(way), 4326)))))::int || ' m', ', ' ORDER BY nr::int) || ')' FROM gsw_tracks;"
psql -d gis -At -c "SELECT 'great short walks not found in this area: ' || string_agg(nr || ' ' || name, ', ' ORDER BY nr::int) FROM gsw_walks WHERE nr NOT IN (SELECT nr FROM gsw_tracks);"
