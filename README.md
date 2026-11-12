# lorimaps-vector-render

Builds the hiking map of [lorimaps-render](https://github.com/lorenzdenk/lorimaps-render) as a **vector map** for
MapLibre (app: [lorimaps](https://github.com/lorenzdenk/lorimaps)): the same data, the same customised OpenTopoMap
style, the same look – but a fraction of the size, and sharp at every zoom.

The raster pipeline draws the map with Mapnik from a PostGIS database. This pipeline keeps that database and that
Mapnik style and **translates** them, instead of writing a second style by hand:

| Step | Script | Result |
|---|---|---|
| Data: OSM, LIST, TASVEG, contours, DEM into PostGIS; style fetched and customised | `render/render.sh` (as in lorimaps-render) | database, `opentopomap.xml` |
| The style's own layer queries, run once per tile with `ST_AsMVT` | `vector/layers.py`, `vector/export_tiles.py` | `<name>-vector.mbtiles` (zoom 0–14) |
| Mapnik rules → MapLibre layers, symbols → sprite | `vector/make_style.py` | `style.json`, `sprite*.png/json` |
| Fonts → glyph files | `vector/node/make_glyphs.js` | `glyphs/<font>/<range>.pbf` |
| Hillshade and relief colours, drawn by Mapnik | `vector/raster_tiles.py` | `<name>-hillshade.mbtiles`, `<name>-relief.mbtiles` |
| Everything together | `vector/build.sh` | the three MBTiles files and `<name>-style.zip` |

A push that changes `render/` or `vector/` starts the workflow *Build vector map*; the result is a release
`vector-N`, log and check pictures are on the branch `render-logs`.

## Using the result

`style.json` refers to its data with placeholder addresses, which the app answers from the files:

| Address in the style | Comes from |
|---|---|
| `lorimaps://map/{z}/{x}/{y}` | `<name>-vector.mbtiles` (gzip-compressed vector tiles) |
| `lorimaps://hillshade/{z}/{x}/{y}`, `lorimaps://relief/{z}/{x}/{y}` | the raster MBTiles files (WebP, 512 px) |
| `lorimaps://sprite` (+ `.png`, `.json`, `@2x`) | `sprite*` in the style package |
| `lorimaps://glyphs/{fontstack}/{range}` | `glyphs/` in the style package |

In MBTiles the rows are counted from the south (TMS): `tile_row = 2^z − 1 − y`.

**Zoom levels.** The style is made for the size the raster map has in the app (512 px tiles shown at 164 px, style
drawn twice as large: `VECTOR_SCALE=1.28`). Level *n* of the raster map is MapLibre zoom *n* − 1.64, and shows
exactly what the raster tiles of level *n* show. Between two levels the vector map simply grows; the content changes
at the half levels.

## How the translation works

* **Zoom.** Each Mapnik rule is valid for OpenTopoMap zoom levels *k* (scale denominators). A vector tile of zoom *t*
  carries the features the rules of *k* = *t* … *t*+2 need; the last tile zoom (14) carries everything above.
  Layers whose query depends on the scale (`!scale_denominator!`) are stored once per class (`roads@0`, `roads@1`).
* **Order.** Mapnik draws layer by layer, style by style, feature by feature in the order of the query. The rules of
  one style are merged into one MapLibre layer per kind (fill, line, symbol) with `case` expressions, so the order of
  the features stays that of the query.
* **Sizes** are multiplied by the scale; dash patterns, offsets, halos, text sizes and wrap widths are converted.
* **Hillshade.** Mapnik blends the hillshade with *grain merge* (`result = colour + opacity · grey − 0.5`), which
  MapLibre does not have. So the colours below the hillshade are shifted by the value flat ground gets, and the
  hillshade tiles hold, pixel by pixel, the colour and transparency that turn the vector colours into Mapnik's result.
* **Labels** are placed by MapLibre, not by Mapnik: the same texts, fonts, sizes and priorities, but not letter for
  letter at the same spot.

Not translated (yet): markers at the first/last point of a line (bridge ends), alternative label positions
(`<Placement>`).

## Fonts

The raster map is drawn with Trebuchet MS. Glyph files are the font itself in another format, and the licence of the
Microsoft core fonts does not allow passing them on in changed form. The vector map therefore uses **Fira Sans**
(SIL Open Font License), which is close in shape and width; `vector/fonts.json` maps the font sets of the style to
font files (`VECTOR_FONT` in `render/region.env`).

## Settings (`render/region.env`)

| Setting | Meaning |
|---|---|
| `REGIONS` | region files in `render/regions/` |
| `MODE` | `vector` (default), or `tiles`/`preview`/`profile` as in lorimaps-render |
| `VECTOR_MAXZOOM` | last zoom of the vector tiles (14) |
| `HILLSHADE_MAXZOOM` | last zoom of the hillshade tiles (12; stretched beyond) |
| `VECTOR_SCALE` | size of lines and text relative to OpenTopoMap on 256 px tiles (1.28) |
| `VECTOR_FONT` | font variant from `vector/fonts.json` |
| `VECTOR_PREVIEW` (region file) | check pictures: `name,lat,lon,level;…` |

## Testing locally

```
REGION=test-hobart bash render/render.sh          # small area around Hobart, everything
cd vector/node && npm install
xvfb-run -a node render.js <style dir> out.png 147.325 -42.885 13.36 450 450 2 \
    map=x-vector.mbtiles hillshade=x-hillshade.mbtiles relief=x-relief.mbtiles
python3 vector/compare.py <raster.mbtiles> <style dir> out.png <lon> <lat> <level> 400 map=… hillshade=… relief=…
```

`compare.py` puts the raster map and the vector map side by side and prints the mean colour difference;
`tile_stats.py` and `tile_dump.py` show what makes a vector tile large.
