"""Site-plan buildings: every building drawn on the plan, with its name, size, area,
orientation, column symbols and floor elevations.

Generic method (no per-drawing rules):

* linework is split into walls and symbols by size; wall ends left open by drafting
  gaps are joined; closed faces are cells;
* a named building is the smallest building-layer cell that contains the centre of
  its name text (containment, never distance);
* unnamed buildings are cells bounded by building-outline-layer linework that no
  named building covers;
* buildings sharing walls are also reported together as a complex, because a drawn
  partition line does not tell whether two cells are one building;
* column symbols are clusters of short pieces attached to an outline; the most
  frequent symbol shape on outlines is reported as columns, others are listed;
* elevation texts inside a building are indoor floor elevations only when the
  drawing's legend says so; outside buildings they are site elevations.

Elevations are never reported as building height.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .constants import (
    BUILDING_GAP_CLOSE_M,
    BUILDING_MIN_AREA_M2,
    BUILDING_MIN_SIDE_M,
    BUILDING_SYMBOL_MAX_M,
    COLUMN_ON_OUTLINE_M,
    RECTANGULARITY_MIN,
    SYMBOL_CLUSTER_GAP_M,
)
from .expand import iter_world_items, transformed_point
from .warehouse_geometry import UNIT_TO_MM

_LEXICON = json.loads((Path(__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8"))
ELEVATION_TEXT = re.compile(r"^[+-]?\d{1,3}\.\d{2,3}$")
CJK = re.compile(r"[一-鿿]")
LATIN = re.compile(r"[A-Za-z]")
NAME_MAX_CHARS = 20


def _matches(layer: str, terms: list[str]) -> bool:
    folded = layer.casefold()
    for term in terms:
        t = term.casefold()
        if t.isascii() and len(t) <= 4:
            if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", folded):
                return True
        elif t in folded:
            return True
    return False


def is_building_layer(layer: str) -> bool:
    return _matches(layer, _LEXICON["building_outline_layers"])


def is_excluded_layer(layer: str) -> bool:
    return _matches(layer, _LEXICON["non_building_linework_layers"])


def is_name(text: str) -> bool:
    """A building name: short, with at least two Chinese characters or three letters, not a road or place name."""

    value = text.strip()
    if not value or len(value) > NAME_MAX_CHARS or ELEVATION_TEXT.match(value):
        return False
    if len(CJK.findall(value)) < 2 and len(LATIN.findall(value)) < 3:
        return False
    return not any(value.endswith(term) for term in _LEXICON["non_building_name_endings"])


def _text_value(entity: Any) -> str:
    try:
        if entity.dxftype() == "MTEXT":
            return entity.plain_text().strip()
        return str(entity.dxf.text or "").strip()
    except Exception:
        return ""


def _text_centre(entity: Any, matrix: Any) -> tuple[float, float] | None:
    from ezdxf import bbox

    try:
        box = bbox.extents([entity], fast=True)
    except Exception:
        box = None
    if box is not None and box.has_data:
        c = box.center
        return transformed_point(matrix, (c.x, c.y, 0.0))
    try:
        return transformed_point(matrix, entity.dxf.insert)
    except Exception:
        return None


def _polyline_points(entity: Any, matrix: Any) -> tuple[list[tuple[float, float]], bool, bool] | None:
    """World points, closed flag, has-curvature flag."""

    kind = entity.dxftype()
    try:
        if kind == "LINE":
            return [transformed_point(matrix, entity.dxf.start), transformed_point(matrix, entity.dxf.end)], False, False
        if kind == "LWPOLYLINE":
            raw = list(entity.get_points("xyb"))
            pts = [transformed_point(matrix, (x, y, 0.0)) for x, y, _ in raw]
            return pts, bool(entity.closed), any(abs(b) > 1e-9 for _, _, b in raw)
        if kind == "POLYLINE" and not entity.is_3d_polyline:
            pts = [transformed_point(matrix, v.dxf.location) for v in entity.vertices]
            return pts, bool(entity.is_closed), False
        if kind == "ARC":
            pts = [transformed_point(matrix, p) for p in entity.flattening(0.05)]
            return pts, False, True
    except Exception:
        return None
    return None


def _style(doc: Any, entity: Any, layer_name: str) -> dict[str, Any]:
    """Drawn style of a line: line type, weight in mm (polyline width or line weight), colour; BYLAYER resolved."""

    layer = doc.layers.get(layer_name) if layer_name in doc.layers else None
    linetype = str(entity.dxf.get("linetype", "BYLAYER"))
    if linetype.upper() in {"BYLAYER", "BYBLOCK"} and layer is not None:
        linetype = str(layer.dxf.get("linetype", "Continuous"))
    weight = float(entity.dxf.get("const_width", 0.0) or 0.0) if entity.dxftype() == "LWPOLYLINE" else 0.0
    lineweight = int(entity.dxf.get("lineweight", -1))
    if lineweight < 0 and layer is not None:
        lineweight = int(layer.dxf.get("lineweight", -3))
    colour = int(entity.dxf.get("color", 256))
    if colour in (0, 256) and layer is not None:
        colour = abs(int(layer.dxf.get("color", 7)))
    return {"linetype": linetype, "polyline_width": round(weight, 3), "lineweight_mm": lineweight / 100.0 if lineweight > 0 else None, "colour": colour}


def collect(doc: Any) -> dict[str, Any]:
    building, other, texts = [], [], []
    for item in iter_world_items(doc):
        entity, kind = item.entity, item.dxftype
        if kind in {"TEXT", "MTEXT"}:
            value = _text_value(entity)
            centre = _text_centre(entity, item.matrix) if value else None
            if centre is not None:
                texts.append({"ref": f"T:{item.ref}", "text": value, "xy": [round(centre[0], 3), round(centre[1], 3)], "layer": item.layer})
            continue
        if is_excluded_layer(item.layer):
            continue
        got = _polyline_points(entity, item.matrix)
        if got is None or len(got[0]) < 2:
            continue
        pts, closed, curved = got
        if closed:
            pts = pts + [pts[0]]
        piece = {"ref": f"E:{item.ref}", "points": pts, "curved": curved, "layer": item.layer, **_style(doc, entity, item.layer)}
        (building if is_building_layer(item.layer) else other).append(piece)
    return {"building_pieces": building, "other_pieces": other, "texts": texts}


def _extent(points: list[tuple[float, float]]) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def _mrr(polygon: Any) -> tuple[float, float, float]:
    """(long side, short side, long-axis angle in [0,180)) of the minimum rotated rectangle."""

    corners = list(polygon.minimum_rotated_rectangle.exterior.coords)
    a = math.dist(corners[0], corners[1])
    b = math.dist(corners[1], corners[2])
    if a >= b:
        angle = math.degrees(math.atan2(corners[1][1] - corners[0][1], corners[1][0] - corners[0][0]))
    else:
        angle = math.degrees(math.atan2(corners[2][1] - corners[1][1], corners[2][0] - corners[1][0]))
    return max(a, b), min(a, b), angle % 180.0


def _column_lines(points: list[tuple[float, float]], angle_deg: float, tol: float) -> list[dict[str, Any]]:
    """Column centres grouped into straight lines along and across the building axis, with spacings."""

    u = (math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg)))
    v = (-u[1], u[0])
    lines = []
    for axis, (a, b) in (("long", (u, v)), ("short", (v, u))):
        coords = sorted(((p[0] * b[0] + p[1] * b[1]), (p[0] * a[0] + p[1] * a[1])) for p in points)
        groups: list[list[tuple[float, float]]] = []
        for across, along in coords:
            if groups and abs(across - groups[-1][-1][0]) <= tol:
                groups[-1].append((across, along))
            else:
                groups.append([(across, along)])
        for g in groups:
            if len(g) < 3:
                continue
            along = sorted(x for _, x in g)
            lines.append({"direction": axis, "count": len(g), "spacings_m": [round(b2 - a2, 2) for a2, b2 in zip(along, along[1:])]})
    return lines


def _cells(pieces: list[dict[str, Any]], m: float) -> tuple[list[Any], int]:
    """Faces of the linework (wall ends joined across drafting gaps) big enough to be a building."""

    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points, polygonize, unary_union
    from shapely.strtree import STRtree

    lines = [LineString(p["points"]) for p in pieces]
    if not lines:
        return [], 0
    tree = STRtree(lines)
    gap = BUILDING_GAP_CLOSE_M / m
    connectors = []
    for line in lines:
        for end in (Point(line.coords[0]), Point(line.coords[-1])):
            if len([i for i in tree.query(end.buffer(1e-6)) if lines[int(i)].distance(end) < 1e-6]) > 1:
                continue  # the end meets another line
            best = None
            for i in tree.query(end.buffer(gap)):
                other = lines[int(i)]
                d = other.distance(end)
                if 1e-6 < d <= gap and (best is None or d < best[0]):
                    best = (d, other)
            if best is not None:
                target = nearest_points(end, best[1])[1]
                dx, dy = target.x - end.x, target.y - end.y
                overshoot = 1e-3 / m / math.hypot(dx, dy)  # 1 mm past the line, so the join is noded
                connectors.append(LineString([end, (target.x + dx * overshoot, target.y + dy * overshoot)]))
    cells = []
    for face in polygonize(unary_union(lines + connectors)):
        _, short_side, _ = _mrr(face)
        if face.area * m * m >= BUILDING_MIN_AREA_M2 and short_side * m >= BUILDING_MIN_SIDE_M:
            cells.append(face)
    return cells, len(connectors)


def read_buildings(doc: Any, unit: dict[str, Any], group_units: Any = None) -> dict[str, Any]:
    """``group_units(context) -> list of units`` replaces the rule-based grouping (e.g. by an LLM judge)."""

    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    to_mm = UNIT_TO_MM.get(str(unit.get("unit")))
    if to_mm is None:
        return {"status": "unit_unknown", "message_zh": "单位不明，不给建筑尺寸", "buildings": []}
    m = to_mm / 1000.0  # metres per drawing unit
    data = collect(doc)

    def split(pieces: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        walls, symbols = [], []
        for piece in pieces:
            (symbols if _extent(piece["points"]) * m < BUILDING_SYMBOL_MAX_M else walls).append(piece)
        return walls, symbols

    building_walls, symbol_pieces = split(data["building_pieces"])
    other_walls, _ = split(data["other_pieces"])
    if not building_walls:
        return {"status": "no_building_outlines", "message_zh": "图上没有建筑物图层的轮廓线", "buildings": []}
    layer_cells, joined_layer = _cells(building_walls, m)
    # Faces of all plan linework were tried as name holders (2026-10-01) and rejected: title-block,
    # legend and frame cells became "buildings" and generic-layer lines split the real outlines.
    plan_cells, joined_plan = layer_cells, 0

    terms = _LEXICON["elevation_legend_terms"]
    legend = {"indoor_floor": False, "ground": False}
    for t in data["texts"]:
        if any(w in t["text"] for w in terms["elevation_words"]):
            legend["indoor_floor"] |= any(w in t["text"] for w in terms["indoor_floor"])
            legend["ground"] |= any(w in t["text"] for w in terms["ground"])

    def smallest(cells: list[Any], xy: list[float]) -> int | None:
        p = Point(*xy)
        holding = sorted((c.area, i) for i, c in enumerate(cells) if c.contains(p))
        return holding[0][1] if holding else None

    grouping_receipt = None
    if group_units is not None:
        got = group_units({"data": data, "m": m, "building_walls": building_walls, "other_walls": other_walls, "layer_cells": layer_cells,
                           "symbol_pieces": symbol_pieces, "legend": legend})
        units, grouping_receipt = got["units"], got["receipt"]
    else:
        units = _rule_units(data, plan_cells, layer_cells, smallest)
    return _describe(units, data, m, unit, legend, symbol_pieces, smallest, grouping_receipt, joined_plan, joined_layer)


def _rule_units(data: dict[str, Any], plan_cells: list[Any], layer_cells: list[Any], smallest: Any) -> list[dict[str, Any]]:
    from shapely.ops import unary_union

    # named buildings: the smallest plan cell holding the name
    named: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in data["texts"]:
        if is_name(t["text"]):
            k = smallest(plan_cells, t["xy"])
            if k is not None:
                named[k].append(t)
    units = [{"polygon": plan_cells[k], "names": sorted(ts, key=lambda t: t["ref"]), "source": "named_cell"} for k, ts in sorted(named.items())]
    named_union = unary_union([u["polygon"] for u in units]) if units else None
    for cell in layer_cells:  # unnamed buildings: building-layer cells no named building covers
        if named_union is not None and cell.intersection(named_union).area > 0.5 * cell.area:
            continue
        units.append({"polygon": cell, "names": [], "source": "building_layer_cell"})
    return units


def _describe(units: list[dict[str, Any]], data: dict[str, Any], m: float, unit: dict[str, Any], legend: dict[str, bool],
              symbol_pieces: list[dict[str, Any]], smallest: Any, grouping_receipt: Any, joined_plan: int, joined_layer: int) -> dict[str, Any]:
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    # elevations
    elevations: dict[int, list[dict[str, Any]]] = defaultdict(list)
    site_elevations = []
    polygons = [u["polygon"] for u in units]
    for t in data["texts"]:
        if not ELEVATION_TEXT.match(t["text"]):
            continue
        k = smallest(polygons, t["xy"])
        if k is not None:
            elevations[k].append({"value": t["text"], "ref": t["ref"], "type": "室内地面标高（依图例）" if legend["indoor_floor"] else "类型待确认"})
        else:
            site_elevations.append({"value": t["text"], "ref": t["ref"], "xy": t["xy"], "type": "场地/道路标高（依图例）" if legend["ground"] else "类型待确认",
                                    "note": "不在任何建筑轮廓内，不归给建筑"})

    # column symbols: clusters of short pieces attached to an outline
    sym_lines = [LineString(p["points"]) for p in symbol_pieces]
    parent = list(range(len(sym_lines)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if sym_lines:
        stree = STRtree(sym_lines)
        for i, g in enumerate(sym_lines):
            for j in stree.query(g.buffer(SYMBOL_CLUSTER_GAP_M / m)):
                j = int(j)
                if j > i and g.distance(sym_lines[j]) <= SYMBOL_CLUSTER_GAP_M / m:
                    parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(sym_lines)):
        clusters[find(i)].append(i)
    attached: list[tuple[int, tuple, tuple[float, float]]] = []
    for members in clusters.values():
        geom = unary_union([sym_lines[i] for i in members])
        centre = geom.centroid
        best = None
        for k, poly in enumerate(polygons):
            d = poly.exterior.distance(centre)
            if (poly.contains(centre) or d <= COLUMN_ON_OUTLINE_M / m) and (best is None or poly.area > best[1]):  # the main structure owns its walls' columns
                best = (k, poly.area)
        if best is None:
            continue
        hull = geom.convex_hull
        dims = sorted(round(v * m, 1) for v in _mrr(hull)[:2]) if hull.area > 0 else [0.0, round(geom.length * m, 1)]
        signature = (len(members), tuple(dims), any(symbol_pieces[i]["curved"] for i in members))
        attached.append((best[0], signature, (centre.x, centre.y)))
    by_signature = Counter(sig for _, sig, _ in attached)
    column_signature = by_signature.most_common(1)[0][0] if by_signature else None

    buildings = []
    for k, u in enumerate(units):
        poly = u["polygon"]
        long_side, short_side, angle = _mrr(poly)
        cols = [xy for i, sig, xy in attached if i == k and sig == column_signature]
        others = Counter(sig for i, sig, _ in attached if i == k and sig != column_signature)
        centroid = poly.centroid
        buildings.append({
            "name": "；".join(t["text"] for t in u["names"]) if u["names"] else None,
            "name_refs": [t["ref"] for t in u["names"]], "status": u.get("status") or ("named" if u["names"] else "未标注"), "found_by": u["source"],
            "cells": u.get("cells", []),
            "area_m2": round(poly.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2),
            "length_width_basis": "沿建筑自身方向的最小外接矩形",
            "rectangular": poly.area / max(poly.minimum_rotated_rectangle.area, 1e-9) >= RECTANGULARITY_MIN,
            "orientation_deg": round(angle, 3), "centroid": [round(centroid.x, 3), round(centroid.y, 3)],
            "outline": [[round(x, 3), round(y, 3)] for x, y in poly.exterior.coords],
            "columns": {"count": len(cols), "lines": _column_lines(cols, angle, COLUMN_ON_OUTLINE_M / m) if cols else [],
                        "note": "按图上重复最多的贴墙符号计" if cols else "图上无柱符号"},
            "other_attached_symbols": [{"signature": list(sig), "count": n, "status": "未判定"} for sig, n in sorted(others.items(), key=str)],
            "elevations": elevations.get(k, []),
            "height": "图上无", "height_note": "室内地面标高不是建筑高度",
        })
    order = sorted(range(len(buildings)), key=lambda i: (buildings[i]["status"] != "named", -buildings[i]["area_m2"]))
    buildings = [buildings[i] for i in order]
    polygons = [polygons[i] for i in order]
    for i, b in enumerate(buildings):
        b["building_id"] = f"BLD-{i + 1:03d}"

    # complexes: buildings sharing walls (a partition line does not tell one building from two)
    parent = list(range(len(buildings)))
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            if polygons[i].boundary.intersection(polygons[j].boundary).length * m >= BUILDING_MIN_SIDE_M:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(buildings)):
        groups[find(i)].append(i)
    complexes = []
    for members in groups.values():
        if len(members) < 2:
            continue
        union = unary_union([polygons[i] for i in members])
        long_side, short_side, angle = _mrr(union)
        complexes.append({"members": [buildings[i]["building_id"] for i in members],
                          "names": [buildings[i]["name"] for i in members if buildings[i]["name"]],
                          "area_m2": round(union.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2),
                          "orientation_deg": round(angle, 3),
                          "outline": [[round(x, 3), round(y, 3)] for x, y in (union.exterior.coords if union.geom_type == "Polygon" else [])]})
    return {"status": "done", "unit": unit.get("unit"), "metres_per_unit": m, "legend": legend, "buildings": buildings,
            "complexes": sorted(complexes, key=lambda c: -c["area_m2"]), "site_elevations": site_elevations,
            "joined_gaps": {"plan": joined_plan, "building_layers": joined_layer},
            "column_symbol_signature": list(column_signature) if column_signature else None, "grouping": grouping_receipt}


def buildings_markdown(result: dict[str, Any]) -> str:
    if result.get("status") != "done":
        return f"# 建筑几何\n\n{result.get('message_zh', result.get('status'))}\n"
    lines = ["# 建筑几何清单", "", "柱子、标高、位置朝向及读全图范围为助手默认，待确认。标高一栏是地面标高，不是建筑高度。", "",
             "| 编号 | 名称 | 面积 m² | 长×宽 m | 矩形 | 朝向° | 柱子 | 标高 | 高度 |", "|---|---|---|---|---|---|---|---|---|"]
    for b in result["buildings"]:
        elev = "；".join(f"{e['value']}（{e['type']}）" for e in b["elevations"]) or "—"
        lines.append(f"| {b['building_id']} | {b['name'] or '未标注'} | {b['area_m2']} | {b['length_m']}×{b['width_m']} | {'是' if b['rectangular'] else '否'} | "
                     f"{b['orientation_deg']} | {b['columns']['count'] or '图上无'} | {elev} | {b['height']} |")
    if result.get("complexes"):
        lines += ["", "相连的建筑（共用墙线；图上分隔线不能说明是一栋还是几栋，两种读法都列出）：", ""]
        for c in result["complexes"]:
            lines.append(f"- {'、'.join(c['members'])}（{'、'.join(c['names']) or '无名称'}）：合计 {c['area_m2']} m²，{c['length_m']}×{c['width_m']} m")
    if result["site_elevations"]:
        lines += ["", "场地/道路标高（不归给任何建筑）：" + "、".join(e["value"] for e in result["site_elevations"])]
    return "\n".join(lines) + "\n"


__all__ = ["buildings_markdown", "collect", "is_building_layer", "is_name", "read_buildings"]
