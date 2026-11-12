#!/usr/bin/env bash
# Loads one LIST layer for the map area into the database: from the release "list-data" (all of Tasmania, made
# by .github/workflows/list-prepare.yml) if there, otherwise directly from theLIST web service.
# Usage: list_data.sh <workdir> <W> <S> <E> <N> <name> <layer id> <fields> <database> <table> [ogr2ogr options]
set -euo pipefail
WORK="$1"; W="$2"; S="$3"; E="$4"; N="$5"; NAME="$6"; LAYER="$7"; FIELDS="$8"; DB="$9"; TABLE="${10}"; shift 10
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$WORK/listdata"
SRC="$WORK/listdata/list_$NAME.gpkg"
if [ ! -s "$SRC" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  if GH_TOKEN="$GITHUB_TOKEN" gh release download list-data -R "$GITHUB_REPOSITORY" -p "list_$NAME.gpkg.xz" -D "$WORK/listdata" --clobber 2>/dev/null; then
    xz -d -f "$SRC.xz"
  fi
fi
# with -sql the layer is chosen by the query, otherwise by its name
LAYER_ARG="$NAME"; case " $* " in *" -sql "*) LAYER_ARG="" ;; esac
if [ -s "$SRC" ]; then
  echo "LIST $NAME from release list-data"
  # area in Web Mercator (the data's projection; -spat_srs cannot be combined with -sql)
  read -r MX0 MY0 MX1 MY1 <<< "$(python3 -c "import math,sys
w,s,e,n=map(float,sys.argv[1:5]); R=6378137
y=lambda la: R*math.log(math.tan(math.pi/4+math.radians(la)/2))
print(R*math.radians(w), y(s), R*math.radians(e), y(n))" "$W" "$S" "$E" "$N")"
  ogr2ogr -f PostgreSQL PG:dbname="$DB" "$SRC" $LAYER_ARG -nln "$TABLE" -overwrite -t_srs EPSG:3857 \
    -spat "$MX0" "$MY0" "$MX1" "$MY1" -lco GEOMETRY_NAME=way --config PG_USE_COPY YES "$@"
else
  echo "LIST $NAME from the web service"
  python3 "$HERE/fetch_layer.py" "$LAYER" "$W" "$S" "$E" "$N" "$WORK/listdata/$NAME.geojson" "$FIELDS"
  ogr2ogr -f PostgreSQL PG:dbname="$DB" "$WORK/listdata/$NAME.geojson" -nln "$TABLE" -overwrite -t_srs EPSG:3857 \
    -lco GEOMETRY_NAME=way --config PG_USE_COPY YES "$@"
  rm -f "$WORK/listdata/$NAME.geojson"
fi
