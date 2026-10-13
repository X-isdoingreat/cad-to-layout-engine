"""Human confirmation of an agent read, in CAD.

1. ``build_items``: the places the program is not sure about become numbered items (C01, C02 ...), each with a
   question, the area it may change, where it is and the shape in question.  They come from the program's own
   findings on the read and, when other reads of the same drawing are given, from where the reads disagree.
2. ``mark_entities``: the items as drawing entities on their own layers, to be added to the delivered DWG.
3. A person opens the DWG in AutoCAD and, per item, moves its number label to the layer 「CTLE_确认_要」 or
   「CTLE_确认_不要」 (zone items: to a category layer), draws a corrected closed outline on 「CTLE_人工轮廓」,
   deletes wrong column circles or adds circles on 「CTLE_人工柱」.
4. ``apply``: the saved drawing (as DXF) is read back, the decisions applied and every number labelled
   AI 读出 / 人工确认 / 人工修改（未标记处） / 待确认.  Unhandled items stay pending; an edit that cannot be tied to
   one item or one building is reported, not guessed.

Plain data in and out (coordinates in drawing units); the CAD file handling lives in scripts/confirm_cad.py.
"""

from __future__ import annotations

import math
from typing import Any

LAYER_LABEL = "CTLE_待确认"
LAYER_AREA = "CTLE_待确认_范围"
LAYER_YES = "CTLE_确认_要"
LAYER_NO = "CTLE_确认_不要"
LAYER_HUMAN_OUTLINE = "CTLE_人工轮廓"
LAYER_HUMAN_COLUMN = "CTLE_人工柱"
LAYER_HUMAN_ZONE = "CTLE_人工区域"
CATEGORY_LAYERS = {"CTLE_类别_存储": "storage", "CTLE_类别_作业": "operation", "CTLE_类别_其他": "other"}
COLUMN_LAYER = "CTLE_COLUMN"
CONFIRM_LAYERS = {LAYER_LABEL: 6, LAYER_AREA: 6, LAYER_YES: 3, LAYER_NO: 1, LAYER_HUMAN_OUTLINE: 4, LAYER_HUMAN_COLUMN: 4, LAYER_HUMAN_ZONE: 5,
                  **{k: 2 for k in CATEGORY_LAYERS}}

DISAGREE_SHARE = 0.01  # reads whose areas for one building differ by more than this disagree (the acceptance tolerance)
SLIVER_SHARE = 0.002  # disagreement pieces smaller than this share of the building are not marked
OVERLAP_MIN = 0.5  # a human outline belongs to a building it overlaps by at least this share of the smaller of the two
TOUCH_SHARE = 0.1  # a human outline covering this share of two or more buildings is ambiguous

QUESTIONS = {
    "outline_beyond_grid": "这块在柱网最外一圈柱子以外，算不算这栋楼？（要＝算进楼；不要＝不算）",
    "outline_face_without_columns": "这块里没有这栋楼的柱子，算不算这栋楼？（要＝算进楼；不要＝不算）",
    "outline_annex_left_out": "这块与楼共墙、里面有同一柱网的柱子，却没算进楼。算不算？（要＝并入；不要＝不并入）",
    "outline_inner_line": "这一侧的柱子落在轮廓外面，轮廓可能走了里线。请在「CTLE_人工轮廓」层沿外墙外边线描出整栋楼；轮廓没问题就选不要。",
    "reads_extra": "另一次读图没有这块，算不算这栋楼？（要＝算；不要＝不算）",
    "reads_missing": "另一次读图有这块，这次没有，算不算这栋楼？（要＝并入；不要＝不并入）",
    "reads_columns": "两次读图这栋楼的柱子数不一致：请删掉多余的柱子圈、在「CTLE_人工柱」层补上漏的；核对无误后选要。",
    "dimension": "图上总尺寸与轮廓长度不一致，轮廓对吗？（要＝轮廓对；不对请在「CTLE_人工轮廓」层重描）",
    "remeasure": "程序重新量出的结果与读图报告不一致，这栋楼的轮廓对吗？（要＝对；不对请重描）",
    "columns": "这栋楼的柱子数可能不对：请删掉多余的柱子圈、在「CTLE_人工柱」层补上漏的，然后选要。",
    "building_not_reported": "这里有建筑名称，但没有读出这栋楼。需要的话在「CTLE_人工轮廓」层描出外轮廓；不需要选不要。",
    "no_building": "这张图一栋楼都没读出来。需要的话在「CTLE_人工轮廓」层描出外轮廓；不需要选不要。",
    "designated_outline_review": "红线是 AI 读出的这栋厂房轮廓：是沿外墙外边线、没有多算空地或别的楼、也没有漏掉附跨吗？对就改到「要」；不对就在「CTLE_人工轮廓」层重描整栋，重描后也改到「要」（以人工轮廓为准，这栋楼的其他轮廓编号，包括“算不算这块”，都不用再管）。",
    "zone_no_area": "这个区域名称没有算出面积：图上它的边界不闭合、没画，或一个框里有几个名称。如果你能看出它的范围，在「CTLE_人工区域」层描一圈闭合线（PL，捕捉拐角，C 闭合），并把编号改到「CTLE_确认_要」；图上看不出范围或它不是功能区，就把编号改到「CTLE_确认_不要」，结果里会写明\"图上未画边界\"。",
    "view_count": "这个视图算不算进面积合计？（要＝算；不要＝不算，例如另一套方案、重复视图、详图）。同一楼层的几个方案只能选一个要；选了不要的视图，里面的其他编号都不用再处理。",
    "zone_reads_missing": "两次读图只有一次读出了这个功能区：它是不是一个带名称的功能区、边界对不对？（要＝计入；不要＝不计入）",
    "zone_reads_area": "两次读图这个功能区的面积不一样：这次的边界对吗？（要＝对；不对请在「CTLE_人工轮廓」层重描该区域）",
    "designated_missing": "指定的厂房没有读出来：请在「CTLE_人工轮廓」层描出要改造的那栋厂房（用捕捉沿外墙外边线点拐角，闭合），然后选要。",
    "height": "这个高度是不是指定厂房的净高？（要＝是；不要＝不是或不确定）",
    "zone_category": "这个区域看不出是存储还是作业：把编号移到「CTLE_类别_存储」「CTLE_类别_作业」或「CTLE_类别_其他」层。",
}
NOT_FOR_PEOPLE = {"bookkeeping", "columns", "reads_columns", "building_not_reported", "no_building"}  # trial scope: no columns, no other buildings  # findings about the agent's own record keeping; they change no number


def _polygon(shape: Any) -> Any:
    """A polygon from a list of points, or from {"shell": [...], "holes": [[...]]} (a piece with holes, e.g. a frame round a building)."""

    from shapely.geometry import Polygon

    p = Polygon(shape["shell"], shape.get("holes") or []) if isinstance(shape, dict) else Polygon(shape)
    return p if p.is_valid else p.buffer(0)


def _shape(g: Any) -> Any:
    shell = [[x, y] for x, y in g.exterior.coords]
    holes = [[[x, y] for x, y in r.coords] for r in g.interiors]
    return {"shell": shell, "holes": holes} if holes else shell


def _parts(geometry: Any) -> list[Any]:
    return [g for g in getattr(geometry, "geoms", [geometry]) if not g.is_empty and g.geom_type == "Polygon"]


def _outline_of(read: dict[str, Any], name: str) -> list[list[float]] | None:
    for c in read.get("checks", []):
        if c.get("name") == name and c.get("outline"):
            return c["outline"]
    return None


def _is_designated(read: dict[str, Any], name: str | None) -> bool:
    return any(b.get("name") == name and b.get("designated") for b in (read.get("answer") or {}).get("buildings", []))


def _columns_of(read: dict[str, Any], name: str | None) -> int | None:
    for b in (read.get("answer") or {}).get("buildings", []):
        if b.get("name") == name:
            count = (b.get("columns") or {}).get("count")
            return int(count) if isinstance(count, (int, float)) else None
    return None


def disagreement_items(read: dict[str, Any], others: list[dict[str, Any]], m: float) -> list[dict[str, Any]]:
    """Where another read of the same drawing put a building's outline differently (assistant default: such places are marked)."""

    items = []
    for c in read.get("checks", []):
        if not c.get("outline"):
            continue
        mine = _polygon(c["outline"])
        for other in others:
            best, best_share, best_name = None, 0.0, None
            for oc in other.get("checks", []):
                if not oc.get("outline"):
                    continue
                theirs = _polygon(oc["outline"])
                share = mine.intersection(theirs).area / max(min(mine.area, theirs.area), 1e-12)
                if share > best_share:
                    best, best_share, best_name = theirs, share, oc.get("name")
            if best is None or best_share < OVERLAP_MIN:
                continue
            counts = (_columns_of(read, c.get("name")), _columns_of(other, best_name))
            if None not in counts and counts[0] != counts[1]:
                p = mine.representative_point()
                items.append({"kind": "reads_columns", "building": c.get("name"), "impact_m2": 0.0, "at": [p.x, p.y], "geometry": None,
                              "text": f"两次读图柱子数不一致：{counts[0]} 根 / {counts[1]} 根"})
            if abs(mine.area - best.area) <= DISAGREE_SHARE * mine.area and mine.symmetric_difference(best).area <= DISAGREE_SHARE * mine.area:
                continue
            for kind, piece in (("reads_extra", mine.difference(best)), ("reads_missing", best.difference(mine))):
                for g in _parts(piece):
                    if g.area >= SLIVER_SHARE * mine.area:
                        p = g.representative_point()
                        items.append({"kind": kind, "building": c.get("name"), "impact_m2": round(g.area * m * m, 1), "at": [p.x, p.y],
                                      "geometry": {"polygons": [_shape(g)]}})
    return items


ROLE_CN = {"floor plan": "平面", "plan": "平面", "scheme": "方案", "existing": "现状", "section": "剖面", "elevation": "立面",
           "detail": "详图", "site plan": "总平面"}


def plain_role(role: Any) -> str:
    r = str(role or "").strip()
    return ROLE_CN.get(r.lower(), r)


def view_items(read: dict[str, Any], extra_views: set | None = None) -> list[dict[str, Any]]:
    """One item per plan view that holds named zones: a person says which views count (schemes and repeats are not added up)."""

    answer = read.get("answer") or {}
    with_zones = {z.get("view") for z in answer.get("zones") or []} | set(extra_views or ())
    items = []
    for v in answer.get("views") or []:
        w = v.get("window")
        if v.get("name") not in with_zones or not (isinstance(w, list) and len(w) == 4):
            continue
        items.append({"kind": "view_count", "building": None, "zone": None, "view": v.get("name"), "impact_m2": 0.0,
                      "at": [w[0], w[3]], "geometry": {"polygons": [[[w[0], w[1]], [w[2], w[1]], [w[2], w[3]], [w[0], w[3]], [w[0], w[1]]]]},
                      "text": f"视图「{v.get('name')}」（{plain_role(v.get('role'))}{'，与「' + v['same_floor_as'] + '」同一楼层' if v.get('same_floor_as') else ''}）"})
    return items


def zone_disagreement_items(read: dict[str, Any], faces: dict[str, Any], others: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Zones another read of the same drawing did not find, or found with another area (by name and overlap)."""

    if not others:
        return []
    items = []
    for z in (read.get("answer") or {}).get("zones") or []:
        face = faces.get(z.get("face"))
        if not face:
            continue
        mine = _polygon(face["outline"])
        p = mine.representative_point()
        for other in others:
            theirs = [oz for oz in (other.get("answer") or {}).get("zones") or [] if oz.get("name") == z.get("name")]
            areas = [float(oz.get("area_m2") or 0) for oz in theirs]
            if not theirs:
                items.append({"kind": "zone_reads_missing", "building": None, "zone": z.get("name"), "view": z.get("view"),
                              "impact_m2": float(face.get("area_m2") or 0), "at": [p.x, p.y], "geometry": {"polygons": [face["outline"]], "face": z.get("face")}})
            elif min(abs(a - float(face.get("area_m2") or 0)) for a in areas) > DISAGREE_SHARE * max(float(face.get("area_m2") or 0), 1e-9):
                items.append({"kind": "zone_reads_area", "building": None, "zone": z.get("name"), "view": z.get("view"),
                              "impact_m2": min(abs(a - float(face.get("area_m2") or 0)) for a in areas), "at": [p.x, p.y],
                              "geometry": {"polygons": [face["outline"]], "face": z.get("face")}})
    return items


def designated_review_items(read: dict[str, Any]) -> list[dict[str, Any]]:
    """The designated building's outline is always looked at once: it is the main deliverable, and two reads can share one error."""

    out = []
    m = float((read.get("answer") or {}).get("unit_m") or 1.0)
    for b in (read.get("answer") or {}).get("buildings") or []:
        outline = _outline_of(read, b.get("name")) if b.get("designated") else None
        if outline:
            p = _polygon(outline)
            x0, y0, x1, y1 = p.bounds
            out.append({"kind": "designated_outline_review", "building": b.get("name"), "impact_m2": round(p.area * m * m, 1),
                        "at": [x0, y1 + 0.03 * (y1 - y0)], "geometry": None})
    return out


def height_items(read: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for b in (read.get("answer") or {}).get("buildings") or []:
        if b.get("designated") and b.get("clear_height") and any(ch.isdigit() for ch in str(b["clear_height"])):
            outline = _outline_of(read, b.get("name"))
            if outline:
                p = _polygon(outline).representative_point()
                out.append({"kind": "height", "building": b.get("name"), "impact_m2": 0.0, "at": [p.x, p.y], "geometry": None,
                            "text": f"读出的高度：{b['clear_height']}"})
    return out


# default zone categories from the name (default, adjustable); a name matching none stays to_confirm
DEFAULT_CATEGORY = [
    ("office_living", ("办公", "休息", "更衣", "卫生间", "厕所", "会议", "值班", "宿舍", "餐厅")),
    ("operation", ("收货", "发货", "入库", "出库", "分拣", "拣选", "包装", "暂存", "暂放", "装卸", "理货", "集货", "复核", "验收", "领料", "退料", "备货")),
    ("storage", ("存储", "仓储", "仓库", "库房", "库区", "货架", "堆存", "存放")),
]


def default_category(name: str | None) -> str | None:
    n = str(name or "").strip()
    for category, words in DEFAULT_CATEGORY:
        if any(w in n for w in words):
            return category
    return "storage" if n.endswith("库") else None  # 飞机轮子存放库, 不可用件库: a named store


def apply_default_categories(read: dict[str, Any]) -> None:
    """Zones the agent left to_confirm get the default category of their name (labelled as such); the rest stay to_confirm."""

    for z in (read.get("answer") or {}).get("zones") or []:
        if z.get("category") == "to_confirm":
            c = default_category(z.get("name"))
            if c:
                z["category"], z["basis"] = c, f"默认口径（按名称，待确认）；原判断：{z.get('basis') or ''}"


def zone_items(read: dict[str, Any], faces: dict[str, Any]) -> list[dict[str, Any]]:
    """One question per zone name still to_confirm (the same name in several views is asked once)."""

    items, seen = [], set()
    for z in (read.get("answer") or {}).get("zones") or []:
        face = faces.get(z.get("face"))
        if z.get("category") == "to_confirm" and face and z.get("name") not in seen:
            seen.add(z.get("name"))
            p = _polygon(face["outline"]).representative_point()
            items.append({"kind": "zone_category", "building": None, "zone": z.get("name"), "view": None,
                          "impact_m2": float(face.get("area_m2") or 0), "at": [p.x, p.y], "geometry": {"polygons": [face["outline"]], "face": z.get("face")}})
    return items


# names of openings and fixtures, not of areas to measure: doors, windows, stairs, lifts, shafts, ramps
NOT_AN_AREA = ("口", "窗", "窗口", "门", "楼梯", "楼梯间", "电梯", "电梯间", "井", "坡道", "雨篷", "台阶")


def zone_no_area_items(read: dict[str, Any], texts: list[dict[str, Any]], suffixes: list[str]) -> list[dict[str, Any]]:
    """Area names written in a view that holds zones, but with no zone of that name: the read gave them no area."""

    answer = read.get("answer") or {}
    items = []
    plan_like = ("plan", "scheme", "existing", "floor")  # sections, elevations, details and site plans hold no zones to measure
    for v in answer.get("views") or []:
        w = v.get("window")
        names = {z.get("name") for z in answer.get("zones") or [] if z.get("view") == v.get("name")}
        role = str(v.get("role") or "").lower()
        site = "site" in role or "总平面" in role or "总平面" in str(v.get("name") or "")  # building names on a site plan are not zones
        if not (isinstance(w, list) and len(w) == 4) or (not names and (site or not any(k in role for k in plan_like))):
            continue  # a read that found no zone in a plan view still gets every written area name marked
        seen = set()
        for t in texts:
            name = t["t"].strip()
            if (w[0] <= t["x"] <= w[2] and w[1] <= t["y"] <= w[3] and 2 <= len(name) <= 14 and any(name.endswith(x) and name != x for x in suffixes)
                    and not name.endswith(NOT_AN_AREA) and name not in names and name not in seen):
                seen.add(name)
                items.append({"kind": "zone_no_area", "building": None, "zone": name, "view": v.get("name"), "impact_m2": 0.0,
                              "at": [t["x"], t["y"]], "geometry": None})
    return items


def build_items(found: list[dict[str, Any]], read: dict[str, Any], faces: dict[str, Any], others: list[dict[str, Any]] | None = None,
                texts: list[dict[str, Any]] | None = None, suffixes: list[str] | None = None) -> list[dict[str, Any]]:
    """Numbered items for a person: program findings (bookkeeping left out) + disagreements with other reads + unsure zones."""

    m = float((read.get("answer") or {}).get("unit_m") or 1.0)
    raw = [f for f in found if f["kind"] not in NOT_FOR_PEOPLE and f.get("at") is not None]
    raw += [d for d in disagreement_items(read, others or [], m) if d["kind"] != "reads_columns" and _is_designated(read, d.get("building"))]
    if not AREA_ONLY:
        apply_default_categories(read)
        raw += zone_items(read, faces)
        raw += zone_disagreement_items(read, faces, others or [])
        no_area = zone_no_area_items(read, texts or [], suffixes or [])
        raw += no_area
        raw += view_items(read, {i.get("view") for i in no_area})
    raw += [] if AREA_ONLY else height_items(read)  # trial (2026-10-10): height is not read
    raw += designated_review_items(read)
    raw = [f for f in raw if f["kind"] not in NOT_FOR_PEOPLE]
    seen_texts, unique = set(), []
    for f in raw:  # the same finding twice (e.g. a dimension drawn twice) is one mark
        key = (f["kind"], f.get("building"), f.get("text")) if f.get("text") else None
        if key is None or key not in seen_texts:
            unique.append(f)
            seen_texts.add(key)
    raw = unique
    raw.sort(key=lambda f: (f["kind"] != "view_count", -float(f.get("impact_m2") or 0), str(f.get("building")), f["kind"]))  # views first: they decide what else matters
    items = []
    for n, f in enumerate(raw, 1):
        items.append({"id": f"C{n:02d}", "kind": f["kind"], "building": f.get("building"), "zone": f.get("zone"),
                      "view": f.get("view"),
                      "question": QUESTIONS.get(f["kind"], f.get("text", "")) + (f"（{f['text']}）" if f["kind"] in {"view_count", "height"} else ""), "impact_m2": f.get("impact_m2", 0.0), "at": f["at"],
                      "geometry": f.get("geometry"), "program_text": f.get("text")})
    return items


def mark_entities(items: list[dict[str, Any]], m: float) -> list[dict[str, Any]]:
    """Drawing entities for the items: the number label (a person moves it to a decision layer) and the area in question."""

    out = []
    height = 2.0 / m
    block = height * 3 * 1.8  # three lines of label
    placed: list[tuple[float, float]] = []
    for it in items:
        what = f"「{it['building']}」" if it.get("building") else (f"「{it['zone']}」" if it.get("zone") else (f"「{it['view']}」" if it.get("view") else ""))
        text = f"{it['id']} {what}\\P{it['question']}\\P影响约 {float(it['impact_m2'] or 0):.0f} m2"
        x, y = it["at"]
        while any(abs(x - px) < 60 * height and abs(y - py) < block for px, py in placed):
            y -= block  # labels of different items never cover each other
        placed.append((x, y))
        out.append({"layer": LAYER_LABEL, "kind": "text", "at": [x, y], "text": text, "height": height, "record": {"item": it["id"], "role": "label"}})
        for poly in (it.get("geometry") or {}).get("polygons", []):
            rings = [poly["shell"]] + poly["holes"] if isinstance(poly, dict) else [poly]
            for ring in rings:
                out.append({"layer": LAYER_AREA, "kind": "outline", "points": ring, "width": 0.3 / m, "record": {"item": it["id"], "role": "area"}})
    return out


TRIAL_SCOPE = True  # 2026-10-07: only the designated building (and drawn-in buildings) and the named zones are reported
AREA_ONLY = True  # 2026-10-09 / 10-10 (target rev 22): only the designated building's total area; zones and rooms are not read
# what an overall "通过" keeps: the AI's own choice for each kind of mark
KEEP_AI = {"outline_beyond_grid": "yes", "outline_face_without_columns": "yes", "reads_extra": "yes",
           "outline_annex_left_out": "no", "reads_missing": "no"}


def zone_result(read: dict[str, Any], items: list[dict[str, Any]], decisions: dict[str, str],
                drawn: list[dict[str, Any]] | None = None, m: float = 1.0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every zone with its category, area and source after the person's choices, and per view the storage / operation totals."""

    apply_default_categories(read)  # the archive keeps the agent's own answer; the same defaults are applied again here
    answer = read.get("answer") or {}
    by_zone: dict[tuple, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        if it["kind"] == "zone_category":
            by_name.setdefault(it.get("zone"), []).append(it)
        elif it.get("zone") is not None:
            by_zone.setdefault((it.get("view"), it.get("zone")), []).append(it)
    zones = []
    for z in answer.get("zones") or []:
        key = (z.get("view"), z.get("name"))
        category, source, pending = z.get("category"), "AI 读出", False
        counted = True
        for it in by_zone.get(key, []) + by_name.get(z.get("name"), []):
            d = decisions[it["id"]]
            if it["kind"] == "zone_category":
                if d.startswith("category:"):
                    category, source = d.split(":", 1)[1], "人工确认"
                else:
                    pending = True
            elif it["kind"] in {"zone_reads_missing", "zone_reads_area"}:
                if d == "no":
                    counted, source = False, "人工确认"
                elif d == "yes":
                    source = "人工确认"
                else:
                    pending = True
        zones.append({"view": z.get("view"), "zone": z.get("name"), "category": category, "area_m2": z.get("area_m2"),
                      "counted": counted, "source": "待确认" if pending else source, "basis": z.get("basis")})
    from shapely.geometry import Point

    for it in items:
        if it["kind"] != "zone_no_area":
            continue
        d = decisions[it["id"]]
        if d == "no":
            continue
        shapes = [o for o in drawn or [] if _polygon(o["points"]).contains(Point(it["at"]))]
        if shapes:
            area = min(_polygon(o["points"]).area for o in shapes) * m * m
            category = default_category(it["zone"]) or "to_confirm"
            zones.append({"view": it.get("view"), "zone": it["zone"], "category": category, "area_m2": round(area, 2), "counted": True,
                          "source": "人工确认", "basis": "人工描出的边界；类别" + ("按默认口径" if category != "to_confirm" else "待确认")})
        else:
            zones.append({"view": it.get("view"), "zone": it["zone"], "category": default_category(it["zone"]) or "to_confirm", "area_m2": None,
                          "counted": True, "source": "待确认", "basis": "没有算出面积，等人描边界"})
    views = []
    view_decision = {it.get("view"): decisions[it["id"]] for it in items if it["kind"] == "view_count"}
    for v in sorted({z["view"] for z in zones}, key=str):
        mine = [z for z in zones if z["view"] == v and z["counted"]]
        d = view_decision.get(v, "pending")
        storage = sum(float(z["area_m2"] or 0) for z in mine if z["category"] == "storage")
        operation = sum(float(z["area_m2"] or 0) for z in mine if z["category"] == "operation")
        unsure = [z["zone"] for z in mine if z["source"] == "待确认" or z["category"] == "to_confirm"]
        views.append({"view": v, "counted": {"yes": "计入", "no": "不计入"}.get(d, "待确认"), "storage_m2": round(storage, 2), "operation_m2": round(operation, 2),
                      "storage_and_operation_m2": round(storage + operation, 2), "pending_zones": unsure,
                      "status": "待确认" if d not in {"yes", "no"} or unsure else "完成"})
    return zones, views


# ---------------------------------------------------------------- read back
def _decision(layer: str | None) -> str:
    if layer is None:
        return "deleted"
    if layer == LAYER_YES:
        return "yes"
    if layer == LAYER_NO:
        return "no"
    if layer in CATEGORY_LAYERS:
        return "category:" + CATEGORY_LAYERS[layer]
    return "pending"


def apply(read: dict[str, Any], items: list[dict[str, Any]], issued: dict[str, Any], returned: dict[str, Any], m: float,
          passed: bool = False) -> dict[str, Any]:
    """Apply what a person did in the drawing.

    ``issued`` is the snapshot written with the drawing: {"labels": {item id: handle}, "entities": {handle: layer}}.
    ``returned`` is what was read back: {"entities": {handle: layer}, "closed_outlines": [{"handle", "layer", "points"}],
    "open_outlines": [{"handle", "layer"}], "human_columns": [[x, y]], "columns": [[x, y]]} (columns: circles still on CTLE_COLUMN).
    ``passed``: the reviewer answered "通过" for the whole result page; every mark the reviewer left untouched keeps the
    AI's choice and every number on the page is labelled 人工复查通过 (target rev 22, rule 20).
    """

    from shapely.geometry import Point
    from shapely.ops import unary_union

    decisions = {}
    for it in items:
        handle = issued["labels"].get(it["id"])
        decisions[it["id"]] = _decision(returned["entities"].get(handle))
    if passed:
        for it in items:
            if decisions[it["id"]] == "pending":
                decisions[it["id"]] = KEEP_AI.get(it["kind"], "pass")
    problems: list[dict[str, Any]] = []
    for it in items:
        if decisions[it["id"]] == "deleted":
            problems.append({"item": it["id"], "problem": "编号被删除，没有作出选择：保持待确认"})
        if decisions[it["id"]].startswith("category:") and it["kind"] != "zone_category":
            problems.append({"item": it["id"], "problem": "类别图层只用于功能区编号：保持待确认"})
            decisions[it["id"]] = "pending"
    for o in returned.get("open_outlines", []):
        if o["layer"] == LAYER_HUMAN_OUTLINE:
            problems.append({"handle": o["handle"], "problem": "人工轮廓没有闭合：不采用，请闭合后再存"})

    buildings = []
    names = [c.get("name") for c in read.get("checks", []) if c.get("outline")]
    designated = {b.get("name") for b in (read.get("answer") or {}).get("buildings", []) if b.get("designated")}
    polys = {n: _polygon(_outline_of(read, n)) for n in names}
    human = [o for o in returned.get("closed_outlines", []) if o["layer"] == LAYER_HUMAN_OUTLINE]
    human_for: dict[str, list[Any]] = {}
    new_buildings = []
    for o in human:
        hp = _polygon(o["points"])
        hits = [n for n, p in polys.items() if p.intersection(hp).area >= OVERLAP_MIN * min(p.area, hp.area)]
        touched = [n for n, p in polys.items() if p.intersection(hp).area >= TOUCH_SHARE * p.area]
        if len(touched) > 1:
            problems.append({"handle": o["handle"], "problem": "人工轮廓同时盖住几栋楼（" + "、".join(touched) + "）：不采用，请分开描"})
        elif len(hits) == 1:
            human_for.setdefault(hits[0], []).append(hp)
        else:
            new_buildings.append(hp)

    for n in names:
        mine = [it for it in items if it.get("building") == n]
        pending = [it["id"] for it in mine if decisions[it["id"]] in {"pending", "deleted"}]
        shape = polys[n]
        source = "AI 读出"
        if n in human_for:
            shape = unary_union(human_for[n])
            outline_items = [it for it in mine if it["kind"].startswith(("outline", "reads", "dimension", "remeasure", "designated_outline"))]
            source = "人工确认" if outline_items else "人工修改（未标记处）"
            pending = [p for p in pending if p not in {it["id"] for it in outline_items}]  # a drawn outline answers every outline question
        else:
            for it in mine:
                d = decisions[it["id"]]
                pieces = [_polygon(p) for p in (it.get("geometry") or {}).get("polygons", [])]
                if it["kind"] in {"outline_beyond_grid", "outline_face_without_columns", "reads_extra"} and d in {"yes", "no"}:
                    if d == "no":
                        shape = shape.difference(unary_union(pieces)) if pieces else shape
                    source = "人工确认"
                elif it["kind"] in {"outline_annex_left_out", "reads_missing"} and d in {"yes", "no"}:
                    if d == "yes":
                        shape = unary_union([shape] + pieces)
                    source = "人工确认"
                elif it["kind"] == "outline_inner_line" and d == "yes":
                    problems.append({"item": it["id"], "problem": "选了要，但没有在「CTLE_人工轮廓」层描出外墙外边线：保持待确认"})
                    pending.append(it["id"])
                elif it["kind"] == "designated_outline_review" and d == "no":
                    problems.append({"item": it["id"], "problem": "选了不对，但没有在「CTLE_人工轮廓」层重描：保持待确认"})
                    pending.append(it["id"])
                elif it["kind"] in {"outline_inner_line", "dimension", "remeasure", "designated_outline_review"} and d in {"yes", "no"}:
                    source = "人工确认" if source == "AI 读出" else source
        shape = max(_parts(shape), key=lambda g: g.area) if _parts(shape) else shape
        area = shape.area * m * m
        rect = shape.minimum_rotated_rectangle
        corners = list(rect.exterior.coords) if rect.geom_type == "Polygon" else []
        if len(corners) < 3:
            problems.append({"building": n, "problem": "轮廓退化（面积为零或不是封闭图形），长宽无法计算：保持待确认"})
            pending.append("outline")
            buildings.append({"name": n, "designated": n in designated, "area_m2": round(area, 2), "outline_source": source,
                              "columns_source": None, "pending_items": sorted(set(pending)), "status": "待确认", "outline": []})
            continue
        sides = sorted([math.dist(corners[0], corners[1]), math.dist(corners[1], corners[2])])
        reach = shape.buffer(1.5 / m)
        kept = [q for q in returned.get("columns", []) if reach.contains(Point(q))]
        added = [q for q in returned.get("human_columns", []) if reach.contains(Point(q))]
        column_items = [it for it in mine if it["kind"] in {"columns", "reads_columns"}]
        issued_columns = sum(1 for h, layer in issued["entities"].items() if layer == COLUMN_LAYER and issued.get("column_building", {}).get(h) == n)
        columns_changed = bool(added) or (issued_columns and len(kept) != issued_columns)
        column_source = ("人工确认" if column_items else "人工修改（未标记处）") if columns_changed else "AI 读出"
        if column_items and not columns_changed and any(decisions[it["id"]] == "yes" for it in column_items):
            column_source = "人工确认"
        buildings.append({"name": n, "designated": n in designated, "area_m2": round(area, 2), "length_m": round(sides[1] * m, 2),
                          "width_m": round(sides[0] * m, 2), "columns": len(kept) + len(added), "outline_source": source,
                          "columns_source": column_source, "pending_items": sorted(set(pending)),
                          "status": "待确认" if pending else "完成", "outline": [[round(x, 3), round(y, 3)] for x, y in shape.exterior.coords]})
    for k, hp in enumerate(new_buildings, 1):
        buildings.append({"name": f"人工补描建筑{k}", "designated": False, "area_m2": round(hp.area * m * m, 2), "outline_source": "人工修改（未标记处）",
                          "columns_source": None, "pending_items": [], "status": "完成", "outline": [[round(x, 3), round(y, 3)] for x, y in hp.exterior.coords]})
    drawn_zones = [o for o in returned.get("closed_outlines", []) if o["layer"] == LAYER_HUMAN_ZONE]
    zones, views = ([], []) if AREA_ONLY else zone_result(read, items, decisions, drawn_zones, m)
    if AREA_ONLY:
        for b in buildings:
            if b["outline_source"] == "人工确认":
                changed = b["name"] in human_for or b["name"] not in polys or abs(b["area_m2"] - polys[b["name"]].area * m * m) > 0.001 * b["area_m2"]
                b["outline_source"] = "人工修改" if changed else "人工复查通过"
            elif b["outline_source"] == "AI 读出" and passed and not b["pending_items"]:
                b["outline_source"] = "人工复查通过"
    changed = [h for h, layer in issued["entities"].items() if returned["entities"].get(h) != layer]
    added_entities = [h for h in returned["entities"] if h not in issued["entities"]]
    unmarked = [b["name"] for b in buildings if "人工修改（未标记处）" in (b.get("outline_source"), b.get("columns_source"))]
    if TRIAL_SCOPE:
        buildings = [b for b in buildings if b["designated"] or b["name"].startswith("人工补描")]
    return {"buildings": sorted(buildings, key=lambda b: (not b["designated"], b["name"])), "zones": zones, "views": views, "decisions": decisions,
            "problems": problems, "pending_items": sorted(i for i, d in decisions.items() if d in {"pending", "deleted"}),
            "unmarked_edits": unmarked, "operations": len(changed) + len(added_entities),
            "operations_note": "以读回时发生变化（改图层、删除）或新增的图元数计"}


__all__ = ["build_items", "mark_entities", "apply", "disagreement_items", "zone_items", "CONFIRM_LAYERS"]
