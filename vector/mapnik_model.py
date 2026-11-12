#!/usr/bin/env python3
"""Reads a Mapnik style (the XML written by mapnik.save_map, with all entities resolved) into a small model
that the tile export and the MapLibre style generator share.

Zoom: "k" is the zoom level in the sense of the Mapnik style, i.e. the level at which OpenTopoMap would draw
a rule on normal 256 px tiles (scale denominator 559082264 / 2^k).
"""
import math
import re
from xml.etree import ElementTree as ET

MAX_K = 19
DENOM0 = 559082264.028


def denom(k):
    return DENOM0 / 2 ** k


# ---------------------------------------------------------------------------- expressions

TOKEN = re.compile(r"""\s*(?:
    (?P<col>\[[^\]]+\]) |
    (?P<str>'(?:[^'\\]|\\.)*') |
    (?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) |
    (?P<op><=|>=|!=|<>|=|<|>|\+|-|\*|/|%|\(|\)|,) |
    (?P<word>[A-Za-z_][A-Za-z_0-9.]*)
)""", re.X | re.S)


def tokenize(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read expression at {text[pos:pos + 30]!r} in {text!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class Parser:
    """Mapnik expression -> nested tuples: ('col', name), ('lit', value), ('null',), (op, a, b), ('not', a), ('neg', a)"""

    def __init__(self, text):
        self.t = tokenize(text)
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        e = self.or_()
        if self.i != len(self.t):
            raise ValueError(f"unexpected {self.peek()}")
        return e

    def or_(self):
        e = self.and_()
        while self.peek() == ("word", "or"):
            self.take()
            e = ("or", e, self.and_())
        return e

    def and_(self):
        e = self.not_()
        while self.peek() == ("word", "and"):
            self.take()
            e = ("and", e, self.not_())
        return e

    def not_(self):
        if self.peek() == ("word", "not"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        e = self.add()
        while self.peek()[0] == "op" and self.peek()[1] in ("=", "!=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            e = ("!=" if op == "<>" else op, e, self.add())
        return e

    def add(self):
        e = self.mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            e = (op, e, self.mul())
        return e

    def mul(self):
        e = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            e = (op, e, self.unary())
        return e

    def unary(self):
        kind, v = self.take()
        if kind == "col":
            return ("col", v[1:-1])
        if kind == "str":
            return ("lit", v[1:-1].replace("\\'", "'"))
        if kind == "num":
            f = float(v)
            return ("lit", int(f) if f == int(f) and "." not in v and "e" not in v.lower() else f)
        if kind == "word":
            if v == "null":
                return ("null",)
            if v in ("true", "false"):
                return ("lit", v == "true")
            raise ValueError(f"unknown word {v!r}")
        if (kind, v) == ("op", "("):
            e = self.or_()
            if self.take() != ("op", ")"):
                raise ValueError("missing )")
            return e
        if (kind, v) == ("op", "-"):
            return ("neg", self.unary())
        raise ValueError(f"unexpected {kind} {v!r}")


def parse_expr(text):
    return Parser(text).parse()


def columns(e, out=None):
    out = set() if out is None else out
    if e is None:
        return out
    if e[0] == "col":
        out.add(e[1])
    else:
        for a in e[1:]:
            if isinstance(a, tuple):
                columns(a, out)
    return out


def sql_ident(name):
    return '"' + name.replace('"', '""') + '"'


def sql_lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(e, types=None):
    """Mapnik semantics in SQL: comparisons with NULL are false (not unknown), '!=' is true for NULL.
    types: column name -> 'num' or 'text' (comparisons cast the column to the type of the literal)."""
    types = types or {}

    def val(x, want=None):
        k = x[0]
        if k == "col":
            c = sql_ident(x[1])
            t = types.get(x[1])
            if want == "num" and t != "num":
                # text column compared with a number: non-numeric text counts as NULL
                return f"(CASE WHEN {c}::text ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN {c}::text::numeric END)"
            if want == "text" and t == "num":
                return f"{c}::text"
            return c
        if k == "lit":
            return sql_lit(x[1])
        if k == "neg":
            return f"(-{val(x[1], 'num')})"
        if k in ("+", "-", "*", "/", "%"):
            if k == "+" and (kind(x[1]) == "text" or kind(x[2]) == "text"):
                return f"(COALESCE({val(x[1], 'text')}::text, '') || COALESCE({val(x[2], 'text')}::text, ''))"
            return f"({val(x[1], 'num')} {k} {val(x[2], 'num')})"
        return f"({boolean(x)})"

    def kind(x):
        if x[0] == "lit":
            return "num" if isinstance(x[1], (int, float)) and not isinstance(x[1], bool) else "text"
        if x[0] == "col":
            return types.get(x[1], "text")
        if x[0] in ("neg", "-", "*", "/", "%"):
            return "num"
        if x[0] == "+":
            return "text" if "text" in (kind(x[1]), kind(x[2])) else "num"
        return "bool"

    def boolean(x):
        k = x[0]
        if k in ("and", "or"):
            return f"({boolean(x[1])} {k.upper()} {boolean(x[2])})"
        if k == "not":
            return f"(NOT {boolean(x[1])})"
        if k in ("=", "!="):
            a, b = x[1], x[2]
            if b[0] == "null" or a[0] == "null":
                other = a if b[0] == "null" else b
                return f"({val(other)} IS {'NOT ' if k == '!=' else ''}NULL)"
            want = "num" if "num" in (kind(a) if a[0] != "col" else None, kind(b) if b[0] != "col" else None) else \
                ("text" if a[0] != "col" or b[0] != "col" else None)
            if a[0] == "col" and b[0] == "col":
                want = None
            l, r = val(a, want), val(b, want)
            return f"({l} IS NOT DISTINCT FROM {r})" if k == "=" else f"({l} IS DISTINCT FROM {r})"
        if k in ("<", ">", "<=", ">="):
            a, b = x[1], x[2]
            want = "num" if "num" in (kind(a) if a[0] != "col" else None, kind(b) if b[0] != "col" else None) else None
            return f"COALESCE({val(a, want)} {k} {val(b, want)}, false)"
        if k == "lit" and isinstance(x[1], bool):
            return "true" if x[1] else "false"
        if k == "col":   # a column used as a truth value: not null, not empty, not 0
            return f"(COALESCE({sql_ident(x[1])}::text, '') NOT IN ('', '0', 'false'))"
        raise ValueError(f"not a condition: {x}")

    return boolean(e)


def to_maplibre(e, types=None):
    """Mapnik semantics as a MapLibre expression. Attributes that are NULL are missing in the vector tiles."""
    types = types or {}

    def kind(x):
        if x[0] == "lit":
            return "num" if isinstance(x[1], (int, float)) and not isinstance(x[1], bool) else "text"
        if x[0] == "col":
            return types.get(x[1], "text")
        if x[0] in ("neg", "-", "*", "/", "%"):
            return "num"
        if x[0] == "+":
            return "text" if "text" in (kind(x[1]), kind(x[2])) else "num"
        return "bool"

    def val(x, want=None, missing=None):
        k = x[0]
        if k == "col":
            g = ["get", x[1]]
            t = types.get(x[1], "text")
            if want == "num":
                # missing or non-numeric: a value for which the comparison is false
                return ["to-number", g, missing if missing is not None else 0] if t != "num" else \
                    (["coalesce", g, missing] if missing is not None else g)
            if want == "text" and t == "num":
                return ["to-string", g]
            return g
        if k == "lit":
            return x[1]
        if k == "neg":
            return ["-", val(x[1], "num")]
        if k in ("+", "-", "*", "/", "%"):
            if k == "+" and "text" in (kind(x[1]), kind(x[2])):
                return ["concat", val(x[1]), val(x[2])]
            return [k, val(x[1], "num"), val(x[2], "num")]
        return boolean(x)

    def boolean(x):
        k = x[0]
        if k in ("and", "or"):
            op = "all" if k == "and" else "any"
            parts = []
            for side in (x[1], x[2]):
                b = boolean(side)
                if isinstance(b, list) and b and b[0] == op:
                    parts.extend(b[1:])
                else:
                    parts.append(b)
            return [op] + parts
        if k == "not":
            return ["!", boolean(x[1])]
        if k in ("=", "!="):
            a, b = x[1], x[2]
            if a[0] == "null":
                a, b = b, a
            if b[0] == "null":
                if a[0] == "col":
                    return ["!", ["has", a[1]]] if k == "=" else ["has", a[1]]
                return ["==" if k == "=" else "!=", val(a), None]
            if a[0] == "col" and b[0] == "lit":
                lit = b[1]
                ct = types.get(a[1], "text")
                if isinstance(lit, (int, float)) and not isinstance(lit, bool) and ct != "num":
                    # text column compared with a number
                    return ["==" if k == "=" else "!=", ["to-number", ["get", a[1]], lit + 1 if k == "=" else lit], lit] \
                        if k == "=" else ["any", ["!", ["has", a[1]]], ["!=", ["to-number", ["get", a[1]], lit + 1], lit]]
                if isinstance(lit, str) and ct == "num":
                    return ["==" if k == "=" else "!=", ["to-string", ["get", a[1]]], lit]
                return ["==" if k == "=" else "!=", ["get", a[1]], lit]
            return ["==" if k == "=" else "!=", val(a), val(b)]
        if k in ("<", ">", "<=", ">="):
            a, b = x[1], x[2]
            # a missing value must make the comparison false
            big, small = 1e300, -1e300
            if k in (">", ">="):
                return [k, val(a, "num", small), val(b, "num", big)]
            return [k, val(a, "num", big), val(b, "num", small)]
        if k == "lit" and isinstance(x[1], bool):
            return x[1]
        if k == "col":
            return ["all", ["has", x[1]], ["!", ["in", ["to-string", ["get", x[1]]], ["literal", ["", "0", "false"]]]]]
        raise ValueError(f"not a condition: {x}")

    return boolean(e)


def to_maplibre_value(e, types=None):
    """Expression used as a value (text of a label, rotation, ...)."""
    types = types or {}
    k = e[0]
    if k == "col":
        return ["get", e[1]]
    if k == "lit":
        return e[1]
    if k == "+":
        def flat(x):
            if x[0] == "+":
                return flat(x[1]) + flat(x[2])
            return [x]
        parts = flat(e)
        if any(p[0] == "lit" and isinstance(p[1], str) for p in parts) or any(types.get(p[1], "text") != "num" for p in parts if p[0] == "col"):
            return ["concat"] + [to_maplibre_value(p, types) for p in parts]
        return ["+"] + [to_maplibre_value(p, types) for p in parts]
    if k in ("-", "*", "/", "%"):
        return [k, ["to-number", to_maplibre_value(e[1], types), 0], ["to-number", to_maplibre_value(e[2], types), 0]]
    if k == "neg":
        return ["-", ["to-number", to_maplibre_value(e[1], types), 0]]
    return to_maplibre(e, types)


# ---------------------------------------------------------------------------- model

class Symbolizer:
    def __init__(self, el):
        self.type = el.tag[:-len("Symbolizer")]     # Line, LinePattern, Polygon, PolygonPattern, Markers, Text, Raster, Point
        self.a = dict(el.attrib)
        self.text = (el.text or "").strip() if self.type == "Text" else ""
        # <Format> children: parts of a label with their own size, face, colour
        self.parts = []
        if self.type == "Text":
            if el.text and el.text.strip():
                self.parts.append(({}, el.text.strip()))
            for child in el:
                if child.tag == "Format":
                    if child.text and child.text.strip():
                        self.parts.append((dict(child.attrib), child.text.strip()))
                    if child.tail and child.tail.strip():
                        self.parts.append(({}, child.tail.strip()))
                elif child.tail and child.tail.strip() and child.tag != "Placement":
                    self.parts.append(({}, child.tail.strip()))
            # <Placement> children: alternative positions tried in order when the first one is taken
            self.placements = [dict(c.attrib) for c in el if c.tag == "Placement"]

    def columns(self):
        cols = set()
        for _, text in self.parts:
            columns(parse_expr(text), cols)
        for v in self.a.values():
            for c in re.findall(r"\[([^\]]+)\]", v):
                cols.add(c)
        return cols


class Rule:
    def __init__(self, el, index):
        self.index = index
        f = el.find("Filter")
        self.filter_text = f.text.strip() if f is not None and f.text else None
        self.filter = parse_expr(self.filter_text) if self.filter_text else None
        self.else_filter = el.find("ElseFilter") is not None
        mn, mx = el.find("MinScaleDenominator"), el.find("MaxScaleDenominator")
        self.min_denom = float(mn.text) if mn is not None else 0.0
        self.max_denom = float(mx.text) if mx is not None else float("inf")
        self.symbolizers = [Symbolizer(c) for c in el if c.tag.endswith("Symbolizer")]

    def active(self, k):
        return self.min_denom <= denom(k) < self.max_denom

    def zooms(self):
        return [k for k in range(0, MAX_K + 1) if self.active(k)]

    def columns(self):
        cols = columns(self.filter) if self.filter else set()
        for s in self.symbolizers:
            cols |= s.columns()
        return cols


class Style:
    def __init__(self, el):
        self.name = el.attrib["name"]
        self.a = dict(el.attrib)
        self.rules = [Rule(r, i) for i, r in enumerate(el.findall("Rule"))]
        # ElseFilter: matches what no other rule of the style matches at that zoom
        self.has_else = any(r.else_filter for r in self.rules)


class Layer:
    def __init__(self, el, styles):
        self.name = el.attrib["name"]
        self.a = dict(el.attrib)
        self.styles = [styles[s.text] for s in el.findall("StyleName") if s.text in styles]
        self.params = {p.attrib["name"]: (p.text or "") for p in el.find("Datasource")}
        self.type = self.params.get("type")
        self.table = self.params.get("table", "")

    def rules(self):
        for s in self.styles:
            for r in s.rules:
                yield s, r

    def zooms(self):
        return sorted({k for _, r in self.rules() for k in r.zooms()})

    def columns(self):
        cols = set()
        for _, r in self.rules():
            cols |= r.columns()
        return cols

    def scale_classes(self):
        """Layers whose SQL depends on !scale_denominator!: the zoom levels fall into classes with the same
        query result (split at the constants the token is compared with). Returns [(first k, last k)]."""
        if "!scale_denominator!" not in self.table:
            return [(0, MAX_K)]
        cuts = sorted({float(m) for m in re.findall(r"!scale_denominator!\s*[<>]=?\s*([0-9.]+)", self.table)}
                      | {float(m) for m in re.findall(r"([0-9.]+)\s*[<>]=?\s*!scale_denominator!", self.table)})
        classes, start = [], 0
        for k in range(1, MAX_K + 1):
            if any((denom(k - 1) > c) != (denom(k) > c) or (denom(k - 1) >= c) != (denom(k) >= c) for c in cuts):
                classes.append((start, k - 1))
                start = k
        classes.append((start, MAX_K))
        return classes


class MapnikMap:
    def __init__(self, path):
        root = ET.parse(path).getroot()
        self.a = dict(root.attrib)
        self.fontsets = {f.attrib["name"]: [x.attrib["face-name"] for x in f.findall("Font")] for f in root.findall("FontSet")}
        self.styles = {s.attrib["name"]: Style(s) for s in root.findall("Style")}
        self.layers = [Layer(l, self.styles) for l in root.findall("Layer")]


if __name__ == "__main__":
    import sys
    m = MapnikMap(sys.argv[1])
    print(len(m.layers), "layers,", len(m.styles), "styles,", sum(len(s.rules) for s in m.styles.values()), "rules")
    for l in m.layers:
        z = l.zooms()
        print(f"{l.name:32s} {l.type:8s} k {z[0] if z else '-'}..{z[-1] if z else '-'} classes {l.scale_classes() if len(l.scale_classes()) > 1 else ''} cols {sorted(l.columns())[:8]}")
