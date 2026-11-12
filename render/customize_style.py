#!/usr/bin/env python3
"""Own changes to the OpenTopoMap style, applied to a fresh checkout before import and rendering.

Each patch checks that the original text is still there, so a change upstream fails loudly
instead of being silently skipped.

  1. extra OSM keys imported: locked, 4wd_only, smoothness, motor_vehicle
  2. viewpoints without a "direction" tag are drawn (all-round symbol), and never hidden by labels
  3. OpenTopoMap's black tracks with clearer track types: grade2 thicker than grade3,
     grade4 densely dashed, grade5 dashed with wide gaps
  4. paths/tracks not visible on the ground or without public access: light grey;
     "4WD" next to tracks with 4wd_only=yes or very bad smoothness; trail names from zoom 14
  5. camp sites: green symbol from zoom 11, name from zoom 13
  6. locked gates and barriers: red "no entry" disc from zoom 13
"""
import os
import shutil
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
mapnik_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
# EARLY_TRAILS=2: paths, tracks and trail names two zoom levels earlier (sparse areas like Tasmania)
EARLY = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 0
# TASVEG=1: vegetation of Tasmania from TASVEG instead of OpenStreetMap's forest and scrub
TASVEG = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else 0
# LIST_TRACKS (Tasmania, LIST Transport Segments): "A" = all tracks and paths from LIST instead of OSM,
# "B" = walking tracks from LIST, vehicle tracks from OSM (OSM paths that LIST also has are left out),
# "C" = all tracks and paths from LIST, plus the OSM tracks and paths that LIST does not have,
# "D" = like C, and the roads from LIST too (render/list/list_roads.sh), OSM only for what LIST does not have
LIST = (sys.argv[4] if len(sys.argv) > 4 else "").strip().upper()
xml_path = mapnik_dir / "opentopomap.xml"
xml = xml_path.read_text()


def patch(old, new, what, text=None):
    global xml
    src = xml if text is None else text
    if old not in src:
        sys.exit(f"style patch failed ({what}): original text not found")
    out = src.replace(old, new, 1)
    print(f"patched: {what}")
    if text is None:
        xml = out
    return out


def one_line(sql):
    return " ".join(sql.split())


# ---------------------------------------------------------------- 1) import more keys
style_file = mapnik_dir / "osm2pgsql" / "opentopomap.style"
s = style_file.read_text()
for line in ["node,way   locked       text         linear",
             "way        4wd_only     text         linear",
             "way        smoothness   text         linear",
             "node,way   motor_vehicle text        linear"]:
    key = line.split()[1]
    if not any(l.split()[1:2] == [key] for l in s.splitlines() if l.strip() and not l.startswith("#")):
        s += line + "\n"
style_file.write_text(s)
print("patched: osm2pgsql style (locked, 4wd_only, smoothness, motor_vehicle)")

# own symbols
for svg in (here / "symbols").glob("*.svg"):
    shutil.copy(svg, mapnik_dir / "symbols-otm" / svg.name)

# ---------------------------------------------------------------- 2) viewpoints
patch("viewpointdirection(direction) AS int_dir FROM planet_osm_point WHERE (tourism='viewpoint' AND direction IS NOT NULL)",
      "viewpointdirection(COALESCE(NULLIF(direction,''),'0-360')) AS int_dir FROM planet_osm_point WHERE tourism='viewpoint'",
      "viewpoints without direction")
vp = mapnik_dir / "styles-otm" / "symbols-viewpoint.xml"
vp_text = vp.read_text()
if 'ignore-placement="true"' not in vp_text:
    sys.exit("style patch failed (viewpoint overlap): original text not found")
vp.write_text(vp_text.replace('ignore-placement="true"', 'ignore-placement="true" allow-overlap="true"'))
print("patched: viewpoint symbols always drawn")

# ---------------------------------------------------------------- 3) track widths and dashes
# OpenTopoMap's black tracks, with a clearer difference between the track types:
# grade1 unchanged, grade2 thicker than grade3, grade4 densely dashed, grade5 dashed with wide gaps.
import re
rc_path = mapnik_dir / "styles-otm" / "roads-casing.xml"
rc = rc_path.read_text()


def rc_sub(pattern, repl, what, count=1):
    global rc
    rc, n = re.subn(pattern, repl, rc)
    if n != count:
        sys.exit(f"style patch failed ({what}): {n} matches instead of {count}")
    print(f"patched: {what}")


def rule(zmax, zmin, flt, width, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f"<Rule>&maxscale_zoom{zmax};&minscale_zoom{zmin};<Filter>{flt}</Filter>"
            f'<LineSymbolizer stroke="black" stroke-width="{width}"{d} /></Rule>')


T = "[highway] = 'track' and [tracktype] = "
# unknown tracktype keeps OpenTopoMap's former grade2 line, grade2 gets its own (thicker) rule
rc_sub(r"\(\[tracktype\] = 'grade1' or \[tracktype\] = 'grade3' or \[tracktype\] = 'grade4' or \[tracktype\] = 'grade5' \)\)",
       "([tracktype] = 'grade1' or [tracktype] = 'grade2' or [tracktype] = 'grade3' or [tracktype] = 'grade4' or [tracktype] = 'grade5' ))",
       "zoom 14: unknown tracktype without grade2")
rc_sub(r"\(\[tracktype\] = 'grade1' or \[tracktype\] = 'grade3' or \[tracktype\] = 'grade4' or \[tracktype\] = 'grade5'\) \)",
       "([tracktype] = 'grade1' or [tracktype] = 'grade2' or [tracktype] = 'grade3' or [tracktype] = 'grade4' or [tracktype] = 'grade5') )",
       "zoom 15+: unknown tracktype without grade2")
for zmax, zmin, old_w, w2, w3, d4, w5, d5, old_d5 in (
        (14, 14, "0.5", "1.4", "0.5", "1.8,1.8", "0.6", "3,6", "3,3"),
        (15, 17, "1", "2.0", "0.9", "2.4,2.4", "0.9", "4,8", "4,4")):
    rc_sub(rf"<Rule>\s*&maxscale_zoom{zmax};\s*&minscale_zoom{zmin};\s*<Filter>\[highway\] = 'track' and \(\[tracktype\] = 'grade3' or \[tracktype\] = 'grade4'\)</Filter>\s*"
           rf'<LineSymbolizer stroke="black" stroke-width="{old_w}" />\s*</Rule>',
           rule(zmax, zmin, T + "'grade2'", w2) + rule(zmax, zmin, T + "'grade3'", w3) + rule(zmax, zmin, T + "'grade4'", w3, d4),
           f"zoom {zmax}: grade2/3/4")
    rc_sub(rf"(<Filter>\[highway\] = 'track' and \[tracktype\] = 'grade5'</Filter>\s*)"
           rf'<LineSymbolizer stroke="black" stroke-width="[0-9.]+" stroke-dasharray="{old_d5}" />',
           rf'\g<1><LineSymbolizer stroke="black" stroke-width="{w5}" stroke-dasharray="{d5}" />',
           f"zoom {zmax}: grade5")

# paths on zoom 14 (and the earlier levels copied from it) dotted like on zoom 15+, not dashed
DOTS = {"1.6,1.6": "1.1,2", "1.6,1.6,1.6,1.6,1.6,4.5": "1.1,2,1.1,2,1.1,6", "1.6,1.6,1.6,5": "1.1,2,1.1,8"}
dotted = 0


def path_dots(m):
    global dotted
    block = m.group(0)
    if re.search(r"&maxscale_zoom14;\s*&minscale_zoom14;", block) and ("'path'" in block or "'footway'" in block):
        def sub(d):
            global dotted
            if d.group(1) in DOTS:
                dotted += 1
                return f'stroke-dasharray="{DOTS[d.group(1)]}"'
            return d.group(0)
        block = re.sub(r'stroke-dasharray="([0-9.,]+)"', sub, block)
    return block


rc = re.sub(r"<Rule>.*?</Rule>", path_dots, rc, flags=re.S)
if dotted < 5:
    sys.exit(f"style patch failed (dotted paths): only {dotted} dash patterns found")
print(f"patched: paths on zoom 14 dotted ({dotted} rules)")

# paths and tracks two zoom levels earlier (option), slightly thinner on the extra levels
if EARLY:
    def scaled(block, factor, zmax, zmin):
        block = re.sub(r"&maxscale_zoom\d+;", f"&maxscale_zoom{zmax};", block, count=1)
        block = re.sub(r"&minscale_zoom\d+;", f"&minscale_zoom{zmin};", block, count=1)
        return re.sub(r'stroke-width="([0-9.]+)"', lambda m: f'stroke-width="{float(m.group(1)) * factor:.2f}"', block)

    PATHS = ("'path'", "'footway'", "'bridleway'", "'steps'")
    added = 0

    def early(m):
        global added
        block = m.group(0)
        flt = re.search(r"<Filter>(.*?)</Filter>", block, re.S)
        if not flt:
            return block
        f = flt.group(1)
        if re.search(r"&maxscale_zoom14;\s*&minscale_zoom14;", block):
            is_track = "'track'" in f
            grade12 = is_track and "(not" not in f and ("'grade1'" in f or "'grade2'" in f)
            if (is_track and not grade12) or (not is_track and any(p in f for p in PATHS)):
                added += 1
                return block + scaled(block, 0.7, 12, 13)
        if re.search(r"&maxscale_zoom13;\s*&minscale_zoom13;", block) and "'track'" in f \
                and ("'grade1'" in f or "'grade2'" in f):
            added += 1
            return block + scaled(block, 0.8, 11, 12)
        return block

    rc = re.sub(r"<Rule>.*?</Rule>", early, rc, flags=re.S)
    if added < 5:
        sys.exit(f"style patch failed (early trails): only {added} rules found")
    print(f"patched: paths and tracks two zoom levels earlier ({added} rules)")
rc_path.write_text(rc)

# ---------------------------------------------------------------- 4) faded ways, 4WD labels
NOACCESS = ("(foot IN ('no','private') OR (access IN ('no','private') "
            "AND COALESCE(foot,'') NOT IN ('yes','designated','permissive')))")
INVISIBLE = "trail_visibility IN ('no','horrible')"
PATHLIKE = "highway IN ('path','footway','bridleway','steps','cycleway')"

# LIST_PLUS: LIST road classes on OSM roads, missing LIST roads added, "privat" labels instead of grey ways
LIST_PLUS = os.environ.get("LIST_PLUS", "0") == "1" and LIST in ("C", "D")
ACCESS_SQL_COND = ("list_class = 'Access Road' AND highway NOT IN ('track','path','footway','bridleway','steps','cycleway') "
                   "AND COALESCE(bridge, 'no') IN ('no', '') AND COALESCE(tunnel, 'no') IN ('no', '')")

# OSM ways replaced by LIST data (option)
OSM_DROP = {"A": f"(highway = 'track' OR {PATHLIKE})", "B": f"({PATHLIKE} AND list_dup)",
            "C": f"((highway = 'track' OR {PATHLIKE}) AND list_dup)",
            "D": f"((highway = 'track' OR {PATHLIKE}) AND list_dup)"}.get(LIST, "false")

# ways without public access are taken out of OpenTopoMap's road layer and drawn light grey in an own layer;
# paths not visible on the ground stay normal and get the label "nicht sichtbar"
start = xml.index('<Layer name="roads">')
end = xml.index("</Layer>", start) + len("</Layer>")
roads = xml[start:end]
if LIST_PLUS:
    # ways without access are drawn normally (with a "privat" label); access roads get an own thinner style
    excl = f"NOT COALESCE({OSM_DROP} OR ({ACCESS_SQL_COND}), false) AND ("
else:
    excl = f"NOT COALESCE(((highway = 'track' OR {PATHLIKE}) AND {NOACCESS}) OR {OSM_DROP}, false) AND ("
roads = patch("FROM planet_osm_line WHERE ", "FROM planet_osm_line WHERE " + excl, "roads layer: exclude faded ways", roads)
roads = patch(" ORDER BY (CASE WHEN highway IN ('motorway'", ") ORDER BY (CASE WHEN highway IN ('motorway'", "roads layer: close condition", roads)
xml = xml[:start] + roads + xml[end:]

GREY = "#b4b4b4"
FADED_FROM = 12 if EARLY else 14
FADED_STYLE = f"""<Style name="own-faded-ways">
<Rule>&maxscale_zoom{FADED_FROM};<Filter>[highway] = 'track'</Filter>
<LineSymbolizer stroke="{GREY}" stroke-width="1.2"/></Rule>
<Rule>&maxscale_zoom{FADED_FROM};<Filter>[highway] != 'track' and [highway] != 'steps'</Filter>
<LineSymbolizer stroke="{GREY}" stroke-width="0.9" stroke-dasharray="1.1,2"/></Rule>
<Rule>&maxscale_zoom15;<Filter>[highway] = 'steps'</Filter>
<LineSymbolizer stroke="{GREY}" stroke-width="2.5" stroke-dasharray="1,1.5"/></Rule>
</Style>
<Style name="own-invisible">
<Rule>&maxscale_zoom{13 if EARLY else 14};
<TextSymbolizer fontset-name="sans-condensed-oblique" size="8" fill="#555555" halo-radius="1.2" halo-fill="rgba(255,255,255,0.85)"
 placement="line" spacing="300" max-char-angle-delta="30" dy="6">'nicht sichtbar'</TextSymbolizer></Rule>
</Style>
<Style name="own-4wd">
<Rule>&maxscale_zoom14;
<TextSymbolizer fontset-name="sans-condensed-bold" size="8" fill="#5a3a00" halo-radius="1.2" halo-fill="rgba(255,255,255,0.85)"
 placement="line" spacing="260" max-char-angle-delta="20" dy="-6">'4WD'</TextSymbolizer></Rule>
</Style>"""

if LIST_PLUS:
    FADED_STYLE = FADED_STYLE.split('<Style name="own-invisible">')[1]
    FADED_STYLE = f"""<Style name="own-faded-ways">
<Rule>&maxscale_zoom14;
<TextSymbolizer fontset-name="sans-condensed-oblique" size="8" fill="#7a1f1f" halo-radius="1.2" halo-fill="rgba(255,255,255,0.85)"
 placement="line" spacing="300" max-char-angle-delta="30" dy="7">'privat'</TextSymbolizer></Rule>
</Style>
<Style name="own-invisible">""" + FADED_STYLE
FADED_SQL = one_line(f"""(SELECT way, highway FROM planet_osm_line
  WHERE (highway = 'track' OR {PATHLIKE}) AND {NOACCESS} AND NOT COALESCE({OSM_DROP}, false)) AS fadedways""")
INVISIBLE_SQL = one_line(f"""(SELECT way FROM planet_osm_line
  WHERE (highway = 'track' OR {PATHLIKE}) AND {INVISIBLE} AND NOT {NOACCESS}) AS invisible""")
FOURWD_SQL = one_line("""(SELECT way FROM planet_osm_line WHERE highway = 'track'
  AND ("4wd_only" = 'yes' OR smoothness IN ('very_bad','horrible','very_horrible','impassable'))) AS fourwd""")


def xml_escape(sql):
    return sql.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def layer(name, styles, sql):
    style_refs = "".join(f"<StyleName>{st}</StyleName>" for st in styles)
    return (f'<Layer name="{name}">{style_refs}<Datasource>'
            f'<Parameter name="table">{xml_escape(sql)}</Parameter>'
            '<Parameter name="geometry_field">way</Parameter><Parameter name="srid">3857</Parameter>'
            '<Parameter name="dbname">gis</Parameter>&postgis-settings;</Datasource></Layer>')


insert = "\n".join([FADED_STYLE, layer("own-faded-ways", ["own-faded-ways"], FADED_SQL)])

# ---------------------------------------------------------------- LIST Transport Segments (option)
if LIST in ("A", "B", "C", "D"):
    # OSM names of the replaced ways are not written either
    rn_start = xml.index('<Layer name="road-names-text">')
    rn_end = xml.index("</Layer>", rn_start)
    rnl = xml[rn_start:rn_end]
    rnl = patch("FROM planet_osm_line WHERE (!scale_denominator!",
                f"FROM planet_osm_line WHERE NOT COALESCE({OSM_DROP}, false) AND ((!scale_denominator!", "road names: drop replaced ways", rnl)
    rnl = patch(" ORDER BY (CASE WHEN highway IN ('motorway','trunk')", ") ORDER BY (CASE WHEN highway IN ('motorway','trunk')", "road names: close condition", rnl)
    xml = xml[:rn_start] + rnl + xml[rn_end:]

    KIND = (("CASE WHEN tran_class = 'Vehicular Track' AND COALESCE(on_osm_path, false) THEN 'none' " if LIST_PLUS else "CASE ") +
            "WHEN tran_class = 'Vehicular Track' AND COALESCE(on_osm_road, false) THEN 'road' "
            "WHEN tran_class = 'Vehicular Track' THEN 'vtrack' "
            "WHEN trans_type = 'Route' AND tran_class = 'Walking' THEN 'route' "
            "WHEN tran_class LIKE 'AS2156 Track Class 4%' THEN 'walk4' "
            "WHEN tran_class LIKE 'AS2156 Track Class 5%' OR tran_class LIKE 'AS2156 Track Class 6%' THEN 'walk5' "
            "WHEN trans_type = 'Track' THEN 'walk' ELSE 'road' END")
    SURF = ("CASE WHEN surface_ty = '4WD required' THEN 'nur 4WD' "
            "WHEN surface_ty = 'Unsealed' AND tran_class = 'Vehicular Track' AND NOT COALESCE(on_osm_road, false) THEN '' "
            "WHEN surface_ty = 'Unsealed' THEN 'unbefestigt' "
            "WHEN surface_ty = 'Sealed' AND tran_class = 'Vehicular Track' THEN 'asphaltiert' ELSE '' END")
    RESTR = "CASE WHEN user_type IN ('Restricted','Private') THEN 1 ELSE 0 END"
    LIST_SQL = one_line(f"""(SELECT way, {KIND} AS kind, {RESTR} AS restr, {SURF} AS surf, COALESCE(pri_name,'') AS name,
      trans_type, tran_class, CASE WHEN on_osm_track THEN 1 ELSE 0 END AS ontrack FROM list_transport) AS lt""")
    lines_kinds = ["walk", "walk4", "walk5", "route"] + (["vtrack"] if LIST in ("A", "C", "D") else [])
    DASH = {"walk": "1.1,2", "walk4": "1.1,2,1.1,6", "walk5": "1.1,6", "route": "1,8", "vtrack": None}
    ZOOMS = ([(12, 13, 0.7)] if EARLY else []) + [(14, 14, 0.9), (15, None, 1.0)]
    rules = []
    for kind in lines_kinds:
        for zf, zt, f in ZOOMS:
            lim = f"&maxscale_zoom{zf};" + (f"&minscale_zoom{zt};" if zt else "")
            w = (1.4 if kind == "vtrack" else 1.0) * f
            d = f' stroke-dasharray="{DASH[kind]}"' if DASH[kind] else ""
            for restr, col in ((0, "black"), (1, "black" if LIST_PLUS else GREY)):
                rules.append(f'<Rule>{lim}<Filter>[kind] = \'{kind}\' and [restr] = {restr}</Filter>'
                             f'<LineSymbolizer stroke="{col}" stroke-width="{w:.2f}"{d}/></Rule>')
    # unsealed roads: small grey dots inside the white road (instead of a label)
    dots = rules if not LIST_PLUS else []
    for zf, zt, f in ([(12, 13, 1.3)] if EARLY else []) + [(14, 14, 1.5), (15, None, 1.7)]:
        lim = f"&maxscale_zoom{zf};" + (f"&minscale_zoom{zt};" if zt else "")
        dots.append(f"<Rule>{lim}<Filter>[kind] = 'road' and [surf] = 'unbefestigt' and [ontrack] = 0</Filter>"
                     f'<LineSymbolizer stroke="#8a8a8a" stroke-width="{f:.2f}" stroke-linecap="round" '
                     f'stroke-dasharray="0.01,{3.2 * f:.1f}"/></Rule>')
    LIST_LINES = f'<Style name="own-list-lines">{"".join(rules)}</Style>'
    pre_insert = ""   # drawn before (below) OpenTopoMap's roads: bigger roads cover the ends of smaller ones
    if LIST_PLUS:
        # LIST "Access Road" (forest and farm access roads): thinner than OpenTopoMap's minor roads, dashed edge
        arules = []
        # zoom 12 (rule 11) and 13 (rule 12): thin black line like a track; from zoom 14 white with a black edge
        arules = ['<Rule>&maxscale_zoom11;&minscale_zoom12;<LineSymbolizer stroke="black" stroke-width="0.55"/></Rule>']
        for zf, zt, cw, fw in ((13, 13, 1.8, 1.0), (14, 14, 2.3, 1.45), (15, None, 2.8, 1.85)):
            lim = f"&maxscale_zoom{zf};" + (f"&minscale_zoom{zt};" if zt else "")
            arules.append(f'<Rule>{lim}<LineSymbolizer stroke="#1a1a1a" stroke-width="{cw}"/>'
                          f'<LineSymbolizer stroke="white" stroke-width="{fw}" stroke-linecap="round"/></Rule>')
        ACCESS_SQL = one_line(f"(SELECT way FROM planet_osm_line WHERE {ACCESS_SQL_COND}) AS accessroads")
        # minor roads at zoom 12 (rule 11, OpenTopoMap draws them only from rule 12): thin black like tracks
        MINOR_SQL = one_line("""(SELECT way FROM planet_osm_line WHERE highway IN ('unclassified','residential','road',
          'living_street','service') AND NOT COALESCE(list_class = 'Access Road', false)) AS minorroads""")
        pre_insert += ('<Style name="own-minor-z12"><Rule>&maxscale_zoom11;&minscale_zoom11;'
                       '<LineSymbolizer stroke="black" stroke-width="0.5"/></Rule></Style>\n'
                       + layer("own-minor-z12", ["own-minor-z12"], MINOR_SQL) + "\n"
                       + f'<Style name="own-access-roads">{"".join(arules)}</Style>' + "\n"
                       + layer("own-access-roads", ["own-access-roads"], ACCESS_SQL) + "\n"
                       + LIST_LINES + "\n" + layer("own-list-lines", ["own-list-lines"], LIST_SQL) + "\n")
        # dots: on LIST roads that are not access roads, without the first and last 20 m (no dots in the crossing road)
        DOTS_SQL = one_line(f"""(SELECT ST_LineSubstring(g, 27 / ST_Length(g), 1 - 27 / ST_Length(g)) AS way,
          'road' AS kind, 'unbefestigt' AS surf, 0 AS ontrack FROM (SELECT (ST_Dump(way)).geom AS g FROM list_transport
          WHERE trans_type = 'Road' AND surface_ty = 'Unsealed' AND tran_class NOT IN ('Vehicular Track', 'Access Road')) t
          WHERE ST_Length(g) > 60) AS listdots""")
        insert += ("\n" + f'<Style name="own-list-dots">{"".join(dots)}</Style>' + "\n"
                   + layer("own-list-dots", ["own-list-dots"], DOTS_SQL))
    else:
        insert += "\n" + LIST_LINES + "\n" + layer("own-list-lines", ["own-list-lines"], LIST_SQL)

    name_kinds = " or ".join(f"[kind] = '{k}'" for k in lines_kinds)
    LIST_LABELS = f"""<Style name="own-list-names">
<Rule>&maxscale_zoom{13 if EARLY else 14};<Filter>({name_kinds}) and [name] != ''</Filter>
<TextSymbolizer fontset-name="sans-condensed" size="9" fill="black" halo-radius="1.5" halo-fill="rgba(255,255,255,0.8)"
 repeat-distance="300" spacing="300" placement="line" max-char-angle-delta="40">[name]</TextSymbolizer></Rule>
</Style>
<Style name="own-list-surface">
<Rule>&maxscale_zoom{14 if LIST_PLUS else (13 if EARLY else 14)};<Filter>[surf] != '' and [surf] != 'unbefestigt'</Filter>
<TextSymbolizer fontset-name="sans-condensed-bold" size="8" fill="#5a3a00" halo-radius="1.2" halo-fill="rgba(255,255,255,0.85)"
 placement="line" spacing="280" repeat-distance="200" max-char-angle-delta="25" dy="-6">[surf]</TextSymbolizer></Rule>
</Style>"""
    LIST_TOP = LIST_LABELS + "\n" + layer("own-list-names", ["own-list-names"], LIST_SQL) + "\n" + \
        layer("own-list-surface", ["own-list-surface"], LIST_SQL) + "\n"
    if LIST_PLUS:
        # "privat" / "gesperrt" beside LIST roads and tracks without public access (instead of drawing them grey)
        PRIV_SQL = one_line("""(SELECT way, CASE user_type WHEN 'Private' THEN 'privat' ELSE 'gesperrt' END AS acc
          FROM list_transport WHERE user_type IN ('Private', 'Restricted') AND trans_type IN ('Road', 'Track')
          AND NOT COALESCE(on_osm_path, false)) AS private""")
        LIST_TOP += f"""<Style name="own-list-private">
<Rule>&maxscale_zoom14;
<TextSymbolizer fontset-name="sans-condensed-oblique" size="8" fill="#7a1f1f" halo-radius="1.2" halo-fill="rgba(255,255,255,0.85)"
 placement="line" spacing="320" repeat-distance="260" max-char-angle-delta="30" dy="7">[acc]</TextSymbolizer></Rule>
</Style>
""" + layer("own-list-private", ["own-list-private"], PRIV_SQL) + "\n"
    print(f"patched: LIST transport segments, variant {LIST}")
else:
    LIST_TOP = ""
    pre_insert = ""
end = xml.index("</Layer>", xml.index('<Layer name="roads">')) + len("</Layer>")
xml = xml[:end] + "\n" + insert + "\n" + xml[end:]
if LIST in ("A", "B", "C", "D") and pre_insert:
    rstart = xml.index('<Layer name="roads">')
    xml = xml[:rstart] + pre_insert + xml[rstart:]

# ---------------------------------------------------------------- LIST small streams (option LIST_HYDRO)
# LIST has many times more streams than OSM: small ones thin and light blue from zoom 14 (as on theLIST),
# indefinite ones dashed from zoom 15 (rule zoom = shown zoom - 1 with MAGNIFY 2). Below the roads.
if os.environ.get("LIST_HYDRO", "0") == "1":
    zs = 1   # all maps are rendered with MAGNIFY=2
    STREAMS = f"""<Style name="own-list-streams">
<Rule>&maxscale_zoom{14 - zs};&minscale_zoom{14 - zs};<Filter>[waterway] = 'list_minor'</Filter><LineSymbolizer stroke="#3d68f2" stroke-width="0.7"/></Rule>
<Rule>&maxscale_zoom{15 - zs};<Filter>[waterway] = 'list_minor'</Filter><LineSymbolizer stroke="#3d68f2" stroke-width="1"/></Rule>
<Rule>&maxscale_zoom{15 - zs};<Filter>[waterway] = 'list_indef'</Filter><LineSymbolizer stroke="#3d68f2" stroke-width="0.8" stroke-dasharray="3,2"/></Rule>
<Rule>&maxscale_zoom{15 - zs};<Filter>[name] != ''</Filter>
<TextSymbolizer fontset-name="sans-condensed-oblique" size="8" fill="#4a7fbf" halo-radius="1" halo-fill="rgba(255,255,255,0.7)"
 placement="line" spacing="400" repeat-distance="300" max-char-angle-delta="30" dy="5">[name]</TextSymbolizer></Rule>
</Style>
""" + layer("own-list-streams", ["own-list-streams"],
            "(SELECT way, waterway, COALESCE(name, '') AS name FROM planet_osm_line WHERE waterway IN ('list_minor', 'list_indef')) AS liststreams")
    # below the own road and track layers (if any), which are below OpenTopoMap's roads
    rstart = xml.find('<Style name="own-minor-z12">')
    if rstart < 0:
        rstart = xml.index('<Layer name="roads">')
    xml = xml[:rstart] + STREAMS + "\n" + xml[rstart:]
    print("patched: LIST small streams")
print("patched: faded-way layer")

# ---------------------------------------------------------------- public parking from zoom 14 (instead of 16)
sr_path = mapnik_dir / "styles-otm" / "symbols-road.xml"
sr, n = re.subn(r"&maxscale_zoom16;(\s*&minscale_zoom17;\s*<Filter>\(\[amenity\] = 'parking'\) and \(\[fee\])",
                r"&maxscale_zoom14;\1", sr_path.read_text())
if n != 2:
    sys.exit(f"style patch failed (parking zoom): {n} matches instead of 2")
sr_path.write_text(sr)
print("patched: public parking from zoom 14")

# ---------------------------------------------------------------- nature reserve borders: thin grey edges along the
# green band for contrast (on both sides, so it works whatever the direction of the outline)
ar_path = mapnik_dir / "styles-otm" / "areas.xml"
ar, n = re.subn(r'(<LineSymbolizer stroke="#85c243" stroke-width="5" stroke-opacity="0.5"/>)',
                r'<LineSymbolizer stroke="#6b6b6b" stroke-width="0.9" stroke-opacity="0.8" offset="2.9"/>'
                r'<LineSymbolizer stroke="#6b6b6b" stroke-width="0.9" stroke-opacity="0.8" offset="-2.9"/>\1', ar_path.read_text())
if n != 3:
    sys.exit(f"style patch failed (reserve borders): {n} matches instead of 3")
ar_path.write_text(ar)
print("patched: grey edges on nature reserve borders")

# ---------------------------------------------------------------- contour numbers without the light halo
ct_path = mapnik_dir / "styles-otm" / "contours.xml"
ct, n = re.subn(r'halo-radius="[0-9.]+"', 'halo-radius="0"', ct_path.read_text())
if n < 2:
    sys.exit(f"style patch failed (contour label halo): {n} matches")
ct_path.write_text(ct)
print(f"patched: contour numbers without halo ({n} rules)")

# ---------------------------------------------------------------- notable single trees (OSM natural=tree with name,
# landmark or natural monument) from zoom 12 instead of 14 (with MAGNIFY=2 they appear from zoom 13)
s2_path = mapnik_dir / "styles-otm" / "symbols-2.xml"
s2, n = re.subn(r"&maxscale_zoom14;(\s*&minscale_zoom17;\s*<Filter>\(\(\[natural\] = 'tree'\))", r"&maxscale_zoom12;\1", s2_path.read_text())
if n != 2:
    sys.exit(f"style patch failed (notable trees zoom): {n} matches instead of 2")
s2_path.write_text(s2)
print("patched: notable single trees from zoom 12")

# ---------------------------------------------------------------- trail and minor road names from zoom 14
# (OpenTopoMap: from zoom 16). "spacing" repeats the name along long ways.
rn_path = mapnik_dir / "styles-otm" / "road-names-text.xml"
rn = rn_path.read_text()
TRAILS = " or ".join(f"[highway] = '{h}'" for h in (
    "path", "footway", "bridleway", "track", "cycleway", "steps",
    "tertiary", "tertiary_link", "unclassified", "residential", "service", "living_street", "road"))
trail_rules = "".join(
    f"<Rule>&maxscale_zoom{z};&minscale_zoom{z};<Filter>{TRAILS}</Filter>"
    f'<TextSymbolizer fontset-name="sans-condensed" size="{size}" fill="black" halo-radius="1.5" halo-fill="rgba(255,255,255,0.8)" '
    f'repeat-distance="{rep}" spacing="{rep}" placement="line" max-char-angle-delta="40">[name]</TextSymbolizer></Rule>'
    for z, size, rep in (((13, 8, 350),) if EARLY else ()) + ((14, 9, 300), (15, 10, 220)))
if "<!-- zoom 15 -->" not in rn:
    sys.exit("style patch failed (trail names): original text not found")
rn_path.write_text(rn.replace("<!-- zoom 15 -->", trail_rules + "\n\t<!-- zoom 15 -->", 1))
print("patched: trail names from zoom 14")

# ---------------------------------------------------------------- forest: only green, no tree symbols
for name in ("landuse.xml", "landuse-over-hillshade.xml"):
    fp = mapnik_dir / "styles-otm" / name
    # the forest patterns contain the green background: replace them with a plain fill of the same colour
    # (landuse.xml); the faint second pattern over the hillshade is dropped
    plain = '<PolygonSymbolizer fill="#b7d97b" />' if name == "landuse.xml" else ""
    t, n = re.subn(r'<PolygonPatternSymbolizer file="symbols-otm/forest[^"]*"[^>]*/>', plain, fp.read_text())
    if n != 3:
        sys.exit(f"style patch failed (forest symbols in {name}): {n} instead of 3")
    fp.write_text(t)
print("patched: forest without tree symbols")

# ---------------------------------------------------------------- TASVEG vegetation instead of OSM forest (option)
# class A "Waldgefühl" (render/tasveg/klassen_waldtypen.csv), colours agreed with the user
TASVEG_COLOURS = {1: "#4F9A57",   # rainforest
                  2: "#82BC6A",   # wet forest over rainforest
                  3: "#B6D778",   # other wet forest
                  4: "#D6C468",   # dry eucalypt forest (light yellow-ochre)
                  5: "#9DBF57",   # other forest / swamp forest (like other wet forest, darker)
                  6: "#EDE8D2",   # open land / highland
                  7: "#F4F0E6",   # farmland (F codes except plantations and urban)
                  8: "#DCDDC6"}   # plantations
if TASVEG:
    # OSM forest, wood and scrub are no longer drawn (all other land use, water etc. stay)
    removed = 0
    for name in ("landuse.xml", "landuse-over-hillshade.xml"):
        fp = mapnik_dir / "styles-otm" / name
        t = fp.read_text()

        def drop(m):
            global removed
            f = re.search(r"<Filter>(.*?)</Filter>", m.group(0), re.S)
            if f and ("'forest'" in f.group(1) or "[natural] = 'scrub'" in f.group(1)):
                removed += 1
                return ""
            return m.group(0)
        fp.write_text(re.sub(r"<Rule>.*?</Rule>", drop, t, flags=re.S))
    lz = mapnik_dir / "styles-otm" / "landuse-lowzoom.xml"   # low zoom table holds forest only
    t, n = re.subn(r'<PolygonSymbolizer fill="#b9d984" />', "", lz.read_text())
    if removed < 8 or n != 1:
        sys.exit(f"style patch failed (OSM forest off): {removed} rules, {n} low zoom")
    lz.write_text(t)
    print(f"patched: OSM forest/scrub off ({removed} rules)")

    def fills(zfrom, zto):
        lim = f"&maxscale_zoom{zfrom};" + (f"&minscale_zoom{zto};" if zto else "")
        return "".join(f'<Rule>{lim}<Filter>[cls_a] = {k}</Filter><PolygonSymbolizer fill="{c}" gamma="0.5"/></Rule>'
                       for k, c in TASVEG_COLOURS.items())

    TV_STYLE = f"""<Style name="own-tasveg-low">{fills(5, 11)}</Style>
<Style name="own-tasveg">{fills(12, None)}
<Rule>&maxscale_zoom12;<Filter>[forest_str] = 'Woodland'</Filter>
<PolygonPatternSymbolizer file="symbols-otm/tasveg_woodland_hatch.svg" alignment="global"/></Rule>
</Style>
<Style name="own-tasveg-trees">
<Rule>&maxscale_zoom15;<PointSymbolizer file="symbols-otm/tasveg_notable_tree.svg" allow-overlap="true"/></Rule>
</Style>"""
    tv_layers = "\n".join([
        TV_STYLE,
        layer("own-tasveg-low", ["own-tasveg-low"], "(SELECT way, cls_a FROM tasveg_low) AS tv"),
        layer("own-tasveg", ["own-tasveg"], "(SELECT way, cls_a, forest_str FROM tasveg) AS tv"),
    ])
    # above OSM land use, below the hillshade and everything else
    xml = patch("\n\t&hillshade;", "\n" + tv_layers + "\n\t&hillshade;", "TASVEG vegetation layer")
    trees = layer("own-tasveg-trees", ["own-tasveg-trees"], "(SELECT way, area, notable_tr FROM tasveg_notable) AS tvt")
    xml = patch("</Map>", trees + "\n</Map>", "TASVEG notable trees")

# ---------------------------------------------------------------- own points: giant trees, walks, waterfalls (render/pois/tasmanien_baeume.csv)
# CSV columns: Art,Name,Breite,Laenge. Art = Riesenbaum (golden star), Triangle (red triangle), Wasserfall (blue waterfall).
# All three types: symbol from zoom 9, name from zoom 12 (with MAGNIFY=2 shown from 10 and 13)
import csv as _csv
trees_out = mapnik_dir / "own_big_trees.csv"
POI_ART = {  # Art -> (symbol, label colour, label offset)
    "Riesenbaum": ("big_tree_star.svg", "#5a3d00", 11),
    "Triangle": ("triangle_red.svg", "#7a0c0c", 11),
    "Wasserfall": ("waterfall_blue.svg", "#0b3d7a", 13),
}
with open(here / "pois" / "tasmanien_baeume.csv", encoding="utf-8-sig", newline="") as f_in, open(trees_out, "w", newline="", encoding="utf-8") as f_out:
    w = _csv.writer(f_out)
    w.writerow(["name", "lat", "lon", "art"])
    for r in _csv.DictReader(f_in):
        art = r["Art"].strip()
        if art not in POI_ART:
            raise SystemExit(f"tasmanien_baeume.csv: unknown Art {art!r} (allowed: {', '.join(POI_ART)})")
        w.writerow([r["Name"].strip(), float(r["Breite"]), float(r["Laenge"]), art])
_poi_rules = "\n".join(
    f"""<Rule>&maxscale_zoom8;<Filter>[art] = '{art}'</Filter><PointSymbolizer file="symbols-otm/{sym}" allow-overlap="true"/></Rule>
<Rule>&maxscale_zoom12;<Filter>[art] = '{art}'</Filter><TextSymbolizer fontset-name="sans-condensed-bold" size="9" fill="{col}" halo-radius="1.5" halo-fill="rgba(255,255,255,0.85)"
 dy="{dy}" wrap-width="80" allow-overlap="false">[name]</TextSymbolizer></Rule>"""
    for art, (sym, col, dy) in POI_ART.items())
BIG_TREES = f"""<Style name="own-big-trees">
{_poi_rules}
</Style>
<Layer name="own-big-trees" srs="+proj=longlat +datum=WGS84 +no_defs"><StyleName>own-big-trees</StyleName><Datasource>
<Parameter name="type">csv</Parameter><Parameter name="file">{trees_out}</Parameter></Datasource></Layer>
"""
# with the walks overlay the giant trees and camp sites are drawn there (switchable), not in the map
POIS_IN_OVERLAY = "walks" in os.environ.get("OVERLAYS", "").split()
if not POIS_IN_OVERLAY:
    xml = patch("</Map>", BIG_TREES + "</Map>", "giant trees, walks, waterfalls (stars, triangles, waterfalls)")

# ---------------------------------------------------------------- 5+6) camp sites, locked gates (on top)
# !bbox! inside both parts: otherwise every tile computes the points of all camp site areas of the region
CAMPS_SQL = one_line("""(SELECT way, name FROM planet_osm_point WHERE tourism = 'camp_site' AND way && !bbox!
  UNION ALL SELECT ST_PointOnSurface(way) AS way, name FROM planet_osm_polygon
  WHERE tourism = 'camp_site' AND way && !bbox!) AS camps""")
GATES_SQL = one_line("""(SELECT way FROM planet_osm_point
  WHERE barrier IN ('gate','lift_gate','swing_gate','chain','bar') AND locked = 'yes') AS lockedgates""")
CAMPS_STYLE = """<Style name="own-camps">
<Rule>&maxscale_zoom11;&minscale_zoom12;<PointSymbolizer file="symbols-otm/camp_highlight.svg" transform="scale(0.75)" allow-overlap="true"/></Rule>
<Rule>&maxscale_zoom13;<PointSymbolizer file="symbols-otm/camp_highlight.svg" allow-overlap="true"/>
<TextSymbolizer fontset-name="sans-condensed-bold" size="10" fill="#1b7f3b" halo-radius="1.5" halo-fill="rgba(255,255,255,0.9)" dy="14" wrap-width="90">[name]</TextSymbolizer></Rule>
</Style>
"""
TOP = f"""{"" if POIS_IN_OVERLAY else CAMPS_STYLE}<Style name="own-gates">
<Rule>&maxscale_zoom13;&minscale_zoom14;<PointSymbolizer file="symbols-otm/gate_locked.svg" transform="scale(0.8)" allow-overlap="true"/></Rule>
<Rule>&maxscale_zoom15;<PointSymbolizer file="symbols-otm/gate_locked.svg" allow-overlap="true"/></Rule>
</Style>
{"" if POIS_IN_OVERLAY else layer("own-camps", ["own-camps"], CAMPS_SQL)}
{layer("own-gates", ["own-gates"], GATES_SQL)}
{layer("own-4wd", ["own-4wd"], FOURWD_SQL) if not LIST else ""}
{layer("own-invisible", ["own-invisible"], INVISIBLE_SQL)}
{LIST_TOP}"""
xml = patch("</Map>", TOP + "</Map>", "camp sites and locked gates on top")

xml_path.write_text(xml)

# ---------------------------------------------------------------- overlays (transparent MBTiles, MBTiles type=overlay)
OVERLAY_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE Map [
	<!ENTITY % entities SYSTEM "inc/entities.xml.inc">
	%entities;
]>
<Map srs="+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +no_defs +over" buffer-size="256">
&fontset-settings;
"""

# walks: all walking tracks highlighted; 60 Great Short Walks and big tree hikes in their own colours, with labels
if LIST in ("A", "B", "C", "D"):
    WALK_SQL = one_line(f"""(SELECT way FROM list_transport WHERE trans_type = 'Track' OR (trans_type = 'Route' AND tran_class = 'Walking')
      UNION ALL SELECT way FROM planet_osm_line WHERE {PATHLIKE} AND NOT COALESCE(list_dup, false)) AS walks""")
else:
    WALK_SQL = one_line(f"(SELECT way FROM planet_osm_line WHERE {PATHLIKE}) AS walks")
GSW_SQL = one_line("""(SELECT way, nr, name, nr || ' ' || name || '  · Grade ' || level || ' · ' || time || ' · ' || distance AS label FROM gsw_tracks) AS gsw""")
GSW_START_SQL = one_line("""(SELECT DISTINCT ON (nr) ST_StartPoint(ST_GeometryN(way, 1)) AS way, nr, name,
  'Grade ' || level || ' · ' || time || ' · ' || distance AS info FROM gsw_tracks ORDER BY nr, ST_Length(ST_GeometryN(way, 1)) DESC) AS gsws""")
BTH_SQL = "(SELECT way, name FROM bigtree_hikes) AS bth"
GSW_COL, BTH_COL, WALK_COL = "#e8590c", "#7b2fa8", "#d6336c"
walks_xml = OVERLAY_HEAD + f"""
<Style name="ov-walks">
<Rule>&maxscale_zoom10;&minscale_zoom12;<LineSymbolizer stroke="{WALK_COL}" stroke-width="2.5" stroke-opacity="0.45" stroke-linecap="round"/></Rule>
<Rule>&maxscale_zoom13;<LineSymbolizer stroke="{WALK_COL}" stroke-width="4" stroke-opacity="0.4" stroke-linecap="round"/></Rule>
</Style>
<Style name="ov-bth">
<Rule>&maxscale_zoom9;<LineSymbolizer stroke="{BTH_COL}" stroke-width="5" stroke-opacity="0.75" stroke-linecap="round" stroke-linejoin="round"/></Rule>
<Rule>&maxscale_zoom12;<TextSymbolizer fontset-name="sans-condensed-bold" size="9" fill="{BTH_COL}" halo-radius="1.5" halo-fill="rgba(255,255,255,0.9)"
 placement="line" spacing="400" repeat-distance="300" max-char-angle-delta="40" dy="9">[name]</TextSymbolizer></Rule>
</Style>
<Style name="ov-gsw">
<Rule>&maxscale_zoom9;<LineSymbolizer stroke="{GSW_COL}" stroke-width="5" stroke-opacity="0.8" stroke-linecap="round" stroke-linejoin="round"/></Rule>
</Style>
<Style name="ov-gsw-start">
<Rule>&maxscale_zoom8;&minscale_zoom11;<MarkersSymbolizer fill="{GSW_COL}" stroke="#ffffff" stroke-width="1.5" width="14" height="14" allow-overlap="true"/>
<TextSymbolizer fontset-name="sans-condensed-bold" size="8" fill="#ffffff" allow-overlap="true">[nr]</TextSymbolizer></Rule>
<Rule>&maxscale_zoom12;<MarkersSymbolizer fill="{GSW_COL}" stroke="#ffffff" stroke-width="1.5" width="16" height="16" allow-overlap="true"/>
<TextSymbolizer fontset-name="sans-condensed-bold" size="9" fill="#ffffff" allow-overlap="true">[nr]</TextSymbolizer>
<TextSymbolizer fontset-name="sans-condensed-bold" size="9" fill="{GSW_COL}" halo-radius="1.5" halo-fill="rgba(255,255,255,0.9)"
 placement-type="simple" placements="E,W,S,N" dx="11" dy="0" horizontal-alignment="right" justify-alignment="left"
 wrap-width="140">[name] + '&#10;' + [info]</TextSymbolizer></Rule>
</Style>
{layer("ov-walks", ["ov-walks"], WALK_SQL)}
{layer("ov-bth", ["ov-bth"], BTH_SQL)}
{layer("ov-gsw", ["ov-gsw"], GSW_SQL)}
{layer("ov-gsw-start", ["ov-gsw-start"], GSW_START_SQL)}
{CAMPS_STYLE + layer("own-camps", ["own-camps"], CAMPS_SQL) if POIS_IN_OVERLAY else ""}
{BIG_TREES if POIS_IN_OVERLAY else ""}
</Map>
"""
(mapnik_dir / "overlay-walks.xml").write_text(walks_xml)
print("written: overlay-walks.xml")

# walkability from the own TASVEG classification (Einteilung B): hatching
if TASVEG:
    # The hatching is made as real lines (not a pattern image: pattern tiles left gaps and dashes at their
    # edges): per map tile, parallel 45° lines in map coordinates (spacing in output pixels, globally aligned,
    # so they join across tiles), cut to the TASVEG areas of each walkability class.
    HATCH_SQL = one_line("""(WITH b AS (SELECT cls_b, ST_Union(way) AS way FROM tasveg WHERE cls_b >= 2 AND way && !bbox! GROUP BY cls_b),
      p AS (SELECT cls_b, way, (CASE cls_b WHEN 2 THEN 60 WHEN 3 THEN 26 ELSE 20 END) * GREATEST(!pixel_width!, 0.01) AS s,
            ST_XMin(!bbox!) AS x0, ST_YMin(!bbox!) AS y0, ST_XMax(!bbox!) AS x1, ST_YMax(!bbox!) AS y1 FROM b),
      l AS (SELECT cls_b, way, ST_MakeLine(ST_MakePoint(x0, x0 + k * s), ST_MakePoint(x1, x1 + k * s)) AS g
              FROM p, generate_series(floor((y0 - x1) / s)::bigint, ceil((y1 - x0) / s)::bigint) AS k
            UNION ALL
            SELECT cls_b, way, ST_MakeLine(ST_MakePoint(x0, k * s - x0), ST_MakePoint(x1, k * s - x1))
              FROM p, generate_series(floor((y0 + x0) / s)::bigint, ceil((y1 + x1) / s)::bigint) AS k WHERE cls_b = 4)
      SELECT cls_b, ST_Intersection(way, ST_SetSRID(ST_Collect(g), 3857)) AS way FROM l GROUP BY cls_b, way) AS wb""")
    walkability_xml = OVERLAY_HEAD + """
<Style name="ov-walkability">
<Rule>&maxscale_zoom10;<Filter>[cls_b] = 2</Filter><LineSymbolizer stroke="#4a3a20" stroke-opacity="0.5" stroke-width="0.6"/></Rule>
<Rule>&maxscale_zoom10;<Filter>[cls_b] = 3</Filter><LineSymbolizer stroke="#4a3a20" stroke-opacity="0.5" stroke-width="0.6"/></Rule>
<Rule>&maxscale_zoom10;<Filter>[cls_b] = 4</Filter><LineSymbolizer stroke="#6a2020" stroke-opacity="0.5" stroke-width="0.55"/></Rule>
</Style>
""" + layer("ov-walkability", ["ov-walkability"], HATCH_SQL) + "\n</Map>\n"
    (mapnik_dir / "overlay-walkability.xml").write_text(walkability_xml)
    print("written: overlay-walkability.xml")

# ---------------------------------------------------------------- access overlay from LIST land tenure (OVERLAYS contains "tenure")
# Colour per tenure class (where camping is allowed), opaque (the app sets the opacity of each overlay), labels "500 m" / "Reserve" on a grid all over the areas.
# LIST Land Tenure (theLIST, layer 26, (c) State of Tasmania, CC BY 3.0 AU)
if "tenure" in os.environ.get("OVERLAYS", "").split():
    TENURE = {  # TEN_CLASS -> (colour, label)
        "Permanent Timber Production Zone Land": ("#1a9850", ""), "Public Reserve": ("#1a9850", ""),
        "Conservation Area": ("#a6d96a", "500 m"), "Regional Reserve": ("#a6d96a", "500 m"),
        "Nature Recreation Area": ("#a6d96a", "500 m"), "Game Reserve": ("#a6d96a", "500 m"),
        "HEC Conservation Area": ("#a6d96a", "500 m"), "State Reserve": ("#a6d96a", "500 m"),
        "Nature Reserve": ("#a6d96a", "500 m"), "Historic Site": ("#a6d96a", "500 m"),
        "National Park": ("#7b3294", "500 m"),
        "Local Government": ("#fdae61", ""), "Local Government Act Reserve": ("#fdae61", ""), "LGA Conservation Area": ("#fdae61", ""),
        "Private Sanctuary": ("#d73027", "Reserve"), "Private Nature Reserve": ("#d73027", "Reserve"),
        "Conservation Covenant": ("#d73027", "Reserve"), "Private Freehold": ("#d73027", ""),
        "Authority Freehold": ("#d73027", ""), "Tas Water": ("#d73027", ""), "Tas Irrigation": ("#d73027", ""),
    }  # all other classes grey (#9e9e9e), "Inland Water" not drawn
    q = lambda v: "'" + v.replace("'", "''") + "'"
    col_case = "CASE ten_class " + " ".join(f"WHEN {q(k)} THEN {q(c)}" for k, (c, _) in TENURE.items()) + " ELSE '#9e9e9e' END"
    # boundary lines a little darker than the fill, so neighbouring areas of the same class stay apart
    dark = lambda c: "#" + "".join(f"{int(int(c[i:i + 2], 16) * 0.72):02x}" for i in (1, 3, 5))
    line_case = "CASE ten_class " + " ".join(f"WHEN {q(k)} THEN {q(dark(c))}" for k, (c, _) in TENURE.items()) + f" ELSE {q(dark('#9e9e9e'))} END"
    lbl_case = "CASE ten_class " + " ".join(f"WHEN {q(k)} THEN {q(l)}" for k, (c, l) in TENURE.items() if l) + " ELSE '' END"
    # cut to the tile (with margin) and simplified to half a pixel: the large parks have hundreds of thousands of points
    TEN_SQL = one_line(f"""(SELECT {col_case} AS col, {line_case} AS line,
      ST_SimplifyPreserveTopology(ST_ClipByBox2D(way, ST_Expand(!bbox!, 64 * !pixel_width!)), !pixel_width! / 2) AS way
      FROM list_tenure WHERE ten_class <> 'Inland Water' AND way && !bbox!) AS ten""")
    # labels on a global grid (every TEN_GRID output pixels, the same points in neighbouring tiles, so labels
    # cut at a tile edge continue in the next tile); only where the whole label fits into the area
    # 3 x 3 labels per 512 px tile; the grid starts at a tile corner, so tiles inside large areas are identical
    # (stored once in the MBTiles)
    TEN_GRID, TEN_HALF_W, TEN_HALF_H = 512 / 3, 62, 22
    TEN_LBL_SQL = one_line(f"""(WITH t AS (SELECT {lbl_case} AS lbl, way FROM list_tenure WHERE way && !bbox!),
      g AS (SELECT ST_Centroid((ST_SquareGrid({TEN_GRID} * !pixel_width!, ST_Expand(!bbox!, {TEN_GRID} * !pixel_width!))).geom) AS pt)
      SELECT t.lbl, g.pt AS way FROM g JOIN t ON t.lbl <> '' AND ST_Intersects(t.way, g.pt)
       WHERE ST_Within(ST_Expand(g.pt, {TEN_HALF_W} * !pixel_width!, {TEN_HALF_H} * !pixel_width!), t.way)) AS tenl""")
    tenure_xml = OVERLAY_HEAD + f"""
<Style name="ov-tenure">
<Rule><PolygonSymbolizer fill="[col]"/><LineSymbolizer stroke="[line]" stroke-width="0.4"/></Rule>
</Style>
<Style name="ov-tenure-labels">
<Rule>&maxscale_zoom9;<TextSymbolizer fontset-name="sans-condensed-bold" size="9" fill="#ffffff" halo-radius="1.6" halo-fill="rgba(30,30,30,0.85)"
 allow-overlap="true" avoid-edges="false" placement="point">[lbl]</TextSymbolizer></Rule>
</Style>
{layer("ov-tenure", ["ov-tenure"], TEN_SQL)}
{layer("ov-tenure-labels", ["ov-tenure-labels"], TEN_LBL_SQL)}
</Map>
"""
    (mapnik_dir / "overlay-tenure.xml").write_text(tenure_xml)
    print("written: overlay-tenure.xml")
