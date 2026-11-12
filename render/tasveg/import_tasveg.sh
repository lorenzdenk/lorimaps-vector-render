#!/usr/bin/env bash
# TASVEG 5.0 (www.theLIST.tas.gov.au, ©State of Tasmania, CC BY 3.0 AU) into the database "gis":
#   tasveg          vegetation polygons with class A ("Waldgefühl", 1-6), forest structure, cut into small pieces
#   tasveg_low      simplified larger polygons for zoom levels up to 11
#   tasveg_notable  one point per polygon with a notable tree species
# Source: the original ZIP from theLIST, as GitHub release "tasveg-data" of this repository,
# or a local file/folder in TASVEG_SRC (ZIP, .shp or folder containing TASVEG_5_0.shp).
# Usage: import_tasveg.sh <workdir> <west> <south> <east> <north>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$1"; W="$2"; S="$3"; E="$4"; N="$5"
mkdir -p "$WORK/tasveg"
cd "$WORK/tasveg"

SHP=$(find . -name 'TASVEG_*.shp' -o -name 'tasveg_kompakt.gpkg' 2>/dev/null | head -1 || true)
if [ -z "$SHP" ] && [ -z "${TASVEG_SRC:-}" ]; then
  # compact copy (see prepare_compact.sh): a few MB instead of several GB
  GH_TOKEN="${GITHUB_TOKEN:-}" gh release download tasveg-data -R "$GITHUB_REPOSITORY" -p 'tasveg_kompakt.gpkg.xz' -D . --clobber \
    && xz -d -f tasveg_kompakt.gpkg.xz && SHP=./tasveg_kompakt.gpkg || echo "no compact TASVEG copy yet, using the original"
fi
if [ -z "$SHP" ]; then
  if [ -n "${TASVEG_SRC:-}" ]; then
    case "$TASVEG_SRC" in
      *.zip) unzip -q -o "$TASVEG_SRC" ;;
      *.shp) cp "${TASVEG_SRC%.shp}".* . ;;
      *) cp -r "$TASVEG_SRC"/. . ;;
    esac
  else
    echo "downloading TASVEG from release tasveg-data of $GITHUB_REPOSITORY"
    GH_TOKEN="${GITHUB_TOKEN:-}" gh release download tasveg-data -R "$GITHUB_REPOSITORY" -p '*' -D . --clobber
    for z in *.zip; do [ -f "$z" ] && unzip -q -o "$z" && rm "$z"; done
  fi
  SHP=$(find . -name 'TASVEG_*.shp' | head -1)
fi
# the compact copy has lower-case column names and is already in Web Mercator
SELECT="VEGCODE,FOREST_STR,NOTABLE_TR"; case "$SHP" in *.gpkg) SELECT="vegcode,forest_str,notable_tr" ;; esac
[ -n "$SHP" ] || { echo "ERROR: no TASVEG shapefile found"; exit 1; }
echo "TASVEG source: $SHP"

# only the needed columns, clipped to the region, in Web Mercator
ogr2ogr -f PostgreSQL PG:dbname=gis "$SHP" -nln tasveg_raw -overwrite \
  -select "$SELECT" -spat "$W" "$S" "$E" "$N" -spat_srs EPSG:4326 \
  -t_srs EPSG:3857 -lco GEOMETRY_NAME=way -nlt PROMOTE_TO_MULTI -makevalid --config PG_USE_COPY YES -gt 100000

# class table (code;...;Einteilung_A;Einteilung_B) -> SQL
python3 - "$HERE/klassen_waldtypen.csv" > classes.sql <<'EOF'
import csv, sys
rows = list(csv.reader(open(sys.argv[1], encoding="utf-8-sig"), delimiter=";"))
print("DROP TABLE IF EXISTS tasveg_class; CREATE TABLE tasveg_class (code text PRIMARY KEY, cls_a int, cls_b int);")
for r in rows[1:]:
    print(f"INSERT INTO tasveg_class VALUES ('{r[0]}', {int(r[4][0])}, {int(r[5][0])});")
EOF
psql -q -d gis -f classes.sql

psql -q -v ON_ERROR_STOP=1 -d gis <<'SQL'
DROP TABLE IF EXISTS tasveg, tasveg_low, tasveg_notable;
-- class A from the table; F codes (left out there) drawn as 7 farmland / 8 plantations,
-- urban areas (FUR, FUM) and O codes (water, sand, rock) stay with OpenStreetMap
UPDATE tasveg_class SET cls_a = CASE WHEN code IN ('FPH','FPS','FPU') THEN 8 ELSE 7 END
  WHERE cls_a = 0 AND code LIKE 'F%' AND code NOT IN ('FUR','FUM');
CREATE TABLE tasveg AS
  SELECT c.cls_a, c.cls_b, r.vegcode, COALESCE(r.forest_str, '') AS forest_str,
         ST_Subdivide(ST_CollectionExtract(ST_MakeValid(r.way), 3), 256) AS way
  FROM tasveg_raw r JOIN tasveg_class c ON c.code = r.vegcode WHERE c.cls_a > 0;
CREATE INDEX tasveg_way ON tasveg USING gist (way);
-- simplifying can make polygons invalid (GEOS errors in ST_Subdivide): repair before cutting
CREATE TABLE tasveg_low AS
  SELECT cls_a, ST_Subdivide(g, 256) AS way FROM (
    SELECT c.cls_a, ST_CollectionExtract(ST_MakeValid(ST_SimplifyPreserveTopology(r.way, 60)), 3) AS g
    FROM tasveg_raw r JOIN tasveg_class c ON c.code = r.vegcode
    WHERE c.cls_a > 0 AND ST_Area(r.way) > 40000) t
  WHERE NOT ST_IsEmpty(g);
CREATE INDEX tasveg_low_way ON tasveg_low USING gist (way);
CREATE TABLE tasveg_notable AS
  SELECT notable_tr, ST_PointOnSurface(way) AS way, ST_Area(way) AS area FROM tasveg_raw
  WHERE COALESCE(notable_tr, '') <> '';
CREATE INDEX tasveg_notable_way ON tasveg_notable USING gist (way);
ANALYZE tasveg; ANALYZE tasveg_low; ANALYZE tasveg_notable;
SQL
psql -d gis -At -c "SELECT 'TASVEG pieces by class A: ' || string_agg(cls_a || '=' || n, ', ') FROM (SELECT cls_a, count(*) n FROM tasveg GROUP BY 1 ORDER BY 1) t;"
# example spots for the check images of the workflow (largest open woodland, largest patch with a notable tree)
INNER="ST_Transform(ST_Expand(ST_MakeEnvelope($W,$S,$E,$N,4326), -0.06), 3857)"   # inside the map area
psql -d gis -At -F';' > "$WORK/tasveg/sample_points.txt" <<SQL
SELECT 'woodland', ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(ST_PointOnSurface(way),4326) p FROM tasveg_raw
  WHERE forest_str = 'Woodland' AND ST_Intersects(ST_PointOnSurface(way), $INNER) ORDER BY ST_Area(way) DESC LIMIT 1) t;
SELECT 'tree', ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(way,4326) p FROM tasveg_notable
  WHERE ST_Intersects(way, $INNER) ORDER BY area DESC LIMIT 1) t;
SQL
cat "$WORK/tasveg/sample_points.txt"
psql -d gis -At -c "SELECT 'woodland pieces: ' || count(*) FILTER (WHERE forest_str = 'Woodland') || ', notable tree points: ' || (SELECT count(*) FROM tasveg_notable) FROM tasveg;"
