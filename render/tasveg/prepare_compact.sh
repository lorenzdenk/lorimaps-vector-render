#!/usr/bin/env bash
# Makes a compact copy of TASVEG for fast map renders and uploads it to the release "tasveg-data":
#   - only VEGCODE, FOREST_STR, NOTABLE_TR
#   - Web Mercator (the map's projection), outlines simplified by 2 m and snapped to a 1 m grid
#     (good enough up to zoom 17: TASVEG itself is mapped at about 1:25 000)
#   - GeoPackage, xz-compressed: tasveg_kompakt.gpkg.xz
# Runs on GitHub Actions (workflow "Prepare TASVEG"); needs GITHUB_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail
WORK="${WORK:-$HOME/work}"
mkdir -p "$WORK/tasveg-src" && cd "$WORK/tasveg-src"

sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends postgresql-16-postgis-3 gdal-bin xz-utils unzip
sudo systemctl start postgresql
sudo -u postgres createuser -s "$USER" 2>/dev/null || true
dropdb --if-exists tv; createdb tv; psql -q -d tv -c "CREATE EXTENSION postgis;"

echo "== download original"
GH_TOKEN="$GITHUB_TOKEN" gh release download tasveg-data -R "$GITHUB_REPOSITORY" -p '*.zip' -D . --clobber
ls -la
for z in *.zip; do unzip -q -o "$z"; rm "$z"; done
SHP=$(find . -name 'TASVEG_*.shp' | head -1)
[ -n "$SHP" ] || { echo "ERROR: no TASVEG shapefile in the release ZIP"; find . | head -50; exit 1; }
echo "source: $SHP ($(du -h "$SHP" | cut -f1))"

echo "== import"
ogr2ogr -f PostgreSQL PG:dbname=tv "$SHP" -nln raw -select VEGCODE,FOREST_STR,NOTABLE_TR \
  -t_srs EPSG:3857 -lco GEOMETRY_NAME=way -lco SPATIAL_INDEX=NONE -nlt PROMOTE_TO_MULTI \
  --config PG_USE_COPY YES -gt 200000
psql -d tv -At -c "SELECT 'original: ' || count(*) || ' polygons, ' || sum(ST_NPoints(way)) || ' points' FROM raw;"

echo "== simplify"
psql -q -d tv <<'SQL'
CREATE TABLE tasveg AS
  SELECT vegcode, COALESCE(forest_str, '') AS forest_str, COALESCE(notable_tr, '') AS notable_tr,
         ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SnapToGrid(ST_SimplifyPreserveTopology(way, 2), 1)), 3)) AS way
  FROM raw;
DELETE FROM tasveg WHERE way IS NULL OR ST_IsEmpty(way);
SQL
psql -d tv -At -c "SELECT 'compact: ' || count(*) || ' polygons, ' || sum(ST_NPoints(way)) || ' points' FROM tasveg;"

echo "== export"
rm -f tasveg_kompakt.gpkg*
ogr2ogr -f GPKG tasveg_kompakt.gpkg PG:dbname=tv tasveg -nln tasveg -lco SPATIAL_INDEX=NO -lco GEOMETRY_NAME=way
xz -T0 -6 tasveg_kompakt.gpkg
ls -la tasveg_kompakt.gpkg.xz
GH_TOKEN="$GITHUB_TOKEN" gh release upload tasveg-data tasveg_kompakt.gpkg.xz -R "$GITHUB_REPOSITORY" --clobber
echo "uploaded tasveg_kompakt.gpkg.xz"
