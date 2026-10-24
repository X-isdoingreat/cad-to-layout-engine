"""One report per drawing for a decision maker: the warehouse (outline, size, area) and the equipment, picture beside words.

  py -3.11 -X utf8 -m scripts.drawing_report --drawing <name> [--area <trial_package out dir>] [--equipment <cli read run dir>] --out <pdf>

Pages (landscape, picture on the left, words on the right):
  1 仓库: the building outline over the drawing; total area, length, width, source and status.
  2 设备总览: the main plan with the equipment coloured by type; counts.
  3 one page per kind of warehouse equipment: where it is (only this type coloured), what it looks like, count and size.
  4 要人看的地方: each with a close-up: kinds the AI could not name, labels not used, copies, untyped candidates.
  5 附录: one tile per kind, numbered as in 设备复查表.csv.
Nothing here is reviewed yet unless the area result says so; the words say what is AI, what is program, what is open.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cad_to_layout.zero_manual.equipment import LABEL_ZH  # noqa: E402
from scripts.equipment_review import COLOURS, WAREHOUSE_TYPES, _block_of, _index, _load, _matplotlib, _tile, items_of, not_counted  # noqa: E402
from scripts.trial_package import wrap_keep  # noqa: E402

AI_SERVICE = "OpenAI" if os.environ.get("CTLE_AGENT", "claude").lower() == "codex" else "Anthropic"  # named in the report footer (claude and api both send to Anthropic)

PAGE = (11.69, 8.27)
UNIT = {"RACK": "格", "PALLET_CONV_CHAIN": "段", "PALLET_CONV_ROLLER": "段", "CONVEYOR_UNKNOWN_SUBTYPE": "段", "BOX_CONVEYOR": "段"}
NAME = {"RACK": "货架格位"}
TYPE_PAGES_MAX = 4  # type pages in the body; the rest is on the overview and in the appendix
TYPE_PAGE_MIN_SHARE = 0.03
ASK_MAX = 6  # questions put before a person in the body


SOURCE_ZH = {"AI 读出": "系统识别", "人工复查通过": "已人工核对", "人工修改": "人工修正", "人工修改（未标记处）": "人工修正", "人工确认": "已人工核对"}
STATUS_ZH = {"待确认": "待核对", "完成": "已完成"}


def confidence(votes: str) -> str:
    """'3/3' -> 高, '2/3' -> 中, otherwise 低."""

    try:
        n, total = (int(v) for v in votes.split("/"))
    except ValueError:
        return "低"
    return "高" if n == total else ("中" if n * 2 > total else "低")


def label(t: str) -> str:
    return NAME.get(t, LABEL_ZH.get(t, t))


def unit(t: str) -> str:
    return UNIT.get(t, "台")
PICTURE = [0.02, 0.05, 0.63, 0.86]


def _words(figure: Any, title: str, lines: list[tuple[str, float, str]], top: float = 0.9) -> None:
    """The right-hand column: a title and short lines (text, size, weight)."""

    figure.text(0.02, 0.945, title, fontsize=17, weight="bold")
    y = top
    for text, size, weight in lines:
        if not text:
            y -= 0.02
            continue
        for k, piece in enumerate(wrap_keep(text, max(12, int(250 / size)))):
            figure.text(0.68 if k == 0 else 0.69, y, piece, fontsize=size, weight=weight)
            y -= 0.028 if size >= 12 else 0.024


def _boxes(axis: Any, records: list[dict[str, Any]], colour_of: Any, alpha: float = 0.5) -> None:
    from matplotlib import patches

    for r in records:
        L, W, a = r["length_mm"], r["width_mm"], math.radians(r.get("rotation_deg") or 0)
        cx, cy = r["centre"]
        corners = [(cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a))
                   for dx, dy in ((-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2))]
        c = colour_of(r)
        axis.add_patch(patches.Polygon(corners, closed=True, edgecolor=c, facecolor=c, alpha=alpha, linewidth=0.5))


def _lines(axis: Any, index: Any, window: list[float]) -> None:
    for ln in index.select(window, limit=150000):
        p = ln["p"]
        if len(p) >= 2:
            axis.plot([q[0] for q in p], [q[1] for q in p], color="#c4c8ce", linewidth=0.2)
    axis.set_xlim(window[0], window[2])
    axis.set_ylim(window[1], window[3])
    axis.set_aspect("equal")
    axis.axis("off")


def _ring(axis: Any, box: list[float], colour: str = "#f59e0b") -> None:
    from matplotlib import patches

    pad = max(box[2] - box[0], box[3] - box[1]) * 0.15 + 1
    axis.add_patch(patches.Rectangle((box[0] - pad, box[1] - pad), box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad,
                                     edgecolor=colour, facecolor="none", linewidth=2.5))


def area_page(pdf: Any, pyplot: Any, area: Path) -> list[float] | None:
    from matplotlib import image as mpimg

    issue = area / "issue"
    result = json.loads((issue / "ai_result.json").read_text(encoding="utf-8"))
    final = area / "result" / "final.json"
    reviewed = final.is_file()
    if reviewed:
        result = json.loads(final.read_text(encoding="utf-8"))
    items = json.loads((issue / "items.json").read_text(encoding="utf-8"))
    read = json.loads((issue / "ai_result_archive.json").read_text(encoding="utf-8"))
    from scripts.trial_package import other_floors

    figure = pyplot.figure(figsize=PAGE)
    picture = (area / "result" / "b_overlay.png") if reviewed and (area / "result" / "b_overlay.png").is_file() else issue / "b_overlay.png"
    axis = figure.add_axes(PICTURE)
    if picture.is_file():
        axis.imshow(mpimg.imread(picture))
    axis.axis("off")
    b = next((x for x in result["buildings"] if x.get("designated")), None)
    if b is None:
        _words(figure, "一、厂房尺寸", [("未能识别指定的厂房。", 13, "bold")])
    else:
        src = next((x for x in (read.get("answer") or {}).get("buildings", []) if x.get("name") == b["name"]), {})
        floors = other_floors(read.get("answer") or {}, src)
        other = [n for n in (b.get("undecided_numbers") or []) if n.get("source") != b.get("source")]
        risks = [r for r in (b.get("risk_review") or []) if isinstance(r, dict) and float(r.get("if_wrong_effect_m2") or 0) > 0][:3]
        state = ([(f"状态：{b['status_zh']}；采用：{b.get('source_zh') or ''}；人工复查：可选", 10.5, "normal")] if b.get("status_code") else
                 [(f"识别方式：{SOURCE_ZH.get(b['outline_source'], b['outline_source'])}；状态：{STATUS_ZH.get(b['status'], b['status'])}", 10.5, "normal")])
        _words(figure, "一、厂房尺寸", [
            (b["name"][:40], 12, "bold"), ("", 0, ""),
            (f"总面积 {b['area_m2']:,.0f} 平方米", 16, "bold"),
            (f"长 {b.get('length_m', 0):.1f} 米 × 宽 {b.get('width_m', 0):.1f} 米", 14, "normal"),
            *[(f"另一种方法（{n.get('source_zh')}）：{float(n.get('area_m2') or 0):,.0f} 平方米，长 {float(n.get('length_m') or 0):.1f} × 宽 {float(n.get('width_m') or 0):.1f} 米", 11, "normal") for n in other],
            *([(f"差在：{str(b.get('difference_region'))[:70]}", 10, "normal")] if other and b.get("difference_region") else []),
            ("", 0, ""),
            ("图中红线为系统识别的厂房外墙轮廓，叠加在客户原图上。", 10.5, "normal"),
            *state,
            *([(f"复查说明：{str(b.get('why'))[:70]}", 9.5, "normal")] if b.get("why") else []),
            *([(f"可能错在：{str(r.get('id'))} {str(r.get('what_seen'))[:40]}（若错约 {float(r.get('if_wrong_effect_m2') or 0):,.0f} m²）", 9, "normal") for r in risks]),
            ("净高：需客户提供。", 10.5, "normal"),
            *([(f"图纸另有其他楼层，未计入：{'、'.join(floors)[:60]}", 10.5, "normal")] if floors else []),
            ("", 0, ""), ("计算口径：按外墙外边线围成的占地面积；多层建筑只计指定楼层（默认首层）。", 9, "normal")])
    pdf.savefig(figure)
    pyplot.close(figure)
    if b and b.get("outline"):
        xs = [q[0] for q in b["outline"]]
        ys = [q[1] for q in b["outline"]]
        return [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]
    return None


def equipment_pages(pdf: Any, pyplot: Any, run: Path, building_at: list[float] | None = None, appendix: Any = None,
                    summary: dict[str, Any] | None = None) -> None:
    import ezdxf
    from matplotlib import patches

    summary = summary if summary is not None else {}
    equipment, views, inventory = _load(run)
    items = items_of(equipment, views)
    skipped = not_counted(equipment, views)
    title = {k: " ".join(str((v.get("title") or {}).get("text") or f"平面图（{n}，图上无标题）").split()) for n, (k, v) in enumerate(views.items(), 1)}
    counted = [r for r in equipment["records"] if r["view_id"] not in skipped]
    per_view = Counter(r["view_id"] for r in counted)
    if not per_view:
        figure = pyplot.figure(figsize=PAGE)
        _words(figure, "二、设备清单", [("本图没有按设备图块识别到仓储设备。", 13, "bold"),
                                    ("可能图纸中没有设备，或设备以散线、零件拼画；这类画法按重复图形统计（加 --repeat 时见下一页）。", 10.5, "normal")])
        pdf.savefig(figure)
        pyplot.close(figure)
        return
    review = run / "review"
    review.mkdir(exist_ok=True)
    index = _index(run, review)
    doc = ezdxf.readfile(run / "convert/source.dxf")
    main = per_view.most_common(1)[0][0]
    if building_at:  # the plan holding the building of page one, so both pages show the same floor
        holding = [v for v in per_view if (lambda w: w and w[0] <= building_at[0] <= w[2] and w[1] <= building_at[1] <= w[3])(views[v].get("window"))]
        candidate = max(holding, key=lambda v: per_view[v]) if holding else main
        # keep the building's floor only when most of the equipment is drawn there (schemes are often drawn elsewhere)
        main = candidate if per_view[candidate] >= 0.5 * per_view[main] else main
    main_records = [r for r in counted if r["view_id"] == main]
    xs = [r["centre"][0] for r in main_records]
    ys = [r["centre"][1] for r in main_records]
    pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.06 + 3000
    window = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]  # zoomed to the equipment, not the whole sheet
    totals = Counter(r["type"] for r in counted)
    summary.update({"floor": title[main], "counts": Counter(r["type"] for r in main_records), "kinds": len(items),
                    "by_text": sum(1 for r in main_records if r["basis"] == "text"), "records": len(main_records)})

    # overview
    figure = pyplot.figure(figsize=PAGE)
    axis = figure.add_axes(PICTURE)
    _lines(axis, index, window)
    _boxes(axis, main_records, lambda r: COLOURS.get(r["type"], "#6b7280"))
    main_counts = Counter(r["type"] for r in main_records)
    lines = [(f"{title[main]}：彩色为系统识别出的设备，灰色为原图，未上色处为未识别。", 10.5, "normal"), ("", 0, "")]
    for t in WAREHOUSE_TYPES + ["OTHER_EQUIPMENT"]:
        if main_counts.get(t):
            lines.append((f"■ {label(t)}　{main_counts[t]:,} {unit(t)}" + ("（可能含生产设备）" if t == "OTHER_EQUIPMENT" else ""), 12, "bold"))
    other_views = [v for v in per_view if v != main]
    if other_views:
        lines += [("", 0, ""), ("其他平面图：", 10.5, "bold")]
        for v in sorted(other_views, key=lambda v: -per_view[v])[:6]:
            c = Counter(r["type"] for r in counted if r["view_id"] == v)
            lines.append((f"· {title[v][:16]}：" + "、".join(f"{label(t)} {n:,}" for t, n in c.most_common(3)), 9.5, "normal"))
        if len(other_views) > 6:
            lines.append((f"· 另有 {len(other_views) - 6} 张，见附录", 9.5, "normal"))
    lines += [("", 0, ""), ("数量由系统直接从图纸统计；剖面图、立面图及重复绘制的图纸不计入。", 9, "normal")]
    _words(figure, "二、设备清单", lines)
    pdf.savefig(figure)
    pyplot.close(figure)

    # one page per warehouse type on the main plan
    shown = [t for t in sorted(WAREHOUSE_TYPES, key=lambda t: -main_counts.get(t, 0)) if main_counts.get(t, 0) >= TYPE_PAGE_MIN_SHARE * len(main_records)][:TYPE_PAGES_MAX]
    for t in shown:
        figure = pyplot.figure(figsize=PAGE)
        axis = figure.add_axes(PICTURE)
        _lines(axis, index, window)
        _boxes(axis, [r for r in main_records if r["type"] != t], lambda r: "#d1d5db", 0.35)
        _boxes(axis, [r for r in main_records if r["type"] == t], lambda r: COLOURS.get(t, "#6b7280"), 0.65)
        kinds = [it for it in items if it["type"] == t]
        sample = kinds[0] if kinds else None
        if sample and sample["drawn_as"] == "block":
            inset = figure.add_axes([0.70, 0.08, 0.26, 0.30])
            _tile(inset, doc, _block_of(inventory, equipment, sample["family"], sample["sample_ref"]))
            figure.text(0.70, 0.39, f"长这样（{sample['id']}，{sample['size_mm'][0]}×{sample['size_mm'][1]} mm）：", fontsize=10)
        basis = Counter("文字" if k["basis"] == "图上文字" else "形状" for k in kinds)
        _words(figure, f"{label(t)}", [
            (f"{title[main]}：{main_counts[t]:,} {unit(t)}", 15, "bold"),
            (f"全部平面图：{totals[t]:,} {unit(t)}", 12, "normal"), ("", 0, ""),
            ("图中彩色为该类设备，浅灰为其他设备。", 10.5, "normal"),
            (f"共 {len(kinds)} 种样式（附录编号 {'、'.join(k['id'] for k in kinds[:8])}{'…' if len(kinds) > 8 else ''}）。", 10.5, "normal"),
            (f"识别方式：{'文字识别 ' + str(basis['文字']) + ' 种' if basis['文字'] else ''}{'；' if basis['文字'] and basis['形状'] else ''}{'形状识别 ' + str(basis['形状']) + ' 种' if basis['形状'] else ''}。", 10.5, "normal")])
        pdf.savefig(figure)
        pyplot.close(figure)

    # places for a person: kinds left open, looked at once more by the AI; only real questions go before a person
    from matplotlib import image as mpimg

    from scripts.equipment_review import SECOND_LOOK_MIN_MM, second_look

    looks = second_look(run, items, inventory, doc, index)
    open_items = [it for it in items if it["type"] == "UNRECOGNISED"]
    small = [it for it in open_items if max(it["size_mm"]) < SECOND_LOOK_MIN_MM]
    asked = [it for it in open_items if it["id"] in looks and looks[it["id"]]["type"] not in {"NOT_EQUIPMENT", "COMPONENT"}]
    asked.sort(key=lambda it: -it["count"] * max(it["size_mm"]))
    not_equipment = [it for it in open_items if it["id"] in looks and looks[it["id"]]["type"] in {"NOT_EQUIPMENT", "COMPONENT"}]
    summary["open"] = {"asked": len(asked[:ASK_MAX]), "small": len(small), "not_equipment": len(not_equipment), "more": max(0, len(asked) - ASK_MAX)}
    if asked:
        figure, axes = pyplot.subplots(2, 3, figsize=PAGE)
        figure.text(0.02, 0.955, "三、请您确认：以下设备的识别结果是否正确", fontsize=16, weight="bold")
        for axis, it in zip(axes.flat, asked[:ASK_MAX]):
            look = looks[it["id"]]
            picture = run / "review" / "second_look" / f"{it['id']}.png"
            if picture.is_file():
                axis.imshow(mpimg.imread(picture))
            axis.axis("off")
            guess = "无法判断" if look["type"] == "UNSURE" else (look["name"] or LABEL_ZH.get(look["type"], look["type"]))
            summary.setdefault("questions", []).append(it["id"])
            axis.set_title(f"确认 {it['id']}（共 {it['count']} 处，{it['size_mm'][0]}×{it['size_mm'][1]} mm）" + "\n"
                           + (f"系统识别为：{guess}（把握：{confidence(look['votes'])}）　是否正确？" if look["type"] != "UNSURE" else "系统无法判断，请告知是什么设备"),
                           fontsize=10, loc="left")
        for axis in list(axes.flat)[len(asked[:ASK_MAX]):]:
            axis.axis("off")
        figure.text(0.02, 0.02, "每格左侧为设备样式，右侧红框为其在图纸中的一处位置。"
                    + (f"另有 {len(asked) - ASK_MAX} 项见附录。" if len(asked) > ASK_MAX else ""), fontsize=10)
        figure.tight_layout(rect=(0, 0.04, 1, 0.93))
        pdf.savefig(figure)
        pyplot.close(figure)
    texts = {t["ref"]: t for t in inventory["texts"]}
    seen_texts = set()
    for w in equipment.get("labels_not_used_for_type") or []:
        key_text = " ".join(w["texts"][0].split())
        if key_text in seen_texts:
            continue
        seen_texts.add(key_text)
        zone = next((z for z in equipment.get("zone_labels") or [] if z["key"] == w["key"] and z["text_ref"] in texts), None)
        if not zone:
            continue
        x, y = texts[zone["text_ref"]]["xy"]
        h = 25000.0
        win = [x - h, y - h * 0.7, x + h, y + h * 0.7]
        figure = pyplot.figure(figsize=PAGE)
        axis = figure.add_axes(PICTURE)
        _lines(axis, index, win)
        members = [r for r in equipment["records"] if r["family"] == w["key"]]
        _boxes(axis, members, lambda r: "#7c3aed", 0.5)
        _ring(axis, [x - 1500, y - 800, x + 1500, y + 800])
        axis.text(x, y, " ".join(texts[zone["text_ref"]]["text"].split()), fontsize=11, color="#b45309", weight="bold")
        if not members:
            pyplot.close(figure)
            continue
        kind_type = LABEL_ZH.get(members[0]["type"], members[0]["type"])
        item_id = next((it["id"] for it in items if it["family"] == w["key"]), "")
        summary.setdefault("questions", []).append(item_id)
        _words(figure, "三、请您确认：图纸文字与识别结果不一致", [
            (f"确认 {item_id}", 13, "bold"),
            (f"图纸标注：「{key_text}」（橙框）", 12, "normal"),
            (f"系统识别：{kind_type}（紫色，共 {len(members)} 处）", 12, "normal"), ("", 0, ""),
            ("该标注只覆盖其中一部分" + ("，且为区域名称" if w["area_name"] else "") + "，系统按形状识别。", 10.5, "normal"),
            ("请确认紫色设备是什么。", 10.5, "bold")])
        pdf.savefig(figure)
        pyplot.close(figure)
    untyped = [c for c in equipment.get("candidates_untyped") or [] if c["view_id"] == main]
    if untyped:
        figure = pyplot.figure(figsize=PAGE)
        axis = figure.add_axes(PICTURE)
        _lines(axis, index, window)
        _boxes(axis, main_records, lambda r: "#d1d5db", 0.3)
        for c in untyped:
            b = c["bbox"]
            axis.add_patch(patches.Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], edgecolor="#f59e0b", facecolor="#fde68a", alpha=0.8, linewidth=0.4))
        summary.setdefault("questions", []).append("黄色方框")
        _words(figure, "三、请您确认：未识别的方框", [(f"黄色方框 {len(untyped):,} 处", 13, "bold"),
                                                ("疑似设备位置，图纸未注明类型，系统未识别，也未计入数量。", 10.5, "normal"),
                                                ("请确认：是货架格位、托盘位，还是其他？", 10.5, "bold")])
        pdf.savefig(figure)
        pyplot.close(figure)

    # appendix (a separate file)
    pdf = appendix if appendix is not None else pdf
    for start in range(0, len(items), 12):
        figure, axes = pyplot.subplots(3, 4, figsize=PAGE)
        figure.text(0.02, 0.955, "附录：设备样式明细", fontsize=14, weight="bold")
        for axis, it in zip(axes.flat, items[start:start + 12]):
            _tile(axis, doc, _block_of(inventory, equipment, it["family"], it["sample_ref"]) if it["drawn_as"] == "block" else None)
            axis.set_title(f"{it['id']} {it['type_zh'] if it['type'] != 'UNRECOGNISED' else '未识别'}{('：' + it['name'][:10]) if it['name'] else ''}\n{it['count']:,} {unit(it['type'])}　{it['size_mm'][0]}×{it['size_mm'][1]} mm\n"
                           + ("文字识别" if it["basis"] == "图上文字" else ("形状识别" if it["type"] != "UNRECOGNISED" else "未识别")),
                           fontsize=8, loc="left")
        for axis in list(axes.flat)[len(items[start:start + 12]):]:
            axis.axis("off")
        figure.tight_layout(rect=(0, 0, 1, 0.93))
        pdf.savefig(figure)
        pyplot.close(figure)
    index.con.close()


def repeat_pages(pdf: Any, pyplot: Any, run: Path, summary: dict[str, Any]) -> None:
    """One page per plan where the program counted repeated linework (equipment drawn as loose lines or parts): the
    places coloured per group, the program's example in a red box, and the questions."""

    from matplotlib import image as mpimg
    from matplotlib import patches

    from scripts.equipment_review import REPEAT_COLOURS, repeats

    found = repeats(run)
    if not found["views"]:
        return
    _, views, _ = _load(run)
    title = {k: " ".join(str((v.get("title") or {}).get("text") or f"平面图（{n}，图上无标题）").split()) for n, (k, v) in enumerate(views.items(), 1)}
    index = _index(run, run / "review")
    # what the AI calls "not equipment" is not hidden (the AI can be wrong: a rack cell straddling two rows looked like
    # steel beams to it); those groups go together on one page for a glance, uncounted unless the person names them
    shown = [v for v in found["views"] if (v["groups"][0].get("suggestion") or {}).get("type") != "NOT_EQUIPMENT"]
    doubted = [v for v in found["views"] if v not in shown]
    for view in shown:
        groups = view["groups"]
        cells = [c for g in groups for c in g["places"]]
        pad = max(max(c[2] for c in cells) - min(c[0] for c in cells), max(c[3] for c in cells) - min(c[1] for c in cells)) * 0.05 + 2000
        window = [min(c[0] for c in cells) - pad, min(c[1] for c in cells) - pad, max(c[2] for c in cells) + pad, max(c[3] for c in cells) + pad]
        figure = pyplot.figure(figsize=PAGE)
        axis = figure.add_axes(PICTURE)
        _lines(axis, index, window)
        for g, colour in zip(groups, REPEAT_COLOURS):
            for c in g["places"]:
                axis.add_patch(patches.Rectangle((c[0], c[1]), c[2] - c[0], c[3] - c[1], edgecolor=colour, facecolor=colour, alpha=0.45, linewidth=0.3))
            e = g["example"]
            axis.add_patch(patches.Rectangle((e[0], e[1]), e[2] - e[0], e[3] - e[1], edgecolor="#111827", facecolor="none", linewidth=2.2))
            axis.annotate(g["id"], ((e[0] + e[2]) / 2, e[3]), xytext=(0, 14), textcoords="offset points", ha="center", fontsize=11, weight="bold",
                          color=colour, arrowprops={"arrowstyle": "-", "color": colour})
        first = groups[0]
        s = first.get("suggestion") or {}
        guess = "无法判断" if s.get("type") in (None, "UNSURE") else ((s.get("name") or label(s["type"])) if s["type"] == "OTHER_EQUIPMENT" else label(s["type"]))
        u = unit(s.get("type", "")) if s.get("type") not in (None, "UNSURE") else "处"
        e = first["example"]
        each = f"每个红框算几{u}" if u != "处" else "每个红框算几台"
        lines = [(f"{first['id']}　系统在图上找到 {len(first['places']):,} 处相同图形（红色）", 12, "bold"),
                 (f"系统识别为：{guess}" + (f"（把握：{confidence(s['votes'])}）" if s.get("votes") else "") + f"，是否正确？{each}？", 11, "normal"),
                 (f"样例为黑框处，{round(e[2] - e[0]):,} × {round(e[3] - e[1]):,} mm，见右下小图红框。一个框可能跨两排背靠背的货架，请按小图数。", 10, "normal"),
                 ("", 0, "")]
        for g in groups[1:]:
            lines.append((f"{g['id']}　另有 {len(g['places']):,} 处相似图形（{['', '橙色', '紫色', '青色'][groups.index(g)]}）：也是{guess}吗？", 11, "bold"))
        lines += [("", 0, ""), ("这类设备没有单独的图块（散线或零件拼画），数量由系统按重复图形统计：先自己挑一个样例，再在全图找相同的（含镜像）。"
                                "后面的编号是系统拿不准的，未计入数量。", 9.5, "normal")]
        _words(figure, f"二、设备清单：按重复图形计数（{title.get(view['view_id'], '')[:14]}）", lines)
        picture = run / "review" / "repeat" / f"{first['id']}.png"
        if picture.is_file():
            inset = figure.add_axes([0.68, 0.05, 0.30, 0.30])
            inset.imshow(mpimg.imread(picture))
            inset.axis("off")
        pdf.savefig(figure)
        pyplot.close(figure)
        summary.setdefault("repeat", []).append({"floor": title.get(view["view_id"], ""), "id": first["id"], "count": len(first["places"]),
                                                 "guess": guess, "unit": u, "questions": [g["id"] for g in groups[1:]]})
        summary.setdefault("questions", []).extend(g["id"] for g in groups)
    if doubted:
        figure, axes = pyplot.subplots(2, 3, figsize=PAGE)
        figure.text(0.02, 0.955, "二、设备清单：系统认为不是设备的重复图形（请看一眼）", fontsize=16, weight="bold")
        for axis, view in zip(axes.flat, doubted[:6]):
            first = view["groups"][0]
            picture = run / "review" / "repeat" / f"{first['id']}.png"
            if picture.is_file():
                axis.imshow(mpimg.imread(picture))
            axis.axis("off")
            what = (first.get("suggestion") or {}).get("name") or "非设备"
            axis.set_title(f"{first['id']}　{title.get(view['view_id'], '')[:12]}：{len(first['places']):,} 处\n系统认为：{what[:14]}（不是设备）", fontsize=10, loc="left")
        for axis in list(axes.flat)[len(doubted[:6]):]:
            axis.axis("off")
        figure.text(0.02, 0.02, "这些未计入数量。如果其中有设备，请回复编号、类型和每处几格，例如“R1 是货架，每处 2 格”。每格左为样例，右为它在原图中的位置（红框）。",
                    fontsize=10)
        figure.tight_layout(rect=(0, 0.05, 1, 0.93))
        pdf.savefig(figure)
        pyplot.close(figure)
        summary["repeat_doubted"] = [v["groups"][0]["id"] for v in doubted]
        summary.setdefault("questions", []).extend(summary["repeat_doubted"])
    index.con.close()


def conclusion(pyplot: Any, drawing: str, area: dict[str, Any] | None, equipment: dict[str, Any]) -> Any:
    figure = pyplot.figure(figsize=PAGE)
    figure.text(0.06, 0.88, "图纸识别报告", fontsize=28, weight="bold")
    figure.text(0.06, 0.83, drawing, fontsize=13, color="#4b5563")
    y = 0.72
    figure.text(0.06, y, "摘要", fontsize=18, weight="bold")
    y -= 0.07
    rows = []
    if area:
        rows.append(("厂房", f"{area['name'][:20]}：总面积 {area['area_m2']:,.0f} 平方米，长 {area.get('length_m', 0):.1f} × 宽 {area.get('width_m', 0):.1f} 米"
                     + (f"；状态：{area['status_zh']}" if area.get("status_zh") else "")))
    if equipment.get("counts"):
        main = "、".join(f"{label(t)} {n:,} {unit(t)}" for t, n in equipment["counts"].most_common())[:80]
        rows.append(("设备", f"{equipment['floor']}：{main}（按设备图块统计）"
                     + ("。本图还有按重复图形统计的结果（下面“重复图形”一行）：散线画的设备按图块读不准，两者不一致时以重复图形和人工确认为准。"
                        if equipment.get("repeat") or equipment.get("repeat_doubted") else "")))
        share = equipment["by_text"] / max(equipment["records"], 1)
        o = equipment.get("open") or {}
        questions = len(equipment.get("questions") or [])
        rows.append(("识别可靠性", f"设备位置和数量由系统直接从图纸统计；" + (f"{share:.0%} 的设备按图纸文字识别，其余按形状识别。" if share > 0 else "类型全部按形状识别（图纸上没有可用的设备名称）。")
                                 + (f"有 {questions} 处需要您确认（第三部分）。" if questions else "")))
    elif equipment and not equipment.get("repeat"):
        rows.append(("设备", "本图未识别到仓储设备。"))
    if equipment.get("repeat_doubted"):
        rows.append(("请看一眼", f"另有 {len(equipment['repeat_doubted'])} 组重复图形系统认为不是设备（{'、'.join(equipment['repeat_doubted'])}），未计入；如有设备请指出。"))
    for r in equipment.get("repeat") or []:
        rows.append(("重复图形", f"{r['floor'][:14]}：{r['guess']} {r['count']:,} 处（{r['id']}，系统按重复图形统计，每处算几{r['unit'] if r['unit'] != '处' else '台'}待您确认）"
                     + (f"；另有 {len(r['questions'])} 组拿不准（{'、'.join(r['questions'])}），未计入" if r["questions"] else "")))
    rows.append(("状态", "厂房面积由两种独立方法读出并经复查 AI 核对（状态见上）；设备为系统识别结果。人工复查为可选步骤。" if area and area.get("status_code")
                 else "以上为系统识别结果，尚未经人工核对。"))
    if equipment.get("questions"):
        q = [x for x in equipment["questions"] if x.startswith("E")]
        rq = [x for x in equipment["questions"] if x.startswith("R")]
        example = ("面积正确。" if area else "") + (f"{q[0]} 正确，" if q else "") + (f"{q[1]} 是货架，" if len(q) > 1 else "")             + (f"{rq[0]} 对、每处 2 格，" if rq else "") + (f"{rq[1]} 是，" if len(rq) > 1 else "") + (f"{rq[2]} 不是，" if len(rq) > 2 else "") + "其余正确；左下角一排输送机未识别。"
        rows.append(("如何回复", f"直接用一句话回复编号和判断即可，例如：“{example}”"))
    lines = sum(len(wrap_keep(text, 46)) for _, text in rows)
    size, width, step = (14, 46, 0.045) if lines <= 9 else (12, 56, 0.037)  # more rows: smaller, so the page holds them
    for head, text in rows:
        figure.text(0.06, y, head, fontsize=size, weight="bold", color="#1f2937")
        for k, piece in enumerate(wrap_keep(text, width)):
            figure.text(0.18, y - k * step, piece, fontsize=size)
        y -= step * max(1, len(wrap_keep(text, width))) + 0.025
    figure.text(0.06, 0.06, f"识别时图纸内容会发送至 {AI_SERVICE} 的 AI 服务处理，请事先征得图纸所有者同意。设备样式明细见同名附录文件；需要放大核对时，请打开同名的核对用 DWG。", fontsize=10, color="#4b5563")
    return figure


def main(argv: list[str] | None = None) -> int:
    from matplotlib.backends.backend_pdf import PdfPages

    parser = argparse.ArgumentParser()
    parser.add_argument("--drawing", required=True)
    parser.add_argument("--area")
    parser.add_argument("--equipment")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeat", action="store_true", help="also count equipment drawn as loose lines or parts by repeated linework (slow on large plans)")
    args = parser.parse_args(argv)
    pyplot = _matplotlib()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = out.with_name(out.stem + "_body.pdf")
    summary: dict[str, Any] = {}
    area_info = None
    with PdfPages(body) as pdf, PdfPages(out.with_name(out.stem + "_附录.pdf")) as appendix:
        building_at = area_page(pdf, pyplot, Path(args.area)) if args.area else None
        if args.area:
            result = json.loads((Path(args.area) / "issue" / "ai_result.json").read_text(encoding="utf-8"))
            area_info = next((b for b in result["buildings"] if b.get("designated")), None)
        if args.equipment:
            equipment_pages(pdf, pyplot, Path(args.equipment), building_at, appendix, summary)
            if args.repeat:
                repeat_pages(pdf, pyplot, Path(args.equipment), summary)
    import fitz

    front = out.with_name(out.stem + "_front.pdf")
    with PdfPages(front) as pdf:
        figure = conclusion(pyplot, args.drawing, area_info, summary if args.equipment else {})
        pdf.savefig(figure)
        pyplot.close(figure)
    merged, rest = fitz.open(front), fitz.open(body)
    merged.insert_pdf(rest)
    merged.save(out)
    rest.close()
    merged.close()
    front.unlink()
    body.unlink()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
