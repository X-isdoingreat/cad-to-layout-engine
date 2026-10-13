"""Deterministic tools an LLM agent uses to read a drawing, like a person zooming and measuring.

The tools never decide what a building is; they list, draw and measure.  The agent
decides, and the program later re-measures every number it reports from the faces
it cites.

  py -3.11 -X utf8 -m cad_to_layout.zero_manual.agent_tools <command> [...]   (run inside the work directory)

  index <dxf>                         build index.sqlite once (later commands read it window by window)
  overview                            units, extent, layer table, most frequent texts
  texts [--match REGEX] [--window x1 y1 x2 y2] [--limit N]
  window x1 y1 x2 y2 [--layers L ...] [--limit N]
  render x1 y1 x2 y2 out.png [--layers L ...] [--faces F ...] [--no-text]
  faces x1 y1 x2 y2 [--layers L ...] [--gap METRES] [--min-area M2]
  measure F [F ...]                   union of faces: area, length, width, orientation, outline
  columns x1 y1 x2 y2 [--layers L ...] [--max-size METRES]
  envelope x1 y1 x2 y2 [--layers L ...] [--close-gaps-m 6]   outer outline of walls, door openings closed
  column_instances x1 y1 x2 y2 --groups G [G ...] [--faces F ...]   count chosen symbol groups fully, grid check inside faces
  dimensions x1 y1 x2 y2              linear dimensions drawn on the drawing, longest first
  zones x1 y1 x2 y2 [--layers L ...] [--match REGEX]   area names, each with the smallest closed face around it
  corners x1 y1 x2 y2 x3 y3 ... [--layers L ...] [--snap-m 1]   trace a building by its outer corners (snapped to the walls)
  elevations x1 y1 x2 y2 [--near-m 5]  elevation numbers with the words beside them and the nearest section/elevation title
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

INDEX = Path("index.sqlite")
FACES = Path("faces.json")
COLUMNS = Path("columns.json")
UNIT_M = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}


# ---------------------------------------------------------------- index
# The index is a small SQLite database with a spatial (R-tree) table: a tool reads only the
# lines inside the window it is asked about, so a drawing with millions of lines is never
# loaded whole into memory.  Lines are written in batches while the drawing is walked.
def build_index(dxf: str, path: Path = INDEX) -> dict[str, Any]:
    import sqlite3

    import ezdxf

    from .buildings import _polyline_points, _style, _text_value, _text_centre
    from .expand import iter_world_items, transformed_point

    doc = ezdxf.readfile(dxf)
    path = Path(path)
    close_all()
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE lines(id INTEGER PRIMARY KEY, h TEXT, l TEXT, k TEXT, c INTEGER, lt TEXT, w REAL, p TEXT, size REAL);"
        "CREATE VIRTUAL TABLE box USING rtree(id, minx, maxx, miny, maxy);"
        "CREATE TABLE texts(h TEXT, t TEXT, x REAL, y REAL, l TEXT);"
        "CREATE TABLE dims(h TEXT, l TEXT, x1 REAL, y1 REAL, x2 REAL, y2 REAL, length REAL, shown TEXT);"
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
    layers: Counter = Counter()
    layer_kinds: dict[str, Counter] = defaultdict(Counter)
    ext = [math.inf, math.inf, -math.inf, -math.inf]
    rows: list[tuple] = []
    boxes: list[tuple] = []
    texts: list[tuple] = []
    dims: list[tuple] = []
    n = 0

    def flush() -> None:
        con.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO box VALUES (?,?,?,?,?)", boxes)
        con.executemany("INSERT INTO texts VALUES (?,?,?,?,?)", texts)
        con.executemany("INSERT INTO dims VALUES (?,?,?,?,?,?,?,?)", dims)
        rows.clear(); boxes.clear(); texts.clear(); dims.clear()

    for item in iter_world_items(doc):
        entity, kind = item.entity, item.dxftype
        layers[item.layer] += 1
        layer_kinds[item.layer][kind] += 1
        if kind in {"TEXT", "MTEXT", "ATTRIB"}:
            value = _text_value(entity)
            centre = _text_centre(entity, item.matrix) if value else None
            if centre is not None:
                texts.append((item.ref, value[:80], round(centre[0], 3), round(centre[1], 3), item.layer))
            continue
        if kind == "DIMENSION":
            got_dim = _linear_dimension(entity, item.matrix, transformed_point)
            if got_dim is not None:
                dims.append((item.ref, item.layer, *got_dim))
            continue
        lt, w = "", 0.0
        if kind == "CIRCLE":
            try:
                c = transformed_point(item.matrix, entity.dxf.center)
                edge = transformed_point(item.matrix, (entity.dxf.center[0] + entity.dxf.radius, entity.dxf.center[1], 0))
                r = math.dist(c, edge)
                pts, curved = [(c[0] + r * math.cos(a / 8 * math.pi), c[1] + r * math.sin(a / 8 * math.pi)) for a in range(17)], True
            except Exception:
                continue
        else:
            got = _polyline_points(entity, item.matrix)
            if got is None or len(got[0]) < 2:
                continue
            pts, closed, curved = got
            if closed:
                pts = pts + [pts[0]]
            style = _style(doc, entity, item.layer)
            lt, w = style["linetype"], style["polyline_width"] or style["lineweight_mm"] or 0
        pts = [(round(x, 3), round(y, 3)) for x, y in pts]
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        n += 1
        rows.append((n, item.ref, item.layer, kind, int(bool(curved)), lt, float(w or 0), json.dumps(pts), (max(xs) - min(xs)) + (max(ys) - min(ys))))
        boxes.append((n, min(xs), max(xs), min(ys), max(ys)))
        ext = [min(ext[0], min(xs)), min(ext[1], min(ys)), max(ext[2], max(xs)), max(ext[3], max(ys))]
        if len(rows) >= 20000:
            flush()
    flush()
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    styles = {layer.dxf.name: {"linetype": str(layer.dxf.get("linetype", "")), "colour": abs(int(layer.dxf.get("color", 7)))} for layer in doc.layers}
    meta = {"dxf": str(dxf), "insunits": insunits, "metres_per_unit": UNIT_M.get(insunits), "extent": ext if n else None,
            "layers": dict(layers), "layer_kinds": {k: dict(v) for k, v in layer_kinds.items()}, "layer_styles": styles}
    con.executemany("INSERT INTO meta VALUES (?,?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    con.commit()
    count_texts = con.execute("SELECT COUNT(*) FROM texts").fetchone()[0]
    con.close()
    return {"lines": n, "texts": count_texts, "layers": len(layers), "insunits": insunits}


def _linear_dimension(entity: Any, matrix: Any, transformed_point: Any) -> tuple | None:
    """A linear or aligned dimension in drawing coordinates: its two measured points, the measured length, the text shown."""

    try:
        kind = int(entity.dxf.get("dimtype", 0)) & 7
        if kind not in (0, 1):
            return None
        a = transformed_point(matrix, entity.dxf.defpoint2)
        b = transformed_point(matrix, entity.dxf.defpoint3)
        if kind == 0:  # rotated: measured along the dimension direction
            angle = math.radians(float(entity.dxf.get("angle", 0.0)))
            o = transformed_point(matrix, (0, 0, 0))
            d = transformed_point(matrix, (math.cos(angle), math.sin(angle), 0))
            ux, uy = d[0] - o[0], d[1] - o[1]
            norm = math.hypot(ux, uy) or 1.0
            length = abs((b[0] - a[0]) * ux + (b[1] - a[1]) * uy) / norm
        else:
            length = math.dist(a, b)
        shown = str(entity.dxf.get("text", "") or "")
        return (round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3), round(length, 3), shown[:40])
    except Exception:
        return None


class Index:
    """Drawing metadata and texts in memory; lines read window by window from the database."""

    _WHERE = "b.maxx >= ? AND b.minx <= ? AND b.maxy >= ? AND b.miny <= ?"

    def __init__(self, path: Path = INDEX) -> None:
        import sqlite3

        if not Path(path).is_file():
            raise SystemExit(f"{path} not found: run `index <dxf>` first")
        self.con = sqlite3.connect(path)
        self.meta = {k: json.loads(v) for k, v in self.con.execute("SELECT k, v FROM meta")}
        self.texts = [{"h": h, "t": t, "x": x, "y": y, "l": l} for h, t, x, y, l in self.con.execute("SELECT h, t, x, y, l FROM texts")]
        try:
            self.dims = [{"h": h, "l": l, "a": (x1, y1), "b": (x2, y2), "length": length, "shown": shown}
                         for h, l, x1, y1, x2, y2, length, shown in self.con.execute("SELECT h, l, x1, y1, x2, y2, length, shown FROM dims")]
        except sqlite3.OperationalError:  # an index built before dimensions were kept
            self.dims = []

    def __getitem__(self, key: str) -> Any:
        return self.texts if key == "texts" else self.meta[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.meta.get(key, default)

    def _query(self, head: str, window: list[float], layers: list[str] | None) -> tuple[str, list[Any]]:
        sql = f"SELECT {head} FROM box b JOIN lines l ON l.id = b.id WHERE {self._WHERE}"
        params: list[Any] = [window[0], window[2], window[1], window[3]]
        if layers:
            sql += " AND l.l IN (%s)" % ",".join("?" * len(layers))
            params += list(layers)
        return sql, params

    def select(self, window: list[float], layers: list[str] | None = None, limit: int | None = None,
               max_extent: float | None = None) -> list[dict[str, Any]]:
        sql, params = self._query("l.h, l.l, l.k, l.c, l.lt, l.w, l.p", window, layers)
        if max_extent is not None:  # width + height of the line's box, in drawing units: only small pieces are read
            sql += " AND l.size <= ?"
            params.append(max_extent)
        if limit:
            sql += " ORDER BY l.size DESC LIMIT ?"  # the largest lines first: outlines before hatching and small symbols
            params.append(limit)
        return [{"h": h, "l": l, "k": k, "c": bool(c), "lt": lt, "w": w, "p": [tuple(q) for q in json.loads(p)]}
                for h, l, k, c, lt, w, p in self.con.execute(sql, params)]

    def count(self, window: list[float], layers: list[str] | None = None) -> int:
        sql, params = self._query("COUNT(*)", window, layers)
        return self.con.execute(sql, params).fetchone()[0]


_OPEN: dict[str, Index] = {}


def _load() -> Index:
    key = str(INDEX.resolve())
    if key not in _OPEN:
        _OPEN[key] = Index()
    return _OPEN[key]


def close_all() -> None:
    """Close the open indexes (a new `index` replaces the file; Windows will not delete an open file)."""

    for idx in _OPEN.values():
        idx.con.close()
    _OPEN.clear()


def _m(idx: Any, override: float | None) -> float:
    m = override or idx.get("metres_per_unit")
    if not m:
        raise SystemExit("drawing unit unknown: pass --unit-m (metres per drawing unit) after deciding it from dimensions or notes")
    return m


RENDER_LIMIT = 150000  # lines drawn per picture at most; beyond this only the largest are drawn
FACES_MAX_LINES = 60000  # faces, around and zones refuse bigger windows (memory)


def _select(idx: Index, window: list[float], layers: list[str] | None, limit: int | None = None) -> list[dict[str, Any]]:
    return idx.select(window, layers, limit)


def _mrr(polygon: Any) -> tuple[float, float, float]:
    corners = list(polygon.minimum_rotated_rectangle.exterior.coords)
    a, b = math.dist(corners[0], corners[1]), math.dist(corners[1], corners[2])
    i = 0 if a >= b else 1
    angle = math.degrees(math.atan2(corners[i + 1][1] - corners[i][1], corners[i + 1][0] - corners[i][0])) % 180.0
    return max(a, b), min(a, b), angle


# ---------------------------------------------------------------- commands
def cmd_overview(args: Any) -> dict[str, Any]:
    idx = _load()
    counts = Counter(t["t"] for t in idx["texts"])
    short = [t for t in idx["texts"] if len(t["t"]) <= 30]
    return {"insunits": idx["insunits"], "metres_per_unit_from_header": idx["metres_per_unit"], "extent": idx["extent"],
            "layers": sorted([{"layer": k, "objects": v, "kinds": idx["layer_kinds"].get(k), **idx["layer_styles"].get(k, {})} for k, v in idx["layers"].items()],
                             key=lambda r: -r["objects"])[:80],
            "texts_total": len(idx["texts"]), "most_frequent_texts": counts.most_common(40),
            "sample_texts": [[t["t"], t["x"], t["y"], t["l"]] for t in sorted(short, key=lambda t: (-len(t["t"]), t["t"]))[:150]]}


def cmd_texts(args: Any) -> dict[str, Any]:
    idx = _load()
    rx = re.compile(args.match) if args.match else None
    out = [[t["t"], t["x"], t["y"], t["l"], t["h"]] for t in idx["texts"]
           if (rx is None or rx.search(t["t"])) and (not args.window or (args.window[0] <= t["x"] <= args.window[2] and args.window[1] <= t["y"] <= args.window[3]))]
    return {"count": len(out), "texts": out[: args.limit]}


def cmd_window(args: Any) -> dict[str, Any]:
    idx = _load()
    total = idx.count(args.box, args.layers)
    sel = _select(idx, args.box, args.layers, RENDER_LIMIT if total > RENDER_LIMIT else None)
    by_layer = Counter((ln["l"], ln["k"]) for ln in sel)
    items = []
    for ln in sel[: args.limit]:
        xs = [x for x, _ in ln["p"]]
        ys = [y for _, y in ln["p"]]
        length = sum(math.dist(a, b) for a, b in zip(ln["p"], ln["p"][1:]))
        items.append({"h": ln["h"], "layer": ln["l"], "kind": ln["k"], "bbox": [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)],
                      "length": round(length, 2), "linetype": ln.get("lt"), "weight": ln.get("w"), "curved": ln.get("c")})
    texts = [[t["t"], t["x"], t["y"], t["l"]] for t in idx["texts"] if args.box[0] <= t["x"] <= args.box[2] and args.box[1] <= t["y"] <= args.box[3]]
    return {"objects": total, "by_layer_and_kind": [[l, k, n] for (l, k), n in by_layer.most_common(40)], "items": items, "texts": texts[:200]}


def cmd_render(args: Any) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import patches, pyplot

    matplotlib.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    idx = _load()
    box = args.box
    width, height = box[2] - box[0], box[3] - box[1]
    figure = pyplot.figure(figsize=(16, max(4.0, 16 * height / max(width, 1e-9))), dpi=110)
    axis = figure.add_axes([0.06, 0.05, 0.92, 0.92])
    axis.set_xlim(box[0], box[2])
    axis.set_ylim(box[1], box[3])
    axis.set_aspect("equal")
    axis.tick_params(labelsize=7)
    axis.grid(True, linewidth=0.2, color="#cbd5e1")
    total = idx.count(box, args.layers)
    for ln in _select(idx, box, args.layers, RENDER_LIMIT if total > RENDER_LIMIT else None):
        xs, ys = zip(*ln["p"])
        dashed = any(k in str(ln.get("lt", "")).lower() for k in ("dash", "hidden", "center", "虚"))
        axis.plot(xs, ys, color="#111827", linewidth=1.6 if (ln.get("w") or 0) else 0.6, linestyle="--" if dashed else "-")
    if args.faces:
        faces = json.loads(FACES.read_text(encoding="utf-8"))
        for fid in args.faces:
            face = faces[fid]
            axis.add_patch(patches.Polygon(face["outline"], closed=True, facecolor="#fca5a5", edgecolor="#dc2626", alpha=0.35))
            axis.text(face["centroid"][0], face["centroid"][1], fid, color="#b91c1c", fontsize=9, ha="center")
    if not args.no_text:
        for t in idx["texts"]:
            if box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3]:
                axis.text(t["x"], t["y"], t["t"][:20], color="#1d4ed8", fontsize=7, ha="center", va="center")
    figure.savefig(args.out, facecolor="white")
    pyplot.close(figure)
    note = "axis ticks are drawing coordinates"
    if total > RENDER_LIMIT:
        note += f"; the window holds {total} lines and only the {RENDER_LIMIT} largest are drawn - zoom in or choose layers"
    return {"image": args.out, "window": box, "lines_in_window": total, "note": note}


def polygon_faces(sel: list[dict[str, Any]], m: float, gap_m: float) -> tuple[list[Any], int]:
    """The closed faces the selected lines form, with line ends joined across gaps of up to gap_m; and how many gaps were joined."""

    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points, polygonize, unary_union
    from shapely.strtree import STRtree

    lines = [LineString(ln["p"]) for ln in sel if len(ln["p"]) >= 2]
    if not lines:
        return [], 0
    tree = STRtree(lines)
    gap = gap_m / m
    connectors = []
    for line in lines:
        for end in (Point(line.coords[0]), Point(line.coords[-1])):
            if len([i for i in tree.query(end.buffer(1e-6)) if lines[int(i)].distance(end) < 1e-6]) > 1:
                continue
            best = None
            for i in tree.query(end.buffer(gap)):
                d = lines[int(i)].distance(end)
                if 1e-6 < d <= gap and (best is None or d < best[0]):
                    best = (d, lines[int(i)])
            if best is not None:
                target = nearest_points(end, best[1])[1]
                dx, dy = target.x - end.x, target.y - end.y
                k = 1e-3 / m / max(math.hypot(dx, dy), 1e-12)
                connectors.append(LineString([end, (target.x + dx * k, target.y + dy * k)]))
    return list(polygonize(unary_union(lines + connectors))), len(connectors)


def cmd_faces(args: Any) -> dict[str, Any]:
    from shapely.geometry import Point

    idx = _load()
    m = _m(idx, args.unit_m)
    total = idx.count(args.box, args.layers)
    if total > FACES_MAX_LINES:  # joining and closing this many lines takes several GB; the machine runs several reads at once
        return {"faces": [], "error": f"the window holds {total} lines (limit {FACES_MAX_LINES}): choose the wall/outline layers with --layers "
                                      "or a smaller window around one building"}
    sel = [ln for ln in _select(idx, args.box, args.layers) if not ln.get("c") or ln["k"] != "CIRCLE"]
    polygons, joined = polygon_faces(sel, m, args.gap)
    if not sel:
        return {"faces": []}
    faces = {}
    stored = json.loads(FACES.read_text(encoding="utf-8")) if FACES.is_file() else {}
    start = len(stored)
    for poly in polygons:
        area = poly.area * m * m
        if area < args.min_area:
            continue
        long_side, short_side, angle = _mrr(poly)
        inside = [t["t"] for t in idx["texts"] if poly.contains(Point(t["x"], t["y"]))][:8]
        fid = f"F{start + len(faces) + 1}"
        faces[fid] = {"area_m2": round(area, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2), "orientation_deg": round(angle, 2),
                      "centroid": [round(poly.centroid.x, 3), round(poly.centroid.y, 3)], "texts_inside": inside,
                      "outline": [[round(x, 3), round(y, 3)] for x, y in poly.exterior.coords], "layers_used": sorted({ln["l"] for ln in sel})[:20]}
    stored.update(faces)
    FACES.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return {"faces": [{"id": k, **{x: v[x] for x in ("area_m2", "length_m", "width_m", "orientation_deg", "centroid", "texts_inside")}}
                      for k, v in sorted(faces.items(), key=lambda kv: -kv[1]["area_m2"])][:120], "joined_gaps": joined,
            "note": "faces are stored in faces.json; cite them by id"}


def cmd_envelope(args: Any) -> dict[str, Any]:
    """Outer outline of the selected linework, with openings up to --close-gaps-m closed (like tracing the outer wall)."""

    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    idx = _load()
    m = _m(idx, args.unit_m)
    sel = [ln for ln in _select(idx, args.box, args.layers) if len(ln["p"]) >= 2]
    if not sel:
        return {"error": "no linework in the window"}
    half = args.close_gaps_m / 2.0 / m
    grown = unary_union([LineString(ln["p"]).buffer(half, cap_style=2, join_style=2) for ln in sel])
    parts = list(getattr(grown, "geoms", [grown]))
    shapes = []
    for part in sorted(parts, key=lambda g: -g.area)[:5]:
        filled = Polygon(part.exterior).buffer(-half, join_style=2)
        if filled.is_empty or filled.area * m * m < args.min_area:
            continue
        if filled.geom_type != "Polygon":
            filled = max(filled.geoms, key=lambda g: g.area)
        filled = filled.simplify(0.05 / m)
        long_side, short_side, angle = _mrr(filled)
        stored = json.loads(FACES.read_text(encoding="utf-8")) if FACES.is_file() else {}
        fid = f"F{len(stored) + 1}"
        stored[fid] = {"area_m2": round(filled.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2),
                       "orientation_deg": round(angle, 2), "centroid": [round(filled.centroid.x, 3), round(filled.centroid.y, 3)], "texts_inside": [],
                       "outline": [[round(x, 3), round(y, 3)] for x, y in filled.exterior.coords], "layers_used": sorted({ln["l"] for ln in sel})[:20],
                       "made_by": f"envelope close_gaps_m={args.close_gaps_m}"}
        FACES.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        shapes.append({"id": fid, **{k: stored[fid][k] for k in ("area_m2", "length_m", "width_m", "orientation_deg", "centroid")}})
    return {"envelopes": shapes, "note": "outer outline of the selected lines with openings up to close_gaps_m closed; stored as faces, cite by id"}


def cmd_dimensions(args: Any) -> dict[str, Any]:
    """Linear dimensions drawn on the drawing (their measured points inside the window), longest first."""

    idx = _load()
    m = _m(idx, args.unit_m)
    box = args.box
    rows = []
    for d in idx.dims:
        if all(box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3] for p in (d["a"], d["b"])):
            rows.append({"length_m": round(d["length"] * m, 3), "shown_text": d["shown"] or None, "from": d["a"], "to": d["b"], "layer": d["l"]})
    rows.sort(key=lambda r: -r["length_m"])
    return {"count": len(rows), "dimensions": rows[: args.limit],
            "note": "shown_text is a text override when the drawer typed one (<> = the measured value); compare the longest ones with your building's length and width"}


ELEVATION_TEXT = re.compile(r"(?:^|[^\d.])([±+\-]?\d{1,3}\.\d{3})(?:$|[^\d])")
VIEW_TITLE = re.compile(r"剖面|立面|断面|section|elevation", re.I)


def cmd_elevations(args: Any) -> dict[str, Any]:
    """Elevation numbers (like 8.550, ±0.000) with the words written next to them and the section/elevation view they sit in."""

    idx = _load()
    m = _m(idx, args.unit_m)
    box = args.box
    inside = [t for t in idx["texts"] if box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3]]
    titles = [t for t in idx["texts"] if VIEW_TITLE.search(t["t"])]
    near = args.near_m / m
    rows = []
    for t in inside:
        hit = ELEVATION_TEXT.search(t["t"])
        if not hit:
            continue
        words = sorted(((math.dist((t["x"], t["y"]), (o["x"], o["y"])), o["t"]) for o in inside
                        if o is not t and not ELEVATION_TEXT.fullmatch(o["t"].strip()) and abs(o["x"] - t["x"]) <= near and abs(o["y"] - t["y"]) <= near))[:3]
        title = min(titles, key=lambda o: math.dist((t["x"], t["y"]), (o["x"], o["y"])), default=None)
        rows.append({"value": hit.group(1), "text": t["t"], "at": [t["x"], t["y"]], "layer": t["l"],
                     "words_next_to_it": [[w, round(d * m, 1)] for d, w in words],
                     "nearest_view_title": [title["t"], round(math.dist((t["x"], t["y"]), (title["x"], title["y"])) * m, 1)] if title else None})
    return {"count": len(rows), "elevations": rows[: args.limit], "view_titles": [[o["t"], o["x"], o["y"]] for o in titles][:40],
            "note": "an elevation's type (floor, column top, eave, ridge, site) comes from the words next to it, the legend or the section it sits in - "
                    "not from its value; a floor level is not a height"}


def _zone_suffixes() -> list[str]:
    lexicon = json.loads(Path(__file__).with_name("lexicon_v1.json").read_text(encoding="utf-8"))
    return [w.lower() for w in lexicon.get("zone_suffixes", [])]


def cmd_zones(args: Any) -> dict[str, Any]:
    """Area names in the window, each with the smallest closed face around it (built from the chosen layers)."""

    from shapely.geometry import Point, Polygon

    idx = _load()
    m = _m(idx, args.unit_m)
    box = args.box
    rx = re.compile(args.match) if args.match else None
    endings = _zone_suffixes()
    names = [t for t in idx["texts"] if box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3] and len(t["t"].strip()) <= 30
             and (rx.search(t["t"]) if rx else any(t["t"].strip().lower().endswith(w) for w in endings))]
    before = json.loads(FACES.read_text(encoding="utf-8")) if FACES.is_file() else {}
    built = cmd_faces(argparse.Namespace(box=box, layers=args.layers, unit_m=args.unit_m, gap=args.gap, min_area=args.min_area))
    if built.get("error"):
        return {"zones": [], "names_without_closed_boundary": [{"name": t["t"], "at": [t["x"], t["y"]]} for t in names], "error": built["error"]}
    faces = {k: v for k, v in (json.loads(FACES.read_text(encoding="utf-8")) if FACES.is_file() else {}).items() if k not in before}
    polys = {k: Polygon(v["outline"]) for k, v in faces.items()}
    rows, unbounded = [], []
    for t in names:
        point = Point(t["x"], t["y"])
        around = sorted((poly.area, k) for k, poly in polys.items() if poly.is_valid and poly.contains(point))
        if around:
            k = around[0][1]
            rows.append({"name": t["t"], "at": [t["x"], t["y"]], "face": k, "area_m2": faces[k]["area_m2"], "length_m": faces[k]["length_m"],
                         "width_m": faces[k]["width_m"], "other_names_in_face": [o["t"] for o in names if o is not t and polys[k].contains(Point(o["x"], o["y"]))][:6]})
        else:
            unbounded.append({"name": t["t"], "at": [t["x"], t["y"]]})
    return {"zones": rows, "names_without_closed_boundary": unbounded, "faces_built": len(faces),
            "note": "check each face on a render (--faces): it must be the area's own boundary, not a building outline or a frame; a face holding "
                    "several names may be several areas whose dividing line is missing - say so rather than splitting by guess"}


def dimension_check(outline: list[list[float]], m: float, idx: Any | None = None) -> list[dict[str, Any]]:
    """Dimensions measuring this outline from end to end, compared with its extent in the same direction."""

    from shapely.geometry import Point, Polygon

    idx = idx or _load()
    poly = Polygon(outline)
    near = COLUMN_ON_OUTLINE_M / m
    out = []
    for d in idx.dims:
        a, b = d["a"], d["b"]
        ends = sorted(poly.exterior.distance(Point(p)) for p in (a, b))
        # starts on the outline and ends on it or not far off it (an outline measured too short still gets compared)
        if d["length"] <= 0 or ends[0] > near or ends[1] > max(near, 0.25 * d["length"]):
            continue
        ux, uy = (b[0] - a[0]) / max(math.dist(a, b), 1e-12), (b[1] - a[1]) / max(math.dist(a, b), 1e-12)
        proj = [x * ux + y * uy for x, y in poly.exterior.coords]
        extent = (max(proj) - min(proj)) * m
        length = d["length"] * m
        if length < 0.9 * extent:
            continue  # a part dimension (one bay, a door), not end to end
        out.append({"dimension_m": round(length, 3), "outline_extent_m": round(extent, 3), "differs_pct": round(100 * (extent - length) / length, 2),
                    "shown_text": d["shown"] or None, "handle": d["h"]})
    return out


def cmd_corners(args: Any) -> dict[str, Any]:
    """Trace a building by its outer corners, like a person clicking round the outside of the exterior wall.

    The agent gives the corners in order (x y x y ...).  Each is snapped to the nearest line end, line bend or
    crossing of two lines (on the chosen layers) within --snap-m; a corner with nothing to snap to keeps the
    given point and is reported as unsnapped.  The closed outline is stored as a face (cite it in measure).
    """

    from shapely.geometry import LineString, Point, Polygon

    idx = _load()
    m = _m(idx, args.unit_m)
    given = [(args.points[i], args.points[i + 1]) for i in range(0, len(args.points) - 1, 2)]
    if len(given) < 3:
        return {"error": "give at least 3 corners as x y pairs"}
    snap = args.snap_m / m
    cx = sum(x for x, _ in given) / len(given)
    cy = sum(y for _, y in given) / len(given)
    snapped, report = [], []
    for x, y in given:
        window = [x - snap, y - snap, x + snap, y + snap]
        lines = [ln for ln in _select(idx, window, args.layers) if len(ln["p"]) >= 2 and not ln.get("c")]
        candidates = [q for ln in lines for q in ln["p"] if math.dist(q, (x, y)) <= snap]
        geoms = [LineString(ln["p"]) for ln in lines]
        for i in range(len(geoms)):
            for j in range(i + 1, len(geoms)):
                hit = geoms[i].intersection(geoms[j])
                for g in getattr(hit, "geoms", [hit]):
                    if g.geom_type == "Point" and math.dist((g.x, g.y), (x, y)) <= snap:
                        candidates.append((g.x, g.y))
        if candidates:
            if getattr(args, "outer", True):
                # the outer face of the exterior wall (GB/T 50353): of the points near the click, the one furthest out from the building's middle
                ox, oy = x - cx, y - cy
                norm = math.hypot(ox, oy) or 1.0
                q = max(candidates, key=lambda c: ((c[0] - x) * ox + (c[1] - y) * oy) / norm - 0.25 * math.dist(c, (x, y)))
            else:
                q = min(candidates, key=lambda c: math.dist(c, (x, y)))
            snapped.append((q[0], q[1]))
            report.append({"given": [x, y], "snapped_to": [round(q[0], 3), round(q[1], 3)], "moved_m": round(math.dist(q, (x, y)) * m, 2)})
        else:
            snapped.append((x, y))
            report.append({"given": [x, y], "snapped_to": None, "note": f"no line end or crossing within {args.snap_m} m: kept as given"})
    poly = Polygon(snapped)
    if not poly.is_valid:
        return {"error": "the corners cross over themselves: give them in order round the building", "corners": report}
    long_side, short_side, angle = _mrr(poly)
    stored = json.loads(FACES.read_text(encoding="utf-8")) if FACES.is_file() else {}
    fid = f"F{len(stored) + 1}"
    stored[fid] = {"area_m2": round(poly.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2),
                   "orientation_deg": round(angle, 2), "centroid": [round(poly.centroid.x, 3), round(poly.centroid.y, 3)], "texts_inside": [],
                   "outline": [[round(x, 3), round(y, 3)] for x, y in list(poly.exterior.coords)], "layers_used": sorted(set(args.layers or [])),
                   "made_by": f"corners snap_m={args.snap_m}"}
    FACES.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return {"id": fid, **{k: stored[fid][k] for k in ("area_m2", "length_m", "width_m", "orientation_deg")}, "corners": report,
            "unsnapped": sum(1 for r in report if r["snapped_to"] is None),
            "note": "render it with --faces to check it follows the outer face of the exterior wall; cite the id in measure like any face"}


def measure_faces(face_ids: list[str], faces: dict[str, Any], m: float) -> dict[str, Any]:
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    union = unary_union([Polygon(faces[f]["outline"]) for f in face_ids])
    if union.geom_type != "Polygon":
        return {"error": "the faces do not form one connected outline", "parts": len(getattr(union, "geoms", []))}
    long_side, short_side, angle = _mrr(union)
    return {"faces": face_ids, "area_m2": round(union.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2),
            "orientation_deg": round(angle, 2), "rectangular": union.area / max(union.minimum_rotated_rectangle.area, 1e-12) >= 0.98,
            "centroid": [round(union.centroid.x, 3), round(union.centroid.y, 3)], "outline": [[round(x, 3), round(y, 3)] for x, y in union.exterior.coords]}


def cmd_measure(args: Any) -> dict[str, Any]:
    idx = _load()
    return measure_faces(args.face_ids, json.loads(FACES.read_text(encoding="utf-8")), _m(idx, args.unit_m))


def cmd_around(args: Any) -> dict[str, Any]:
    """Every face that touches the given faces: candidates the agent must decide one by one."""

    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    idx = _load()
    m = _m(idx, args.unit_m)
    faces = json.loads(FACES.read_text(encoding="utf-8"))
    core = unary_union([Polygon(faces[f]["outline"]) for f in args.face_ids])
    margin = args.margin_m / m
    box = [core.bounds[0] - margin, core.bounds[1] - margin, core.bounds[2] + margin, core.bounds[3] + margin]
    before = set(faces)
    built = cmd_faces(argparse.Namespace(box=box, layers=args.layers, unit_m=args.unit_m, gap=args.gap, min_area=args.min_area))
    faces = json.loads(FACES.read_text(encoding="utf-8"))
    # the symbol count is only a hint; on a crowded drawing searching every layer takes several GB per call (seen 2026-10-09)
    crowded = idx.count(box, None) > FACES_MAX_LINES
    symbols = [] if crowded else cmd_columns(argparse.Namespace(box=box, layers=None, unit_m=args.unit_m, max_size=1.5))["symbol_groups"]
    centres = [Point(*c) for g in symbols[:3] for c in g["centres"]]
    rows = []
    for fid in [f for f in faces if f not in before]:
        poly = Polygon(faces[fid]["outline"])
        if poly.area <= 0 or core.contains(poly.representative_point()) and poly.area <= core.area * 1.0001 and core.buffer(1e-6).contains(poly):
            continue
        shared = poly.boundary.intersection(core.boundary).length * m
        if shared < 1.0:
            continue
        rows.append({"id": fid, "shared_wall_m": round(shared, 1), "area_m2": faces[fid]["area_m2"], "length_m": faces[fid]["length_m"], "width_m": faces[fid]["width_m"],
                     "symbols_inside_from_top3_groups": None if crowded else sum(1 for c in centres if poly.contains(c)), "texts_inside": faces[fid]["texts_inside"]})
    return {"around": sorted(rows, key=lambda r: -r["shared_wall_m"]), "note": "decide every face listed: part of the building, semi-outdoor (covered, not enclosed), separate building, or not a building - with a reason",
            "new_faces": len(set(faces) - before), "faces_built": len(built.get("faces", []))}


def cmd_columns(args: Any) -> dict[str, Any]:
    from shapely.geometry import LineString, MultiPoint
    from shapely.strtree import STRtree

    idx = _load()
    m = _m(idx, args.unit_m)
    small = [ln for ln in idx.select(args.box, args.layers, max_extent=args.max_size / m * 1.42) if math.dist((min(x for x, _ in ln["p"]), min(y for _, y in ln["p"])),
                                                                         (max(x for x, _ in ln["p"]), max(y for _, y in ln["p"]))) * m <= args.max_size]
    geoms = [LineString(ln["p"]) for ln in small]
    parent = list(range(len(geoms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if geoms:
        tree = STRtree(geoms)
        for i, g in enumerate(geoms):
            for j in tree.query(g.buffer(SYMBOL_JOIN_M / m)):
                j = int(j)
                # pieces of one symbol share a layer; joining across layers glued columns to door and wall bits
                if j > i and small[i]["l"] == small[j]["l"] and g.distance(geoms[j]) <= SYMBOL_JOIN_M / m:
                    parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(geoms)):
        groups[find(i)].append(i)
    # one symbol part = touching pieces on one layer; parts are grouped by shape (size and the kinds of pieces), on any layer:
    # the same column section is often drawn on several layers, and its outline varies with the wall it stands in
    symbols: dict[tuple, list[tuple[float, float, str]]] = defaultdict(list)
    for members in groups.values():
        pts = [q for i in members for q in small[i]["p"]]
        hull = MultiPoint(pts).convex_hull
        dims = tuple(sorted(round(v * m, 2) for v in _mrr(hull)[:2])) if hull.area > 0 else (0.0, round(hull.length * m, 2))
        dims = tuple(round(v * 20) / 20 for v in dims)
        kinds = tuple(sorted(Counter(small[i]["k"] for i in members).items()))
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        symbols[(dims, kinds)].append((round((min(xs) + max(xs)) / 2, 3), round((min(ys) + max(ys)) / 2, 3), small[members[0]]["l"]))
    rows = []
    for (dims, kinds), found in sorted(symbols.items(), key=lambda kv: -len(kv[1]))[:getattr(args, "top", 30)]:
        rows.append({"size_m": list(dims), "pieces": dict(kinds), "count": len(found), "layers": Counter(l for _, _, l in found).most_common(4),
                     "centres": [(x, y) for x, y, _ in found][:600], "_all": [(x, y) for x, y, _ in found]})
    named = Counter(ln["l"] for ln in _select(idx, args.box, None) if COLUMN_LAYER.search(ln["l"]))
    if not getattr(args, "keep_all", False):
        for r in rows:
            r.pop("_all")
    return {"symbol_groups": rows, "layers_named_like_columns": named.most_common(20),
            "note": "groups of identical small shapes (any layer), numbered from 0 in this order; decide which groups are columns from layer names, legend, shape and position "
                    "- one column type may show as several groups (section and outline, different sizes) - then count with `column_instances`"}


SYMBOL_JOIN_M = 0.01  # small pieces on one layer that touch (within 1 cm) form one part of a symbol
COLUMN_LAYER = re.compile(r"柱|colu|col$|^s-col|pillar", re.I)


def _piece_key(ln: dict[str, Any], m: float) -> tuple[str, float, int]:
    """What a piece of a symbol looks like: kind, end-to-end extent (0.05 m steps), number of points."""

    xs = [x for x, _ in ln["p"]]
    ys = [y for _, y in ln["p"]]
    extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) * m
    return ln["k"], round(extent * 20) / 20, len(ln["p"])


def cmd_column_instances(args: Any) -> dict[str, Any]:
    """Count the columns of a building from the symbol groups the agent chose, then check them against the column grid.

    1. Every listed symbol of the chosen groups is a column.
    2. Small symbols (few pieces) touching other lines are not listed as their own group; for those the
       program takes the pieces present around nearly every listed symbol as a template and accepts
       every place where the whole template is present.
    3. Inside the given faces the columns are projected on the building's two directions; column lines
       (3 or more columns in a row) give the grid.  At grid crossings, gaps and continuations of column
       lines without a column, symbol-sized linework is looked for and listed for the agent to check
       (not counted: door and wall bits look the same to the program).
    """

    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    idx = _load()
    m = _m(idx, args.unit_m)
    groups = cmd_columns(argparse.Namespace(box=args.box, layers=args.layers, unit_m=args.unit_m, max_size=args.max_size, keep_all=True))["symbol_groups"]
    keyed = []
    for ln in idx.select(args.box, args.layers, max_extent=args.max_size / m * 1.42):
        key = _piece_key(ln, m)
        if len(ln["p"]) >= 2 and key[1] <= args.max_size:
            xs = [x for x, _ in ln["p"]]
            ys = [y for _, y in ln["p"]]
            keyed.append((ln["l"], key, ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), (min(xs), min(ys), max(xs), max(ys))))
    cell = max(args.max_size / m, 1e-9)
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, item in enumerate(keyed):
        buckets[(int(item[2][0] // cell), int(item[2][1] // cell))].append(i)

    def near(x: float, y: float, half: float) -> list[int]:
        gx, gy = int(x // cell), int(y // cell)
        return [i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for i in buckets.get((gx + dx, gy + dy), [])
                if abs(keyed[i][2][0] - x) <= half and abs(keyed[i][2][1] - y) <= half]

    found: list[tuple[float, float, float]] = []  # centre, and the distance within which another find is the same symbol
    report = []
    sizes = []
    for g in args.groups:
        if g >= len(groups):
            report.append({"group": g, "error": "no such group"})
            continue
        group = groups[g]
        half = max(group["size_m"]) / m * 0.6
        sizes.append(max(group["size_m"]))
        found += [(x, y, half) for x, y in group["_all"]]
        row = {"group": g, "layers": group["layers"], "size_m": group["size_m"], "listed": group["count"]}
        seen = [Counter((keyed[i][0], keyed[i][1]) for i in near(cx, cy, half)) for cx, cy in group["_all"]]
        need = Counter()
        for key in set().union(*seen) if seen else set():
            n = sorted(s.get(key, 0) for s in seen)[int(len(seen) * 0.2)]
            if n:
                need[key] = n
        anchors = [k for k in need if need[k] == 1]
        anchor = max(anchors, key=lambda k: k[1][1]) if anchors else None
        anchor_total = sum(1 for layer, key, _, _ in keyed if (layer, key) == anchor)
        # searched only for a small template whose anchor piece is rare elsewhere (else plain wall and door bits would match)
        if anchor and sum(need.values()) <= TEMPLATE_MAX_PIECES and anchor_total <= TEMPLATE_ANCHOR_MAX_SHARE * len(group["_all"]):
            cx, cy = group["_all"][0]
            offsets = [(keyed[i][2][0] - cx, keyed[i][2][1] - cy) for i in near(cx, cy, half) if (keyed[i][0], keyed[i][1]) == anchor]
            dx, dy = offsets[0] if offsets else (0.0, 0.0)
            extra, partial = [], []
            for layer, key, c, _ in keyed:
                if (layer, key) != anchor:
                    continue
                best = None
                for ox, oy in {(c[0] - dx, c[1] - dy), (c[0] + dy, c[1] - dx), (c[0] + dx, c[1] + dy), (c[0] - dy, c[1] + dx)}:  # turned by 0/90/180/270 degrees
                    got = Counter((keyed[j][0], keyed[j][1]) for j in near(ox, oy, half))
                    missing = {f"{k[0]}:{k[1][0]}:{k[1][1]}m": n - got.get(k, 0) for k, n in need.items() if got.get(k, 0) < n}
                    if best is None or sum(missing.values()) < sum(best[2].values()):
                        best = (round(ox, 3), round(oy, 3), missing)
                if all(math.dist(best[:2], q) > half for q in group["_all"]):
                    (partial if best[2] else extra).append(best)
            found += [(x, y, half) for x, y, _ in extra]
            row.update({"template_pieces": sum(need.values()), "found_by_template": len(extra), "incomplete_by_template": [list(p) for p in partial][:50]})
        report.append(row)
    unique: list[tuple[float, float]] = []
    reach_of: list[float] = []
    for x, y, r in found:  # one symbol found twice counts once
        if all(math.dist((x, y), q) > max(r, s, 0.3 / m) for q, s in zip(unique, reach_of)):
            unique.append((x, y))
            reach_of.append(r)
    result: dict[str, Any] = {"groups": report, "columns_from_symbols": len(unique), "centres": unique[:600]}
    if args.faces and unique:
        faces = json.loads(FACES.read_text(encoding="utf-8"))
        outline = unary_union([Polygon(faces[f]["outline"]) for f in args.faces])
        angle = math.radians(_mrr(outline)[2])
        u_dir, v_dir = (math.cos(angle), math.sin(angle)), (-math.sin(angle), math.cos(angle))
        reach = outline.buffer(COLUMN_ON_OUTLINE_M / m)
        inside = [p for p in unique if reach.contains(Point(p))]
        # the same column shapes just outside the faces: the building may be larger than the faces chosen
        chosen = {(tuple(groups[g]["size_m"]), tuple(sorted(groups[g]["pieces"].items()))) for g in args.groups if g < len(groups)}
        ob = outline.bounds
        margin = OUTSIDE_SEARCH_M / m
        wider = cmd_columns(argparse.Namespace(box=[ob[0] - margin, ob[1] - margin, ob[2] + margin, ob[3] + margin], layers=args.layers,
                                               unit_m=args.unit_m, max_size=args.max_size, keep_all=True, top=100000))["symbol_groups"]
        outside: list[tuple[float, float]] = []
        for w in wider:
            if (tuple(w["size_m"]), tuple(sorted(w["pieces"].items()))) in chosen:
                for q in w["_all"]:
                    if not reach.contains(Point(q)) and all(math.dist(q, o) * m > 0.3 for o in outside):
                        outside.append(q)
        result.update({"same_columns_outside_faces": len(outside), "outside_positions": [list(q) for q in outside[:200]]})

        def dot(p: tuple[float, float], d: tuple[float, float]) -> float:
            return p[0] * d[0] + p[1] * d[1]

        def lines_of(across: tuple[float, float], along: tuple[float, float]) -> list[dict[str, Any]]:
            """Column lines: 3 or more columns sharing one cross coordinate, with spacings and gaps where a column seems missing."""

            pts = sorted(inside, key=lambda p: dot(p, across))
            runs, run = [], [pts[0]]
            for p in pts[1:]:
                if (dot(p, across) - dot(run[-1], across)) * m <= GRID_LINE_TOLERANCE_M:
                    run.append(p)
                else:
                    runs.append(run); run = [p]
            runs.append(run)
            out = []
            for run in runs:
                if len(run) < 3:
                    continue
                pos = sorted(dot(p, along) for p in run)
                at = sum(dot(p, across) for p in run) / len(run)
                gaps = [(b - a) * m for a, b in zip(pos, pos[1:])]
                # the smallest spacing that occurs at least twice: a line of 9 m bays with some 18 m gaps is a 9 m line
                common = [v for v, n in Counter(round(g * 2) / 2 for g in gaps).items() if n >= 2 and v > 0]
                typical = min(common) if common else Counter(round(g * 2) / 2 for g in gaps).most_common(1)[0][0]
                missing, ahead = [], []
                for a, g in zip(pos, gaps):
                    if typical > 0 and g >= 1.8 * typical:
                        for k in range(1, round(g / typical)):
                            t = a + k * typical / m
                            missing.append([round(t * along[0] + at * across[0], 3), round(t * along[1] + at * across[1], 3)])
                if typical > 0:  # the line continued past its first and last column, as far as the building goes
                    for start, step in ((pos[0], -typical / m), (pos[-1], typical / m)):
                        t = start + step
                        while reach.contains(Point(t * along[0] + at * across[0], t * along[1] + at * across[1])):
                            ahead.append((t * along[0] + at * across[0], t * along[1] + at * across[1]))
                            t += step
                out.append({"at": at, "columns": len(run), "typical_spacing_m": typical, "spacings_m": [round(g, 2) for g in gaps], "possible_missing": missing, "_ahead": ahead})
            return out

        symbol = max(sizes or [1.0]) / m
        by_grid: list[tuple[float, float]] = []
        seeds = len(inside)
        for _ in range(GRID_ROUNDS):  # columns found at crossings extend the column lines, which give new crossings
            long_lines, short_lines = lines_of(v_dir, u_dir), lines_of(u_dir, v_dir)
            empty, added = [], 0
            places = [(b["at"] * u_dir[0] + a["at"] * v_dir[0], b["at"] * u_dir[1] + a["at"] * v_dir[1]) for a in long_lines for b in short_lines]
            places += [tuple(q) for ln in long_lines + short_lines for q in ln["possible_missing"]] + [q for ln in long_lines + short_lines for q in ln["_ahead"]]
            for x, y in places:
                    if not reach.contains(Point(x, y)) or any(math.dist((x, y), q) <= symbol for q in inside + by_grid):
                        continue
                    parts = near(x, y, symbol * 0.6)
                    if parts:
                        bx = [min(keyed[i][3][0] for i in parts), min(keyed[i][3][1] for i in parts), max(keyed[i][3][2] for i in parts), max(keyed[i][3][3] for i in parts)]
                        w, h = (bx[2] - bx[0]) * m, (bx[3] - bx[1]) * m
                        if GRID_SYMBOL_MIN_M <= min(w, h) and max(w, h) <= args.max_size:
                            centre = (round((bx[0] + bx[2]) / 2, 3), round((bx[1] + bx[3]) / 2, 3))
                            if all(math.dist(centre, q) > symbol for q in inside + by_grid):
                                by_grid.append(centre); added += 1
                                continue
                    empty.append([round(x, 3), round(y, 3)])
            if not added:
                break
        for ln in long_lines + short_lines:
            ln.pop("at"); ln.pop("_ahead")
        result.update({"columns_in_faces": seeds, "to_check_on_grid": len(by_grid), "to_check_positions": [list(q) for q in by_grid[:300]], "empty_grid_crossings": len(empty),
                       "lines_along_long_side": long_lines, "lines_along_short_side": short_lines})
        # kept per set of faces, the last run wins: the program compares each reported building with it
        runs = json.loads(COLUMNS.read_text(encoding="utf-8")) if COLUMNS.is_file() else {}
        runs["+".join(sorted(args.faces))] = {"faces": args.faces, "groups": args.groups, "box": args.box, "layers": args.layers, **result,
                                             "empty_grid_crossing_positions": empty}
        COLUMNS.write_text(json.dumps(runs, ensure_ascii=False), encoding="utf-8")
    result["note"] = ("same_columns_outside_faces > 0: the same column shapes stand within 30 m outside the faces - decide for each cluster whether "
                      "the building extends there (then add those faces and measure again) or it is another building, with a reason. "
                      "columns_in_faces = chosen symbols inside the faces (counted). to_check_positions = places on the column grid (crossings, gaps and "
                      "continuations of column lines) where symbol-sized linework stands but no chosen symbol: NOT counted - render them (small windows) and add "
                      "the shape groups that are columns, or say why not; also look at incomplete template finds")
    return result


TEMPLATE_MAX_PIECES = 8  # a template with more pieces (hatched symbols) is not searched for: hatching varies between copies
OUTSIDE_SEARCH_M = 30.0  # how far beyond the faces the chosen column shapes are looked for
TEMPLATE_ANCHOR_MAX_SHARE = 1.5  # the anchor piece may occur at most this many times the listed symbols
GRID_ROUNDS = 5  # rounds of extending column lines with columns found at crossings
GRID_SYMBOL_MIN_M = 0.15  # linework at a grid crossing at least this wide both ways counts as a column drawn another way


COLUMN_ON_OUTLINE_M = 1.5  # a column this close to a building outline belongs to it (same value as constants.COLUMN_ON_OUTLINE_M)
GRID_LINE_TOLERANCE_M = 1.0  # columns whose cross coordinate differs by at most this stand on one column line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index"); p.add_argument("dxf")
    sub.add_parser("overview")
    p = sub.add_parser("texts"); p.add_argument("--match"); p.add_argument("--window", nargs=4, type=float); p.add_argument("--limit", type=int, default=300)
    p = sub.add_parser("window"); p.add_argument("box", nargs=4, type=float); p.add_argument("--layers", nargs="*"); p.add_argument("--limit", type=int, default=150)
    p = sub.add_parser("render"); p.add_argument("box", nargs=4, type=float); p.add_argument("out"); p.add_argument("--layers", nargs="*"); p.add_argument("--faces", nargs="*"); p.add_argument("--no-text", action="store_true")
    p = sub.add_parser("around"); p.add_argument("face_ids", nargs="+"); p.add_argument("--layers", nargs="*"); p.add_argument("--unit-m", type=float)
    p.add_argument("--margin-m", type=float, default=40.0); p.add_argument("--gap", type=float, default=1.0); p.add_argument("--min-area", type=float, default=5.0)
    p = sub.add_parser("envelope"); p.add_argument("box", nargs=4, type=float); p.add_argument("--layers", nargs="*"); p.add_argument("--unit-m", type=float)
    p.add_argument("--close-gaps-m", type=float, default=6.0); p.add_argument("--min-area", type=float, default=20.0)
    p = sub.add_parser("elevations"); p.add_argument("box", nargs=4, type=float); p.add_argument("--unit-m", type=float)
    p.add_argument("--near-m", type=float, default=5.0); p.add_argument("--limit", type=int, default=150)
    p = sub.add_parser("corners"); p.add_argument("points", nargs="+", type=float); p.add_argument("--layers", nargs="*"); p.add_argument("--unit-m", type=float)
    p.add_argument("--snap-m", type=float, default=1.0)
    p.add_argument("--nearest", dest="outer", action="store_false", help="snap to the nearest point instead of the outermost one")
    p = sub.add_parser("zones"); p.add_argument("box", nargs=4, type=float); p.add_argument("--layers", nargs="*"); p.add_argument("--unit-m", type=float)
    p.add_argument("--match"); p.add_argument("--gap", type=float, default=1.0); p.add_argument("--min-area", type=float, default=2.0)
    p = sub.add_parser("dimensions"); p.add_argument("box", nargs=4, type=float); p.add_argument("--unit-m", type=float); p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("column_instances"); p.add_argument("box", nargs=4, type=float); p.add_argument("--groups", nargs="+", type=int, required=True)
    p.add_argument("--faces", nargs="*"); p.add_argument("--layers", nargs="*"); p.add_argument("--unit-m", type=float); p.add_argument("--max-size", type=float, default=1.5)
    for name in ("faces", "measure", "columns"):
        p = sub.add_parser(name)
        if name == "measure":
            p.add_argument("face_ids", nargs="+")
        else:
            p.add_argument("box", nargs=4, type=float); p.add_argument("--layers", nargs="*")
        p.add_argument("--unit-m", type=float)
        if name == "faces":
            p.add_argument("--gap", type=float, default=1.0); p.add_argument("--min-area", type=float, default=5.0)
        if name == "columns":
            p.add_argument("--max-size", type=float, default=1.5)
    args = parser.parse_args(argv)
    if args.cmd == "index":
        result = build_index(args.dxf)
    else:
        result = {"overview": cmd_overview, "texts": cmd_texts, "window": cmd_window, "render": cmd_render, "faces": cmd_faces,
                  "measure": cmd_measure, "columns": cmd_columns, "envelope": cmd_envelope, "around": cmd_around,
                  "column_instances": cmd_column_instances, "dimensions": cmd_dimensions,
                  "elevations": cmd_elevations, "zones": cmd_zones, "corners": cmd_corners}[args.cmd](args)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
