"""Split a drawing into views without any per-drawing window.

1. Paper-space viewports become model-space windows; per layout, the viewport
   covering the largest model area with real content is the main view.
2. Model-space drawing frames (standard paper size x standard scale) are views.
3. Remaining model-space content is clustered by gaps (frozen constants), with
   oversized container blocks opened up; every cluster is a view.
4. Titles and roles come from the frozen lexicon (view_role_terms).
View ids are geometry hashes, never names.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from shapely.geometry import box
from shapely.strtree import STRtree

from .constants import (
    CONTAINER_EXPAND_MEDIAN_FACTOR,
    CONTAINER_MAX_DEPTH,
    SHEET_FRAME_SIZE_TOLERANCE,
    STANDARD_DRAWING_SCALES,
    STANDARD_PAPER_MM,
    VIEW_GAP_EXTENT_FRACTION,
    VIEW_GAP_MEDIAN_SIZE_FACTOR,
    VIEW_MAIN_MIN_CONTENT,
    VIEW_OVERSIZE_EXTENT_FRACTION,
)

_LEXICON = json.loads((Path(__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8"))


def _view_id(window: list[float]) -> str:
    return "V:" + hashlib.sha256(json.dumps([round(v) for v in window]).encode()).hexdigest()[:10]


_ROLE_ORDER = ("elevation", "section", "detail", "structural", "plan")  # specific before generic


def _role(title: str) -> str | None:
    terms = _LEXICON["view_role_terms"]
    for role in _ROLE_ORDER:
        if any(term.lower() in title.lower() for term in terms.get(role, [])):
            return role
    return None


def _inside(bbox: list[float], window: list[float]) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    return window[0] <= cx <= window[2] and window[1] <= cy <= window[3]


def _top_level_boxes(inventory: dict[str, Any]) -> list[tuple[str, list[float]]]:
    rows: list[tuple[str, list[float]]] = []
    for row in inventory["occurrences"]:
        if row["depth"] == 0 and row["bbox"]:
            rows.append((row["ref"], row["bbox"]))
    for row in inventory["closed_outlines"]:
        if row["depth"] == 0:
            rows.append((row["ref"], row["bbox"]))
    for row in inventory["texts"]:
        if row["depth"] == 0:
            x, y = row["xy"]
            rows.append((row["ref"], [x, y, x, y]))
    return rows


def _cluster(boxes: list[tuple[str, list[float]]], extent: list[float]) -> list[dict[str, Any]]:
    span = max(extent[2] - extent[0], extent[3] - extent[1], 1.0)
    sizes = [max(b[2] - b[0], b[3] - b[1]) for _, b in boxes]
    median = statistics.median([s for s in sizes if s > 0] or [0.0])
    gap = max(span * VIEW_GAP_EXTENT_FRACTION, median * VIEW_GAP_MEDIAN_SIZE_FACTOR)
    kept = [(ref, b) for ref, b in boxes if max(b[2] - b[0], b[3] - b[1]) <= span * VIEW_OVERSIZE_EXTENT_FRACTION]
    if not kept:
        return []
    geoms = [box(b[0] - gap / 2, b[1] - gap / 2, b[2] + gap / 2, b[3] + gap / 2) for _, b in kept]
    tree = STRtree(geoms)
    parent = list(range(len(kept)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, geom in enumerate(geoms):
        for j in tree.query(geom, predicate="intersects"):
            a, b = find(i), find(int(j))
            if a != b:
                parent[a] = b
    groups: dict[int, list[int]] = {}
    for i in range(len(kept)):
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for members in groups.values():
        xs1 = [kept[i][1][0] for i in members]; ys1 = [kept[i][1][1] for i in members]
        xs2 = [kept[i][1][2] for i in members]; ys2 = [kept[i][1][3] for i in members]
        clusters.append({"window": [min(xs1), min(ys1), max(xs2), max(ys2)], "member_count": len(members)})
    return clusters


def _is_rectangle(points: list[list[float]]) -> bool:
    pts = [tuple(p) for p in points]
    if len(pts) == 5 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) != 4:
        return False
    xs = sorted({round(p[0], 1) for p in pts})
    ys = sorted({round(p[1], 1) for p in pts})
    return len(xs) == 2 and len(ys) == 2


def _standard_sheet(width: float, height: float) -> str | None:
    long_side, short_side = max(width, height), min(width, height)
    for paper_long, paper_short in STANDARD_PAPER_MM:
        for scale in STANDARD_DRAWING_SCALES:
            if (abs(long_side - paper_long * scale) <= paper_long * scale * SHEET_FRAME_SIZE_TOLERANCE
                    and abs(short_side - paper_short * scale) <= paper_short * scale * SHEET_FRAME_SIZE_TOLERANCE):
                return f"{paper_long:g}x{paper_short:g} @ 1:{scale}"
    return None


def _sheet_frames(inventory: dict[str, Any], exclude_windows: list[list[float]]) -> list[dict[str, Any]]:
    """Top-level rectangles of standard paper size x drawing scale, outermost only."""

    candidates: list[tuple[str, list[float]]] = []
    for row in inventory["closed_outlines"]:
        if row["depth"] == 0 and _is_rectangle(row["points"]):
            candidates.append((row["ref"], row["bbox"]))
    for row in inventory["occurrences"]:
        if row["depth"] == 0 and row["bbox"]:
            candidates.append((row["ref"], row["bbox"]))
    frames = []
    for ref, b in candidates:
        sheet = _standard_sheet(b[2] - b[0], b[3] - b[1])
        if sheet is None or any(_inside(b, w) for w in exclude_windows):
            continue
        frames.append({"ref": ref, "window": [round(v, 3) for v in b], "sheet": sheet})

    def contains(outer: list[float], inner: list[float]) -> bool:
        return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3] and outer != inner

    outermost = [f for f in frames if not any(contains(g["window"], f["window"]) for g in frames)]
    unique: dict[tuple[float, ...], dict[str, Any]] = {}
    for frame in outermost:
        unique.setdefault(tuple(round(v) for v in frame["window"]), frame)
    return list(unique.values())


def _children_index(inventory: dict[str, Any]) -> dict[str, list[tuple[str, list[float]]]]:
    index: dict[str, list[tuple[str, list[float]]]] = {}
    for row in inventory["occurrences"]:
        if row["parent_ref"] and row["bbox"]:
            index.setdefault(row["parent_ref"], []).append((row["ref"], row["bbox"]))
    for key in ("closed_outlines", "texts"):
        for row in inventory[key]:
            if row["depth"] == 0:
                continue
            parent = "I:" + row["ref"].split(":", 1)[1].rsplit("/", 1)[0]
            bbox = row["bbox"] if key == "closed_outlines" else [*row["xy"], *row["xy"]]
            index.setdefault(parent, []).append((row["ref"], bbox))
    return index


def inventory_boxes(inventory: dict[str, Any]):
    """Every INSERT occurrence with a box (any depth), for looking up opened containers."""

    for row in inventory["occurrences"]:
        if row["bbox"]:
            yield row["ref"], row["bbox"]


def _open_containers(boxes: list[tuple[str, list[float]]], inventory: dict[str, Any]) -> tuple[list[tuple[str, list[float]]], list[str]]:
    """Replace oversized container INSERTs by their children (up to CONTAINER_MAX_DEPTH levels)."""

    sizes = [max(b[2] - b[0], b[3] - b[1]) for _, b in boxes]
    positive = [v for v in sizes if v > 0]
    if not positive:
        return boxes, []
    limit = statistics.median(positive) * CONTAINER_EXPAND_MEDIAN_FACTOR
    index: dict[str, list[tuple[str, list[float]]]] | None = None
    opened: list[str] = []
    result: list[tuple[str, list[float]]] = []
    stack = [(ref, b, 0) for ref, b in boxes]
    while stack:
        ref, b, level = stack.pop()
        if ref.startswith("I:") and level < CONTAINER_MAX_DEPTH and max(b[2] - b[0], b[3] - b[1]) > limit:
            if index is None:
                index = _children_index(inventory)
            children = index.get(ref, [])
            if children:
                opened.append(ref)
                stack.extend((child_ref, child_box, level + 1) for child_ref, child_box in children)
                continue
        result.append((ref, b))
    return result, opened


def assign_titles(views: list[dict[str, Any]], inventory: dict[str, Any], main_windows: list[list[float]]) -> None:
    """Title = the largest text inside the view naming a view role (lexicon); role from it."""

    for view in views:
        # a cluster's title may not come from text that belongs to a frame/viewport view
        others = main_windows if view["source"] in {"model_space_cluster", "judged_split"} else []
        candidates = [
            t for t in inventory["texts"]
            if _inside([*t["xy"], *t["xy"]], view["window"]) and _role(t["text"])
            and not any(_inside([*t["xy"], *t["xy"]], w) for w in others)
        ]
        if candidates:
            title = max(candidates, key=lambda t: t["height"])
            view["title"] = {"text": title["text"], "ref": title["ref"]}
            view["role"] = _role(title["text"])
        else:
            view["title"] = None
            view["role"] = "plan" if view.get("layout_role") == "main" else None


def segment_views(inventory: dict[str, Any]) -> dict[str, Any]:
    views: list[dict[str, Any]] = []
    main_windows: list[list[float]] = []
    top_boxes = _top_level_boxes(inventory)

    def content(window: list[float]) -> int:
        return sum(1 for _, b in top_boxes if _inside(b, window))

    for layout in inventory.get("layouts", []):
        # the main view of a sheet is the viewport covering the largest model area
        # among those that show real content (a title-block viewport can be large on
        # paper but covers a tiny model area; a dense detail covers a small area)
        def rank(port: dict[str, Any]) -> tuple[bool, float]:
            w = port["model_window"]
            return (content(w) >= VIEW_MAIN_MIN_CONTENT, (w[2] - w[0]) * (w[3] - w[1]))

        ports = sorted(layout["viewports"], key=rank, reverse=True)
        for index, port in enumerate(ports):
            window = port["model_window"]
            role = "main" if index == 0 else "detail"
            views.append({
                "view_id": _view_id(window), "window": window, "source": "paper_viewport",
                "layout": layout["name"], "viewport_handle": port["handle"], "layout_role": role,
                "twist_deg": port["twist_deg"],
            })
            if role == "main":
                main_windows.append(window)

    for frame in _sheet_frames(inventory, main_windows):
        views.append({"view_id": _view_id(frame["window"]), "window": frame["window"], "source": "sheet_frame",
                      "frame_ref": frame["ref"], "sheet": frame["sheet"]})
        main_windows.append(frame["window"])

    boxes = [(ref, b) for ref, b in top_boxes if not any(_inside(b, w) for w in main_windows)]
    box_by_ref = {ref: b for ref, b in inventory_boxes(inventory)}
    boxes, opened = _open_containers(boxes, inventory)
    boxes = [(ref, b) for ref, b in boxes if not any(_inside(b, w) for w in main_windows)]
    if boxes:
        extent = [min(b[0] for _, b in boxes), min(b[1] for _, b in boxes), max(b[2] for _, b in boxes), max(b[3] for _, b in boxes)]
    else:
        extent = [0.0, 0.0, 1.0, 1.0]
    for cluster in _cluster(boxes, extent):
        if cluster["member_count"] < 3:
            continue
        views.append({"view_id": _view_id(cluster["window"]), "window": [round(v, 3) for v in cluster["window"]],
                      "source": "model_space_cluster", "member_count": cluster["member_count"]})

    assign_titles(views, inventory, main_windows)

    # identical windows listed twice (a viewport showing a cluster) keep one entry
    unique: dict[str, dict[str, Any]] = {}
    for view in views:
        unique.setdefault(view["view_id"], view)
    return {"views": list(unique.values()), "opened_containers": opened, "main_windows": main_windows,
            # opened containers stay in the region data as backdrops ("C:"): their plain-line
            # content (e.g. racks) is not listed as children, but it is there and blocks cuts
            "region_boxes": [[ref, b] for ref, b in boxes] + [
                ["C:" + ref[2:], box_by_ref[ref]] for ref in opened
                if ref in box_by_ref and not any(_inside(box_by_ref[ref], w) for w in main_windows)
            ],
            "method": "paper viewports + standard-size frames + gap clustering with container opening (frozen constants)"}


__all__ = ["assign_titles", "segment_views"]
