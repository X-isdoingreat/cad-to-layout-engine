"""Program-only candidates for equipment that is not drawn as "one block = one machine".

Every finding here is reproducible from the inventory alone, without any LLM:

* regular arrays - equal blocks under the same parent, on one line at a constant
  pitch and packed side by side, are modules of one group (rack bays in a row,
  conveyor segments in a lane);
* outline cell groups - equal closed rectangles sharing full edges (rack cells drawn
  as plain rectangles instead of blocks);
* number series - distinct numeric texts on one layer (the drawing's own equipment
  numbers); each number is one numbered unit, whether or not a block is drawn for it;
* bound texts - a text naming a machine that lies inside a closed outline makes that
  outline the machine; when the outline (or the extent of a group of equal blocks)
  holds several equal members instead, the text names those members (a zone label).

Texts bind by containment only, never by distance.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

from .constants import (
    ARRAY_ALIGN_TOLERANCE_MM,
    ARRAY_MAX_PITCH_SIZE_RATIO,
    ARRAY_MIN_MEMBERS,
    ARRAY_PITCH_TOLERANCE_FRACTION,
    CELL_GROUP_MIN_CELLS,
    CELL_GROUP_MIN_CELLS_KNOWN_SIZE,
    CELL_MAX_SIDE_MM,
    CELL_MIN_SIDE_MM,
    CELL_TOUCH_TOLERANCE_MM,
    EQUIPMENT_MIN_SIZE_MM,
    NUMBER_SERIES_MIN_DISTINCT_SHARE,
    NUMBER_SERIES_MIN_MEMBERS,
    ZONE_CLUSTER_GAP_MM,
    ZONE_MIN_MEMBERS,
)
from .warehouse_geometry import layer_role

STRUCTURE_ROLES = {"wall", "axis", "column"}
# "共 6 台": a drawn statement of how many machines there are, not a label of one.
QUANTITY_NOTE = re.compile(r"共\s*(\d+)\s*(台|套|部|个|组)(?![一-鿿])")


def centre_of(bbox: list[float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def view_of(point: tuple[float, float], views: list[dict[str, Any]]) -> str | None:
    """The smallest non-detail view holding the point."""

    best = None
    for v in views:
        if v.get("layout_role") == "detail":
            continue
        w = v["window"]
        if w[0] <= point[0] <= w[2] and w[1] <= point[1] <= w[3]:
            area = (w[2] - w[0]) * (w[3] - w[1])
            if best is None or area < best[0]:
                best = (area, v["view_id"])
    return None if best is None else best[1]


def top_level(ref: str, opened: Iterable[str]) -> bool:
    """True when the object is drawn in the view itself: every block above it was
    opened as a container (a whole plan placed as one block), none is a machine."""

    parts = ref.split(":", 1)[-1].split("/")
    opened = opened if isinstance(opened, (set, frozenset)) else set(opened)
    return all("I:" + "/".join(parts[:i]) in opened for i in range(1, len(parts)))


class _Union:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return list(out.values())


# ---------------------------------------------------------------- regular arrays
def _runs(points: list[tuple[float, float, int]], size_along: float) -> list[tuple[list[int], float]]:
    """points: (along, across, index) in millimetres.  Returns (member indexes, pitch)."""

    runs: list[tuple[list[int], float]] = []
    points = sorted(points, key=lambda p: (p[1], p[0]))
    lines: list[list[tuple[float, float, int]]] = []
    for p in points:
        if lines and abs(p[1] - lines[-1][-1][1]) <= ARRAY_ALIGN_TOLERANCE_MM:
            lines[-1].append(p)
        else:
            lines.append([p])
    for line in lines:
        line.sort()
        unique = [line[0]]
        for p in line[1:]:
            if p[0] - unique[-1][0] > 1.0:  # exact copies at the same place are one member
                unique.append(p)
        start = 0
        while start + 1 < len(unique):
            pitch = unique[start + 1][0] - unique[start][0]
            end = start + 1
            while end + 1 < len(unique) and abs((unique[end + 1][0] - unique[end][0]) - pitch) <= max(pitch * ARRAY_PITCH_TOLERANCE_FRACTION, 5.0):
                end += 1
            if end - start + 1 >= ARRAY_MIN_MEMBERS and pitch <= ARRAY_MAX_PITCH_SIZE_RATIO * size_along:
                runs.append(([p[2] for p in unique[start:end + 1]], pitch))
                start = end + 1
            else:
                start += 1
    return runs


def regular_arrays(families: dict[str, dict[str, Any]], scale: float) -> list[dict[str, Any]]:
    arrays: list[dict[str, Any]] = []
    for key in sorted(families):
        groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for occ in families[key]["occurrences"]:
            fp = occ["footprint"]
            if len(fp) < 4:
                continue
            angle = math.degrees(math.atan2(fp[1][1] - fp[0][1], fp[1][0] - fp[0][0])) % 180.0
            groups[(occ["parent_ref"], occ["view_id"], int(round(angle)) % 180)].append(occ)
        for (parent, view_id, _), members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            if len(members) < ARRAY_MIN_MEMBERS:
                continue
            fp = members[0]["footprint"]
            length = math.dist(fp[0], fp[1])
            if length == 0:
                continue
            u = ((fp[1][0] - fp[0][0]) / length, (fp[1][1] - fp[0][1]) / length)
            v = (-u[1], u[0])
            side_u, side_v = length * scale, math.dist(fp[1], fp[2]) * scale
            coords = []
            for index, occ in enumerate(members):
                cx = sum(p[0] for p in occ["footprint"]) / len(occ["footprint"])
                cy = sum(p[1] for p in occ["footprint"]) / len(occ["footprint"])
                coords.append(((cx * u[0] + cy * u[1]) * scale, (cx * v[0] + cy * v[1]) * scale, index))
            found = [(m, p, "length") for m, p in _runs(coords, side_u)]
            found += [(m, p, "width") for m, p in _runs([(b, a, i) for a, b, i in coords], side_v)]
            taken: set[int] = set()
            for indexes, pitch, axis in sorted(found, key=lambda r: (-len(r[0]), r[2], r[0][0])):
                free = [i for i in indexes if i not in taken]
                if len(free) < ARRAY_MIN_MEMBERS or len(free) != len(indexes):
                    continue
                taken.update(free)
                boxes = [members[i]["bbox"] for i in free]
                arrays.append({
                    "family": key, "parent_ref": parent, "view_id": view_id, "members": [members[i]["ref"] for i in free],
                    "count": len(free), "pitch_mm": round(pitch, 1), "along": axis,
                    "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
                })
    return arrays


# ---------------------------------------------------------------- closed rectangles
def _rectangle(points: list[list[float]]) -> tuple[float, tuple[float, float], tuple[float, float]] | None:
    """(angle of the frame in [0, 90), centre, (side along frame x, side along frame y)) or None."""

    pts = [tuple(p) for p in points]
    if len(pts) == 5 and math.dist(pts[0], pts[4]) < 1e-6:
        pts = pts[:4]
    if len(pts) != 4:
        return None
    edges = [(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)]
    lengths = [math.hypot(*e) for e in edges]
    if min(lengths) <= 0:
        return None
    if abs(lengths[0] - lengths[2]) > 1e-3 * lengths[0] or abs(lengths[1] - lengths[3]) > 1e-3 * lengths[1]:
        return None
    if abs(edges[0][0] * edges[1][0] + edges[0][1] * edges[1][1]) > 1e-3 * lengths[0] * lengths[1]:
        return None
    angle = math.degrees(math.atan2(edges[0][1], edges[0][0])) % 180.0
    sides = (lengths[0], lengths[1])
    if angle >= 90.0 - 1e-6:
        angle -= 90.0
        sides = (lengths[1], lengths[0])
    centre = (sum(p[0] for p in pts) / 4.0, sum(p[1] for p in pts) / 4.0)
    return angle, centre, sides


def _shares_full_edge(a: tuple[float, float, float, float], b: tuple[float, float, float, float], tol: float) -> bool:
    for lo, hi, other_lo, other_hi in ((0, 2, 1, 3), (1, 3, 0, 2)):
        touching = abs(a[hi] - b[lo]) <= tol or abs(b[hi] - a[lo]) <= tol
        if touching and abs(a[other_lo] - b[other_lo]) <= tol and abs(a[other_hi] - b[other_hi]) <= tol:
            return True
    return False


def outline_cell_groups(outlines: list[dict[str, Any]], views: list[dict[str, Any]], scale: float,
                        opened: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Groups of closed rectangles that share full edges and repeat one cell size."""

    from shapely.geometry import box as shapely_box
    from shapely.strtree import STRtree

    opened = set(opened)
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for outline in outlines:
        if layer_role(outline["layer"]) in STRUCTURE_ROLES or not top_level(outline["ref"], opened):
            continue
        rect = _rectangle(outline["points"])
        if rect is None:
            continue
        angle, centre, sides = rect
        sides_mm = (sides[0] * scale, sides[1] * scale)
        if min(sides_mm) < CELL_MIN_SIDE_MM or max(sides_mm) > CELL_MAX_SIDE_MM:
            continue
        view_id = view_of(centre, views)
        if view_id is None:
            continue
        bucket = int(round(angle * 2.0)) % 180  # half-degree frames; 90 degrees wraps to 0
        theta = math.radians(bucket / 2.0)
        fx = (centre[0] * math.cos(theta) + centre[1] * math.sin(theta)) * scale
        fy = (-centre[0] * math.sin(theta) + centre[1] * math.cos(theta)) * scale
        frame = (fx - sides_mm[0] / 2.0, fy - sides_mm[1] / 2.0, fx + sides_mm[0] / 2.0, fy + sides_mm[1] / 2.0)
        buckets[(view_id, bucket)].append({"ref": outline["ref"], "bbox": outline["bbox"], "frame": frame, "layer": outline["layer"],
                                           "size_mm": [round(max(sides_mm)), round(min(sides_mm))], "angle_deg": bucket / 2.0})
    components: list[tuple[str, list[dict[str, Any]], tuple, int]] = []
    for (view_id, _), cells in sorted(buckets.items()):
        seen: set[tuple] = set()
        unique = []
        for cell in sorted(cells, key=lambda c: c["ref"]):
            key = tuple(round(v) for v in cell["frame"])
            if key not in seen:  # a rectangle drawn twice at the same place is one cell
                seen.add(key)
                unique.append(cell)
        boxes = [shapely_box(*c["frame"]) for c in unique]
        tree = STRtree(boxes)
        union = _Union(len(unique))
        for i, geom in enumerate(boxes):
            for j in tree.query(geom.buffer(CELL_TOUCH_TOLERANCE_MM)):
                j = int(j)
                if j > i and _shares_full_edge(unique[i]["frame"], unique[j]["frame"], CELL_TOUCH_TOLERANCE_MM):
                    union.join(i, j)
        for indexes in union.groups():
            members = [unique[i] for i in indexes]
            sizes = Counter(tuple(round(v / 10.0) * 10 for v in m["size_mm"]) for m in members)
            size, repeats = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            components.append((view_id, members, size, repeats))
    known = {(view_id, size) for view_id, _, size, repeats in components if repeats >= CELL_GROUP_MIN_CELLS}
    groups: list[dict[str, Any]] = []
    for view_id, members, size, repeats in components:
        if repeats < CELL_GROUP_MIN_CELLS:
            # a short run (beside a column) of cells whose size a full group in this view already has
            sizes = sorted({tuple(round(v / 10.0) * 10 for v in m["size_mm"]) for m in members} & {s for v, s in known if v == view_id})
            if len(members) < CELL_GROUP_MIN_CELLS_KNOWN_SIZE or not sizes:
                continue
            size = sizes[0]
        members.sort(key=lambda m: m["ref"])
        digest = hashlib.sha256("|".join(m["ref"] for m in members).encode("utf-8")).hexdigest()[:10]
        groups.append({
            "group_id": f"G:{digest}", "view_id": view_id, "cell_size_mm": [int(size[0]), int(size[1])], "count": len(members),
            "cells": [{"ref": m["ref"], "bbox": m["bbox"], "size_mm": m["size_mm"], "angle_deg": m["angle_deg"]} for m in members],
            "bbox": [min(m["bbox"][0] for m in members), min(m["bbox"][1] for m in members),
                     max(m["bbox"][2] for m in members), max(m["bbox"][3] for m in members)],
        })
    return sorted(groups, key=lambda g: g["group_id"])


# ---------------------------------------------------------------- equipment number series
NUMBER_TEXT = re.compile(r"\d{3,6}")


def number_series(texts: list[dict[str, Any]], views: list[dict[str, Any]], opened: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Series of distinct numeric texts drawn in a view (equipment numbers)."""

    opened = set(opened)
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for text in texts:
        value = text["text"].strip()
        if not NUMBER_TEXT.fullmatch(value) or layer_role(text["layer"]) in STRUCTURE_ROLES or not top_level(text["ref"], opened):
            continue
        view_id = view_of(tuple(text["xy"]), views)
        if view_id is None:
            continue
        height = float(text.get("height") or 0.0)
        # the middle of the written number (insertion point is its lower-left corner)
        anchor = [text["xy"][0] + 0.35 * height * len(value), text["xy"][1] + 0.5 * height]
        groups[(view_id, text["layer"], len(value), round(height))].append({"ref": text["ref"], "number": value, "xy": text["xy"], "anchor": anchor, "height": height})
    series = []
    for (view_id, layer, digits, height), members in sorted(groups.items()):
        distinct = len({m["number"] for m in members})
        if len(members) < NUMBER_SERIES_MIN_MEMBERS or distinct < NUMBER_SERIES_MIN_DISTINCT_SHARE * len(members):
            continue
        repeats = Counter(m["number"] for m in members)
        series.append({"view_id": view_id, "layer": layer, "digits": digits, "text_height": height, "count": len(members), "distinct": distinct,
                       "repeated_numbers": sorted(n for n, k in repeats.items() if k > 1), "members": sorted(members, key=lambda m: m["ref"])})
    return series


# ---------------------------------------------------------------- texts bound by containment
def _clusters(members: list[dict[str, Any]], gap: float) -> list[dict[str, Any]]:
    from shapely.geometry import box as shapely_box
    from shapely.strtree import STRtree

    boxes = [shapely_box(*m["bbox"]) for m in members]
    tree = STRtree(boxes)
    union = _Union(len(members))
    for i, geom in enumerate(boxes):
        for j in tree.query(geom.buffer(gap)):
            j = int(j)
            if j > i and members[i]["view_id"] == members[j]["view_id"] and geom.distance(boxes[j]) <= gap:
                union.join(i, j)
    out = []
    for indexes in union.groups():
        group = [members[i]["bbox"] for i in indexes]
        out.append({"count": len(indexes), "view_id": members[indexes[0]]["view_id"],
                    "bbox": [min(b[0] for b in group), min(b[1] for b in group), max(b[2] for b in group), max(b[3] for b in group)]})
    return out


def bind_texts(texts: list[dict[str, Any]], outlines: list[dict[str, Any]], views: list[dict[str, Any]], scale: float,
               lookup: Callable[[str], dict[str, Any] | None], member_sets: dict[str, list[dict[str, Any]]],
               opened: Iterable[str] = ()) -> dict[str, Any]:
    """Bind machine-name texts to what they lie inside.

    ``lookup(text)`` returns ``{"type", "name", "term"}`` when the text names exactly
    one machine kind.  ``member_sets`` maps a key (a block family or a cell size
    class) to its members ``{"ref", "bbox", "view_id"}``.
    """

    from shapely.geometry import Point, Polygon
    from shapely.strtree import STRtree

    opened = set(opened)
    polygons, rows = [], []
    for outline in outlines:
        if layer_role(outline["layer"]) in STRUCTURE_ROLES or not top_level(outline["ref"], opened):
            continue
        b = outline["bbox"]
        if max(b[2] - b[0], b[3] - b[1]) * scale < EQUIPMENT_MIN_SIZE_MM:
            continue
        polygon = Polygon(outline["points"])
        if not polygon.is_valid or polygon.area <= 0:
            continue
        polygons.append(polygon)
        rows.append(outline)
    tree = STRtree(polygons) if polygons else None
    centres = {key: [(centre_of(m["bbox"]), m["view_id"]) for m in members] for key, members in member_sets.items()}
    member_key = {m["ref"]: key for key, members in member_sets.items() for m in members}
    clusters: dict[str, list[dict[str, Any]]] = {}

    devices: dict[str, dict[str, Any]] = {}
    zones, quantities, unbound, conflicts = [], [], [], []
    for text in sorted(texts, key=lambda t: t["ref"]):
        if not top_level(text["ref"], opened):
            continue
        view_id = view_of(tuple(text["xy"]), views)
        if view_id is None:
            continue
        named = lookup(text["text"])
        if named is None:
            continue
        base = {"text": text["text"][:60], "text_ref": text["ref"], "view_id": view_id, **named}
        note = QUANTITY_NOTE.search(text["text"])
        if note:
            quantities.append({**base, "stated": int(note.group(1))})
            continue
        point = Point(*text["xy"])
        holder = None
        if tree is not None:
            inside = [int(i) for i in tree.query(point) if polygons[int(i)].contains(point)]
            if inside:
                holder = min(inside, key=lambda i: polygons[i].area)
        if holder is not None:
            polygon, outline = polygons[holder], rows[holder]
            if outline["ref"] in member_key:  # written inside one cell of a group: it names the group's cells
                key = member_key[outline["ref"]]
                zones.append({**base, "key": key, "members_inside": sum(1 for _, v in centres[key] if v == view_id),
                              "kind": "zone_label_inside_member", "holder_ref": outline["ref"]})
                continue
            counts = Counter({key: sum(1 for c, v in pts if v == view_id and polygon.contains(Point(*c))) for key, pts in centres.items()})
            top = max(counts.values(), default=0)
            best = [k for k, n in counts.items() if n >= ZONE_MIN_MEMBERS and n == top]
            if len(best) == 1:
                zones.append({**base, "key": best[0], "members_inside": counts[best[0]], "kind": "zone_label_inside_outline", "holder_ref": outline["ref"]})
                continue
            if len(best) > 1:
                conflicts.append({**base, "reason": "several_member_kinds_equally_common", "keys": sorted(best)})
                continue
            if named.get("zone_only"):  # an area name never makes the outline one machine
                unbound.append({**base, "reason": "area_name_without_equal_members", "holder_ref": outline["ref"]})
                continue
            corners = list(polygon.minimum_rotated_rectangle.exterior.coords)
            a, b = math.dist(corners[0], corners[1]) * scale, math.dist(corners[1], corners[2]) * scale
            device = {**base, "ref": outline["ref"], "layer": outline["layer"], "bbox": outline["bbox"], "kind": "label_inside_outline",
                      "size_mm": [round(max(a, b), 1), round(min(a, b), 1)], "points": outline["points"],
                      "rotation_deg": round(math.degrees(math.atan2(corners[1][1] - corners[0][1], corners[1][0] - corners[0][0])) % 180.0, 2)}
            earlier = devices.get(outline["ref"])
            if earlier is None:
                devices[outline["ref"]] = device
            elif earlier.get("type") != device["type"] or earlier.get("name") != device["name"]:
                earlier["conflict"] = True
                conflicts.append({**base, "reason": "outline_holds_two_machine_names", "holder_ref": outline["ref"]})
            continue
        holding = []
        for key, members in member_sets.items():
            if key not in clusters:
                clusters[key] = _clusters(members, ZONE_CLUSTER_GAP_MM / scale)
            for cluster in clusters[key]:
                b = cluster["bbox"]
                if cluster["view_id"] == view_id and cluster["count"] >= ZONE_MIN_MEMBERS and b[0] <= text["xy"][0] <= b[2] and b[1] <= text["xy"][1] <= b[3]:
                    holding.append((cluster["count"], key, cluster["bbox"]))
        holding.sort(key=lambda h: (-h[0], h[1]))
        if holding and (len(holding) == 1 or holding[0][0] > holding[1][0]):
            zones.append({**base, "key": holding[0][1], "members_inside": holding[0][0], "kind": "zone_label_inside_group_extent", "group_bbox": holding[0][2]})
        elif holding:
            conflicts.append({**base, "reason": "several_member_kinds_equally_common", "keys": sorted(h[1] for h in holding if h[0] == holding[0][0])})
        else:
            unbound.append(base)
    return {"devices": [d for d in devices.values() if not d.get("conflict")], "zones": zones, "stated_quantities": quantities,
            "unbound_labels": unbound, "conflicts": conflicts}


__all__ = ["QUANTITY_NOTE", "bind_texts", "centre_of", "number_series", "outline_cell_groups", "regular_arrays", "top_level", "view_of"]
