#!/usr/bin/env bash
# Renders an OpenTopoMap-style raster MBTiles file for the region in render/region.env.
# Runs on Ubuntu 24.04 (laptop, WSL2, Docker or a GitHub Actions runner); uses sudo, apt and a local PostgreSQL.
#
# Options (environment variables):
#   SKIP_APT=1   do not install software (already installed)
#   SKIP_DATA=1  keep the database, DEM and water data of the previous run (same region!):
#                only the style is fetched and patched again, then rendered. For style experiments.
#   WORK=...     working directory (default ~/work), OUT=... output directory (default $WORK/out)
#
# Data:  OpenStreetMap (Geofabrik extracts), Copernicus GLO-30 DEM (AWS open data),
#        OSM water polygons (osmdata.openstreetmap.de)
# Style: https://github.com/der-stefan/OpenTopoMap (mapnik)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
USER="${USER:-$(id -un)}"   # not set in Docker
source "$HERE/region.env"
export TASVEG="${TASVEG:-0}"
export OVERLAYS="${OVERLAYS:-}" LIST_PLUS="${LIST_PLUS:-0}" LIST_HYDRO="${LIST_HYDRO:-0}"   # with the walks overlay, giant trees and camp sites move from the map into it
# ONLY_OVERLAYS="walks": render only these overlays, no base map (the base map of an earlier run stays)
[ -n "${ONLY_OVERLAYS:-}" ] && export OVERLAYS="$ONLY_OVERLAYS"
WORK="${WORK:-$HOME/work}"
OTM="$WORK/OpenTopoMap"
DEM="$OTM/mapnik/dem"
DATA="$OTM/mapnik/data"
OUT="${OUT:-$WORK/out}"
mkdir -p "$WORK" "$OUT"

IFS=, read -r W S E N <<< "$BBOX"
BUF=0.05   # degrees of extra data around the box, so labels and lines at the edges are complete
BW=$(echo "$W - $BUF" | bc -l); BS=$(echo "$S - $BUF" | bc -l)
BE=$(echo "$E + $BUF" | bc -l); BN=$(echo "$N + $BUF" | bc -l)

step() { echo; echo "===== $(date +%H:%M:%S) $*"; }
sub() { echo "----- $(date +%H:%M:%S) $*"; }

# ---------------------------------------------------------------- software
if [ "${SKIP_APT:-0}" != 1 ]; then
step "Installing software"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
  postgresql-16-postgis-3 postgresql-contrib osm2pgsql osmium-tool \
  gdal-bin python3-gdal libgdal-dev python3-mapnik mapnik-utils \
  fonts-dejavu-core fonts-dejavu-extra fonts-unifont fonts-noto-core \
  libdbd-pg-perl bc unzip zip sqlite3 build-essential python3-pil
echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | sudo debconf-set-selections
for pkg in fonts-roboto-unhinted fonts-open-sans ttf-mscorefonts-installer fonts-wine fonts-urw-base35 fonts-texgyre; do   # optional fonts
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$pkg" || echo "WARN: $pkg not available"
done
fi
python3 -c "import mapnik; print('mapnik', mapnik.mapnik_version())"

# ---------------------------------------------------------------- style
step "Getting OpenTopoMap style"
if [ "${SKIP_DATA:-0}" = 1 ] && [ -d "$OTM/.git" ]; then
  # keep dem/ and data/ (generated, untracked), reset everything else to the original style
  git -C "$OTM" checkout -q . && git -C "$OTM" clean -qfd -e dem -e data
else
  rm -rf "$OTM"
  git clone -q --depth 1 https://github.com/der-stefan/OpenTopoMap "$OTM"
fi
git -C "$OTM" log -1 --format='OpenTopoMap commit %H (%cd)'
python3 "$HERE/customize_style.py" "$OTM/mapnik" "${EARLY_TRAILS:-0}" "${TASVEG:-0}" "${LIST_TRACKS:-}"
mkdir -p "$DEM" "$DATA"

# ---------------------------------------------------------------- database
step "Starting PostgreSQL"
pg() { sudo systemctl "$1" postgresql 2>/dev/null || sudo service postgresql "$1"; }   # systemd or not (WSL, Docker)
pg start
if [ "${SKIP_DATA:-0}" != 1 ]; then
for setting in "shared_buffers='1GB'" "work_mem='256MB'" "maintenance_work_mem='1GB'" "fsync=off" "synchronous_commit=off" "full_page_writes=off"; do
  sudo -u postgres psql -q -c "ALTER SYSTEM SET $setting"
done
pg restart
sudo -u postgres createuser -s "$USER" 2>/dev/null || true
for db in gis contours; do
  dropdb --if-exists "$db"; createdb "$db"
  psql -q -d "$db" -c "CREATE EXTENSION postgis; CREATE EXTENSION hstore; CREATE EXTENSION dblink;"
done

fi

if [ "${SKIP_DATA:-0}" != 1 ]; then
# ---------------------------------------------------------------- OSM data
step "Downloading and clipping OSM data"
cd "$WORK"
# On GitHub the clipped extract is kept in the release "osm-cache" and reused for up to 7 days
# (downloading all of Australia from Geofabrik is the slowest step of a small render).
CACHE_KEY=$(echo "$BW $BS $BE $BN $PBF_URLS" | md5sum | cut -c1-10)
CACHED=""
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  export GH_TOKEN="$GITHUB_TOKEN"
  CACHED=$(gh release view osm-cache -R "$GITHUB_REPOSITORY" --json assets -q '.assets[].name' 2>/dev/null \
    | grep "^osm-$CACHE_KEY-" | sort | tail -1 || true)
  if [ -n "$CACHED" ]; then
    day=$(echo "$CACHED" | sed -E 's/.*-([0-9]{8})\.osm\.pbf/\1/')
    age=$(( ( $(date -u +%s) - $(date -u -d "$day" +%s) ) / 86400 ))
    if [ "$age" -le 7 ]; then
      sub "using cached OSM extract $CACHED ($age days old)"
      gh release download osm-cache -R "$GITHUB_REPOSITORY" -p "$CACHED" -O region.osm.pbf --clobber || CACHED=""
    else
      sub "cached OSM extract $CACHED is $age days old, downloading new data"
      CACHED=""
    fi
  fi
fi
if [ -z "$CACHED" ]; then
  parts=()
  i=0
  for url in $PBF_URLS; do
    i=$((i+1))
    curl -fsSL --retry 3 -o "src$i.osm.pbf" "$url"
    osmium extract -s smart -b "$BW,$BS,$BE,$BN" "src$i.osm.pbf" -o "part$i.osm.pbf" --overwrite
    rm "src$i.osm.pbf"
    parts+=("part$i.osm.pbf")
  done
  if [ ${#parts[@]} -gt 1 ]; then
    osmium merge "${parts[@]}" -o region.osm.pbf --overwrite
  else
    mv "${parts[0]}" region.osm.pbf
  fi
  if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    name="osm-$CACHE_KEY-$(date -u +%Y%m%d).osm.pbf"
    cp region.osm.pbf "$name"
    gh release view osm-cache -R "$GITHUB_REPOSITORY" >/dev/null 2>&1 \
      || gh release create osm-cache -R "$GITHUB_REPOSITORY" --latest=false --title "OSM cache" \
           --notes "Clipped OpenStreetMap extracts reused by the render workflow for up to 7 days (© OpenStreetMap contributors, ODbL)." \
      || true
    gh release upload osm-cache -R "$GITHUB_REPOSITORY" "$name" --clobber || echo "WARN: cache upload failed"
    for old in $(gh release view osm-cache -R "$GITHUB_REPOSITORY" --json assets -q '.assets[].name' | grep "^osm-$CACHE_KEY-" | grep -v "^$name$" || true); do
      gh release delete-asset osm-cache "$old" -R "$GITHUB_REPOSITORY" -y || true
    done
    rm -f "$name"
  fi
fi
ls -la region.osm.pbf

step "Importing OSM data (osm2pgsql)"
osm2pgsql --slim -d gis -C 4000 --number-processes 4 \
  --style "$OTM/mapnik/osm2pgsql/opentopomap.style" region.osm.pbf

# ---------------------------------------------------------------- TASVEG vegetation (Tasmania, option)
if [ "${TASVEG:-0}" = 1 ]; then
  step "Importing TASVEG vegetation"
  bash "$HERE/tasveg/import_tasveg.sh" "$WORK" "$BW" "$BS" "$BE" "$BN"
fi

# ---------------------------------------------------------------- LIST transport segments (Tasmania, option)
if [ -n "${LIST_TRACKS:-}" ]; then
  step "Importing LIST transport segments (variant $LIST_TRACKS)"
  bash "$HERE/list/import_list_transport.sh" "$WORK" "$BW" "$BS" "$BE" "$BN"
  if [ "$LIST_TRACKS" = D ]; then bash "$HERE/list/list_roads.sh"; fi
  if [ "${LIST_PLUS:-0}" = 1 ]; then bash "$HERE/list/list_plus.sh"; fi
fi
# rivers and lakes from LIST instead of OSM (option)
if [ "${LIST_HYDRO:-0}" = 1 ]; then
  step "Importing LIST rivers and lakes"
  bash "$HERE/list/import_list_hydro.sh" "$WORK" "$BW" "$BS" "$BE" "$BN"
fi

# ---------------------------------------------------------------- own walk data for the walks overlay (option)
case " ${OVERLAYS:-} " in *" walks "*)
  step "Importing walk data (overlay)"
  bash "$HERE/data/import_walks.sh" ;;
esac

# ---------------------------------------------------------------- LIST land tenure for the access overlay (option)
case " ${OVERLAYS:-} " in *" tenure "*)
  step "Importing LIST land tenure (overlay)"
  bash "$HERE/list/list_data.sh" "$WORK" "$BW" "$BS" "$BE" "$BN" land_tenure 26 "TEN_CLASS" gis list_tenure \
    -nlt PROMOTE_TO_MULTI -lco SPATIAL_INDEX=GIST -makevalid
  psql -d gis -At -c "SELECT 'land tenure: ' || count(*) || ' areas, ' || count(DISTINCT ten_class) || ' classes' FROM list_tenure;" ;;
esac

# ---------------------------------------------------------------- DEM
step "Downloading Copernicus GLO-30 DEM"
rm -rf "$WORK/dem-src"; mkdir -p "$WORK/dem-src"
for lat in $(seq "$(printf %.0f "$(echo "($BS)/1 - 0.5" | bc -l)")" "$(printf %.0f "$(echo "($BN)/1 - 0.5" | bc -l)")"); do
  for lon in $(seq "$(printf %.0f "$(echo "($BW)/1 - 0.5" | bc -l)")" "$(printf %.0f "$(echo "($BE)/1 - 0.5" | bc -l)")"); do
    if [ "$lat" -lt 0 ]; then ns=S; la=$(( -lat )); else ns=N; la=$lat; fi
    if [ "$lon" -lt 0 ]; then ew=W; lo=$(( -lon )); else ew=E; lo=$lon; fi
    tile=$(printf "Copernicus_DSM_COG_10_%s%02d_00_%s%03d_00_DEM" "$ns" "$la" "$ew" "$lo")
    echo "  $tile"
    curl -fsSL --retry 3 -o "$WORK/dem-src/$tile.tif" \
      "https://copernicus-dem-30m.s3.amazonaws.com/$tile/$tile.tif" || echo "  (not available, probably sea)"
  done
done

step "Processing DEM (warp, hillshade, relief, contours)"
cd "$DEM"
GDAL="-co TILED=YES -co COMPRESS=LZW -co PREDICTOR=2 -co BIGTIFF=IF_SAFER"
MERC="+proj=merc +ellps=sphere +R=6378137 +a=6378137 +units=m"
gdalbuildvrt -q src.vrt "$WORK"/dem-src/*.tif
gdalwarp -q -overwrite $GDAL -te "$BW" "$BS" "$BE" "$BN" src.vrt raw.tif
ln -sf raw.tif dem-srtm.tiff   # used by the saddle/isolation tools
for r in 5000 1000 700 500 90; do
  gdalwarp -q -overwrite $GDAL -t_srs "$MERC" -r bilinear -tr $r $r raw.tif warp-$r.tif
done
gdalwarp -q -overwrite $GDAL -t_srs "$MERC" -r cubicspline -tr 30 30 raw.tif warp-30.tif

gdaldem color-relief -q -co COMPRESS=LZW -co PREDICTOR=2 -alpha warp-5000.tif "$OTM/mapnik/relief_color_text_file.txt" relief-5000.tif
gdaldem color-relief -q -co COMPRESS=LZW -co PREDICTOR=2 -alpha warp-500.tif "$OTM/mapnik/relief_color_text_file.txt" relief-500.tif
gdaldem hillshade -q -z 7 -compute_edges -co COMPRESS=JPEG warp-5000.tif hillshade-5000.tif
gdaldem hillshade -q -z 7 -compute_edges -co TILED=YES -co COMPRESS=JPEG warp-1000.tif hillshade-1000.tif
gdaldem hillshade -q -z 4 -compute_edges -co TILED=YES -co COMPRESS=JPEG warp-700.tif hillshade-700.tif
gdaldem hillshade -q -z 5 -compute_edges -co TILED=YES -co COMPRESS=JPEG warp-500.tif hillshade-500.tif
gdaldem hillshade -q -z 2 -compute_edges -co TILED=YES -co COMPRESS=JPEG warp-90.tif hillshade-90.tif
gdaldem hillshade -q -z 2 -compute_edges -co TILED=YES -co COMPRESS=JPEG warp-30.tif hillshade-30m-jpeg.tif

# Tasmania: sharper hillshade from theLIST (HillshadeGrey, about 7 m, CC BY 3.0 AU) instead of the 30 m one
# (used from zoom 9). Release asset made by .github/workflows/hillshade-prepare.yml.
if [ "${LIST_HILLSHADE:-0}" != 0 ]; then
  LHS="$WORK/lhs/list_hillshade_z${LIST_HILLSHADE_LEVEL:-14}.tif"
  mkdir -p "$WORK/lhs"
  [ -s "$LHS" ] || GH_TOKEN="${GITHUB_TOKEN:-}" gh release download hillshade-data -R "$GITHUB_REPOSITORY" \
      -p "$(basename "$LHS")" -D "$WORK/lhs" --clobber || echo "WARN: LIST hillshade not available"
  if [ -s "$LHS" ]; then
    python3 "$HERE/data/remap_hillshade.py" "$LHS" hillshade-30m-jpeg.tif warp-30.tif "$BW" "$BS" "$BE" "$BN" hillshade-list.tif \
      && mv hillshade-list.tif hillshade-30m-jpeg.tif && echo "using LIST hillshade" \
      || echo "WARN: LIST hillshade failed, keeping the 30 m hillshade"
  fi
fi

# overviews: lower zoom levels read reduced rasters instead of the full 30 m data
for f in hillshade-*.tif relief-*.tif; do
  gdaladdo -q -r average --config COMPRESS_OVERVIEW DEFLATE "$f" 2 4 8 16 32 64 || echo "WARN: no overviews for $f"
done

# contour lines every 10 m from the 30 m DEM, into database "contours", table "contours(way, ele)"
if [ "${LIST_CONTOURS:-0}" = 1 ]; then
  # Tasmania: the 10 m contours of theLIST (LIST Contours 10m, CC BY 3.0 AU, from a much finer elevation model)
  bash "$HERE/list/list_data.sh" "$WORK" "$BW" "$BS" "$BE" "$BN" contours 16 "ELEVATION" contours contours \
    -nlt LINESTRING -explodecollections -dialect OGRSQL -sql "SELECT ELEVATION AS ele FROM contours"
else
  gdal_contour -q -a ele -i 10 -f GPKG warp-30.tif "$WORK/contours.gpkg"
  ogr2ogr --config PG_USE_COPY YES -gt 100000 -f PostgreSQL PG:dbname=contours "$WORK/contours.gpkg" -nln contours -a_srs EPSG:3857 \
    -lco GEOMETRY_NAME=way -nlt LINESTRING -explodecollections -overwrite
fi
psql -q -d contours -c "ALTER TABLE contours ALTER COLUMN ele TYPE integer USING round(ele)::integer; DELETE FROM contours WHERE ST_Length(way) < 20;"
# contour lines from gdal_contour are very long (whole mountain sides, 100 000+ points) and every tile
# fetched them completely: 90-97 % of the render time. Split into pieces of at most 1000 points
# (long enough for the height labels, which need a minimum line length).
psql -q -d contours -c "CREATE TABLE contours_split AS SELECT ele, ST_Subdivide(way, 1000) AS way FROM contours;
  DROP TABLE contours; ALTER TABLE contours_split RENAME TO contours;
  CREATE INDEX contours_way ON contours USING gist (way); ANALYZE contours;"
psql -d contours -t -c "SELECT 'contour lines: ' || count(*) FROM contours;"
ls -la "$DEM"

# ---------------------------------------------------------------- water polygons (oceans)
step "Water polygons"
cd "$WORK"
read -r MX0 MY0 <<< "$(echo "$BW $BS" | gdaltransform -s_srs EPSG:4326 -t_srs EPSG:3857 -output_xy)"
read -r MX1 MY1 <<< "$(echo "$BE $BN" | gdaltransform -s_srs EPSG:4326 -t_srs EPSG:3857 -output_xy)"
# the clipped sea polygons are kept in the release "osm-cache" (coastlines hardly change; the download server is
# sometimes unreachable): used for 30 days, or longer if osmdata.openstreetmap.de cannot be reached
WATER_ASSET="water-$CACHE_KEY.zip"
have_cache=""
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  wc_date=$(gh release view osm-cache -R "$GITHUB_REPOSITORY" --json assets -q ".assets[] | select(.name==\"$WATER_ASSET\") | .updatedAt" 2>/dev/null || true)
  if [ -n "$wc_date" ] && gh release download osm-cache -R "$GITHUB_REPOSITORY" -p "$WATER_ASSET" -O water-cache.zip --clobber; then
    have_cache=1
    wc_age=$(( ( $(date -u +%s) - $(date -u -d "$wc_date" +%s) ) / 86400 ))
  fi
fi
if [ -n "$have_cache" ] && [ "$wc_age" -le 30 ]; then
  sub "using cached sea polygons ($wc_age days old)"
  unzip -q -o water-cache.zip -d "$DATA"
else
  ok=1
  # one quick check first: when the server is unreachable, every download attempt waits a minute (8 minutes in all)
  curl -fsS -o /dev/null --connect-timeout 20 --max-time 40 https://osmdata.openstreetmap.de/ || ok=""
  for set in simplified-water-polygons-split-3857:simplified_water_polygons water-polygons-split-3857:water_polygons; do
    [ -n "$ok" ] || break
    dir=${set%%:*}; layer=${set##*:}
    if curl -fsSL --retry 3 --connect-timeout 60 -o "$dir.zip" "https://osmdata.openstreetmap.de/download/$dir.zip"; then
      unzip -q -o "$dir.zip" && rm "$dir.zip"
      mkdir -p "$DATA/$dir"
      ogr2ogr -f "ESRI Shapefile" -spat "$MX0" "$MY0" "$MX1" "$MY1" "$DATA/$dir/$layer.shp" "$dir/$layer.shp" -overwrite
      rm -rf "$dir"
    else
      ok=""
    fi
  done
  if [ -n "$ok" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    (cd "$DATA" && zip -q -r "$WORK/$WATER_ASSET" simplified-water-polygons-split-3857 water-polygons-split-3857)
    gh release upload osm-cache -R "$GITHUB_REPOSITORY" "$WORK/$WATER_ASSET" --clobber || echo "WARN: sea polygon cache upload failed"
  elif [ -z "$ok" ] && [ -n "$have_cache" ]; then
    sub "osmdata.openstreetmap.de not reachable, using cached sea polygons ($wc_age days old)"
    unzip -q -o water-cache.zip -d "$DATA"
  elif [ -z "$ok" ]; then
    # no download, no cache: build the sea polygons from the coastline in our own OSM extract
    sub "osmdata.openstreetmap.de not reachable, building sea polygons with osmcoastline"
    command -v osmcoastline >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends osmcoastline
    osmcoastline -o coast.db --output-polygons=water --srs=3857 -f "$WORK/region.osm.pbf" || true
    for set in simplified-water-polygons-split-3857:simplified_water_polygons water-polygons-split-3857:water_polygons; do
      dir=${set%%:*}; layer=${set##*:}
      mkdir -p "$DATA/$dir"
      ogr2ogr -f "ESRI Shapefile" "$DATA/$dir/$layer.shp" coast.db water_polygons -nln "$layer" -overwrite
    done
    ls -la "$DATA"/water-polygons-split-3857
    # keep the result: the next renders of this area do not need to build it again
    if [ -s "$DATA/water-polygons-split-3857/water_polygons.shp" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
      (cd "$DATA" && zip -q -r "$WORK/$WATER_ASSET" simplified-water-polygons-split-3857 water-polygons-split-3857)
      gh release upload osm-cache -R "$GITHUB_REPOSITORY" "$WORK/$WATER_ASSET" --clobber || echo "WARN: sea polygon cache upload failed"
    fi
  fi
fi

# ---------------------------------------------------------------- OpenTopoMap preprocessing
step "OpenTopoMap preprocessing"
cd "$OTM/mapnik/tools"
cc -o saddledirection saddledirection.c -lm -lgdal -I/usr/include/gdal || echo "WARN: saddledirection not compiled"
cc -Wall -o isolation isolation.c -lgdal -lm -O2 -I/usr/include/gdal || echo "WARN: isolation not compiled"

for t in planet_osm_point planet_osm_polygon; do
  psql -q -d gis -c "ALTER TABLE $t ADD COLUMN IF NOT EXISTS otm_isolation text; ALTER TABLE $t ADD COLUMN IF NOT EXISTS hiking text;"
done
psql -q -d gis -c "ALTER TABLE planet_osm_point ADD COLUMN IF NOT EXISTS direction text;"

timeout 30m psql -q -d gis < arealabel.sql || echo "WARN: arealabel.sql failed"
timeout 40m bash update_lowzoom.sh || echo "WARN: update_lowzoom.sh failed"

# the update scripts expect fixed home paths; patch them to ours
for s in update_saddles.sh update_isolations.sh update_parking.sh; do
  sed -e "s#cd ~/OpenTopoMap/#cd $OTM/#" \
      -e "s#cd /home/otmuser/OpenTopoMap/mapnik/tools/#cd $OTM/mapnik/tools/#" \
      -e "s#/home/otmuser/OpenTopoMap#$OTM#g" "$s" > "local_$s"
  # the peak isolation search radius is reduced (default 100 km is very slow for large areas)
  [ "$s" = update_isolations.sh ] && sed -i 's#isolation -f \$demfile#isolation -r 25000 -f $demfile#' "local_$s"
  timeout 40m bash "local_$s" || echo "WARN: $s failed or timed out"
done
for f in stationdirection.sql viewpointdirection.sql pitchicon.sql; do
  psql -q -d gis < "$f" || echo "WARN: $f failed"
done
psql -q -d gis -c "ANALYZE;"
else
  step "SKIP_DATA=1: using database, DEM and water polygons of the previous run"
fi

# ---------------------------------------------------------------- render
cd "$OTM/mapnik"
if [ "${MODE:-tiles}" = "profile" ]; then
  step "Measuring render time per layer"
  [ -n "${FONT:-}" ] && python3 "$HERE/render_preview.py" --style opentopomap.xml --set-font "$FONT"
  ENV="ST_Transform(ST_MakeEnvelope($W,$S,$E,$N,4326),3857)"
  # typical tiles: random roads/tracks/paths, plus the inside of the largest forest and protected areas
  psql -d gis -At -F';' > "$WORK/points.txt" <<SQL
SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(way,0.5),4326) p FROM planet_osm_line
  WHERE highway IS NOT NULL AND way && $ENV AND GeometryType(way)='LINESTRING' ORDER BY random() LIMIT 8) t;
SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_Transform(ST_PointOnSurface(way),4326) p FROM planet_osm_polygon
  WHERE (landuse='forest' OR "natural" IN ('wood','scrub') OR boundary IN ('national_park','protected_area')) AND way && $ENV
  ORDER BY way_area DESC NULLS LAST LIMIT 6) t;
SQL
  cat "$WORK/points.txt"
  psql -d gis -At -c "SELECT 'largest polygons (points): ' || string_agg(n::text, ', ') FROM (SELECT ST_NPoints(way) n FROM planet_osm_polygon ORDER BY 1 DESC LIMIT 10) t;"
  for z in ${PROFILE_ZOOMS:-13}; do
    echo "----- zoom $z"
    PYTHONPATH="$HERE" python3 "$HERE/render_profile.py" --style opentopomap.xml --points "$WORK/points.txt" \
      --zoom "$z" --scale "$SCALE" --magnify "${MAGNIFY:-1}"
  done
elif [ "${MODE:-tiles}" = "preview" ]; then
  step "Rendering preview images"
  # fixed places plus example spots found in the data (viewpoint, invisible path, private way)
  PLACES="$WORK/places.txt"
  cp "$HERE/preview_places.txt" "$PLACES"
  ENV="ST_Transform(ST_MakeEnvelope($W,$S,$E,$N,4326),3857)"
  psql -d gis -At -F';' >> "$PLACES" <<SQL
SELECT 'Aussichtspunkt', ST_Y(p), ST_X(p), 16 FROM (SELECT ST_Transform(way,4326) p FROM planet_osm_point
  WHERE tourism='viewpoint' AND way && $ENV ORDER BY (name IS NULL), osm_id LIMIT 1) t;
SELECT 'Zeltplatz', ST_Y(p), ST_X(p), 15 FROM (
  SELECT ST_Transform(way,4326) p, name FROM planet_osm_point WHERE tourism='camp_site' AND way && $ENV
  UNION ALL SELECT ST_Transform(ST_PointOnSurface(way),4326), name FROM planet_osm_polygon WHERE tourism='camp_site' AND way && $ENV
  ORDER BY 2 NULLS LAST LIMIT 1) t;
SELECT 'Tor_verschlossen', ST_Y(p), ST_X(p), 15 FROM (SELECT ST_Transform(way,4326) p FROM planet_osm_point
  WHERE barrier IN ('gate','lift_gate','swing_gate','chain','bar') AND locked='yes' AND way && $ENV ORDER BY osm_id LIMIT 1) t;
SELECT 'Pisten', ST_Y(p), ST_X(p), 14 FROM (
  SELECT ST_Transform(c,4326) p, (SELECT count(DISTINCT COALESCE(o.tracktype,'?')) FROM planet_osm_line o
    WHERE o.highway='track' AND ST_DWithin(o.way, c, 1500)) n
  FROM (SELECT ST_Centroid(way) c FROM planet_osm_line WHERE highway='track' AND way && $ENV ORDER BY random() LIMIT 400) cand
  ORDER BY n DESC LIMIT 1) t;
SELECT 'Pisten_4WD', ST_Y(p), ST_X(p), 15 FROM (SELECT ST_Transform(ST_LineInterpolatePoint(way,0.5),4326) p FROM planet_osm_line
  WHERE highway='track' AND ("4wd_only"='yes' OR smoothness IN ('very_bad','horrible','very_horrible','impassable'))
  AND way && $ENV AND GeometryType(way)='LINESTRING' ORDER BY ST_Length(way) DESC LIMIT 1) t;
SELECT 'Pfad_unsichtbar', ST_Y(p), ST_X(p), 16 FROM (SELECT ST_Transform(ST_LineInterpolatePoint(way,0.5),4326) p FROM planet_osm_line
  WHERE highway IN ('path','footway','track') AND trail_visibility IN ('no','horrible') AND way && $ENV AND GeometryType(way)='LINESTRING'
  ORDER BY ST_Length(way) DESC LIMIT 1) t;
SELECT 'Weg_privat', ST_Y(p), ST_X(p), 16 FROM (SELECT ST_Transform(ST_LineInterpolatePoint(way,0.5),4326) p FROM planet_osm_line
  WHERE highway IN ('path','footway','track') AND access IN ('no','private') AND (foot IS NULL OR foot IN ('no','private')) AND way && $ENV
  AND GeometryType(way)='LINESTRING' ORDER BY ST_Length(way) DESC LIMIT 1) t;
SQL
  psql -d gis -At -c "SELECT 'tracks by tracktype: ' || string_agg(t || '=' || n, ', ') FROM (SELECT COALESCE(tracktype,'unknown') t, count(*) n FROM planet_osm_line WHERE highway='track' GROUP BY 1 ORDER BY 1) x;"
  psql -d gis -At -c "SELECT 'camp sites: ' || (SELECT count(*) FROM planet_osm_point WHERE tourism='camp_site') + (SELECT count(*) FROM planet_osm_polygon WHERE tourism='camp_site') || ', locked gates: ' || (SELECT count(*) FROM planet_osm_point WHERE locked='yes') || ', 4wd tracks: ' || (SELECT count(*) FROM planet_osm_line WHERE highway='track' AND (\"4wd_only\"='yes' OR smoothness IN ('very_bad','horrible','very_horrible','impassable')));"
  psql -d gis -At -c "SELECT 'viewpoints: ' || count(*) || ', with direction: ' || count(direction) FROM planet_osm_point WHERE tourism='viewpoint';"
  psql -d gis -At -c "SELECT 'paths not visible: ' || count(*) FILTER (WHERE trail_visibility IN ('no','horrible')) || ', no access: ' || count(*) FILTER (WHERE access IN ('no','private') OR foot IN ('no','private')) FROM planet_osm_line WHERE highway IN ('path','footway','bridleway','track');"
  cat "$PLACES"
  python3 "$HERE/render_preview.py" --style opentopomap.xml --outdir "$OUT/preview" \
    --places "$PLACES" --fonts "${PREVIEW_FONTS:-dejavu,noto,roboto,opensans}" --only "${PREVIEW_PLACES:-}" --scale "$SCALE"
else
  step "Rendering tiles"
  [ -n "${FONT:-}" ] && python3 "$HERE/render_preview.py" --style opentopomap.xml --set-font "$FONT"
  PART=""; [ "${SHARDS:-1}" -gt 1 ] && PART=".part$SHARD"
  # leave out open sea above zoom 11 (TASVEG covers all land of Tasmania)
  LAND_ARGS=""; [ "${TASVEG:-0}" = 1 ] && LAND_ARGS="--land-table tasveg --land-zoom 11"
  # wilderness (far from roads and from paths of a day walk) only up to zoom WILD_MAXZOOM
  if [ -n "${WILD_MAXZOOM:-}" ] && [ "$WILD_MAXZOOM" -lt "$MAXZOOM" ]; then
    sub "Wilderness only up to zoom $WILD_MAXZOOM"
    WILD_ROAD_KM="${WILD_ROAD_KM:-2}" WILD_HIKE_KM="${WILD_HIKE_KM:-10}" WILD_TRAIL_KM="${WILD_TRAIL_KM:-1}" \
      bash "$HERE/wild.sh" "$(echo "($S + $N) / 2" | bc -l)"
    LAND_ARGS="$LAND_ARGS --detail-table wild_detail --detail-zoom $WILD_MAXZOOM"
  fi
  # GitHub jobs end after 6 hours: stop in time and keep the tiles (JOB_START is set by the workflow)
  BASE_DEADLINE=""; OV_DEADLINE=""
  if [ -n "${JOB_START:-}" ]; then
    BASE_DEADLINE="--deadline $((JOB_START + 285 * 60))"; OV_DEADLINE="--deadline $((JOB_START + 330 * 60))"
  fi
  # FILL_SKIP/FILL_LIMIT (fill.yml): only the tiles a run stopped by the time limit left out, no overlays
  FILL_ARGS=""
  if [ -n "${FILL_SKIP:-}" ]; then
    FILL_ARGS="--skip $FILL_SKIP --limit ${FILL_LIMIT:-0}"; PART=".fill$SHARD-$FILL_SKIP"; OVERLAYS=""
  fi
  if [ -n "${ONLY_OVERLAYS:-}" ]; then
    echo "ONLY_OVERLAYS=$ONLY_OVERLAYS: base map not rendered"
  else
  python3 "$HERE/render_tiles.py" --shard "${SHARD:-0}" --shards "${SHARDS:-1}" \
    --style opentopomap.xml --out "$OUT/$NAME-otm$PART.mbtiles" \
    --bbox "$BBOX" --minzoom "$MINZOOM" --maxzoom "$MAXZOOM" --scale "$SCALE" --magnify "${MAGNIFY:-1}" \
    --name "$TITLE" --processes "$(nproc)" --format "${TILE_FORMAT:-png8:m=h}" \
    --high-zoom-quality "${HIGH_ZOOM_QUALITY:-0}" --high-zoom "${HIGH_ZOOM:-16}" $LAND_ARGS $BASE_DEADLINE $FILL_ARGS
  fi
fi

  # transparent overlays (MBTiles type=overlay), selectable in the app's map menu
  [ "${MODE:-tiles}" = tiles ] && for ov in ${OVERLAYS:-}; do
    step "Rendering overlay $ov"
    # own highest zoom per overlay (OVERLAY_MAXZOOM_<name>); the app enlarges the last level beyond it
    omz_var="OVERLAY_MAXZOOM_$ov"; omz=${!omz_var:-$MAXZOOM}; [ "$omz" -gt "$MAXZOOM" ] && omz=$MAXZOOM
    python3 "$HERE/render_tiles.py" --shard "${SHARD:-0}" --shards "${SHARDS:-1}" --overlay \
      --style "overlay-$ov.xml" --out "$OUT/$NAME-$ov$PART.mbtiles" \
      --bbox "$BBOX" --minzoom "$MINZOOM" --maxzoom "$omz" --scale "$SCALE" --magnify "${MAGNIFY:-1}" \
      --name "$TITLE – $ov" --processes "$(nproc)" --format "${TILE_FORMAT:-png8:m=h}" $LAND_ARGS $OV_DEADLINE
  done
# example spots (TASVEG) for the check images of the workflow
[ -f "$WORK/tasveg/sample_points.txt" ] && cp "$WORK/tasveg/sample_points.txt" "$OUT/samples-$NAME-otm.txt"
ls -laR "$OUT"
step "Done"
