#!/usr/bin/env python3
"""Preview images of the map style instead of a full tile set, and font switching.

  --places FILE --fonts a,b,c   render each place with each font variant and put the
                                variants side by side in one image per place
  --set-font VARIANT            only rewrite the style's fontset (used before tile rendering)

Fonts are chosen from the faces that are actually installed; the choices are printed.
"""
import argparse
import math
import os
import sys

import mapnik

R = 6378137.0

# role -> candidate face names per variant (first installed one wins)
VARIANTS = {
    "dejavu": {
        "label": "DejaVu Sans (bisher)",
        "serif-book": ["DejaVu Serif Book"],
        "sans-book": ["DejaVu Sans Book"],
        "sans-oblique": ["DejaVu Sans Oblique"],
        "sans-condensed": ["DejaVu Sans Condensed"],
        "sans-condensed-oblique": ["DejaVu Sans Condensed Oblique"],
        "sans-condensed-bold": ["DejaVu Sans Condensed Bold"],
        "sans-extralight": ["DejaVu Sans ExtraLight"],
    },
    "noto": {
        "label": "Noto Sans",
        "serif-book": ["Noto Serif Regular"],
        "sans-book": ["Noto Sans Regular"],
        "sans-oblique": ["Noto Sans Italic"],
        "sans-condensed": ["Noto Sans SemiCondensed Regular", "Noto Sans SemiCondensed", "Noto Sans Condensed Regular", "Noto Sans Condensed", "Noto Sans Regular"],
        "sans-condensed-oblique": ["Noto Sans SemiCondensed Italic", "Noto Sans Condensed Italic", "Noto Sans Italic"],
        "sans-condensed-bold": ["Noto Sans SemiCondensed Bold", "Noto Sans Condensed Bold", "Noto Sans Bold"],
        "sans-extralight": ["Noto Sans Light", "Noto Sans ExtraLight", "Noto Sans Regular"],
    },
    "roboto": {
        "label": "Roboto",
        "serif-book": ["Noto Serif Regular", "DejaVu Serif Book"],
        "sans-book": ["Roboto Regular"],
        "sans-oblique": ["Roboto Italic"],
        "sans-condensed": ["Roboto Condensed Regular", "Roboto Condensed"],
        "sans-condensed-oblique": ["Roboto Condensed Italic"],
        "sans-condensed-bold": ["Roboto Condensed Bold"],
        "sans-extralight": ["Roboto Light"],
    },
    "sourcesans": {
        "label": "Source Sans 3",
        "serif-book": ["Noto Serif Regular", "DejaVu Serif Book"],
        "sans-book": ["Source Sans 3 Regular", "Source Sans 3"],
        "sans-oblique": ["Source Sans 3 Italic"],
        "sans-condensed": ["Source Sans 3 Regular", "Source Sans 3"],
        "sans-condensed-oblique": ["Source Sans 3 Italic"],
        "sans-condensed-bold": ["Source Sans 3 Semibold", "Source Sans 3 SemiBold", "Source Sans 3 Bold"],
        "sans-extralight": ["Source Sans 3 Light", "Source Sans 3 ExtraLight"],
    },
    "opensans": {
        "label": "Open Sans",
        "serif-book": ["Noto Serif Regular", "DejaVu Serif Book"],
        "sans-book": ["Open Sans Regular"],
        "sans-oblique": ["Open Sans Italic"],
        "sans-condensed": ["Open Sans Regular"],
        "sans-condensed-oblique": ["Open Sans Italic"],
        "sans-condensed-bold": ["Open Sans Semibold", "Open Sans SemiBold", "Open Sans Bold"],
        "sans-extralight": ["Open Sans Light"],
    },
    # 90s/2000s screen fonts (Microsoft core fonts, Tahoma from Wine)
    "verdana": {
        "label": "Verdana",
        "serif-book": ["Georgia Regular", "DejaVu Serif Book"],
        "sans-book": ["Verdana Regular", "Verdana"],
        "sans-oblique": ["Verdana Italic"],
        "sans-condensed": ["Verdana Regular", "Verdana"],
        "sans-condensed-oblique": ["Verdana Italic"],
        "sans-condensed-bold": ["Verdana Bold"],
        "sans-extralight": ["Verdana Regular", "Verdana"],
    },
    "trebuchet": {
        "label": "Trebuchet MS",
        "serif-book": ["Georgia Regular", "DejaVu Serif Book"],
        "sans-book": ["Trebuchet MS Regular", "Trebuchet MS"],
        "sans-oblique": ["Trebuchet MS Italic"],
        "sans-condensed": ["Trebuchet MS Regular", "Trebuchet MS"],
        "sans-condensed-oblique": ["Trebuchet MS Italic"],
        "sans-condensed-bold": ["Trebuchet MS Bold"],
        "sans-extralight": ["Trebuchet MS Regular", "Trebuchet MS"],
    },
    "tahoma": {
        "label": "Tahoma",
        "serif-book": ["Georgia Regular", "DejaVu Serif Book"],
        "sans-book": ["Tahoma Regular", "Tahoma"],
        "sans-oblique": ["Tahoma Regular", "Tahoma"],
        "sans-condensed": ["Tahoma Regular", "Tahoma"],
        "sans-condensed-oblique": ["Tahoma Regular", "Tahoma"],
        "sans-condensed-bold": ["Tahoma Bold"],
        "sans-extralight": ["Tahoma Regular", "Tahoma"],
    },
    # Helvetica-like (URW Nimbus Sans, with the narrow cut for condensed labels)
    "helvetica": {
        "label": "Nimbus Sans (Helvetica)",
        "serif-book": ["C059 Roman", "Nimbus Roman Regular", "DejaVu Serif Book"],
        "sans-book": ["Nimbus Sans Regular", "TeX Gyre Heros Regular"],
        "sans-oblique": ["Nimbus Sans Italic", "Nimbus Sans Oblique", "TeX Gyre Heros Italic"],
        "sans-condensed": ["Nimbus Sans Narrow Regular", "TeX Gyre Heros Cn Regular", "Nimbus Sans Regular"],
        "sans-condensed-oblique": ["Nimbus Sans Narrow Oblique", "Nimbus Sans Narrow Italic", "TeX Gyre Heros Cn Italic"],
        "sans-condensed-bold": ["Nimbus Sans Narrow Bold", "TeX Gyre Heros Cn Bold", "Nimbus Sans Bold"],
        "sans-extralight": ["Nimbus Sans Regular", "TeX Gyre Heros Regular"],
    },
    # geometric sans in the spirit of Futura (URW Gothic = Avant Garde Gothic clone)
    "geometric": {
        "label": "URW Gothic (geometrisch, à la Futura)",
        "serif-book": ["C059 Roman", "DejaVu Serif Book"],
        "sans-book": ["URW Gothic Book", "TeX Gyre Adventor Regular"],
        "sans-oblique": ["URW Gothic Book Oblique", "URW Gothic Book Italic", "TeX Gyre Adventor Italic"],
        "sans-condensed": ["URW Gothic Book", "TeX Gyre Adventor Regular"],
        "sans-condensed-oblique": ["URW Gothic Book Oblique", "URW Gothic Book Italic", "TeX Gyre Adventor Italic"],
        "sans-condensed-bold": ["URW Gothic Demi", "TeX Gyre Adventor Bold"],
        "sans-extralight": ["URW Gothic Book", "TeX Gyre Adventor Regular"],
    },
    # serif lettering like older Swiss national maps: upright serif for places, italic for nature names
    "swiss": {
        "label": "Serifen (alte Landeskarte)",
        "serif-book": ["C059 Roman", "TeX Gyre Schola Regular"],
        "sans-book": ["C059 Roman", "TeX Gyre Schola Regular"],
        "sans-oblique": ["C059 Italic", "TeX Gyre Schola Italic"],
        "sans-condensed": ["Nimbus Roman Regular", "TeX Gyre Termes Regular"],
        "sans-condensed-oblique": ["Nimbus Roman Italic", "TeX Gyre Termes Italic"],
        "sans-condensed-bold": ["Nimbus Roman Bold", "TeX Gyre Termes Bold"],
        "sans-extralight": ["Nimbus Roman Regular", "TeX Gyre Termes Regular"],
    },
}
FALLBACK = {"serif-book": "DejaVu Serif Book"}


def register_fonts():
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/wine/fonts"):
        for dirpath, _, files in os.walk(root, followlinks=True):
            for f in files:
                if f.lower().endswith((".ttf", ".otf", ".ttc")):
                    mapnik.FontEngine.register_font(os.path.join(dirpath, f))
    return set(mapnik.FontEngine.face_names())


def write_fontset(style_path, variant, faces):
    spec = VARIANTS[variant]
    out = []
    for role in ("serif-book", "sans-book", "sans-condensed", "sans-condensed-oblique",
                 "sans-extralight", "sans-condensed-bold", "sans-oblique"):
        chosen = next((f for f in spec[role] if f in faces), None)
        fallback = FALLBACK.get(role, "DejaVu Sans Book")
        print(f"  {variant:10s} {role:24s} -> {chosen or 'MISSING, using ' + fallback}")
        out.append(f'<FontSet name="{role}">')
        if chosen:
            out.append(f'  <Font face-name="{chosen}" />')
        out.append(f'  <Font face-name="{fallback}" />')
        out.append("  <Font face-name=\"Noto Sans Regular\" />")
        out.append("</FontSet>")
    inc = os.path.join(os.path.dirname(os.path.abspath(style_path)), "inc", "fontset-settings.xml.inc")
    with open(inc, "w") as f:
        f.write("\n".join(out) + "\n")


def merc(lon, lat):
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def render_view(style, lon, lat, zoom, width, height, scale):
    m = mapnik.Map(width * scale, height * scale)
    mapnik.load_map(m, style)
    res = 2 * math.pi * R / (256 * 2 ** zoom)  # metres per (unscaled) pixel
    x, y = merc(lon, lat)
    hw, hh = width * res / 2, height * res / 2
    m.zoom_to_box(mapnik.Box2d(x - hw, y - hh, x + hw, y + hh))
    im = mapnik.Image(m.width, m.height)
    mapnik.render(m, im, float(scale))
    return im


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--style", required=True)
    p.add_argument("--set-font")
    p.add_argument("--places")
    p.add_argument("--fonts", default="dejavu")
    p.add_argument("--outdir", default="preview")
    p.add_argument("--only", default="", help="comma separated place names (default: all)")
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--width", type=int, default=400)   # in map (unscaled) pixels
    p.add_argument("--height", type=int, default=400)
    a = p.parse_args()

    faces = register_fonts()
    print(f"{len(faces)} font faces; sans families:",
          sorted({f.rsplit(' ', 1)[0] for f in faces if any(k in f for k in ('Sans', 'Roboto', 'Verdana', 'Trebuchet', 'Tahoma', 'Georgia', 'Nimbus', 'URW', 'C059', 'Gyre'))}))

    if a.set_font:
        write_fontset(a.style, a.set_font, faces)
        return

    from PIL import Image, ImageDraw, ImageFont
    os.makedirs(a.outdir, exist_ok=True)
    places = []
    for line in open(a.places):
        line = line.strip()
        if line and not line.startswith("#"):
            name, lat, lon, zoom = [v.strip() for v in line.split(";")]
            if not a.only or name in a.only.split(","):
                places.append((name, float(lat), float(lon), int(zoom)))

    # skip variants whose main font is not installed (they would only show the fallback font)
    fonts = [f for f in a.fonts.split(",") if f in VARIANTS and any(c in faces for c in VARIANTS[f]["sans-book"])]
    print("font variants:", fonts)
    images = {}  # (place, font) -> PIL image
    for font in fonts:
        write_fontset(a.style, font, faces)
        for name, lat, lon, zoom in places:
            im = render_view(a.style, lon, lat, zoom, a.width, a.height, a.scale)
            path = os.path.join(a.outdir, f"{name}_{font}.png")
            im.save(path, "png")
            images[(name, font)] = Image.open(path).convert("RGB")
            print(f"  rendered {path}", flush=True)

    if len(fonts) == 1:
        # one font: a single overview sheet with all places
        font = fonts[0]
        label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11 * a.scale)
        w, h = a.width * a.scale, a.height * a.scale
        cap = 24 * a.scale
        cols = 2 if len(places) > 1 else 1
        rows = math.ceil(len(places) / cols)
        sheet = Image.new("RGB", (cols * w + (cols - 1) * 8, rows * (h + cap) + (rows - 1) * 8), "white")
        d = ImageDraw.Draw(sheet)
        for i, (name, lat, lon, zoom) in enumerate(places):
            x0 = (i % cols) * (w + 8)
            y0 = (i // cols) * (h + cap + 8)
            d.text((x0 + 10, y0 + 4), f"{name.replace('_', ' ')}  ·  Zoom {zoom}  ·  {lat:.4f}, {lon:.4f}", fill="black", font=label_font)
            sheet.paste(images[(name, font)], (x0, y0 + cap))
        out = os.path.join(a.outdir, "overview.jpg")
        sheet.save(out, "jpeg", quality=88)
        print(f"overview {out}")
        return

    # one comparison image per place: font variants side by side with a caption
    label_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11 * a.scale)
    w, h = a.width * a.scale, a.height * a.scale
    cap = 30 * a.scale
    cols = 2 if len(fonts) > 2 else len(fonts)
    rows = math.ceil(len(fonts) / cols)
    for name, lat, lon, zoom in places:
        sheet = Image.new("RGB", (cols * w + (cols - 1) * 8, rows * (h + cap) + (rows - 1) * 8), "white")
        d = ImageDraw.Draw(sheet)
        for i, font in enumerate(fonts):
            x0 = (i % cols) * (w + 8)
            y0 = (i // cols) * (h + cap + 8)
            d.text((x0 + 10, y0 + 4), f"{chr(65 + i)}: {VARIANTS[font]['label']}  ·  {name}, Zoom {zoom}", fill="black", font=label_font)
            sheet.paste(images[(name, font)], (x0, y0 + cap))
        out = os.path.join(a.outdir, f"compare_{name}.jpg")
        sheet.save(out, "jpeg", quality=88)
        print(f"comparison {out}")


if __name__ == "__main__":
    main()
