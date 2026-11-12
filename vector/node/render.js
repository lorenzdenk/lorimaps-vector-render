#!/usr/bin/env node
// Draws the vector map with MapLibre Native (the renderer of the app) into a PNG, for checking the style.
// Usage: render.js <style dir> <out.png> <lon> <lat> <zoom> [width=800] [height=800] [ratio=2] [name=file.mbtiles ...]
// The style's lorimaps:// addresses are answered from the style directory (sprite, glyphs) and from the
// MBTiles files given as name=path ("map" is the vector source, the others are raster sources).
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { DatabaseSync } = require('node:sqlite');
const mbgl = require('@maplibre/maplibre-gl-native');
const sharp = require('sharp');

const [styleDir, out, lon, lat, zoom, width = 800, height = 800, ratio = 2, ...sources] = process.argv.slice(2);
const dbs = {};
for (const s of sources) {
  const [name, file] = s.split('=');
  dbs[name] = new DatabaseSync(file, { readOnly: true }).prepare(
    'SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?');
}
let missing = new Set();

function request(req, callback) {
  const url = req.url.replace(/^lorimaps:\/\//, '');
  try {
    let m;
    if ((m = url.match(/^sprite(@2x)?\.(png|json)$/))) {
      return callback(null, { data: fs.readFileSync(path.join(styleDir, url)) });
    }
    if ((m = url.match(/^glyphs\/(.+)\/(\d+-\d+)$/))) {
      const stack = decodeURIComponent(m[1]).split(',')[0];
      const file = path.join(styleDir, 'glyphs', stack, m[2] + '.pbf');
      if (fs.existsSync(file)) return callback(null, { data: fs.readFileSync(file) });
      missing.add('glyphs ' + stack + ' ' + m[2]);
      return callback(null, {});
    }
    if ((m = url.match(/^([^/]+)\/(\d+)\/(\d+)\/(\d+)$/))) {
      const [, name, z, x, y] = m;
      const st = dbs[name];
      if (!st) { missing.add('source ' + name); return callback(null, {}); }
      const row = st.get(+z, +x, (1 << +z) - 1 - +y);
      if (!row) return callback(null, {});
      let data = Buffer.from(row.tile_data);
      if (data[0] === 0x1f && data[1] === 0x8b) data = zlib.gunzipSync(data);
      return callback(null, { data });
    }
    missing.add(url);
    callback(null, {});
  } catch (e) {
    callback(e);
  }
}

const map = new mbgl.Map({ request, ratio: +ratio });
map.load(JSON.parse(fs.readFileSync(path.join(styleDir, 'style.json'), 'utf8')));
map.render({ zoom: +zoom, center: [+lon, +lat], width: +width, height: +height }, (err, buffer) => {
  if (err) { console.error('render error:', err.message || err); process.exit(1); }
  sharp(buffer, { raw: { width: Math.round(width * ratio), height: Math.round(height * ratio), channels: 4 } })
    .png().toFile(out).then(() => {
      if (missing.size) console.log('not found:', [...missing].slice(0, 10).join('; '));
      console.log('written', out);
      map.release();
    });
});
