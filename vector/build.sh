#!/usr/bin/env bash
# Builds the vector map from the prepared database and the customised Mapnik style.
# Called by render/render.sh (MODE=vector) in the directory of the style (OpenTopoMap/mapnik).
# Needs: WORK, OUT, NAME, TITLE, BBOX; optional: VECTOR_MAXZOOM, HILLSHADE_MAXZOOM, VECTOR_SCALE, VECTOR_FONT
set -euo pipefail
V="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAPNIK_DIR="$PWD"
VW="$WORK/vector"; mkdir -p "$VW" "$OUT"
PY="${PYTHON:-python3}"
VMAX="${VECTOR_MAXZOOM:-14}"
HMAX="${HILLSHADE_MAXZOOM:-12}"
SCALE="${VECTOR_SCALE:-1.28}"
FONT="${VECTOR_FONT:-firasans}"
sub() { echo "----- $(date +%H:%M:%S) $*"; }

sub "Mapnik style with everything resolved"
$PY - <<PYEOF 2>&1 | grep -v "SVG" || true
import mapnik
m = mapnik.Map(512, 512)
mapnik.load_map(m, "opentopomap.xml")
mapnik.save_map(m, "$VW/resolved.xml")
print("layers:", len(m.layers))
PYEOF

sub "Layers of the vector tiles"
$PY "$V/layers.py" "$VW/resolved.xml" "$VW/plan.json" "$MAPNIK_DIR" > "$VW/layers.txt"
tail -3 "$VW/layers.txt"

sub "Style, sprite and fonts ($FONT)"
$PY "$V/make_style.py" --style "$VW/resolved.xml" --plan "$VW/plan.json" --symbols "$MAPNIK_DIR" --scale "$SCALE" \
  --maxzoom "$VMAX" --raster-maxzoom "$HMAX" --font "$FONT" --name "$TITLE" --out "$VW/style"
mkdir -p "$VW/fonts"
$PY - "$V/fonts.json" "$FONT" "$VW/fonts" "$VW/style/fonts.json" > "$VW/glyph-args.txt" <<'PYEOF'
import json, os, sys, urllib.request
conf = json.load(open(sys.argv[1]))[sys.argv[2]]
used = set(json.load(open(sys.argv[4])))
for file in sorted(set(conf["sets"].values()) | {conf["default"]}):
    stack = os.path.splitext(os.path.basename(file))[0]
    if stack not in used:
        continue
    path = file
    if not os.path.isabs(file):
        path = os.path.join(sys.argv[3], file)
        if not os.path.exists(path):
            urllib.request.urlretrieve(conf["download"] + file, path)
    print(f"{stack}={path},{conf['fallback']}")
PYEOF
cat "$VW/glyph-args.txt"
( cd "$V/node" && [ -d node_modules ] || npm install --no-audit --no-fund >/dev/null )
mapfile -t GLYPHS < "$VW/glyph-args.txt"
node "$V/node/make_glyphs.js" "$VW/style/glyphs" "${GLYPHS[@]}"

sub "Vector tiles, zoom 0-$VMAX"
$PY "$V/export_tiles.py" --style "$VW/resolved.xml" --plan "$VW/plan.json" --bbox "$BBOX" --minzoom 0 --maxzoom "$VMAX" \
  --name "$TITLE" --processes "$(nproc)" --out "$OUT/$NAME-vector.mbtiles"

sub "Relief colours and hillshade (raster tiles)"
SHIFT=$($PY -c "import json; r = json.load(open('$VW/style/raster.json')); o = r['hillshade_opacity']; print(round(o.get('5', o.get('8', 0.9)) * r['flat'] - 0.5, 4) if o else 0)")
$PY "$V/raster_tiles.py" --style "$VW/resolved.xml" --base "$MAPNIK_DIR" --group relief --bbox "$BBOX" --maxzoom 7 \
  --flat-shift "$SHIFT" --out "$OUT/$NAME-relief.mbtiles" 2>&1 | grep -v "SVG"
$PY "$V/raster_tiles.py" --style "$VW/resolved.xml" --base "$MAPNIK_DIR" --group hillshade --bbox "$BBOX" --maxzoom "$HMAX" \
  --out "$OUT/$NAME-hillshade.mbtiles" 2>&1 | grep -v "SVG"

sub "Style package"
( cd "$VW/style" && rm -f "$OUT/$NAME-style.zip" && zip -q -r "$OUT/$NAME-style.zip" style.json sprite.png sprite.json sprite@2x.png sprite@2x.json glyphs )
ls -la "$OUT"

# check pictures of the vector map, drawn with MapLibre Native (the renderer of the app)
if [ -n "${VECTOR_PREVIEW:-}" ] && command -v xvfb-run >/dev/null; then
  sub "Check pictures"
  mkdir -p "$OUT/preview"
  while IFS=, read -r pname plat plon plevel; do
    [ -z "$pname" ] && continue
    zoom=$($PY -c "import math; print($plevel + math.log2(164 / 512))")
    xvfb-run -a node "$V/node/render.js" "$VW/style" "$OUT/preview/$pname-$plevel.png" "$plon" "$plat" "$zoom" 450 450 2 \
      "map=$OUT/$NAME-vector.mbtiles" "hillshade=$OUT/$NAME-hillshade.mbtiles" "relief=$OUT/$NAME-relief.mbtiles" 2>&1 \
      | grep -v "Experimental\|trace-warnings" || true
  done <<< "$(echo "$VECTOR_PREVIEW" | tr ';' '\n')"
fi
