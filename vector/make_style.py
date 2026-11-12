#!/usr/bin/env python3
"""Translates a Mapnik style into a MapLibre style (JSON) plus the sprite with its symbols and patterns.

How Mapnik's model is mapped:
  * A Mapnik rule applies at the zoom levels k given by its scale denominators. The MapLibre style shows
    the rules of level k around the camera zoom  Z(k) = k - 1 + log2(SCALE), from half a level below to
    half a level above, and draws everything SCALE times as large as Mapnik on normal 256 px tiles.
    (SCALE 1.28 is the size of the raster map in the app: 512 px tiles drawn four times as large as
    OpenTopoMap, shown at 164 dp per 256 px.)
  * Mapnik draws a style feature by feature; MapLibre draws layer by layer. Rules of one style that draw
    with the same kind of symbolizer become ONE MapLibre layer per zoom level range, with "case"
    expressions for colour, width etc., so the features keep the order of the SQL query (ORDER BY).
    Zoom dependent filters only take effect at whole zoom levels in MapLibre, therefore a separate layer
    per range of k (with fractional minzoom / maxzoom) instead of zoom expressions.
  * Labels and symbols: Mapnik places what comes first in the style first; MapLibre gives priority to the
    last layer. The symbol layers are therefore put on top in reverse order.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

from layers import GEOM_DRAW, placement, source_layer
from mapnik_model import MapnikMap, parse_expr, to_maplibre, to_maplibre_value

K_TOP = 17          # the last zoom level OpenTopoMap has rules for; shown at all higher zooms too
SVG_RATIO = 4       # SVG symbols are rendered this many times their size
OPEN_END = 24


# ---------------------------------------------------------------------------- helpers

def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def r3(x):
    x = round(float(x), 3)
    return int(x) if x == int(x) else x


def color(v, opacity=None):
    v = v.strip()
    if opacity is not None and opacity < 1:
        m = re.match(r"rgba?\(([^)]+)\)", v)
        if m:
            p = [x.strip() for x in m.group(1).split(",")]
            a = (float(p[3]) if len(p) > 3 else 1.0) * opacity
            return f"rgba({p[0]},{p[1]},{p[2]},{r3(a)})"
    return v.replace(" ", "")


def parse_transform(t):
    """Mapnik/SVG transform -> dict(scale=expr, rotate=expr, translate=(x, y)); expressions as MapLibre values"""
    out = {}
    for name, arg in re.findall(r"(\w+)\(([^()]*(?:\([^()]*\)[^()]*)*)\)", t or ""):
        if name == "translate":
            parts = [float(x) for x in re.split(r"[ ,]+", arg.strip()) if x]
            out["translate"] = (parts[0], parts[1] if len(parts) > 1 else 0.0)
        elif name in ("scale", "rotate"):
            first = arg.split(",")[0]
            out[name] = to_maplibre_value(parse_expr(first))
            if isinstance(out[name], list):
                out[name] = ["to-number", out[name], 1 if name == "scale" else 0] if out[name][0] == "get" else out[name]
    return out


class Sprite:
    def __init__(self, base_dir):
        self.base = base_dir
        self.images = {}      # name -> (path, width, height) in Mapnik pixels

    def add(self, file):
        path = file if os.path.isabs(file) else os.path.join(self.base, file)
        name = os.path.splitext(os.path.basename(path))[0]
        if name in self.images:
            if self.images[name][0] != path:
                name = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(file)[0])
            else:
                return name
        self.images[name] = (path,) + self.size(path)
        return name

    @staticmethod
    def size(path):
        if path.lower().endswith(".svg"):
            out = subprocess.run(["rsvg-convert", "--format=png", path], capture_output=True, check=True).stdout
            import io
            from PIL import Image
            return Image.open(io.BytesIO(out)).size
        from PIL import Image
        return Image.open(path).size

    def height(self, name):
        return self.images[name][2]

    def width(self, name):
        return self.images[name][1]

    def write(self, out_prefix, scale):
        """sprite.png/.json and sprite@2x.png/.json (the same picture; sizes are given by pixelRatio)"""
        import io
        from PIL import Image
        items = []
        for name, (path, w, h) in sorted(self.images.items()):
            if path.lower().endswith(".svg"):
                png = subprocess.run(["rsvg-convert", "--format=png", "--zoom", str(SVG_RATIO), path], capture_output=True, check=True).stdout
                im, ratio = Image.open(io.BytesIO(png)).convert("RGBA"), SVG_RATIO
            else:
                im, ratio = Image.open(path).convert("RGBA"), 1
            items.append((name, im, ratio))
        # simple shelf packing
        width, x, y, row_h, pos = 1024, 0, 0, 0, {}
        for name, im, ratio in sorted(items, key=lambda i: -i[1].height):
            if x + im.width + 2 > width:
                x, y, row_h = 0, y + row_h + 2, 0
            pos[name] = (x, y)
            x += im.width + 2
            row_h = max(row_h, im.height)
        sheet = Image.new("RGBA", (width, y + row_h + 2), (0, 0, 0, 0))
        index = {}
        for name, im, ratio in items:
            px, py = pos[name]
            sheet.paste(im, (px, py))
            # logical size = picture size / pixelRatio = Mapnik size * scale
            index[name] = {"x": px, "y": py, "width": im.width, "height": im.height, "pixelRatio": r3(ratio / scale)}
        for suffix in ("", "@2x"):
            sheet.save(out_prefix + suffix + ".png", optimize=True)
            json.dump(index, open(out_prefix + suffix + ".json", "w"), indent=0)
        return len(index)


# ---------------------------------------------------------------------------- symbolizers -> properties

class Conv:
    def __init__(self, m, scale, sprite):
        self.m, self.f, self.sprite = m, scale, sprite

    def kind(self, s, point_layer):
        """(MapLibre kind, static part that cannot be data driven) of a symbolizer"""
        a = s.a
        if s.type == "Line":
            w = num(a.get("stroke-width"), 1.0)
            dash = None
            if a.get("stroke-dasharray"):
                d = [num(x) for x in re.split(r"[ ,]+", a["stroke-dasharray"].strip()) if x]
                if w > 0 and any(d):
                    dash = tuple(r3(max(x, 0.01) / w) for x in d)
            return "line", (dash, a.get("stroke-linecap", "butt"), a.get("stroke-linejoin", "miter"))
        if s.type == "LinePattern":
            return "linepattern", ()
        if s.type == "Polygon":
            return "fill", ()
        if s.type == "PolygonPattern":
            return "fillpattern", ()
        if s.type in ("Point", "Markers"):
            p = a.get("placement", "point")
            if s.type == "Markers" and p == "line" and not point_layer:
                return "iconline", (r3(num(a.get("spacing"), 100.0)), a.get("allow-overlap") == "true", a.get("ignore-placement") == "true")
            if s.type == "Markers" and p in ("vertex-first", "vertex-last"):
                return None, ()   # markers at the ends of a line (bridge ends): not translated yet
            return "icon", (a.get("allow-overlap") == "true", a.get("ignore-placement") == "true")
        if s.type == "Text":
            line = a.get("placement") == "line" and not point_layer
            return ("textline" if line else "text"), (
                a.get("allow-overlap") == "true",
                r3(num(a.get("spacing"), 0.0)) if line else 0,
                r3(num(a.get("max-char-angle-delta"), 22.5)) if line else 0,
                r3(max(num(a.get("margin"), 0.0), num(a.get("minimum-distance"), 0.0) / 2)),
                r3(num(a.get("repeat-distance"), 0.0)),
            )
        if s.type == "Raster":
            return "raster", ()
        return None, ()

    # every function returns {property: value} for one symbolizer; values may be expressions
    def props(self, kind, s):
        a, f = s.a, self.f
        if kind == "line":
            w = num(a.get("stroke-width"), 1.0)
            p = {"line-color": color(a.get("stroke", "rgb(0,0,0)")), "line-width": r3(w * f)}
            if "stroke-opacity" in a:
                p["line-opacity"] = r3(num(a["stroke-opacity"], 1.0))
            if "offset" in a:
                p["line-offset"] = r3(-num(a["offset"]) * f)   # Mapnik: positive = left, MapLibre: positive = right
            return p
        if kind == "linepattern":
            name = self.sprite.add(a["file"])
            return {"line-pattern": name, "line-width": r3(self.sprite.height(name) * f)}
        if kind == "fill":
            p = {"fill-color": color(a.get("fill", "rgb(128,128,128)"))}
            if "fill-opacity" in a:
                p["fill-opacity"] = r3(num(a["fill-opacity"], 1.0))
            return p
        if kind == "fillpattern":
            p = {"fill-pattern": self.sprite.add(a["file"])}
            if "opacity" in a:
                p["fill-opacity"] = r3(num(a["opacity"], 1.0))
            return p
        if kind in ("icon", "iconline"):
            name = self.sprite.add(a["file"])
            t = parse_transform(a.get("transform"))
            p = {"icon-image": name, "icon-size": t.get("scale", 1), "icon-rotate": t.get("rotate", 0)}
            tx, ty = t.get("translate", (0.0, 0.0))
            p["icon-offset"] = ["literal", [r3(tx * f), r3(ty * f)]]
            return p
        if kind in ("text", "textline"):
            size = num(a.get("size"), 10.0)
            face = self.m.fontsets.get(a.get("fontset-name"), [a.get("face-name", "DejaVu Sans Book")])[0]
            parts = []
            for fmt, text in s.parts:
                e = to_maplibre_value(parse_expr(text))
                opts = {}
                if "size" in fmt:
                    opts["font-scale"] = r3(num(fmt["size"], size) / size)
                if "fill" in fmt:
                    opts["text-color"] = color(fmt["fill"])
                if "fontset-name" in fmt or "face-name" in fmt:
                    opts["text-font"] = ["literal", [self.m.fontsets.get(fmt.get("fontset-name"), [fmt.get("face-name", face)])[0]]]
                parts.append((e, opts))
            if len(parts) == 1 and not parts[0][1]:
                field = parts[0][0]
                if not isinstance(field, list):
                    field = str(field)
                elif field[0] == "get":
                    field = ["to-string", field]
            else:
                field = ["format"]
                for e, opts in parts:
                    field += [e if isinstance(e, list) else str(e), opts]
            opacity = num(a.get("opacity"), 1.0) * num(a.get("text-opacity"), 1.0)
            p = {
                "text-field": field,
                "text-font": ["literal", [face]],
                "text-size": r3(size * f),
                "text-color": color(a.get("fill", "rgb(0,0,0)")),
                "text-halo-color": color(a.get("halo-fill", "rgb(255,255,255)")),
                "text-halo-width": r3(num(a.get("halo-radius"), 0.0) * f),
                "text-letter-spacing": r3(num(a.get("character-spacing"), 0.0) / size),
            }
            if opacity < 1:
                p["text-opacity"] = r3(opacity)
            if a.get("text-transform") in ("uppercase", "lowercase"):
                p["text-transform"] = a["text-transform"]
            dx, dy = num(a.get("dx"), 0.0), num(a.get("dy"), 0.0)
            if kind == "text":
                # Mapnik: a displaced label lies completely on that side of the point
                v = "top" if dy > 0 else ("bottom" if dy < 0 else "")
                h = "" if a.get("horizontal-alignment") == "middle" else ("left" if dx > 0 else ("right" if dx < 0 else ""))
                p["text-anchor"] = (v + ("-" if v and h else "") + h) or "center"
                p["text-offset"] = ["literal", [r3(dx / size), r3(dy / size)]]
                wrap = num(a.get("wrap-width"), 0.0)
                p["text-max-width"] = r3(wrap / size) if wrap > 0 else 1000
                just = a.get("justify-alignment")
                p["text-justify"] = just if just in ("left", "right", "center") else ("left" if h == "left" else "right" if h == "right" else "center")
                if "line-spacing" in a:
                    p["text-line-height"] = r3(1.2 + num(a["line-spacing"]) / size)
            else:
                p["text-offset"] = ["literal", [0, r3(dy / size)]]
            return p
        if kind == "raster":
            return {"raster-opacity": r3(num(a.get("opacity"), 1.0))}
        raise ValueError(kind)

    def static_layout(self, kind, static):
        f = self.f
        if kind == "line":
            dash, cap, join = static
            lay = {"line-cap": cap, "line-join": join}
            return lay, ({"line-dasharray": list(dash)} if dash else {})
        if kind == "icon":
            return {"icon-allow-overlap": static[0], "icon-ignore-placement": static[1], "icon-padding": 0,
                    "icon-rotation-alignment": "map"}, {}
        if kind == "iconline":
            return {"symbol-placement": "line", "symbol-spacing": max(1, r3(static[0] * f)),
                    "icon-allow-overlap": static[1], "icon-ignore-placement": static[2], "icon-padding": 0}, {}
        if kind == "text":
            return {"text-allow-overlap": static[0], "text-padding": r3(static[3] * f + 1)}, {}
        if kind == "textline":
            lay = {"symbol-placement": "line", "text-allow-overlap": static[0], "text-max-angle": static[2],
                   "text-padding": r3(static[3] * f + 1),
                   # Mapnik spacing 0: one label per line
                   "symbol-spacing": max(1, r3((static[1] or static[4]) * f)) if (static[1] or static[4]) else 100000}
            return lay, {}
        return {}, {}


LAYOUT_PROPS = {"icon-image", "icon-size", "icon-rotate", "icon-offset", "text-field", "text-font", "text-size", "text-letter-spacing",
                "text-transform", "text-anchor", "text-offset", "text-max-width", "text-justify", "text-line-height"}
ML_TYPE = {"line": "line", "linepattern": "line", "fill": "fill", "fillpattern": "fill", "icon": "symbol", "iconline": "symbol",
           "text": "symbol", "textline": "symbol", "raster": "raster"}


def zoom_start(k, scale):
    return k - 1.5 + math.log2(scale)


FLAT = 180.5 / 255     # grey value of flat ground in the hillshade (see raster_tiles.py)


def hillshade_opacity(m):
    """{k: opacity of the hillshade drawn at zoom level k}"""
    out = {}
    for l in m.layers:
        if l.type == "gdal" and l.name.startswith("hillshade"):
            for _, r in l.rules():
                for s in r.symbolizers:
                    if s.a.get("comp-op") == "grain-merge":
                        for k in r.zooms():
                            out[k] = num(s.a.get("opacity"), 1.0)
    return out


def lighten(value, amount):
    """adds amount (0-1) to every colour in a paint value (plain colour or case expression)"""
    if isinstance(value, str):
        mm = re.match(r"rgba?\(([^)]+)\)", value)
        if not mm:
            return value
        p = [x.strip() for x in mm.group(1).split(",")]
        rgb = [min(255, max(0, round(float(c) + amount * 255))) for c in p[:3]]
        return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})" if len(p) < 4 else f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{p[3]})"
    if isinstance(value, list) and value and value[0] == "case":
        return ["case"] + [v if i % 2 == 0 and i < len(value) - 2 else lighten(v, amount) for i, v in enumerate(value[1:])]
    return value


def build(m, plan, scale, sprite, kmin_first=0):
    conv = Conv(m, scale, sprite)
    shade = hillshade_opacity(m)
    below_hillshade = bool(shade)
    plan_by = {p["name"]: p for p in plan["layers"]}
    geometry_layers, symbol_segments, n_rules_skipped = [], [[]], 0

    for l in m.layers:
        if l.a.get("clear-label-cache") == "true":
            symbol_segments.append([])
        if l.type == "gdal" and l.name.startswith("hillshade"):
            below_hillshade = False
        if l.type == "gdal":
            # raster layers (relief colours, hillshade) come from raster tiles; see raster_tiles.py
            # all layers of a group (relief-5000, relief-500, ... -> "relief") come from one raster source
            ks = l.zooms()
            group = l.name.split("-")[0]
            known = [g for g in geometry_layers if g.get("_raster") == group]
            if known:
                known[0]["ks"] = sorted(set(known[0]["ks"]) | set(ks))
            elif ks:
                geometry_layers.append({"_raster": group, "ks": ks})
            continue
        p = plan_by.get(l.name)
        if p is None:
            continue
        types = p["columns"]
        point_layer = p["dims"] == [0]
        for cls in p["classes"]:
            for style in l.styles:
                # ---- groups of draw operations that become one MapLibre layer per zoom range
                groups = {}       # key -> list of (rule, symbolizer)
                order = []
                for r in style.rules:
                    count = {}
                    for s in r.symbolizers:
                        kind, static = conv.kind(s, point_layer)
                        if kind is None:
                            n_rules_skipped += 1
                            continue
                        j = count.get(kind, 0)
                        count[kind] = j + 1
                        src_kind = "geom" if point_layer else placement(s)
                        key = (kind, static, j, src_kind)
                        # two rules with the same filter at the same zoom would both draw: keep them in separate layers
                        n = 0
                        while any(o.filter_text == r.filter_text and set(o.zooms()) & set(r.zooms()) for o, _ in groups.get(key + (n,), [])):
                            n += 1
                        key += (n,)
                        if key not in groups:
                            groups[key] = []
                            order.append(key)
                        groups[key].append((r, s))
                for gi, key in enumerate(order):
                    kind, static, j, src_kind, n = key
                    ops = groups[key]
                    src = source_layer(l.name, src_kind, cls, len(p["classes"]))
                    runs = []     # [k_first, k_last, definition]
                    for k in range(max(cls[0], 0), min(cls[1], K_TOP) + 1):
                        act = [(r, s) for r, s in ops if r.active(k)]
                        if not act:
                            continue
                        d = definition(conv, kind, static, act, style, k, types)
                        if runs and runs[-1][1] == k - 1 and runs[-1][2] == d:
                            runs[-1][1] = k
                        else:
                            runs.append([k, k, d])
                    for k0, k1, d in runs:
                        layer = {"id": f"{l.name}/{style.name}/{gi}-{kind}/{k0}" + (f"@{cls[0]}" if len(p['classes']) > 1 else ""),
                                 "type": ML_TYPE[kind], "source": "map", "source-layer": src}
                        if k0 > kmin_first:
                            layer["minzoom"] = r3(max(0, zoom_start(k0, scale)))
                        if k1 < K_TOP:
                            layer["maxzoom"] = r3(max(0, zoom_start(k1 + 1, scale)))
                        if layer.get("maxzoom") == 0:
                            continue
                        layer.update(d)
                        if below_hillshade and k0 in shade and "paint" in layer:
                            # what the hillshade adds on flat ground (the hillshade tiles hold only the difference to it)
                            amount = shade[k0] * FLAT - 0.5
                            layer["paint"] = {key: (lighten(v, amount) if key in ("fill-color", "line-color") else v) for key, v in layer["paint"].items()}
                        if ML_TYPE[kind] == "symbol":
                            symbol_segments[-1].append(layer)
                        else:
                            geometry_layers.append(layer)
    return geometry_layers, symbol_segments, n_rules_skipped, shade


def case_of(pairs, default_last=True):
    """pairs: [(filter expression or None, value)] -> literal, or ["case", f1, v1, ..., last value]"""
    vals = [v for _, v in pairs]
    if all(v == vals[0] for v in vals):
        return vals[0]
    out = ["case"]
    # rules without a filter match everything: they go last
    rest = [(f, v) for f, v in pairs if f is not None]
    always = [v for f, v in pairs if f is None]
    for f, v in rest[:-1] if not always else rest:
        out += [f, v]
    out.append(always[0] if always else rest[-1][1])
    return out if len(out) > 2 else out[-1]


def definition(conv, kind, static, act, style, k, types):
    """filter, layout and paint of one MapLibre layer for the rules active at zoom level k"""
    filters = []
    for r, s in act:
        if r.else_filter:
            others = [o.filter for o in style.rules if o.active(k) and o.filter is not None and not o.else_filter]
            f = ["!", ["any"] + [to_maplibre(o, types) for o in others]] if others else None
        else:
            f = to_maplibre(r.filter, types) if r.filter is not None else None
        filters.append(f)
    props = [conv.props(kind, s) for _, s in act]
    layout, paint = conv.static_layout(kind, static)
    layout, paint = dict(layout), dict(paint)
    keys = []
    for p in props:
        for key in p:
            if key not in keys:
                keys.append(key)
    defaults = {"line-opacity": 1, "line-offset": 0, "fill-opacity": 1, "text-opacity": 1, "text-transform": "none", "text-line-height": 1.2}
    for key in keys:
        pairs = [(f, p.get(key, defaults.get(key))) for f, p in zip(filters, props)]
        v = case_of(pairs)
        (layout if key in LAYOUT_PROPS else paint)[key] = v
    d = {}
    if all(f is not None for f in filters):
        uniq = []
        for f in filters:
            if f not in uniq:
                uniq.append(f)
        d["filter"] = uniq[0] if len(uniq) == 1 else ["any"] + uniq
    if layout:
        d["layout"] = layout
    if paint:
        d["paint"] = paint
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--symbols", required=True, help="directory the style's file paths are relative to (OpenTopoMap/mapnik)")
    ap.add_argument("--scale", type=float, default=1.28, help="size of lines, symbols and text relative to Mapnik on 256 px tiles")
    ap.add_argument("--maxzoom", type=int, default=14, help="last zoom level of the vector tiles")
    ap.add_argument("--raster-maxzoom", type=int, default=12)
    ap.add_argument("--name", default="LoriMaps")
    ap.add_argument("--out", required=True, help="directory for style.json and sprite files")
    args = ap.parse_args()

    m = MapnikMap(args.style)
    plan = json.load(open(args.plan))
    for p in plan["layers"]:          # file layers were turned into tables by layers.py
        for l in m.layers:
            if l.name == p["name"]:
                l.type, l.table = "postgis", p["table"]
    os.makedirs(args.out, exist_ok=True)
    sprite = Sprite(args.symbols)
    geometry_layers, symbol_segments, skipped, shade = build(m, plan, args.scale, sprite)

    # the background lies below the hillshade too; its opacity changes between zoom levels (paint: any zoom works)
    bg = color(m.a.get("background-color", "rgb(255,255,255)"))
    if shade:
        stops, last = [], None
        for k in sorted(shade):
            c = lighten(bg, shade[k] * FLAT - 0.5)
            if c != last:
                stops.append((k, c))
                last = c
        bg = stops[0][1] if len(stops) == 1 else ["step", ["zoom"], stops[0][1]] + [x for k, c in stops[1:] for x in (r3(zoom_start(k, args.scale)), c)]
    layers = [{"id": "background", "type": "background", "paint": {"background-color": bg}}]
    for g in geometry_layers:
        if "_raster" in g:
            ks = g["ks"]
            layer = {"id": "raster/" + g["_raster"], "type": "raster", "source": "raster-" + g["_raster"],
                     "paint": {"raster-opacity": 1, "raster-fade-duration": 0, "raster-resampling": "linear"}}
            if ks[0] > 0:
                layer["minzoom"] = r3(max(0, zoom_start(ks[0], args.scale)))
            if ks[-1] < K_TOP:
                layer["maxzoom"] = r3(max(0, zoom_start(ks[-1] + 1, args.scale)))
            layers.append(layer)
        else:
            layers.append(g)
    # label priority: later Mapnik segments (after clear-label-cache) win over earlier ones; inside a segment
    # the first layer wins. MapLibre places the last layer first.
    for seg in symbol_segments:
        layers += list(reversed(seg))

    sources = {"map": {"type": "vector", "tiles": ["lorimaps://map/{z}/{x}/{y}"], "minzoom": 0, "maxzoom": args.maxzoom}}
    for g in geometry_layers:
        if "_raster" in g:
            sources["raster-" + g["_raster"]] = {"type": "raster", "tiles": [f"lorimaps://{g['_raster']}/{{z}}/{{x}}/{{y}}"],
                                                 "tileSize": 512, "minzoom": 0, "maxzoom": args.raster_maxzoom}
    style = {
        "version": 8, "name": args.name,
        "metadata": {"lorimaps:scale": args.scale, "lorimaps:source": "translated from the OpenTopoMap Mapnik style by make_style.py"},
        "sources": sources,
        "sprite": "lorimaps://sprite",
        "glyphs": "lorimaps://glyphs/{fontstack}/{range}",
        "layers": layers,
    }
    ids = [l["id"] for l in layers]
    assert len(ids) == len(set(ids)), "layer ids are not unique"
    json.dump(style, open(os.path.join(args.out, "style.json"), "w"), separators=(",", ":"))
    n = sprite.write(os.path.join(args.out, "sprite"), args.scale)
    fonts = sorted({f for l in layers for f in font_names(l)})
    json.dump(fonts, open(os.path.join(args.out, "fonts.json"), "w"))
    # for raster_tiles.py --flat-shift of the relief colours
    json.dump({"hillshade_opacity": shade, "flat": FLAT}, open(os.path.join(args.out, "raster.json"), "w"))
    kinds = {}
    for l in layers:
        kinds[l["type"]] = kinds.get(l["type"], 0) + 1
    print(f"{len(layers)} layers {kinds}, {n} sprite images, fonts {fonts}, {skipped} symbolizers not translated")
    print(f"style.json: {os.path.getsize(os.path.join(args.out, 'style.json')) / 1024:.0f} KB")


def font_names(layer):
    out = set()

    def walk(v):
        if isinstance(v, list):
            if len(v) == 2 and v[0] == "literal" and isinstance(v[1], list) and all(isinstance(x, str) for x in v[1]):
                return
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for key, x in v.items():
                if key == "text-font":
                    grab(x)
                else:
                    walk(x)

    def grab(x):
        if isinstance(x, list):
            if x and x[0] == "literal":
                out.update(x[1])
            else:
                for y in x:
                    grab(y)

    lay = layer.get("layout", {})
    if "text-font" in lay:
        grab(lay["text-font"])
    walk(lay.get("text-field"))
    return out


if __name__ == "__main__":
    main()
