"""Drawing inventory: everything the mapper and the checker may cite, for any drawing.

References are stable strings: ``B:<block name>``, ``I:<handle path>`` (an INSERT
occurrence), ``T:<handle path>`` (text/attribute), ``P:<handle path>`` (closed
outline), ``R:<handle path>`` (circle/ellipse mark), ``D:<handle path>``
(dimension), ``L:<layer>``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezbbox

from .expand import dynamic_original_name, handle_of, iter_world_items, matrix_2d, transformed_point

INVENTORY_SCHEMA = "ctle-drawing-inventory-1.0"
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _text_value(entity: Any) -> str:
    kind = entity.dxftype()
    try:
        if kind == "MTEXT":
            return entity.plain_text().strip()
        return str(entity.dxf.text or "").strip()
    except Exception:
        return ""


def _local_extents(doc: Any, block: Any, cache: Any) -> tuple[float, float, float, float] | None:
    try:
        box = ezbbox.extents(block, cache=cache)
    except Exception:
        return None
    if not box.has_data:
        return None
    return (float(box.extmin.x), float(box.extmin.y), float(box.extmax.x), float(box.extmax.y))


def _signature(block: Any, extents: tuple[float, float, float, float] | None) -> str:
    """Name-independent shape signature: entity-type histogram + rounded size."""

    histogram = Counter(entity.dxftype() for entity in block)
    size = [0.0, 0.0] if extents is None else sorted([round(extents[2] - extents[0]), round(extents[3] - extents[1])])
    payload = json.dumps({"types": sorted(histogram.items()), "size": size}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _corners(extents: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    x1, y1, x2, y2 = extents
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _bbox(points: list[tuple[float, float]]) -> list[float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]


def _closed_points(entity: Any, matrix: Any) -> list[tuple[float, float]] | None:
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE" and entity.closed:
            return [transformed_point(matrix, (x, y, 0.0)) for x, y, *_ in entity.get_points()]
        if kind == "POLYLINE" and entity.is_closed and not entity.is_3d_polyline:
            return [transformed_point(matrix, vertex.dxf.location) for vertex in entity.vertices]
    except Exception:
        return None
    return None


def _dimension(entity: Any, matrix: Any) -> dict[str, Any] | None:
    try:
        measurement = float(entity.get_measurement())
    except Exception:
        return None
    if not math.isfinite(measurement):
        return None
    override = str(getattr(entity.dxf, "text", "") or "").strip()
    try:
        dimlfac = float(entity.get_dim_style().dxf.get("dimlfac", 1.0) or 1.0)
        dimlfac = float(entity.dim_style_attributes().get("dimlfac", dimlfac))
    except Exception:
        dimlfac = 1.0
    points = []
    for name in ("defpoint", "defpoint2", "defpoint3"):
        if entity.dxf.hasattr(name):
            points.append(transformed_point(matrix, entity.dxf.get(name)))
    return {"measurement": _r(measurement, 4), "text_override": override, "dimlfac": dimlfac, "defpoints": [[_r(x), _r(y)] for x, y in points]}


def build_inventory(dxf_path: str | Path) -> dict[str, Any]:
    path = Path(dxf_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = ezdxf.readfile(path)
    cache = ezbbox.Cache()

    blocks: dict[str, dict[str, Any]] = {}
    for block in doc.blocks:
        name = block.name
        if name.lower().startswith(("*model_space", "*paper_space")):
            continue
        extents = _local_extents(doc, block, cache)
        blocks[name] = {
            "ref": f"B:{name}",
            "name": name,
            "local_bbox": None if extents is None else [_r(v) for v in extents],
            "size": None if extents is None else sorted([_r(extents[2] - extents[0]), _r(extents[3] - extents[1])], reverse=True),
            "signature": _signature(block, extents),
            "entity_types": dict(Counter(entity.dxftype() for entity in block)),
            "attdefs": [
                {"tag": str(a.dxf.tag), "prompt": str(getattr(a.dxf, "prompt", "") or ""), "default": str(getattr(a.dxf, "text", "") or "")}
                for a in block.query("ATTDEF")
            ],
            "texts_inside": sorted({t for t in (_text_value(e) for e in block if e.dxftype() in {"TEXT", "MTEXT"}) if t})[:20],
            "occurrence_count": 0,
        }

    layer_counts: Counter[str] = Counter()
    occurrences: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    outlines: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    extent_points: list[tuple[float, float]] = []

    for item in iter_world_items(doc):
        entity = item.entity
        kind = item.dxftype
        layer_counts[item.layer] += 1
        if kind == "INSERT":
            name = str(entity.dxf.name)
            info = blocks.get(name)
            if info is not None:
                info["occurrence_count"] += 1
            try:
                own = entity.matrix44() @ item.matrix
            except Exception:
                continue
            local = None if info is None else info["local_bbox"]
            footprint = [transformed_point(own, (x, y, 0.0)) for x, y in _corners(tuple(local))] if local else []
            occurrences.append({
                "ref": f"I:{item.ref}",
                "handle": handle_of(entity),
                "block": name,
                "original_block": dynamic_original_name(doc, entity) or name,
                "depth": item.depth,
                "parent_ref": None if item.parent_ref is None else f"I:{item.parent_ref}",
                "layer": item.layer,
                "matrix": matrix_2d(own),
                "footprint": [[_r(x), _r(y)] for x, y in footprint],
                "bbox": _bbox(footprint) if footprint else None,
                "attribs": {str(a.dxf.tag): _text_value(a) for a in (getattr(entity, "attribs", []) or [])},
            })
            if item.depth == 0 and footprint:
                extent_points.extend(footprint)
        elif kind in TEXT_TYPES:
            value = _text_value(entity)
            if not value:
                continue
            try:
                x, y = transformed_point(item.matrix, entity.dxf.insert)
            except Exception:
                continue
            height = float(getattr(entity.dxf, "char_height" if kind == "MTEXT" else "height", 0.0) or 0.0)
            scale = math.hypot(*matrix_2d(item.matrix)[0:2])
            texts.append({"ref": f"T:{item.ref}", "type": kind, "text": value, "xy": [_r(x), _r(y)],
                          "height": _r(height * scale), "layer": item.layer, "depth": item.depth})
            if item.depth == 0:
                extent_points.append((x, y))
        elif kind in {"CIRCLE", "ELLIPSE"}:
            try:
                cx, cy = transformed_point(item.matrix, entity.dxf.center)
                scale = math.hypot(*matrix_2d(item.matrix)[0:2])
                if kind == "CIRCLE":
                    radius = float(entity.dxf.radius) * scale
                else:
                    radius = math.hypot(*entity.dxf.major_axis[:2]) * scale
            except Exception:
                continue
            marks.append({"ref": f"R:{item.ref}", "type": kind, "center": [_r(cx), _r(cy)], "radius": _r(radius), "layer": item.layer, "depth": item.depth})
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            points = _closed_points(entity, item.matrix)
            if points and len(points) >= 3:
                outlines.append({"ref": f"P:{item.ref}", "layer": item.layer, "points": [[_r(x), _r(y)] for x, y in points],
                                 "bbox": _bbox(points), "depth": item.depth})
                if item.depth == 0:
                    extent_points.extend(points)
        elif kind == "DIMENSION":
            dimension = _dimension(entity, item.matrix)
            if dimension is not None:
                dimensions.append({"ref": f"D:{item.ref}", "layer": item.layer, "depth": item.depth, **dimension})
        elif item.depth == 0 and kind in {"LINE", "ARC"}:
            try:
                if kind == "LINE":
                    extent_points.extend([transformed_point(item.matrix, entity.dxf.start), transformed_point(item.matrix, entity.dxf.end)])
                else:
                    extent_points.append(transformed_point(item.matrix, entity.dxf.center))
            except Exception:
                pass

    layouts = []
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        viewports = []
        for viewport in layout.query("VIEWPORT"):
            if int(getattr(viewport.dxf, "id", 0) or 0) == 1:
                continue
            try:
                center = viewport.dxf.view_center_point
                height = float(viewport.dxf.view_height)
                ratio = float(viewport.dxf.width) / float(viewport.dxf.height) if float(viewport.dxf.height) else 0.0
            except Exception:
                continue
            if height <= 0 or ratio <= 0:
                continue
            width = height * ratio
            viewports.append({
                "handle": handle_of(viewport),
                "model_window": [_r(center[0] - width / 2), _r(center[1] - height / 2), _r(center[0] + width / 2), _r(center[1] + height / 2)],
                "paper_size": [_r(float(viewport.dxf.width)), _r(float(viewport.dxf.height))],
                "twist_deg": _r(float(getattr(viewport.dxf, "view_twist_angle", 0.0) or 0.0)),
            })
        layouts.append({"name": layout.name, "viewports": viewports,
                        "texts": [_text_value(e) for e in layout if e.dxftype() in {"TEXT", "MTEXT"} and _text_value(e)][:50]})

    return {
        "schema": INVENTORY_SCHEMA,
        # no file path or name: the inventory of a drawing must not change when the file is renamed
        "source": {"dxf_sha256": digest, "dxfversion": doc.dxfversion, "size_bytes": path.stat().st_size},
        "header": {"insunits": int(doc.header.get("$INSUNITS", 0) or 0), "measurement": int(doc.header.get("$MEASUREMENT", 0) or 0)},
        "extent": _bbox(extent_points) if extent_points else None,
        "layers": [{"ref": f"L:{name}", "name": name, "count": count} for name, count in sorted(layer_counts.items())],
        "blocks": sorted(blocks.values(), key=lambda row: row["name"]),
        "occurrences": occurrences,
        "texts": texts,
        "closed_outlines": outlines,
        "marks": marks,
        "dimensions": dimensions,
        "layouts": layouts,
    }


def write_inventory(inventory: dict[str, Any], path: str | Path) -> str:
    payload = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    Path(path).write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["INVENTORY_SCHEMA", "build_inventory", "write_inventory"]
