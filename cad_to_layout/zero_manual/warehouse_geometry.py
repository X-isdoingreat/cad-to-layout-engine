"""Warehouse geometry per view: axis-grid extent and exterior-wall outline, zero manual.

For every view the building is measured two ways, each with its evidence:

* by axis grid: the perpendicular distance between the first and last grid line in
  each of the two dominant directions (not the line extents: grid lines overshoot
  the building and carry label bubbles), the bay spacings, and the first/last
  labels; the result is checked against dimensions drawn on the sheet;
* by exterior wall: wall linework thickened, joined, door openings closed, the
  outer ring taken; its minimum rotated rectangle gives length x width.

Structure layers are found only by the frozen lexicon (``structure_roles``);
nothing is written for a particular drawing.  A view without a known unit gets no
numbers.  Elevation texts are listed verbatim; no roof height is inferred.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from .constants import (
    AXIS_DIMENSION_MATCH_FRACTION,
    AXIS_DIRECTION_TOLERANCE_DEG,
    AXIS_MERGE_MM,
    AXIS_MIN_RELATIVE_LENGTH,
    DOOR_OPENING_MAX_MM,
    RECTANGULARITY_MIN,
    WALL_AXIS_MAX_DIFFERENCE_FRACTION,
    WALL_OUTLINE_MIN_EXTENT_FRACTION,
    WALL_THICKNESS_MAX_MM,
    WALL_THICKNESS_MIN_MM,
    WALL_THICKNESS_MIN_SEGMENT_MM,
)
from .expand import iter_world_items

_LEXICON = json.loads((Path(__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8"))
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inch": 25.4, "foot": 304.8}
_ELEVATION_NUMBER = re.compile(r"(?:%%[pP]|±|\+|-)?\d+\.\d{3}")
ROLES = {"BUILDING_WALL": "wall", "COLUMN": "column", "AXIS_LINE": "axis", "ELEVATION_MARK": "elevation"}


def layer_role(layer: str) -> str | None:
    folded = layer.casefold()
    for key, terms in _LEXICON["structure_roles"].items():
        if any(term.casefold() in folded for term in terms):
            return ROLES.get(key)
    return None


@dataclass
class StructureItems:
    """World-coordinate structure geometry of one drawing (one walk, filtered per view)."""

    segments: list[dict[str, Any]] = field(default_factory=list)   # role, layer, ref, points (open polyline)
    polygons: list[dict[str, Any]] = field(default_factory=list)   # role, layer, ref, points (closed ring)
    circles: list[dict[str, Any]] = field(default_factory=list)    # role, layer, ref, center, radius
    texts: list[dict[str, Any]] = field(default_factory=list)      # layer, ref, text, xy
    layers: dict[str, str] = field(default_factory=dict)           # layer -> role


def _pt(matrix: Any, point: Any) -> tuple[float, float]:
    v = matrix.transform((float(point[0]), float(point[1]), float(point[2]) if len(point) > 2 else 0.0))
    return float(v.x), float(v.y)


def collect_structure(doc: Any) -> StructureItems:
    """One walk over model space collecting wall/column/axis geometry and all texts."""

    from ezdxf import path as ezpath

    items = StructureItems()
    for item in iter_world_items(doc):
        kind = item.dxftype
        entity = item.entity
        if kind in {"TEXT", "MTEXT", "ATTRIB"}:
            try:
                text = entity.plain_text().strip() if kind == "MTEXT" else str(entity.dxf.text or "").strip()
                if text:
                    items.texts.append({"layer": item.layer, "ref": f"T:{item.ref}", "text": text, "xy": list(_pt(item.matrix, entity.dxf.insert))})
            except Exception:
                pass
            continue
        role = layer_role(item.layer)
        if role is None or role == "elevation":
            continue
        items.layers[item.layer] = role
        try:
            if kind == "LINE":
                items.segments.append({"role": role, "layer": item.layer, "ref": f"S:{item.ref}",
                                       "points": [_pt(item.matrix, entity.dxf.start), _pt(item.matrix, entity.dxf.end)]})
            elif kind == "LWPOLYLINE":
                points = [_pt(item.matrix, (x, y, 0.0)) for x, y, *_ in entity.get_points()]
                target = items.polygons if entity.closed and len(points) >= 3 else items.segments
                if len(points) >= 2:
                    target.append({"role": role, "layer": item.layer, "ref": f"S:{item.ref}", "points": points})
            elif kind == "POLYLINE" and not entity.is_3d_polyline:
                points = [_pt(item.matrix, v.dxf.location) for v in entity.vertices]
                target = items.polygons if entity.is_closed and len(points) >= 3 else items.segments
                if len(points) >= 2:
                    target.append({"role": role, "layer": item.layer, "ref": f"S:{item.ref}", "points": points})
            elif kind == "HATCH":
                for path in ezpath.from_hatch(entity):
                    points = [_pt(item.matrix, p) for p in path.flattening(10.0, segments=8)]
                    if len(points) >= 3:
                        items.polygons.append({"role": role, "layer": item.layer, "ref": f"H:{item.ref}", "points": points})
            elif kind == "CIRCLE":
                scale = math.hypot(*(_pt(item.matrix, (1, 0, 0))[i] - _pt(item.matrix, (0, 0, 0))[i] for i in range(2)))
                items.circles.append({"role": role, "layer": item.layer, "ref": f"C:{item.ref}",
                                      "center": list(_pt(item.matrix, entity.dxf.center)), "radius": float(entity.dxf.radius) * scale})
        except Exception:
            continue
    return items


def _inside(point: Any, window: list[float]) -> bool:
    return window[0] <= point[0] <= window[2] and window[1] <= point[1] <= window[3]


def _centre(points: list[Any]) -> tuple[float, float]:
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _pieces(rows: list[dict[str, Any]], role: str, window: list[float]) -> list[dict[str, Any]]:
    return [r for r in rows if r["role"] == role and _inside(_centre(r["points"]), window)]


def _segments_of(rows: list[dict[str, Any]]) -> list[tuple[tuple[float, float], tuple[float, float], str]]:
    out = []
    for row in rows:
        pts = row["points"]
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) > 1e-6:
                out.append((a, b, row["ref"]))
    return out


def _angle(a: Any, b: Any) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _dominant_directions(segments: list[tuple[Any, Any, str]]) -> tuple[float, float] | None:
    weights: dict[int, float] = {}
    for a, b, _ in segments:
        key = int(round(_angle(a, b))) % 180
        weights[key] = weights.get(key, 0.0) + math.dist(a, b)
    if not weights:
        return None
    first = max(weights, key=weights.get)
    perpendicular = [k for k in weights if _angle_diff(k, first + 90) <= AXIS_DIRECTION_TOLERANCE_DEG]
    second = max(perpendicular, key=weights.get) if perpendicular else (first + 90) % 180
    return float(first), float(second)


def axis_grid(items: StructureItems, window: list[float], scale_mm: float, dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _pieces(items.segments, "axis", window) + _pieces(items.polygons, "axis", window)
    segments = _segments_of(rows)
    if not segments:
        return {"status": "no_axis_linework", "message_zh": "视图内没有轴线图层的线"}
    longest = max(math.dist(a, b) for a, b, _ in segments)
    segments = [s for s in segments if math.dist(s[0], s[1]) >= longest * AXIS_MIN_RELATIVE_LENGTH]
    directions = _dominant_directions(segments)
    if directions is None:
        return {"status": "no_axis_linework", "message_zh": "视图内没有轴线图层的线"}
    bubbles = [c for c in items.circles if c["role"] == "axis" and _inside(c["center"], window)]
    groups = []
    for direction in directions:
        u = (math.cos(math.radians(direction)), math.sin(math.radians(direction)))
        n = (-u[1], u[0])
        if n[0] < -1e-9 or (abs(n[0]) <= 1e-9 and n[1] < 0):  # offsets grow with x (then y)
            n = (-n[0], -n[1])
        members = [s for s in segments if _angle_diff(_angle(s[0], s[1]), direction) <= AXIS_DIRECTION_TOLERANCE_DEG]
        offsets = sorted(((s[0][0] + s[1][0]) / 2 * n[0] + (s[0][1] + s[1][1]) / 2 * n[1], s[2]) for s in members)
        lines: list[dict[str, Any]] = []
        for value, ref in offsets:
            if lines and (value - lines[-1]["offsets"][-1]) * scale_mm <= AXIS_MERGE_MM:
                lines[-1]["offsets"].append(value)
                lines[-1]["refs"].append(ref)
            else:
                lines.append({"offsets": [value], "refs": [ref]})
        for line in lines:
            line["offset"] = statistics.fmean(line["offsets"])
            line["label"] = None
        # a bubble labels the grid line whose offset passes through it
        for bubble in bubbles:
            c = bubble["center"]
            along = c[0] * n[0] + c[1] * n[1]
            best = min(lines, key=lambda ln: abs(ln["offset"] - along), default=None)
            if best is None or abs(best["offset"] - along) > bubble["radius"] * 1.2:
                continue
            label = next((t["text"] for t in items.texts if math.dist(t["xy"], c) <= bubble["radius"] * 1.2 and 1 <= len(t["text"]) <= 4), None)
            if label and best["label"] is None:
                best["label"] = label
        if len(lines) < 2:
            groups.append({"direction_deg": direction, "line_count": len(lines), "extent_mm": None})
            continue
        extent = (lines[-1]["offset"] - lines[0]["offset"]) * scale_mm
        spacings = [round((b["offset"] - a["offset"]) * scale_mm, 1) for a, b in zip(lines, lines[1:])]
        groups.append({
            "direction_deg": direction, "line_count": len(lines), "extent_mm": round(extent, 1),
            "spacings_mm": spacings, "first_label": lines[0]["label"], "last_label": lines[-1]["label"],
            "labels": [ln["label"] for ln in lines], "evidence_refs": [r for ln in (lines[0], lines[-1]) for r in ln["refs"][:2]],
            "offsets_world": [round(lines[0]["offset"], 3), round(lines[-1]["offset"], 3)],
        })
    measured = [g for g in groups if g.get("extent_mm")]
    if len(measured) < 2:
        return {"status": "incomplete_grid", "groups": groups, "message_zh": "只找到一个方向的轴线"}
    # cross-check against dimensions drawn in the view
    view_dims = [d for d in dimensions if any(_inside(p, window) for p in d.get("defpoints") or [])]
    for group in measured:
        target = group["extent_mm"]
        match = next((d for d in view_dims if abs(float(d["measurement"]) * float(d.get("dimlfac") or 1.0) * scale_mm - target) <= target * AXIS_DIMENSION_MATCH_FRACTION), None)
        group["dimension_check"] = {"status": "matches_drawn_dimension", "ref": match["ref"], "value_mm": round(float(match["measurement"]) * scale_mm, 1)} if match else {"status": "no_matching_dimension"}
    length, width = sorted((g["extent_mm"] for g in measured), reverse=True)[:2]
    return {"status": "found", "length_mm": length, "width_mm": width, "groups": groups}


def _wall_thickness(segments: list[tuple[Any, Any, str]], scale_mm: float) -> float | None:
    votes: list[float] = []
    long_segments = [s for s in segments if math.dist(s[0], s[1]) * scale_mm >= WALL_THICKNESS_MIN_SEGMENT_MM]
    for i, (a, b, _) in enumerate(long_segments):
        direction = _angle(a, b)
        u = ((b[0] - a[0]) / math.dist(a, b), (b[1] - a[1]) / math.dist(a, b))
        n = (-u[1], u[0])
        best = None
        for j, (c, d, _) in enumerate(long_segments):
            if i == j or _angle_diff(_angle(c, d), direction) > 1.0:
                continue
            gap = abs((c[0] - a[0]) * n[0] + (c[1] - a[1]) * n[1]) * scale_mm
            if not (WALL_THICKNESS_MIN_MM <= gap <= WALL_THICKNESS_MAX_MM):
                continue
            t1 = sorted(((a[0] - a[0]) * u[0] + (a[1] - a[1]) * u[1], (b[0] - a[0]) * u[0] + (b[1] - a[1]) * u[1]))
            t2 = sorted(((c[0] - a[0]) * u[0] + (c[1] - a[1]) * u[1], (d[0] - a[0]) * u[0] + (d[1] - a[1]) * u[1]))
            if min(t1[1], t2[1]) - max(t1[0], t2[0]) <= 0:
                continue
            best = gap if best is None else min(best, gap)
        if best is not None:
            votes.append(round(best / 10.0) * 10.0)
    if not votes:
        return None
    return statistics.mode(votes)


def exterior_wall(items: StructureItems, window: list[float], scale_mm: float) -> dict[str, Any]:
    rows = _pieces(items.segments, "wall", window)
    polys = _pieces(items.polygons, "wall", window)
    segments = _segments_of(rows) + _segments_of([{"points": [*p["points"], p["points"][0]], "ref": p["ref"]} for p in polys])
    if not segments:
        return {"status": "no_wall_linework", "message_zh": "视图内没有墙图层的线"}
    thickness_mm = _wall_thickness(segments, scale_mm)
    join_mm = thickness_mm if thickness_mm is not None else WALL_THICKNESS_MAX_MM
    half = join_mm / 2.0 / scale_mm
    shapes = [LineString([a, b]).buffer(half, cap_style=2) for a, b, _ in segments]
    shapes += [Polygon(p["points"]).buffer(0) for p in polys if len(p["points"]) >= 3]
    merged = unary_union([s for s in shapes if not s.is_empty])
    door = DOOR_OPENING_MAX_MM / 2.0 / scale_mm
    closed = merged.buffer(door, join_style=2).buffer(-door, join_style=2)
    parts = list(closed.geoms) if hasattr(closed, "geoms") else [closed]
    parts = [p for p in parts if isinstance(p, Polygon) and not p.is_empty]
    if not parts:
        return {"status": "open_wall_linework", "message_zh": "墙线未闭合"}
    body = max(parts, key=lambda p: p.area)
    outline = Polygon(body.exterior).buffer(-half, join_style=2)
    if outline.is_empty:
        return {"status": "open_wall_linework", "message_zh": "墙线未闭合"}
    if hasattr(outline, "geoms"):
        outline = max(outline.geoms, key=lambda p: p.area)
    xs = [p[0] for a, b, _ in segments for p in (a, b)]
    ys = [p[1] for a, b, _ in segments for p in (a, b)]
    linework_extent = [(max(xs) - min(xs)) * scale_mm, (max(ys) - min(ys)) * scale_mm]
    ox1, oy1, ox2, oy2 = outline.bounds
    if ((ox2 - ox1) * scale_mm < linework_extent[0] * WALL_OUTLINE_MIN_EXTENT_FRACTION
            or (oy2 - oy1) * scale_mm < linework_extent[1] * WALL_OUTLINE_MIN_EXTENT_FRACTION):
        return {"status": "open_wall_linework",
                "message_zh": "墙线不闭合（例如只画了柱或零散墙段），不给外墙轮廓",
                "linework_extent_mm": [round(v, 1) for v in linework_extent],
                "linework_extent_note_zh": "墙图层线条的分布范围，不是轮廓",
                "wall_pieces": len(rows) + len(polys), "wall_layers": sorted({r["layer"] for r in rows + polys})}
    rect = outline.minimum_rotated_rectangle
    corners = list(rect.exterior.coords)[:4]
    sides = sorted([math.dist(corners[0], corners[1]) * scale_mm, math.dist(corners[1], corners[2]) * scale_mm], reverse=True)
    rectangularity = outline.area / rect.area if rect.area else 0.0
    return {
        "status": "found",
        "length_mm": round(sides[0], 1), "width_mm": round(sides[1], 1),
        "area_m2": round(outline.area * scale_mm * scale_mm / 1e6, 1),
        "rectangular": rectangularity >= RECTANGULARITY_MIN, "rectangularity": round(rectangularity, 3),
        "wall_thickness_mm": thickness_mm, "wall_thickness_status": "measured" if thickness_mm is not None else "not_measured_joined_with_max",
        "outline": [[round(x, 3), round(y, 3)] for x, y in list(outline.exterior.coords)],
        "other_parts": len(parts) - 1,
        "wall_layers": sorted({r["layer"] for r in rows + polys}),
    }


def elevation_texts(items: StructureItems, window: list[float]) -> list[dict[str, Any]]:
    words = [w for w in _LEXICON["structure_roles"]["ELEVATION_MARK"]]
    found = []
    for t in items.texts:
        if not _inside(t["xy"], window):
            continue
        if _ELEVATION_NUMBER.search(t["text"]) or any(w in t["text"] for w in words):
            found.append({"text": t["text"][:60], "ref": t["ref"], "xy": [round(t["xy"][0]), round(t["xy"][1])]})
    return found[:30]


def measure_views(items: StructureItems, views: list[dict[str, Any]], unit: dict[str, Any], dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    scale = UNIT_TO_MM.get(str(unit.get("unit")))
    for view in views:
        if view.get("layout_role") == "detail":
            continue
        window = view["window"]
        row: dict[str, Any] = {"view_id": view["view_id"], "window": window, "source": view["source"],
                               "title": (view.get("title") or {}).get("text"), "role": view.get("role")}
        if scale is None:
            row.update({"status": "unit_unknown", "message_zh": "单位不明，不给数"})
            results.append(row)
            continue
        axis = axis_grid(items, window, scale, dimensions)
        wall = exterior_wall(items, window, scale)
        row["axis"] = axis
        row["wall"] = wall
        row["elevation_texts"] = elevation_texts(items, window)
        row["height_status"] = "annotated_levels_listed_not_interpreted" if row["elevation_texts"] else "none_on_drawing"
        if axis.get("status") != "found" and wall.get("status") != "found":
            row["status"] = "no_building_geometry"
            row["message_zh"] = "源图此视图无墙、无轴线"
        elif axis.get("status") == "found" and wall.get("status") == "found":
            diff = max(abs(wall["length_mm"] - axis["length_mm"]) / axis["length_mm"], abs(wall["width_mm"] - axis["width_mm"]) / axis["width_mm"])
            row["status"] = "consistent" if diff <= WALL_AXIS_MAX_DIFFERENCE_FRACTION else "needs_review"
            row["wall_axis_difference"] = round(diff, 4)
        else:
            row["status"] = "one_basis_only"
            if axis.get("status") != "found":
                row["message_zh"] = "只有墙线、没有轴网（可能是立面、剖面或详图），待核"
        if axis.get("status") == "found":
            confirmed = [g for g in axis["groups"] if g.get("dimension_check", {}).get("status") == "matches_drawn_dimension"]
            axis["confirmation"] = "both_directions" if len(confirmed) >= 2 else ("one_direction" if confirmed else "none")
            if not confirmed and row["status"] in {"one_basis_only", "consistent"}:
                row["status"] = "needs_review"
                row["message_zh"] = "轴网总长在图面上找不到对应标注，待核"
        if view.get("role") == "structural":
            row["note_zh"] = "结构图，非仓库平面"
        results.append(row)
    return results


def summary_markdown(results: list[dict[str, Any]], unit: dict[str, Any]) -> str:
    lines = ["# 仓库几何", "", f"单位：{unit.get('unit') or '不明'}（{unit.get('status')}）", "",
             "| 视图 | 标题 | 按轴线 长×宽 (m) | 轴线核对 | 按外墙 长×宽 (m) | 墙厚 (mm) | 状态 | 说明 |",
             "|---|---|---|---|---|---|---|---|"]
    hidden = 0
    for r in results:
        axis, wall = r.get("axis") or {}, r.get("wall") or {}
        if r.get("status") == "no_building_geometry":
            hidden += 1
            continue
        a = f"{axis['length_mm'] / 1000:.2f} × {axis['width_mm'] / 1000:.2f}" if axis.get("status") == "found" else "—"
        checks = [g.get("dimension_check", {}).get("status") for g in axis.get("groups", []) if g.get("extent_mm")]
        check = "与图面标注一致" if checks and all(c == "matches_drawn_dimension" for c in checks) else ("部分一致" if "matches_drawn_dimension" in checks else "未找到对应标注") if checks else "—"
        w = f"{wall['length_mm'] / 1000:.2f} × {wall['width_mm'] / 1000:.2f}" if wall.get("status") == "found" else "—"
        lines.append(f"| {r['view_id']} | {r.get('title') or ''} | {a} | {check} | {w} | {wall.get('wall_thickness_mm') or '—'} | {r.get('status')} | {r.get('note_zh') or r.get('message_zh') or ''} |")
    lines += ["", f"另有 {hidden} 个视图无墙、无轴线（说明、详图、设备区等），未列出。"]
    return "\n".join(lines) + "\n"


__all__ = ["StructureItems", "axis_grid", "collect_structure", "elevation_texts", "exterior_wall", "layer_role", "measure_views", "summary_markdown"]
