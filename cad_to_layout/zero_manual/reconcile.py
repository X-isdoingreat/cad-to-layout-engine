"""Reconcile product equipment records with a frozen independent source audit.

Views are paired by window overlap; records by handle path first (the audited
handle itself, or drawn inside the audited block), audited module groups by centre
inside the group extent, otherwise by footprint overlap (intersection over union
>= RECONCILE_MIN_IOU).  The report keeps the two evidence classes apart, never
merges views, and separates "missed, not even a candidate" from "found but untyped".
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import box

from .constants import RECONCILE_MIN_IOU

AUDIT_TO_PRODUCT = {"RACK": "RACK", "STACKER": "STACKER", "PALLET_CONV_CHAIN": "PALLET_CONV_CHAIN", "PALLET_CONV_ROLLER": "PALLET_CONV_ROLLER",
                    "BOX_LIFT": "BOX_LIFT", "BOX_CONVEYOR": "BOX_CONVEYOR", "ROBOT_ARM": "ROBOT_ARM"}


def _iou(a: list[float], b: list[float]) -> float:
    ga, gb = box(*a), box(*b)
    union = ga.union(gb).area
    return ga.intersection(gb).area / union if union else 0.0


def _audit_type(value: str) -> str:
    if value in AUDIT_TO_PRODUCT:
        return value
    if value.upper().startswith("OTHER"):
        return "OTHER_EQUIPMENT"
    return "UNKNOWN"


def _path(ref: str) -> str:
    return ref.split(":", 1)[-1].upper()


def _record_box(record: dict[str, Any]) -> list[float]:
    return [record["centre"][0] - record["length_mm"] / 2, record["centre"][1] - record["width_mm"] / 2,
            record["centre"][0] + record["length_mm"] / 2, record["centre"][1] + record["width_mm"] / 2]


def _area(bbox: list[float]) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _inside(point: list[float], bbox: list[float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


PRIORITY = {"handle": 0, "inside_audited_block": 1, "inside_group_extent": 2, "footprint_overlap": 3}


def _match(path: str, centre: list[float], handles: set[str], bbox: list[float] | None, same_type: bool, modules: bool) -> str | None:
    """How a product item belongs to an audited unit or group: the audited handle itself; or, only
    for an item of the audited type, drawn inside the audited block (handle path below it) or - for
    groups counted in modules - centre inside the group extent."""

    if path in handles:
        return "handle"
    if same_type and any(path.startswith(h + "/") for h in handles):
        return "inside_audited_block"
    if same_type and modules and bbox and _inside(centre, bbox):
        return "inside_group_extent"
    return None


def reconcile(product: dict[str, Any], product_views: list[dict[str, Any]], audit: dict[str, Any]) -> dict[str, Any]:
    windows = {v["view_id"]: v["window"] for v in product_views}
    audit_windows = [(a.get("view_id"), a["window"]) for a in audit.get("views", [])]

    def audited_view(point: list[float]) -> str | None:
        """The smallest audited window holding the point (audited windows may lie inside one another)."""

        holding = [(_area(w), vid) for vid, w in audit_windows if _inside(point, w)]
        return min(holding)[1] if holding else None

    def middle(bbox: list[float]) -> list[float]:
        return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]

    report_views = []
    for aview in audit.get("views", []):
        if not aview.get("equipment"):
            continue
        # The product's own views that pair with the audited view: reported, not used for selection, so a
        # view-splitting error stays visible without hiding the equipment result.
        matched_views = [vid for vid, w in windows.items() if _iou(w, aview["window"]) >= 0.3 or box(*aview["window"]).contains(box(*w).centroid)]
        name = aview.get("view_id")
        records = [r for r in product.get("records", []) if audited_view(r["centre"]) == name]
        untyped = [u for u in product.get("candidates_untyped", []) if audited_view(middle(u["bbox"])) == name]
        # the drawing's own equipment numbers that the product typed, by the handle of the number text
        numbered = {_path(u["ref"]): u for u in product.get("numbered_units", []) if u.get("type") and audited_view(u["xy"]) == name}
        record_index = {r["ref"]: i for i, r in enumerate(records)}
        used_numbers: set[str] = set()
        used: set[int] = set()
        used_untyped: set[int] = set()
        parts = 0
        rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"audit": 0, "matched": 0, "matched_text": 0, "matched_llm": 0, "type_mismatch": 0,
                                                               "missed": 0, "missed_but_candidate_untyped": 0, "missed_not_a_candidate": 0})
        for item in aview["equipment"]:
            kind = _audit_type(item["type"])
            modules = item.get("count_unit") == "module"
            groups = item.get("groups") or []
            if not groups or (modules and not any(g.get("count") for g in groups)):
                # no per-group counts: the item is one group with the item's count
                handles = set(item.get("handles", [])) | {h for g in groups for h in g.get("handles", [])}
                groups = [{"handles": sorted(handles), "count": item.get("count"), "bbox": item.get("bbox")}]
            for group in groups:
                handles = {h.upper() for h in group.get("handles", [])}
                n = int(group.get("count") or 1)
                rows[kind]["audit"] += n
                found = []
                hits = []
                by_number = 0
                for handle in sorted(handles & set(numbered) - used_numbers):
                    if by_number >= n:
                        break
                    unit = numbered[handle]
                    used_numbers.add(handle)
                    by_number += 1
                    if unit.get("host_ref") in record_index:
                        used.add(record_index[unit["host_ref"]])  # the body that carries this number is accounted for
                    if unit["type"] == kind or kind == "UNKNOWN":
                        rows[kind]["matched"] += 1
                        rows[kind]["matched_llm" if unit["basis"] == "llm_shape" else "matched_text"] += 1
                    else:
                        rows[kind]["type_mismatch"] += 1
                n -= by_number
                for index, record in enumerate(records):
                    if index in used:
                        continue
                    how = _match(_path(record["ref"]), record["centre"], handles, group.get("bbox"), record["type"] == kind, modules)
                    if how is None and not modules and group.get("bbox") and _iou(group["bbox"], _record_box(record)) >= RECONCILE_MIN_IOU:
                        how = "footprint_overlap"
                    if how is not None:
                        hits.append((PRIORITY[how], index, how))
                for _, index, how in sorted(hits):
                    if len(found) < n:
                        found.append(index)
                        used.add(index)
                    elif how == "handle":  # a further listed part of an audited unit (e.g. the second segment of one lane)
                        used.add(index)
                        parts += 1
                for index in found:
                    record = records[index]
                    if record["type"] == kind or kind == "UNKNOWN":
                        rows[kind]["matched"] += 1
                        rows[kind]["matched_llm" if record["basis"] == "llm_shape" else "matched_text"] += 1
                    else:
                        rows[kind]["type_mismatch"] += 1
                missed = n - len(found)
                covered = 0
                for index, candidate in enumerate(untyped):
                    if covered >= missed:
                        break
                    if index in used_untyped:
                        continue
                    centre = [(candidate["bbox"][0] + candidate["bbox"][2]) / 2, (candidate["bbox"][1] + candidate["bbox"][3]) / 2]
                    if _match(_path(candidate["ref"]), centre, handles, group.get("bbox"), True, modules) or (
                            not modules and group.get("bbox") and _iou(group["bbox"], candidate["bbox"]) >= RECONCILE_MIN_IOU):
                        used_untyped.add(index)
                        covered += 1
                rows[kind]["missed"] += missed
                rows[kind]["missed_but_candidate_untyped"] += covered
                rows[kind]["missed_not_a_candidate"] += missed - covered
        extra: dict[str, dict[str, int]] = defaultdict(lambda: {"text_or_geometry": 0, "llm_shape": 0})
        for index, record in enumerate(records):
            if index not in used:
                extra[record["type"]]["llm_shape" if record["basis"] == "llm_shape" else "text_or_geometry"] += 1
        report_views.append({"audit_view": aview.get("view_id"), "title": aview.get("title"), "product_views": matched_views,
                             "by_type": {k: dict(v) for k, v in rows.items()}, "extra_in_product": {k: dict(v) for k, v in extra.items()},
                             "further_parts_of_audited_units": parts, "audit_unresolved": len(aview.get("unresolved", []))})
    return {"views": report_views, "selection": "product items by position inside the audited window (smallest audited window first); product_views lists the product's own views that pair with it",
            "rule": f"handle path first (exact, or drawn inside the audited block), module groups by centre inside the group extent, else footprint IoU >= {RECONCILE_MIN_IOU}",
            "note": "views never merged; 'missed_but_candidate_untyped' = the program found the object but gave it no type"}


def count_table(product: dict[str, Any], product_views: list[dict[str, Any]], audit: dict[str, Any]) -> list[dict[str, Any]]:
    """Per audited view and type: audit count vs product count (text/geometry and LLM shape apart)."""

    windows = {v["view_id"]: v["window"] for v in product_views}
    rows = []
    for aview in audit.get("views", []):
        if not aview.get("equipment"):
            continue
        matched_views = [vid for vid, w in windows.items() if _iou(w, aview["window"]) >= 0.3 or box(*aview["window"]).contains(box(*w).centroid)]
        audit_counts: dict[str, int] = defaultdict(int)
        for item in aview["equipment"]:
            audit_counts[_audit_type(item["type"])] += int(item.get("count") or 1)
        product_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"text_or_geometry": 0, "llm_shape": 0})
        for r in product.get("records", []):
            if r["view_id"] in matched_views:
                product_counts[r["type"]]["llm_shape" if r["basis"] == "llm_shape" else "text_or_geometry"] += 1
        for kind in sorted(set(audit_counts) | set(product_counts)):
            p = product_counts.get(kind, {"text_or_geometry": 0, "llm_shape": 0})
            rows.append({"audit_view": aview.get("view_id"), "product_views": matched_views, "type": kind, "audit_count": audit_counts.get(kind, 0),
                         "product_text_or_geometry": p["text_or_geometry"], "product_llm_shape": p["llm_shape"]})
    return rows


def load_frozen_audit(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


__all__ = ["count_table", "load_frozen_audit", "reconcile"]
