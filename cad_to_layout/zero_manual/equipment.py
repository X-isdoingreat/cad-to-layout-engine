"""Zero-manual equipment recognition: block families -> typed equipment records per view.

Order (acceptance target revision 5):

1. General rules without any LLM: size (tiny blocks are parts), lexicon words in the
   block name / texts inside the block / attributes, structure layers; then texts
   bound by containment (a machine name inside a closed outline, a zone label inside
   a group of equal blocks or cells).  Evidence is reproducible without an LLM.
2. Families still undecided go to the guided LLM shape judgement: contact sheets of
   block drawings with printed facts, a frozen guidance prompt, three asks with the
   tile order shuffled, adopted only when at least two asks agree.  Everything sent
   and received is archived and can be replayed.
3. Program checks: the type's plausible size range, and no double counting (an
   occurrence inside another counted machine is not counted again; but packed
   regular arrays of modules are counted and an outer block the LLM alone called a
   machine is then not).

Counts keep the two evidence classes apart; LLM-shape items stay "pending" until
the independent count and the spot check pass.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from .candidates import bind_texts, number_series, outline_cell_groups, regular_arrays, row_blocks, row_kinds
from .constants import (
    BACKGROUND_MIN_FAMILIES,
    BACKGROUND_ONCE_ONLY_SHARE,
    EQUIPMENT_MIN_SIZE_MM,
    PALLET_CONVEYOR_MIN_WIDTH_MM,
    SPLIT_JUDGE_MAJORITY,
    SPLIT_JUDGE_REPEATS,
    TYPE_JUDGE_TILES_PER_SHEET,
    DUPLICATE_VIEW_MIN_OVERLAP,
    TYPE_SIZE_RANGES_MM,
    ZONE_LABEL_MIN_COVERAGE,
)
from .warehouse_geometry import UNIT_TO_MM, layer_role

_LEXICON = json.loads((Path(__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8"))
GUIDANCE_PATH = Path(__file__).with_name("type_guidance_v3.md")  # v3 (2026-10-23): every tile answers "wrong_if"; earlier judgements are weighed, not copied
GUIDANCE = GUIDANCE_PATH.read_text(encoding="utf-8")
GUIDANCE_ID = "type_guidance_v3"
TYPES = {"RACK", "STACKER", "PALLET_CONV_CHAIN", "PALLET_CONV_ROLLER", "CONVEYOR_UNKNOWN_SUBTYPE", "BOX_CONVEYOR",
         "BOX_LIFT", "ROBOT_ARM", "OTHER_EQUIPMENT", "COMPONENT", "NOT_EQUIPMENT", "UNSURE"}
EQUIPMENT_TYPES = TYPES - {"COMPONENT", "NOT_EQUIPMENT", "UNSURE"}
LABEL_ZH = {"RACK": "货架", "STACKER": "堆垛机", "PALLET_CONV_CHAIN": "托盘输送机（链条）", "PALLET_CONV_ROLLER": "托盘输送机（滚筒）",
            "CONVEYOR_UNKNOWN_SUBTYPE": "输送机（子类未识别）", "BOX_CONVEYOR": "箱式输送机", "BOX_LIFT": "箱式提升机",
            "ROBOT_ARM": "机械臂", "OTHER_EQUIPMENT": "其他设备"}


# ---------------------------------------------------------------- families
def _centre(bbox: list[float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _view_of(point: tuple[float, float], views: list[dict[str, Any]]) -> str | None:
    best = None
    for v in views:
        w = v["window"]
        if w[0] <= point[0] <= w[2] and w[1] <= point[1] <= w[3]:
            area = (w[2] - w[0]) * (w[3] - w[1])
            if best is None or area < best[0]:
                best = (area, v["view_id"])
    return None if best is None else best[1]


def _footprint_size(footprint: list[list[float]], scale: float) -> tuple[float, float]:
    if len(footprint) < 3:
        return (0.0, 0.0)
    a = math.dist(footprint[0], footprint[1]) * scale
    b = math.dist(footprint[1], footprint[2]) * scale
    return (max(a, b), min(a, b))


def normalise_anonymous(inventory: dict[str, Any]) -> dict[str, Any]:
    """Anonymous blocks (``*U…``) that a dynamic block does not explain are grouped by what they are: their shape
    signature plus, level by level, the grouped kinds of block they hold.  Copies of one assembly (a rack bay made of
    uprights, beams and pallets) are then judged once, not once per anonymous name; the assembly itself stays the
    candidate, so its parts are not judged on their own.
    """

    import hashlib as _hashlib

    blocks = {b["name"]: b for b in inventory["blocks"]}
    by_ref = {o["ref"]: o for o in inventory["occurrences"]}
    contents: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sample: dict[str, str] = {}
    for o in inventory["occurrences"]:
        parent = by_ref.get(o["parent_ref"]) if o["parent_ref"] else None
        if parent is not None and sample.setdefault(parent["block"], parent["ref"]) == parent["ref"]:
            contents[parent["block"]].append(o)  # what one occurrence of the block holds (all occurrences hold the same)
    keys: dict[str, str] = {}

    def key(o: dict[str, Any]) -> str:
        if not o["original_block"].startswith("*"):
            return o["original_block"]
        name = o["block"]
        if name not in keys:
            keys[name] = "anon:" + name  # guards a self-reference while the contents are keyed
            held = Counter(key(c) for c in contents.get(name, []))
            digest = _hashlib.sha1(json.dumps(sorted(held.items()), ensure_ascii=False).encode("utf-8")).hexdigest()[:10] if held else ""
            keys[name] = "anon:" + str((blocks.get(name) or {}).get("signature") or name) + (":" + digest if digest else "")
        return keys[name]

    out = [{**o, "original_block": key(o)} for o in inventory["occurrences"]]
    return {**inventory, "occurrences": out, "anonymous_names_grouped": len(keys), "anonymous_groups": len(set(keys.values()))}


def build_families(inventory: dict[str, Any], views: list[dict[str, Any]], unit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scale = UNIT_TO_MM.get(str(unit.get("unit")), 1.0)
    usable = [v for v in views if v.get("layout_role") != "detail"]
    blocks = {b["name"]: b for b in inventory["blocks"]}
    by_ref = {o["ref"]: o for o in inventory["occurrences"]}
    families: dict[str, dict[str, Any]] = {}
    for o in inventory["occurrences"]:
        if not o["bbox"]:
            continue
        view_id = _view_of(_centre(o["bbox"]), usable)
        if view_id is None:
            continue
        key = o["original_block"]
        fam = families.setdefault(key, {"family": key, "block_names": set(), "occurrences": [], "parents": Counter(), "children": Counter(),
                                        "layers": Counter(), "attribs": Counter(), "views": Counter(), "sizes": []})
        fam["block_names"].add(o["block"])
        fam["occurrences"].append({"ref": o["ref"], "view_id": view_id, "footprint": o["footprint"], "bbox": o["bbox"], "matrix": o["matrix"], "parent_ref": o["parent_ref"]})
        fam["layers"][o["layer"]] += 1
        fam["views"][view_id] += 1
        fam["sizes"].append(_footprint_size(o["footprint"], scale))
        for value in o["attribs"].values():
            if value:
                fam["attribs"][value] += 1
        parent = by_ref.get(o["parent_ref"]) if o["parent_ref"] else None
        if parent is not None:
            fam["parents"][parent["original_block"]] += 1
            families.setdefault(parent["original_block"], {"family": parent["original_block"], "block_names": set(), "occurrences": [], "parents": Counter(),
                                                           "children": Counter(), "layers": Counter(), "attribs": Counter(), "views": Counter(), "sizes": []})["children"][key] += 1
    for fam in families.values():
        sizes = sorted(fam["sizes"])
        fam["size_mm"] = sizes[len(sizes) // 2] if sizes else (0.0, 0.0)
        texts: list[str] = []
        for name in fam["block_names"]:
            texts += blocks.get(name, {}).get("texts_inside", [])
        fam["texts_inside"] = sorted(set(texts))[:10]
        fam["block_names"] = sorted(fam["block_names"])
    return {k: v for k, v in families.items() if v["occurrences"]}


# ---------------------------------------------------------------- stage 1: rules
def _has(term: str, folded: str) -> bool:
    term = term.casefold()
    if term.isascii() and len(term) <= 4:  # short ASCII words only as whole words ("rack" is not in "track")
        return re.search(rf"(?<![a-z]){re.escape(term)}(?:s|es)?(?![a-z])", folded) is not None
    return term in folded


def _lexicon_types(text: str) -> dict[str, str]:
    """{type key: matched word}.  Keys: a warehouse equipment type, ``OTHER:<class>`` or ``IGNORED:<class>``."""

    folded = text.casefold()
    found: dict[str, str] = {}

    def add(key: str, terms: list[str]) -> None:
        hits = [term for term in terms if _has(term, folded)]
        if hits:
            found[key] = max(hits, key=len)

    for key, spec in _LEXICON["equipment_types"].items():
        add(key, spec["terms"])
    for key, terms in _LEXICON["other_known_equipment"].items():
        add("OTHER:" + key, terms)
    for key, terms in _LEXICON["ignored_types"].items():
        add("IGNORED:" + key, terms)
    # a word contained in another hit's longer word is part of that word, not a second machine
    found = {k: t for k, t in found.items() if not any(t != other and t.casefold() in other.casefold() for other in found.values())}
    if not found:
        for term in _LEXICON["generic_conveyor_terms"]:
            if _has(term, folded):
                q = _LEXICON["subtype_qualifiers"]
                chain = any(_has(t, folded) for t in q["chain"])
                roller = any(_has(t, folded) for t in q["roller"])
                box = any(_has(t, folded) for t in q["box"])
                kind = "BOX_CONVEYOR" if box else ("PALLET_CONV_CHAIN" if chain and not roller else ("PALLET_CONV_ROLLER" if roller and not chain else "CONVEYOR_UNKNOWN_SUBTYPE"))
                found[kind] = term
                break
    return found


def text_type(text: str) -> dict[str, Any] | None:
    """The one machine kind a text names, or None (no machine word, several kinds, or an ignored kind)."""

    hits = _lexicon_types(text)
    if len(hits) != 1:
        return None
    key, term = next(iter(hits.items()))
    if key.startswith("IGNORED:"):
        return None
    zone_only = text.strip().casefold().endswith(tuple(s.casefold() for s in _LEXICON["zone_suffixes"]))
    if key.startswith("OTHER:"):
        return {"type": "OTHER_EQUIPMENT", "name": term, "term": term, "zone_only": zone_only}
    return {"type": key, "name": "", "term": term, "zone_only": zone_only}


def _narrow_conveyor(kind: str, text: str, short_side_mm: float) -> bool:
    """A roller or unspecified conveyor too narrow for a pallet, with no pallet word in its text."""

    if kind not in {"PALLET_CONV_ROLLER", "CONVEYOR_UNKNOWN_SUBTYPE"} or short_side_mm >= PALLET_CONVEYOR_MIN_WIDTH_MM:
        return False
    return not any(_has(t, text.casefold()) for t in _LEXICON["subtype_qualifiers"]["pallet"])


def rule_decision(fam: dict[str, Any]) -> dict[str, Any] | None:
    long_side = fam["size_mm"][0]
    if long_side < EQUIPMENT_MIN_SIZE_MM:
        return {"type": "COMPONENT", "basis": "geometry", "evidence": [{"kind": "size_rule", "size_mm": [round(v) for v in fam["size_mm"]], "limit_mm": EQUIPMENT_MIN_SIZE_MM}]}
    roles = {layer_role(layer) for layer in fam["layers"]}
    if roles and roles <= {"wall", "axis", "column"}:
        return {"type": "NOT_EQUIPMENT", "basis": "geometry", "evidence": [{"kind": "structure_layer", "layers": sorted(fam["layers"])}]}
    sources = [("block_name", name, f"B:{name}") for name in [fam["family"], *fam["block_names"]]]
    sources += [("inside_text", t, f"B:{fam['block_names'][0]}") for t in fam["texts_inside"]]
    sources += [("attribute", t, fam["occurrences"][0]["ref"]) for t in fam["attribs"]]
    hits: dict[str, dict[str, Any]] = {}
    for kind, text, ref in sources:
        for key, term in _lexicon_types(text).items():
            hits.setdefault(key, {"kind": kind, "text": text[:60], "term": term, "ref": ref})
    if len(hits) == 1:
        key, ev = next(iter(hits.items()))
        if key.startswith("IGNORED:"):
            return {"type": "NOT_EQUIPMENT", "basis": "text", "ignored": key.split(":", 1)[1], "evidence": [ev]}
        if key.startswith("OTHER:"):
            return {"type": "OTHER_EQUIPMENT", "basis": "text", "name": ev["term"], "evidence": [ev]}
        if _narrow_conveyor(key, ev["text"], fam["size_mm"][1]):
            return {"type": "BOX_CONVEYOR", "basis": "text", "evidence": [ev, {"kind": "width_rule", "short_side_mm": round(fam["size_mm"][1]),
                                                                              "pallet_minimum_mm": PALLET_CONVEYOR_MIN_WIDTH_MM, "was": key}]}
        return {"type": key, "basis": "text", "evidence": [ev]}
    return None


# ---------------------------------------------------------------- stage 2: LLM shape judgement
def _nearby_texts(fam: dict[str, Any], texts: list[dict[str, Any]], scale: float) -> list[str]:
    radius = max(fam["size_mm"][0], 1000.0) / scale
    found: dict[str, float] = {}
    for occ in fam["occurrences"][:5]:
        c = _centre(occ["bbox"])
        for t in texts:
            d = math.dist(c, t["xy"])
            if d <= radius and len(t["text"]) <= 30:
                found[t["text"]] = min(found.get(t["text"], d), d)
    return [t for t, _ in sorted(found.items(), key=lambda kv: kv[1])[:3]]


def facts_line(tile_id: str, fam: dict[str, Any], nearby: list[str], array: dict[str, Any] | None = None,
               earlier: list[tuple[str, str]] | None = None, row: dict[str, Any] | None = None) -> str:
    """The printed facts of one tile; `array` is the family's largest regular row (how the block is laid out: racks come in
    rows, conveyors end to end), `earlier` the (type, reason) pairs of judgements already given for this tile (extra votes),
    `row` the cells this block holds when it is itself one row (judged as one unit; its cells are never asked about alone)."""

    L, W = fam["size_mm"]
    parents = ", ".join(n for n, _ in fam["parents"].most_common(2)) or "none (top level)"
    children = ", ".join(n for n, _ in fam["children"].most_common(4)) or "none"
    views = len(fam["views"])
    inside = "; ".join(fam["texts_inside"][:3]) or "none"
    near = "; ".join(nearby) or "none"
    extra = ""
    if array:
        extra += f"; arrangement: {array['count']} identical copies in one straight row, pitch {float(array['pitch_mm']):.0f} mm along the block's {array['along']}"
    if row:
        groups = f" in {row['groups']} groups" if row.get("groups", 1) > 1 else ""
        extra += (f"; THIS BLOCK IS ONE ROW: it holds {row['cells_per_row']} identical cells ({row['cell_family'][:20]}, "
                  f"{row['cell_size_mm'][0]} x {row['cell_size_mm'][1]} mm) at {float(row['pitch_mm']):.0f} mm pitch along its {row['along']}{groups}, "
                  "spanning the block. Judge the row as one unit (a row of racking bays, a conveyor line of segments, ...); its cells are "
                  "its bays or segments, and a row placed inside a bigger block is still a row, not a COMPONENT")
        for other in row.get("also_drawn_by") or []:  # the same kind of row under another block name: the judge is told, not asked twice
            extra += (f". The same kind of row is also drawn by block {other['family'][:40]}: {other['occurrences']} copies, cells {other['cell_family'][:20]} "
                      f"{other['cell_size_mm'][0]} x {other['cell_size_mm'][1]} mm, the same {row['cells_per_row']} cells{groups} at {float(other['pitch_mm']):.0f} mm pitch, "
                      f"{other['depth_mm']} mm deep; its nearest copy stands {max(other['nearest_gap_mm'], 0) / 1000:.1f} m from a copy of this block. "
                      "One answer covers both blocks")
    if earlier:
        extra += "; earlier judgements: " + " | ".join(f"{t} ({r[:80]})" if r else t for t, r in earlier)
    return (f"{tile_id}: size {L:.0f} x {W:.0f} mm; occurs {len(fam['occurrences'])} times in {views} view(s), "
            f"{context_count(fam)} of them inside the surroundings picture; "
            f"placed inside: {parents}; contains: {children}; texts inside: {inside}; texts nearby: {near}{extra}; block name: {fam['family'][:40]}")


def _majority(votes: list[str]) -> tuple[str | None, int]:
    """The answer given by more than half of the votes (at least SPLIT_JUDGE_MAJORITY), or None."""

    counts = Counter(votes)
    if not counts:
        return None, 0
    top, n = counts.most_common(1)[0]
    need = max(SPLIT_JUDGE_MAJORITY, len(votes) // 2 + 1)
    return (top if n >= need else None), n


def _context_window(fam: dict[str, Any]) -> list[float]:
    """Window around the family's middle occurrence, six times its size each way."""

    occurrences = sorted(fam["occurrences"], key=lambda o: (o["bbox"][0], o["bbox"][1]))
    b = occurrences[len(occurrences) // 2]["bbox"]
    half = 6.0 * max(b[2] - b[0], b[3] - b[1], 1.0)
    cx, cy = _centre(b)
    return [cx - half, cy - half, cx + half, cy + half]


def context_count(fam: dict[str, Any]) -> int:
    w = _context_window(fam)
    return sum(1 for o in fam["occurrences"] if w[0] <= _centre(o["bbox"])[0] <= w[2] and w[1] <= _centre(o["bbox"])[1] <= w[3])


def render_sheet(doc: Any, tiles: list[tuple[str, dict[str, Any]]], path: Path, surroundings: Any = None) -> str:
    """Contact sheet: per tile the block itself (top) and its surroundings (bottom)."""

    import matplotlib

    matplotlib.use("Agg")
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib import patches, pyplot

    TILE_CONFIG = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    from shapely.geometry import box as shapely_box

    matplotlib.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    cols = 4
    rows = max(1, math.ceil(len(tiles) / cols))
    figure, axes = pyplot.subplots(rows * 2, cols, figsize=(cols * 3.2, rows * 6.0), dpi=90, squeeze=False)
    for axis in axes.flat:
        axis.axis("off")
    for index, (tile_id, fam) in enumerate(tiles):
        top = axes[(index // cols) * 2][index % cols]
        bottom = axes[(index // cols) * 2 + 1][index % cols]
        block = doc.blocks.get(fam["block_names"][0])
        try:
            if block is not None:
                # black on white: colour 7 is otherwise drawn white and vanishes; the frontend does not set the axis limits itself
                Frontend(RenderContext(doc), MatplotlibBackend(top), config=TILE_CONFIG).draw_entities(block)
                extents = bbox.extents(block)
                if extents.has_data:
                    top.set_xlim(extents.extmin.x, extents.extmax.x)
                    top.set_ylim(extents.extmin.y, extents.extmax.y)
        except Exception:
            pass
        top.set_aspect("equal")
        top.set_title(f"{tile_id}  {fam['size_mm'][0] / 1000:.2f} x {fam['size_mm'][1] / 1000:.2f} m", fontsize=10)
        window = _context_window(fam)
        if surroundings is not None:
            tree, boxes = surroundings
            for hit in tree.query(shapely_box(*window)):
                b = boxes[int(hit)]
                if (b[2] - b[0]) > (window[2] - window[0]) * 0.9 and (b[3] - b[1]) > (window[3] - window[1]) * 0.9:
                    continue  # a container covering the whole window would hide everything
                bottom.add_patch(patches.Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], linewidth=0.4, edgecolor="#9ca3af", facecolor="none"))
        for occ in fam["occurrences"]:
            b = occ["bbox"]
            if b[2] < window[0] or b[0] > window[2] or b[3] < window[1] or b[1] > window[3]:
                continue
            bottom.add_patch(patches.Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], linewidth=0.8, edgecolor="#dc2626", facecolor="#fca5a5", alpha=0.8))
        bottom.set_xlim(window[0], window[2])
        bottom.set_ylim(window[1], window[3])
        bottom.set_aspect("equal")
        bottom.set_title(f"{tile_id} surroundings", fontsize=8)
    figure.tight_layout()
    figure.savefig(path, facecolor="white")
    pyplot.close(figure)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_tiles(text: str) -> dict[str, dict[str, Any]] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    out = {}
    for row in value.get("tiles", []) if isinstance(value, dict) else []:
        if isinstance(row, dict) and row.get("type") in TYPES and isinstance(row.get("id"), str):
            out[row["id"]] = {"type": row["type"], "name": str(row.get("name") or "")[:40], "reason": str(row.get("reason") or "")[:200],
                              "wrong_if": str(row.get("wrong_if") or "")[:200]}
    return out


def judge_shapes(doc: Any, families: dict[str, dict[str, Any]], undecided: list[str], texts: list[dict[str, Any]], scale: float,
                 work_dir: Path, transport: Callable | None, allow_external: bool, replay: Path | None = None,
                 all_boxes: list[list[float]] | None = None, start: int = 0, arrays: list[dict[str, Any]] | None = None,
                 row_info: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Returns {family: {"status": agreed|disagreed|skipped, "type", "votes", "reasons", "wrong_if", "sheet", "tile_id"}}.

    Every sheet is asked SPLIT_JUDGE_REPEATS times.  A family that occurs at least HIGH_COUNT_KIND_MIN times decides a large part
    of the count, so when its votes do not reach a majority the sheet is asked TYPE_JUDGE_EXTRA_VOTES more times with the earlier
    judgements' reasons printed (2026-10-23); the majority is then over all votes."""

    from .constants import HIGH_COUNT_KIND_MIN, TYPE_JUDGE_EXTRA_VOTES

    work_dir.mkdir(parents=True, exist_ok=True)
    array_info: dict[str, dict[str, Any]] = {}
    for a in arrays or []:
        if a["count"] > array_info.get(a["family"], {}).get("count", 0):
            array_info[a["family"]] = a
    surroundings = None
    if all_boxes:
        from shapely.geometry import box as shapely_box
        from shapely.strtree import STRtree

        surroundings = (STRtree([shapely_box(*b) for b in all_boxes]), all_boxes)
    guidance_sha = hashlib.sha256(GUIDANCE.encode("utf-8")).hexdigest()
    saved = json.loads(replay.read_text(encoding="utf-8")) if replay and replay.is_file() else {}
    own = work_dir / "type_judge_archive.json"
    if own.is_file():  # sheets of earlier rounds of this run
        saved.update(json.loads(own.read_text(encoding="utf-8")))
    results: dict[str, dict[str, Any]] = {}
    # Replay per block family: an archived judgement of a family under the same guidance
    # stays valid when other families left the sheet (decided by a rule since).
    archived: dict[str, dict[str, Any]] = {}
    for entry in saved.values():
        if entry.get("guidance_sha256") != guidance_sha:
            continue
        for tid, family in entry.get("tiles", []):
            rows = [(ask.get("parsed") or {}).get(tid) for ask in entry.get("asks", [])]
            archived[family] = {"votes": [r["type"] for r in rows if r], "names": [(r or {}).get("name", "") for r in rows],
                                "reasons": [(r or {}).get("reason", "") for r in rows], "wrong_if": [(r or {}).get("wrong_if", "") for r in rows]}
    for family in [k for k in undecided if k in archived and archived[k]["votes"]]:
        top, n = _majority(archived[family]["votes"])
        results[family] = {"status": "agreed" if top else "disagreed", "type": top, "votes": archived[family]["votes"], "tile_id": "archive", "sheet": "replayed_per_family",
                           "names": archived[family]["names"], "reasons": archived[family]["reasons"], "wrong_if": archived[family]["wrong_if"]}
    undecided = [k for k in undecided if k not in results]
    ordered = sorted(undecided, key=lambda k: (-len(families[k]["occurrences"]), k))
    for sheet_index in range(0, len(ordered), TYPE_JUDGE_TILES_PER_SHEET):
        chunk = ordered[sheet_index:sheet_index + TYPE_JUDGE_TILES_PER_SHEET]
        tiles = [(f"T{start + sheet_index + i + 1}", families[k]) for i, k in enumerate(chunk)]
        sheet_dir = work_dir / f"sheet_{(start + sheet_index) // TYPE_JUDGE_TILES_PER_SHEET + 1:03d}"
        sheet_dir.mkdir(exist_ok=True)
        nearby = {tid: _nearby_texts(fam, texts, scale) for tid, fam in tiles}
        key = hashlib.sha256(json.dumps([guidance_sha, [[tid, fam["family"], [round(v) for v in fam["size_mm"]]] for tid, fam in tiles]], ensure_ascii=False).encode()).hexdigest()
        votes: dict[str, list[str]] = defaultdict(list)
        reasons: dict[str, list[str]] = defaultdict(list)
        asks: list[dict[str, Any]] = []

        def record(parsed: dict[str, dict[str, Any]] | None) -> None:
            for tid, row in (parsed or {}).items():
                votes[tid].append(row["type"])
                reasons[tid].append(row.get("reason", ""))

        def ask_once(ask_index: int, earlier: dict[str, list[tuple[str, str]]]) -> None:
            order = list(tiles)
            random.Random(ask_index).shuffle(order)  # real variation between asks: tile layout and fact order
            image = sheet_dir / f"ask_{ask_index}.png"
            image_sha = render_sheet(doc, order, image, surroundings)
            facts = "\n".join(facts_line(tid, fam, nearby[tid], array_info.get(fam["family"]), earlier.get(tid), (row_info or {}).get(fam["family"]))
                              for tid, fam in order)
            prompt = GUIDANCE + "\n\nFacts per tile:\n" + facts
            (sheet_dir / f"ask_{ask_index}_prompt.txt").write_text(prompt, encoding="utf-8")
            call_dir = sheet_dir / f"ask_{ask_index}"
            call_dir.mkdir(exist_ok=True)
            raw = transport(prompt, image, call_dir)
            parsed = parse_tiles(raw.get("text", ""))
            asks.append({"ask": ask_index, "image_sha256": image_sha, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                         "provider": raw.get("provider"), "model": raw.get("model"), "error": raw.get("error"), "raw_text": raw.get("text", ""), "parsed": parsed,
                         "with_earlier_judgements": bool(earlier)})
            record(parsed)

        if key in saved:
            asks = saved[key]["asks"]
            for ask in asks:
                record(ask.get("parsed"))
        elif transport is not None and allow_external:
            for ask_index in range(1, SPLIT_JUDGE_REPEATS + 1):
                ask_once(ask_index, {})
            # a high-count family without a majority: more votes, with the earlier judgements and their reasons shown
            if any(max(len(fam["occurrences"]), (row_info or {}).get(fam["family"], {}).get("kind_occurrences", 0)) >= HIGH_COUNT_KIND_MIN
                   and _majority(votes[tid])[0] is None for tid, fam in tiles):
                for extra in range(1, TYPE_JUDGE_EXTRA_VOTES + 1):
                    earlier = {tid: list(zip(votes[tid], reasons[tid])) for tid, _ in tiles if votes[tid]}
                    ask_once(SPLIT_JUDGE_REPEATS + extra, earlier)
            saved[key] = {"tiles": [[tid, fam["family"]] for tid, fam in tiles], "asks": asks, "guidance_id": GUIDANCE_ID,
                          "guidance_sha256": hashlib.sha256(GUIDANCE.encode("utf-8")).hexdigest()}
        for tid, fam in tiles:
            if not votes.get(tid):
                results[fam["family"]] = {"status": "skipped", "tile_id": tid, "sheet": sheet_dir.name, "votes": []}
                continue
            top, n = _majority(votes[tid])
            results[fam["family"]] = {"status": "agreed" if top else "disagreed", "type": top, "votes": votes[tid], "reasons": reasons[tid], "tile_id": tid, "sheet": sheet_dir.name,
                                      "names": [(ask.get("parsed") or {}).get(tid, {}).get("name", "") for ask in asks],
                                      "wrong_if": [(ask.get("parsed") or {}).get(tid, {}).get("wrong_if", "") for ask in asks]}
        # saved after every sheet, so an interrupted run can be resumed from its own archive
        (work_dir / "type_judge_archive.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    (work_dir / "type_judge_archive.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


# ---------------------------------------------------------------- stage 3: checks and records
def size_plausible(kind: str, size_mm: tuple[float, float], row: bool = False) -> bool:
    """A row is as long as its cells make it: only its depth (short side) is held against the type's width range."""

    ranges = TYPE_SIZE_RANGES_MM.get(kind)
    if ranges is None:
        return True
    (l_lo, l_hi), (w_lo, w_hi) = ranges
    if row:
        return w_lo <= size_mm[1] <= w_hi
    return l_lo <= size_mm[0] <= l_hi and w_lo <= size_mm[1] <= w_hi


def recognise(inventory: dict[str, Any], views: list[dict[str, Any]], unit: dict[str, Any], doc: Any, work_dir: Path,
              transport: Callable | None, allow_external: bool, replay: Path | None = None,
              opened: list[str] | None = None) -> dict[str, Any]:
    scale = UNIT_TO_MM.get(str(unit.get("unit")))
    if scale is None:
        return {"status": "unit_unknown", "message_zh": "单位不明，不识别设备"}
    opened_set = set(opened or [])
    inventory = normalise_anonymous(inventory)
    families = build_families(inventory, views, unit)
    decisions: dict[str, dict[str, Any]] = {}
    stage_counts = Counter()
    if inventory["anonymous_names_grouped"]:
        stage_counts["anonymous_names_grouped"] = inventory["anonymous_names_grouped"]
        stage_counts["anonymous_groups"] = inventory["anonymous_groups"]
    # --- candidates that are not "one block = one machine" (no LLM)
    arrays = regular_arrays(families, scale)
    array_of = {ref: index for index, a in enumerate(arrays) for ref in a["members"]}
    # a block holding one kind of cell in a regular array that spans it is a row: judged as one unit, its cells through it
    rows = row_blocks(families, arrays)
    # rows of one kind drawn by different blocks: the block with the most copies is asked, the others follow its answer
    follower_of = row_kinds(rows, families, scale)
    cell_rows: dict[str, list[str]] = defaultdict(list)
    for info in rows.values():
        cell_rows[info["cell_family"]].append(info["row_family"])
    for key, fam in families.items():
        d = rule_decision(fam)
        if d is None:
            continue
        if key in cell_rows and d["basis"] == "geometry" and d["evidence"][0]["kind"] == "size_rule":
            stage_counts["cell_size_rule_waived"] += 1  # a cell is judged through its row, however small it is
            continue
        decisions[key] = {**d, "stage": "rules"}
        stage_counts["rules"] += 1
    cell_groups = outline_cell_groups(inventory["closed_outlines"], views, scale, opened_set)
    cell_classes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in cell_groups:
        group["key"] = f"cells:{group['cell_size_mm'][0]}x{group['cell_size_mm'][1]}"
        cell_classes[group["key"]].append(group)
    member_sets = {k: [{"ref": o["ref"], "bbox": o["bbox"], "view_id": o["view_id"]} for o in f["occurrences"]]
                   for k, f in families.items() if k not in decisions}
    for key, groups in cell_classes.items():
        member_sets[key] = [{"ref": c["ref"], "bbox": c["bbox"], "view_id": g["view_id"]} for g in groups for c in g["cells"]]
    bound = bind_texts(inventory["texts"], inventory["closed_outlines"], views, scale, text_type, member_sets, opened_set)
    zone_votes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    weak_labels: list[dict[str, Any]] = []
    for zone in bound["zones"]:
        zone_votes[zone["key"]].append(zone)
    for key, zones in sorted(zone_votes.items()):
        kinds = {(z["type"], z["name"]) for z in zones}
        if len(kinds) != 1:
            stage_counts["zone_label_conflict"] += 1
            continue
        # a label names a whole family only when it is not an area name ("叉车充电区" is where forklifts charge, not forklifts)
        # and the labelled places hold (nearly) every member of the family; otherwise the shape is judged and the label stays a hint
        # a label written inside one member (one cell) covers that member, not the members of its whole group
        covered = sum(1 if z["kind"] == "zone_label_inside_member" else z["members_inside"] for z in zones)
        if any(z.get("zone_only") for z in zones) or covered < ZONE_LABEL_MIN_COVERAGE * max(len(member_sets.get(key, [])), 1):
            stage_counts["zone_label_not_used"] += 1
            weak_labels.append({"key": key, "texts": sorted({z["text"] for z in zones}), "members_inside": covered,
                                "members": len(member_sets.get(key, [])), "area_name": any(z.get("zone_only") for z in zones)})
            continue
        kind, name = next(iter(kinds))
        decisions[key] = {"type": kind, "name": name, "basis": "text", "stage": "rules",
                          "evidence": [{"kind": z["kind"], "text": z["text"], "term": z["term"], "ref": z["text_ref"], "members_inside": z["members_inside"]} for z in zones]}
        stage_counts["zone_label"] += 1

    # --- LLM shape judgement, top-down through the block hierarchy
    by_ref = {o["ref"]: o for o in inventory["occurrences"]}
    for key, fam in families.items():
        if key not in decisions and any(o["ref"] in opened_set for o in fam["occurrences"]):
            decisions[key] = {"type": "NOT_EQUIPMENT", "basis": "geometry", "stage": "rules", "evidence": [{"kind": "opened_as_whole_plan_container"}]}
            stage_counts["container"] += 1
    parents: dict[str, set[str | None]] = {}
    for key, fam in families.items():
        found = set()
        for occ in fam["occurrences"]:
            parent = by_ref.get(occ["parent_ref"]) if occ["parent_ref"] else None
            found.add(parent["original_block"] if parent is not None and parent["original_block"] in families else None)
        parents[key] = found

    # --- rows: cells named by a text name their row without a judge; a row's other children are its parts
    for row_key, info in rows.items():
        cell = decisions.get(info["cell_family"])
        if row_key not in decisions and cell is not None and cell["basis"] == "text" and cell["type"] in EQUIPMENT_TYPES:
            decisions[row_key] = {"type": cell["type"], "basis": "text", "stage": "rules", "name": cell.get("name", ""),
                                  "evidence": [{"kind": "row_of_text_named_cells", **info}]}
            stage_counts["row_from_named_cells"] += 1
        for child in families[row_key]["children"]:
            if child != info["cell_family"] and child not in decisions and child not in rows and parents.get(child) and all(p in rows for p in parents[child]):
                decisions[child] = {"type": "COMPONENT", "basis": "geometry", "stage": "rules", "evidence": [{"kind": "part_of_a_row", "row": row_key}]}
                stage_counts["part_of_row"] += 1

    def decide_followers() -> None:
        """A row of the same kind as a judged row, drawn by another block, takes that row's answer: one kind, one question."""

        for key, lead in follower_of.items():
            if key in decisions or lead not in decisions:
                continue
            source = decisions[lead]
            wrong = "; or ".join([w for w in [source.get("wrong_if") or ""] if w] + [f"{key[:40]} is not the same kind of row as {lead[:40]}"])
            decisions[key] = {"type": source["type"], "basis": "row_kind", "via": source["basis"], "stage": "rules", "name": source.get("name", ""),
                              "same_kind_as": lead, "votes": source.get("votes"), "reason": "row_kind_not_recognised" if source["type"] == "UNRECOGNISED" else None,
                              "wrong_if": wrong, "evidence": [{"kind": "same_row_kind_as", "row": lead, **{k: rows[key][k] for k in ("cells_per_row", "groups", "pitch_mm")}}]}

    def decide_cells() -> None:
        """A cell family takes its rows' type once every row holding it is decided; cells are never judged alone."""

        decide_followers()  # a follower row is decided with the leader of its kind; its cells then follow it
        for cell_key, row_keys in cell_rows.items():
            if cell_key in decisions or any(r not in decisions for r in row_keys):
                continue
            # the type of the recognised rows holding it (the most common; sorted order on a tie); unrecognised only when none is
            kinds = Counter(decisions[r]["type"] for r in sorted(row_keys) if decisions[r]["type"] != "UNRECOGNISED")
            kind = kinds.most_common(1)[0][0] if kinds else "UNRECOGNISED"
            source = next(decisions[r] for r in sorted(row_keys) if decisions[r]["type"] == kind)
            decisions[cell_key] = {"type": kind, "basis": "row_structure", "via": source.get("via") or source["basis"], "stage": "rules", "name": source.get("name", ""),
                                   "rows": sorted(row_keys), "reason": "row_not_recognised" if kind == "UNRECOGNISED" else None, "wrong_if": source.get("wrong_if", ""),
                                   "evidence": [{"kind": "cells_of_a_row", "rows": sorted(row_keys)}]}

    decide_cells()

    # a pasted background drawing: its once-only blocks are listed, not judged
    below: dict[str, set[str]] = defaultdict(set)
    for key, fam in families.items():
        if key in decisions:
            continue
        for occ in fam["occurrences"]:
            outer, parent = None, occ["parent_ref"]
            while parent and parent in by_ref:
                outer, parent = parent, by_ref[parent]["parent_ref"]
            if outer:
                below[outer].add(key)
    backgrounds = []
    for outer, keys in sorted(below.items()):
        once = sorted(k for k in keys if len(families[k]["occurrences"]) == 1)
        if len(keys) >= BACKGROUND_MIN_FAMILIES and len(once) >= BACKGROUND_ONCE_ONLY_SHARE * len(keys):
            backgrounds.append({"container_ref": outer, "container_family": by_ref[outer]["original_block"], "families": len(keys), "once_only_not_judged": len(once)})
            for key in once:
                decisions[key] = {"type": "NOT_JUDGED", "basis": "geometry", "stage": "rules",
                                  "evidence": [{"kind": "once_only_block_inside_a_background_drawing", "container": outer}]}
                stage_counts["background_not_judged"] += 1

    def holds_machines(key: str) -> bool | None:
        """False: a machine or a part (what is inside it is its parts).  True: not a machine, look inside.  None: not decided yet."""

        d = decisions.get(key)
        if d is None:
            return None
        return not (d["type"] in EQUIPMENT_TYPES or d["type"] in {"COMPONENT", "NOT_JUDGED"})

    array_families = {a["family"] for a in arrays}
    all_boxes = [o["bbox"] for o in inventory["occurrences"] if o["bbox"]]
    pending = {k for k in families if k not in decisions and k not in cell_rows and k not in follower_of}  # cells and follower rows are decided through their rows / kind
    start = 0
    while pending:
        ready = sorted(k for k in pending if k in array_families or k in rows or any(p is None or holds_machines(p) is True for p in parents[k]))
        parts = sorted(k for k in pending if k not in ready and all(p is not None and holds_machines(p) is False for p in parents[k]))
        for key in parts:
            decisions[key] = {"type": "COMPONENT", "basis": "geometry", "stage": "rules",
                              "evidence": [{"kind": "only_drawn_inside_a_machine_or_part", "inside": sorted(p for p in parents[key] if p)[:5]}]}
            stage_counts["part_of_machine"] += 1
        pending -= set(parts)
        if not ready:
            if parts:
                continue
            break
        judged = judge_shapes(doc, families, ready, inventory["texts"], scale, work_dir / "type_judge", transport, allow_external, replay, all_boxes, start,
                              arrays=arrays, row_info=rows)
        start += math.ceil(len(ready) / TYPE_JUDGE_TILES_PER_SHEET) * TYPE_JUDGE_TILES_PER_SHEET
        pending -= set(ready)
        for key, j in judged.items():
            # what the judges said would make their answer wrong, kept with the decision (target rule 4): the sentence given with the adopted type first
            wrong = [w for w, t in zip(j.get("wrong_if") or [], j.get("votes") or []) if w and t == j.get("type")] + [w for w in j.get("wrong_if") or [] if w]
            extra = {"reasons": j.get("reasons", []), "wrong_if": wrong[0] if wrong else "", "wrong_if_all": list(dict.fromkeys(wrong))[:5]}
            if j["status"] == "agreed" and j["type"] == "UNSURE":
                decisions[key] = {"type": "UNRECOGNISED", "basis": "llm_shape", "stage": "llm", "reason": "judge_unsure", "votes": j["votes"],
                                  "tile": [j["sheet"], j["tile_id"]], **extra}
                stage_counts["llm_unsure"] += 1
            elif j["status"] == "agreed":
                decisions[key] = {"type": j["type"], "basis": "llm_shape", "stage": "llm", "votes": j["votes"], "tile": [j["sheet"], j["tile_id"]],
                                  "name": next((n for n in j.get("names", []) if n), ""), **extra}
                stage_counts["llm_agreed"] += 1
            else:
                decisions[key] = {"type": "UNRECOGNISED", "basis": "llm_shape", "stage": "llm", "reason": f"judge_{j['status']}", "votes": j.get("votes", []),
                                  "tile": [j["sheet"], j["tile_id"]], **extra}
                stage_counts[f"llm_{j['status']}"] += 1
        decide_cells()
    for key in sorted(pending):  # cannot happen in a block hierarchy; kept visible if it ever does
        decisions[key] = {"type": "UNRECOGNISED", "basis": "geometry", "stage": "rules", "reason": "not_reached_by_the_top_down_judgement"}
        stage_counts["not_reached"] += 1
    decide_cells()
    for key, d in decisions.items():
        if d.get("basis") in ("row_structure", "row_kind"):
            continue  # cells follow their row, follower rows their leader: re-derived below
        size = families[key]["size_mm"] if key in families else tuple(float(v) for v in key.split(":", 1)[1].split("x"))
        if d["type"] in EQUIPMENT_TYPES and not size_plausible(d["type"], size, row=key in rows):
            d["rejected_type"] = d["type"]
            d["type"] = "UNRECOGNISED"
            d["reason"] = "size_implausible_for_type"
            stage_counts["size_rejected"] += 1
    for key in [k for k, d in decisions.items() if d.get("basis") in ("row_structure", "row_kind")]:
        del decisions[key]
    decide_cells()
    stage_counts["cells_of_row"] = sum(1 for d in decisions.values() if d.get("basis") == "row_structure")
    stage_counts["rows_of_same_kind"] = sum(1 for d in decisions.values() if d.get("basis") == "row_kind")

    # --- block occurrences: which are counted
    equipment_families = {k for k, d in decisions.items() if k in families and d["type"] in EQUIPMENT_TYPES}
    unrecognised_families = {k for k, d in decisions.items() if k in families and d["type"] == "UNRECOGNISED"}

    def machine_ancestors(occ: dict[str, Any]) -> list[dict[str, Any]]:
        found, parent = [], occ["parent_ref"]
        while parent:
            p = by_ref.get(parent)
            if p is None:
                break
            if p["original_block"] in equipment_families:
                found.append(p)
            parent = p["parent_ref"]
        return found

    def in_row(key: str, occ: dict[str, Any]) -> bool:
        """This occurrence is a cell drawn inside one of its rows (counted through the row, never on its own)."""

        parent = by_ref.get(occ["parent_ref"]) if occ["parent_ref"] else None
        return parent is not None and parent["original_block"] in rows and rows[parent["original_block"]]["cell_family"] == key

    countable: list[tuple[str, dict[str, Any]]] = []
    assemblies: dict[str, dict[str, Any]] = {}
    nested_skipped = 0
    cells_in_rows = 0
    for key in sorted(equipment_families):
        for occ in families[key]["occurrences"]:
            if in_row(key, occ):
                cells_in_rows += 1
                continue
            above = machine_ancestors(occ)
            if not above:
                countable.append((key, occ))
            elif (key in rows or occ["ref"] in array_of) and all(decisions[p["original_block"]]["basis"] == "llm_shape" for p in above):
                # rows, or a packed regular array of modules: they are counted, the outer block is their assembly
                countable.append((key, occ))
                for p in above:
                    assemblies.setdefault(p["ref"], {"ref": p["ref"], "family": p["original_block"], "llm_type": decisions[p["original_block"]]["type"],
                                                     "reason": "holds_counted_rows" if key in rows else "holds_a_packed_regular_array_of_counted_modules",
                                                     "module_family": key})
            else:
                nested_skipped += 1
    from shapely.geometry import Point, Polygon

    outline_machines, rejected_outlines = [], []
    for device in bound["devices"]:
        points = device.pop("points")
        if size_plausible(device["type"], tuple(device["size_mm"])):
            outline_machines.append((device, Polygon(points)))
        else:
            rejected_outlines.append({**device, "reason": "size_implausible_for_type"})
    records = []
    duplicates_skipped = 0
    parts_of_outline_machines = 0
    seen_positions: set[tuple] = set()
    for key, occ in countable:
        if occ["ref"] in assemblies:
            continue
        d = decisions[key]
        if d["basis"] == "llm_shape" and any(v["view_id"] == occ["view_id"] and polygon.contains(Point(*_centre(occ["bbox"]))) for v, polygon in outline_machines):
            parts_of_outline_machines += 1  # drawn inside a machine identified by its written name
            continue
        position = (key, tuple(tuple(round(v, 1) for v in pt) for pt in occ["footprint"]))
        if position in seen_positions:  # an exact copy inserted at the same place
            duplicates_skipped += 1
            continue
        seen_positions.add(position)
        L, W = _footprint_size(occ["footprint"], scale)
        m = occ["matrix"]
        basis = d.get("via", d["basis"]) if d["basis"] in ("row_structure", "row_kind") else d["basis"]  # a loose cell / follower row rests on what decided its row / kind
        record = {"ref": occ["ref"], "view_id": occ["view_id"], "type": d["type"], "type_zh": LABEL_ZH.get(d["type"], d["type"]),
                  "name": d.get("name", ""), "family": key, "basis": basis,
                  "drawn_as": "row" if key in rows else "loose_cell" if key in cell_rows else "block",
                  "status": "accepted" if basis != "llm_shape" else "pending_independent_count_and_spot_check",
                  "centre": [round(v, 1) for v in _centre(occ["bbox"])], "length_mm": round(L, 1), "width_mm": round(W, 1),
                  "rotation_deg": round(math.degrees(math.atan2(m[1], m[0])) % 360.0, 2)}
        if key in rows:  # one row is one record; its cells are the second level of the count
            record.update({"cells": rows[key]["cells_per_row"], "cell_family": rows[key]["cell_family"], "cell_size_mm": rows[key]["cell_size_mm"],
                           "row_kind": follower_of.get(key, key)})
        if occ["ref"] in array_of:
            record["array"] = array_of[occ["ref"]]
        records.append(record)

    # --- cells drawn as closed rectangles
    untyped: list[dict[str, Any]] = []
    for group in cell_groups:
        d = decisions.get(group["key"])
        group["type"] = d["type"] if d else None
        for cell in group["cells"]:
            if d is not None and d["type"] in EQUIPMENT_TYPES:
                records.append({"ref": cell["ref"], "view_id": group["view_id"], "type": d["type"], "type_zh": LABEL_ZH.get(d["type"], d["type"]),
                                "name": d.get("name", ""), "family": group["key"], "basis": "text", "drawn_as": "outline_cell", "status": "accepted",
                                "group": group["group_id"], "centre": [round(v, 1) for v in _centre(cell["bbox"])],
                                "length_mm": float(cell["size_mm"][0]), "width_mm": float(cell["size_mm"][1]), "rotation_deg": cell["angle_deg"]})
            else:
                untyped.append({"ref": cell["ref"], "view_id": group["view_id"], "key": group["key"], "bbox": cell["bbox"], "drawn_as": "outline_cell",
                                "reason": (d or {}).get("reason", "no_text_names_this_group")})
    # --- machines drawn as a closed outline with their name inside
    for device, _ in outline_machines:
        records.append({"ref": device["ref"], "view_id": device["view_id"], "type": device["type"], "type_zh": LABEL_ZH.get(device["type"], device["type"]),
                        "name": device["name"], "family": f"outline:{device['term']}", "basis": "text", "drawn_as": "labelled_outline", "status": "accepted",
                        "label_ref": device["text_ref"], "label_text": device["text"], "centre": [round(v, 1) for v in _centre(device["bbox"])],
                        "length_mm": device["size_mm"][0], "width_mm": device["size_mm"][1], "rotation_deg": device["rotation_deg"]})
    # --- block occurrences that are candidates but got no type
    for key in sorted(unrecognised_families):
        for occ in families[key]["occurrences"]:
            if not machine_ancestors(occ) and not in_row(key, occ):
                untyped.append({"ref": occ["ref"], "view_id": occ["view_id"], "key": key, "bbox": occ["bbox"], "drawn_as": "block",
                                "reason": decisions[key].get("reason")})

    # --- the drawing's own equipment numbers: each takes the type of the smallest typed machine block that holds it
    series = number_series(inventory["texts"], views, opened_set)
    numbered: list[dict[str, Any]] = []
    by_number: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"text_or_geometry": 0, "llm_shape": 0, "untyped": 0}))
    if series:
        from shapely.strtree import STRtree

        hosts = [(key, occ, Polygon(occ["footprint"])) for key in sorted(equipment_families) for occ in families[key]["occurrences"] if len(occ["footprint"]) >= 3]
        hosts = [h for h in hosts if h[2].is_valid and h[2].area > 0]
        tree = STRtree([h[2] for h in hosts]) if hosts else None
        for group in series:
            for member in group["members"]:
                point = Point(*member["anchor"])
                inside = [int(i) for i in tree.query(point) if hosts[int(i)][2].contains(point) and hosts[int(i)][1]["view_id"] == group["view_id"]] if tree is not None else []
                unit = {"number": member["number"], "ref": member["ref"], "view_id": group["view_id"], "xy": member["xy"], "layer": group["layer"]}
                if inside:
                    key, occ, _ = hosts[min(inside, key=lambda i: hosts[i][2].area)]
                    d = decisions[key]
                    unit.update({"type": d["type"], "type_zh": LABEL_ZH.get(d["type"], d["type"]), "name": d.get("name", ""), "basis": d["basis"],
                                 "host_ref": occ["ref"], "host_family": key})
                    by_number[group["view_id"]][d["type"]]["llm_shape" if d["basis"] == "llm_shape" else "text_or_geometry"] += 1
                else:
                    unit.update({"type": None, "basis": None, "host_ref": None})
                    by_number[group["view_id"]]["UNTYPED"]["untyped"] += 1
                    h = member["height"] or 1.0
                    untyped.append({"ref": member["ref"], "view_id": group["view_id"], "key": f"number:{group['layer']}", "drawn_as": "number_tag",
                                    "bbox": [member["xy"][0], member["xy"][1], member["xy"][0] + 0.7 * h * len(member["number"]), member["xy"][1] + h],
                                    "reason": "number_not_inside_a_typed_machine"})
                numbered.append(unit)

    # --- drawn quantity notes ("共 6 台") against the program's own count
    stated = []
    for note in bound["stated_quantities"]:
        counted = sum(1 for r in records if r["view_id"] == note["view_id"] and r["type"] == note["type"]
                      and (note["type"] != "OTHER_EQUIPMENT" or r["name"] == note["name"]))
        stated.append({**note, "counted": counted, "status": "consistent" if counted == note["stated"] else "differs"})

    summary: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"text_or_geometry": 0, "llm_shape": 0}))
    for r in records:
        summary[r["view_id"]][r["type"]]["llm_shape" if r["basis"] == "llm_shape" else "text_or_geometry"] += 1
    unrecognised = [{"family": k, "occurrences": len(families[k]["occurrences"]), "size_mm": [round(v) for v in families[k]["size_mm"]], **{x: decisions[k].get(x) for x in ("reason", "votes", "rejected_type", "wrong_if")}}
                    for k in sorted(unrecognised_families)]
    row_summary = []
    for row_key, info in sorted(rows.items()):
        if row_key in follower_of:
            continue  # listed under the leader of its kind
        d = decisions.get(row_key) or {}
        kind_keys = [row_key] + sorted(k for k, lead in follower_of.items() if lead == row_key)
        counted = [r for r in records if r["family"] in kind_keys]
        entry = {**info, "type": d.get("type"), "type_zh": LABEL_ZH.get(d.get("type"), d.get("type")), "basis": d.get("basis"),
                 "rows_counted": len(counted), "cells_total": sum(r.get("cells") or 0 for r in counted), "wrong_if": d.get("wrong_if", "")}
        if len(kind_keys) > 1:  # one kind of row drawn by several blocks: one entry, each block's share shown
            entry["drawn_by"] = {k: sum(1 for r in counted if r["family"] == k) for k in kind_keys}
            entry["also_drawn_by"] = [{**f, "rows_counted": entry["drawn_by"][f["family"]],
                                       "cells_total": sum(r.get("cells") or 0 for r in counted if r["family"] == f["family"])} for f in info.get("also_drawn_by", [])]
        row_summary.append(entry)
    return {
        "status": "done",
        "stage_counts": dict(stage_counts), "nested_occurrences_not_recounted": nested_skipped, "labels_not_used_for_type": weak_labels,
        "rows": row_summary, "cell_occurrences_counted_through_rows": cells_in_rows,
        "exact_duplicates_not_recounted": duplicates_skipped, "parts_inside_named_outlines_not_recounted": parts_of_outline_machines,
        "families": {k: {"size_mm": [round(v) for v in f["size_mm"]], "occurrences": len(f["occurrences"]), "decision": decisions.get(k)} for k, f in families.items()},
        "records": records,
        "summary": {v: dict(t) for v, t in summary.items()},
        "unrecognised": sorted(unrecognised, key=lambda r: -r["occurrences"]),
        "regular_arrays": [{k: a[k] for k in ("family", "parent_ref", "view_id", "count", "pitch_mm", "along", "bbox")} for a in arrays],
        "cell_groups": [{**{k: g[k] for k in ("group_id", "view_id", "key", "cell_size_mm", "count", "bbox", "type")}, "decision": decisions.get(g["key"])} for g in cell_groups],
        "assemblies_not_counted": sorted(assemblies.values(), key=lambda a: a["ref"]),
        "zone_labels": bound["zones"], "unbound_labels": bound["unbound_labels"], "label_conflicts": bound["conflicts"],
        "outlines_rejected": rejected_outlines, "stated_quantities": stated,
        "number_series": [{k: s[k] for k in ("view_id", "layer", "digits", "text_height", "count", "distinct", "repeated_numbers")} for s in series],
        "numbered_units": numbered, "summary_by_number": {v: dict(t) for v, t in by_number.items()},
        "candidates_untyped": untyped, "background_drawings": backgrounds,
        "duplicate_views": duplicate_views(records, views),
    }


def duplicate_views(records: list[dict[str, Any]], views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Views with the same title whose equipment (family and type, counted) is nearly the same: one drawn set, copied.

    The later view is listed as a copy of the earlier one; counts of copies are not to be added up.
    """

    titles = {v["view_id"]: ((v.get("title") or {}).get("text") or "").strip() for v in views}
    content: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        content[r["view_id"]][(r.get("family"), r["type"])] += 1
    order = [v["view_id"] for v in views if v["view_id"] in content]
    copies, taken = [], set()
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            if b in taken or not titles.get(a) or titles.get(a) != titles.get(b):
                continue
            shared = sum((content[a] & content[b]).values())
            overlap = shared / max(sum(content[a].values()), sum(content[b].values()), 1)
            if overlap >= DUPLICATE_VIEW_MIN_OVERLAP:
                copies.append({"view_id": b, "same_as": a, "title": titles[a], "overlap": round(overlap, 3)})
                taken.add(b)
    return copies


def equipment_markdown(result: dict[str, Any], views: list[dict[str, Any]]) -> str:
    if result.get("status") != "done":
        return f"# 设备清单\n\n{result.get('message_zh', result.get('status'))}\n"
    titles = {v["view_id"]: (v.get("title") or {}).get("text") or "" for v in views}
    lines = ["# 设备清单（零人工识别）", "", "依据分两列：文字/几何依据（程序可复核）与 LLM 外形判断（待独立清点和抽查后才算已认出）。", "",
             f"判定阶段：{json.dumps(result['stage_counts'], ensure_ascii=False)}；套在设备里的子块不重复计数：{result['nested_occurrences_not_recounted']} 处；同位置重复插入不重复计数：{result.get('exact_duplicates_not_recounted', 0)} 处。", "",
             "| 视图 | 标题 | 设备类型 | 文字/几何依据 | LLM 外形判断 | 合计 |", "|---|---|---|---|---|---|"]
    for view_id, types in sorted(result["summary"].items()):
        for kind, n in sorted(types.items()):
            lines.append(f"| {view_id} | {titles.get(view_id, '')[:20]} | {LABEL_ZH.get(kind, kind)} | {n['text_or_geometry']} | {n['llm_shape']} | {n['text_or_geometry'] + n['llm_shape']} |")
    for copy in result.get("duplicate_views") or []:
        lines.append(f"\n{copy['view_id']}（{copy['title']}）与 {copy['same_as']} 的设备几乎相同（重合 {copy['overlap']:.0%}），是同一套图画了两遍：两处的数量不能相加。")
    drawn = Counter(r.get("drawn_as", "block") for r in result["records"])
    lines += ["", f"画法：一个图块一台 {drawn.get('block', 0)}；成排画的图块（整排计 1）{drawn.get('row', 0)}；排外零散的格位 {drawn.get('loose_cell', 0)}；"
              f"闭合轮廓里写着设备名 {drawn.get('labelled_outline', 0)}；用闭合矩形画的格位 {drawn.get('outline_cell', 0)}。",
              f"成排等间距的相同图块：{len(result.get('regular_arrays', []))} 组；外层图块因里面是成排模块而不计：{len(result.get('assemblies_not_counted', []))} 处。"]
    for row in result.get("rows") or []:
        if row.get("rows_counted"):
            shares = "".join(f"，其中图块 {f['family'][:30]} 画的 {f['rows_counted']} 排" for f in row.get("also_drawn_by") or [] if f.get("rows_counted"))
            lines.append(f"成排画的{row['type_zh']}：{row['row_family'][:30]} {row['rows_counted']} 排{shares}，每排 {row['cells_per_row']} 格"
                         f"（{row['cell_size_mm'][0]}×{row['cell_size_mm'][1]} mm，节距 {float(row['pitch_mm']):.0f} mm），共 {row['cells_total']} 格；上表按排计。")
    if result.get("stated_quantities"):
        lines += ["", "图上写的数量与程序计数对照：", "", "| 视图 | 图上文字 | 图上写 | 程序数到 | 结论 |", "|---|---|---|---|---|"]
        for q in result["stated_quantities"]:
            lines.append(f"| {q['view_id']} | {q['text']} | {q['stated']} | {q['counted']} | {'一致' if q['status'] == 'consistent' else '不一致'} |")
    if result.get("summary_by_number"):
        lines += ["", "按图上的设备编号数（与上表按图形数是两种数法，不相加；一台设备可能带多个编号）：", "",
                  "| 视图 | 设备类型 | 文字/几何依据 | LLM 外形判断 | 还没认出类型 |", "|---|---|---|---|---|"]
        for view_id, types in sorted(result["summary_by_number"].items()):
            for kind, n in sorted(types.items()):
                lines.append(f"| {view_id} | {LABEL_ZH.get(kind, '—' if kind == 'UNTYPED' else kind)} | {n['text_or_geometry']} | {n['llm_shape']} | {n['untyped']} |")
    untyped = Counter((u["view_id"], u["key"], u["drawn_as"]) for u in result.get("candidates_untyped", []))
    if untyped:
        lines += ["", "已找到、但还没认出类型的候选（不计入上表）：", ""]
        drawn_zh = {"outline_cell": "闭合矩形格位", "block": "图块", "number_tag": "设备编号"}
        for (view_id, key, drawn_as), n in sorted(untyped.items(), key=lambda kv: -kv[1])[:20]:
            lines.append(f"- {view_id}：{key[:30]}（{drawn_zh.get(drawn_as, drawn_as)}）{n} 个")
    lines += ["", f"未识别的图块种类：{len(result['unrecognised'])}（按出现次数排序，前 15）：", ""]
    for u in result["unrecognised"][:15]:
        lines.append(f"- {u['family'][:30]}：{u['occurrences']} 次，{u['size_mm'][0]}×{u['size_mm'][1]} mm，原因 {u.get('reason')}，投票 {u.get('votes')}"
                     + (f"，判官说会判错的情况：{u['wrong_if']}" if u.get("wrong_if") else ""))
    return "\n".join(lines) + "\n"


__all__ = ["build_families", "equipment_markdown", "judge_shapes", "parse_tiles", "recognise", "rule_decision", "size_plausible", "text_type"]
