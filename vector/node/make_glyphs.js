#!/usr/bin/env node
// Writes the glyph files (signed distance fields, one .pbf per 256 characters) MapLibre needs for labels.
// Usage: make_glyphs.js <out dir> "<font stack name>=<font file>[,<fallback font file>...]" ...
// The first font decides the look; characters it does not have are taken from the fallback fonts.
const fs = require('fs');
const path = require('path');
const fontnik = require('fontnik');

const range = (font, start) => new Promise((resolve, reject) =>
  fontnik.range({ font, start, end: start + 255 }, (err, data) => err ? reject(err) : resolve(data)));

async function main() {
  const [outDir, ...specs] = process.argv.slice(2);
  for (const spec of specs) {
    const eq = spec.indexOf('=');
    const name = spec.slice(0, eq);
    const fonts = spec.slice(eq + 1).split(',').map(f => fs.readFileSync(f));
    const dir = path.join(outDir, name);
    fs.mkdirSync(dir, { recursive: true });
    let files = 0, bytes = 0;
    for (let start = 0; start < 65536; start += 256) {
      const parts = [];
      for (const font of fonts) {
        const pbf = await range(font, start);
        if (pbf.length > 60) parts.push(pbf);          // an empty range is only the header with the font name
      }
      if (!parts.length) continue;
      const data = parts.length > 1
        ? await new Promise((resolve, reject) => fontnik.composite(parts, (err, d) => err ? reject(err) : resolve(d)))
        : parts[0];
      fs.writeFileSync(path.join(dir, `${start}-${start + 255}.pbf`), data);
      files++; bytes += data.length;
    }
    console.log(`${name}: ${files} ranges, ${(bytes / 1e6).toFixed(1)} MB`);
  }
}
main().catch(e => { console.error(e); process.exit(1); });
