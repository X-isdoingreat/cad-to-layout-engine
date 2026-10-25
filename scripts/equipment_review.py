"""Equipment review for a person: check the types per kind, find what is not coloured, never recount.

  make:   py -3.11 -X utf8 scripts/equipment_review.py make --run <dir of `cli read`>
          writes <run>/review/: 设备复查.pdf (counts per view; one tile per kind of equipment with its type, basis,
          count and size; per plan view the equipment coloured over the drawing; what does not add up) and
          设备复查表.csv (the reviewer fills 复查: 对 / 不是设备 / the right type).
  apply:  py -3.11 -X utf8 scripts/equipment_review.py apply --run <dir> --table <filled csv> [--reviewer NAME --minutes N]
          writes <run>/review/设备结果（复查后）.md and .csv: counts recomputed from the reviewed types.

Counts are never typed in by a person: a kind's count is how often the program found it in the drawing, so a
reviewer checks kinds (a hundred or so) and looks for equipment left uncoloured, instead of counting machines.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cad_to_layout.zero_manual.equipment import LABEL_ZH  # noqa: E402

# warehouse equipment types; everything else is listed apart (it may be production equipment, which is not read)
WAREHOUSE_TYPES = ["RACK", "STACKER", "PALLET_CONV_CHAIN", "PALLET_CONV_ROLLER", "CONVEYOR_UNKNOWN_SUBTYPE", "BOX_CONVEYOR", "BOX_LIFT", "ROBOT_ARM"]
ZH_TO_TYPE = {v: k for k, v in LABEL_ZH.items()}
COLOURS = {"RACK": "#ef4444", "STACKER": "#111827", "PALLET_CONV_CHAIN": "#7c3aed", "PALLET_CONV_ROLLER": "#2563eb",
           "CONVEYOR_UNKNOWN_SUBTYPE": "#0891b2", "BOX_CONVEYOR": "#16a34a", "BOX_LIFT": "#d97706", "ROBOT_ARM": "#db2777",
           "OTHER_EQUIPMENT": "#6b7280"}
UNRECOGNISED_MIN_OCCURRENCES = 5  # kinds the program could not type are shown to the reviewer when they occur at least this often
TILES_PER_PAGE = 12


def _matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot

    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    return pyplot


def _load(run: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    equipment = json.loads((run / "equipment.json").read_text(encoding="utf-8"))
    views = json.loads((run / "views.json").read_text(encoding="utf-8"))["views"]
    inventory = json.loads((run / "inventory.json").read_text(encoding="utf-8"))
    return equipment, {v["view_id"]: v for v in views}, inventory


NOT_A_PLAN = ("剖面", "立面", "详图", "大样", "section", "elevation", "detail")


def not_counted(equipment: dict[str, Any], views: dict[str, Any]) -> set[str]:
    """Views whose equipment is not counted: a copy of another view, or a section / elevation / detail (equipment is counted on plans)."""

    out = {c["view_id"] for c in equipment.get("duplicate_views") or []}
    for view_id, v in views.items():
        text = (str((v.get("title") or {}).get("text") or "") + " " + str(v.get("role") or "")).lower()
        if any(w in text for w in NOT_A_PLAN):
            out.add(view_id)
    return out


def items_of(equipment: dict[str, Any], views: dict[str, Any]) -> list[dict[str, Any]]:
    """One review item per kind (family) the program counted, plus kinds it could not type that occur often."""

    copies = not_counted(equipment, views)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in equipment["records"]:
        by_family[r.get("row_kind") or r["family"]].append(r)  # rows of one kind drawn by several blocks: one item, under the block asked
    items = []
    for family, records in by_family.items():
        first = next((r for r in records if r["family"] == family), records[0])
        sizes = sorted((r["length_mm"], r["width_mm"]) for r in records)
        per_view = Counter(r["view_id"] for r in records)
        decision = (equipment["families"].get(family) or {}).get("decision") or {}
        votes = decision.get("votes") or []
        cells = sum(r.get("cells") or 0 for r in records if r["view_id"] not in copies)  # a row is one record; its cells are the second level
        items.append({"family": family, "type": first["type"], "type_zh": first["type_zh"], "name": first.get("name") or "",
                      "basis": "图上文字" if first["basis"] == "text" else (f"AI 看图（{Counter(votes).most_common(1)[0][1]}/{len(votes)} 次一致）" if votes else "AI 看图"),
                      "wrong_if": decision.get("wrong_if") or "",
                      "drawn_as": first.get("drawn_as", "block"), "sample_ref": first["ref"],
                      "count": sum(n for v, n in per_view.items() if v not in copies), "per_view": dict(per_view),
                      "cells_per_row": first.get("cells"), "cells": cells or None,
                      "drawn_by": dict(Counter(r["family"] for r in records)) if any(r["family"] != family for r in records) else None,
                      "size_mm": [round(v) for v in sizes[len(sizes) // 2]], "warehouse": first["type"] in WAREHOUSE_TYPES})
    for u in equipment.get("unrecognised") or []:
        if u["occurrences"] >= UNRECOGNISED_MIN_OCCURRENCES and u["family"] not in by_family:
            items.append({"family": u["family"], "type": "UNRECOGNISED", "type_zh": "没认出", "name": "", "basis": f"AI 没认出（{u.get('reason') or ''}）",
                          "drawn_as": "block", "sample_ref": None, "count": u["occurrences"], "per_view": {}, "size_mm": u["size_mm"], "warehouse": False})
    order = {t: n for n, t in enumerate(WAREHOUSE_TYPES + ["OTHER_EQUIPMENT", "UNRECOGNISED"])}
    items.sort(key=lambda i: (order.get(i["type"], 99), -i["count"]))
    for n, it in enumerate(items, 1):
        it["id"] = f"E{n:02d}"
    return items


BLOCK_DRAWN = {"block", "row", "loose_cell"}  # kinds whose sample is a block definition (a tile can be drawn)


def count_text(it: dict[str, Any]) -> str:
    """'18 排×57 格＝1026 格' for a kind drawn as rows, '18 个' otherwise."""

    return f"{it['count']} 排×{it['cells_per_row']} 格＝{it['cells']} 格" if it.get("cells") else f"{it['count']} 个"


def _block_of(inventory: dict[str, Any], equipment: dict[str, Any], family: str, sample_ref: str | None) -> str | None:
    by_ref = {o["ref"]: o for o in inventory["occurrences"]}
    if sample_ref and sample_ref in by_ref:
        return by_ref[sample_ref]["block"]
    occurrences = inventory["occurrences"]
    if family.startswith("anon:"):  # grouped anonymous blocks: the same grouping as the recognition
        from cad_to_layout.zero_manual.equipment import normalise_anonymous

        if "_normalised" not in inventory:
            inventory["_normalised"] = normalise_anonymous(inventory)["occurrences"]
        occurrences = inventory["_normalised"]
    for o in occurrences:
        if o["original_block"] == family:
            return o["block"]
    return None


def _tile(axis: Any, doc: Any, block_name: str | None) -> None:
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    axis.axis("off")
    block = doc.blocks.get(block_name) if block_name else None
    if block is None:
        axis.text(0.5, 0.5, "（不是图块，见叠图）", ha="center", va="center", fontsize=8, transform=axis.transAxes)
        return
    try:
        config = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
        Frontend(RenderContext(doc), MatplotlibBackend(axis), config=config).draw_entities(block)
        e = bbox.extents(block)
        if e.has_data:
            axis.set_xlim(e.extmin.x, e.extmax.x)
            axis.set_ylim(e.extmin.y, e.extmax.y)
        axis.set_aspect("equal")
    except Exception:  # a block the renderer cannot draw still gets its row in the table
        axis.text(0.5, 0.5, "（画不出）", ha="center", va="center", fontsize=8, transform=axis.transAxes)


def _index(run: Path, review: Path) -> Any:
    from cad_to_layout.zero_manual import agent_tools

    path = review / "index.sqlite"
    if not path.is_file():
        subprocess.run([sys.executable, "-X", "utf8", "-m", "cad_to_layout.zero_manual.agent_tools", "index", str((run / "convert/source.dxf").resolve())],
                       cwd=str(review), check=True, capture_output=True, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    return agent_tools.Index(path)


def _overlay(axis: Any, index: Any, records: list[dict[str, Any]], window: list[float]) -> None:
    from matplotlib import patches

    for ln in index.select(window, limit=150000):
        p = ln["p"]
        if len(p) >= 2:
            axis.plot([q[0] for q in p], [q[1] for q in p], color="#c4c8ce", linewidth=0.2)
    for r in records:
        L, W, a = r["length_mm"], r["width_mm"], math.radians(r.get("rotation_deg") or 0)
        cx, cy = r["centre"]
        corners = [(cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a))
                   for dx, dy in ((-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2))]
        colour = COLOURS.get(r["type"], "#6b7280")
        axis.add_patch(patches.Polygon(corners, closed=True, edgecolor=colour, facecolor=colour, alpha=0.45, linewidth=0.6))
    axis.set_xlim(window[0], window[2])
    axis.set_ylim(window[1], window[3])
    axis.set_aspect("equal")
    axis.axis("off")


def make(args: argparse.Namespace) -> int:
    import ezdxf
    from matplotlib import patches
    from matplotlib.backends.backend_pdf import PdfPages

    from scripts.trial_package import wrap_keep

    run = Path(args.run).resolve()
    review = run / "review"
    review.mkdir(exist_ok=True)
    equipment, views, inventory = _load(run)
    items = items_of(equipment, views)
    copies = {c["view_id"]: c for c in equipment.get("duplicate_views") or []}
    skipped = not_counted(equipment, views)
    title = {k: " ".join(str((v.get("title") or {}).get("text") or f"视图{n}（图上无标题）").split()) for n, (k, v) in enumerate(views.items(), 1)}
    doc = ezdxf.readfile(run / "convert/source.dxf")
    pyplot = _matplotlib()
    # what does not add up: stated quantities, labels not used, untyped candidates
    checks = [f"图上写「{' '.join(q['text'].split())}」{q['stated']} 台，程序数到 {q['counted']} 台" for q in equipment.get("stated_quantities") or [] if q["status"] != "consistent"]
    checks += [f"{title.get(c['view_id'], c['view_id'])} 与 {title.get(c['same_as'], c['same_as'])} 是同一套图画了两遍（重合 {c['overlap']:.0%}），台数只算一套"
               for c in copies.values()]
    checks += [f"标签「{'、'.join(' '.join(x.split()) for x in w['texts'])}」只盖住同类 {w['members']} 个中的 {w['members_inside']} 个{'（是区域名）' if w['area_name'] else ''}，没用来定类型，请看这类设备的类型对不对"
               for w in equipment.get("labels_not_used_for_type") or []]
    untyped = Counter(c["view_id"] for c in equipment.get("candidates_untyped") or [])
    if untyped:
        checks.append(f"还有 {sum(untyped.values())} 个形状像设备、但类型没定的候选（多为方框画的格位），在叠图上是灰色未上色的地方")
    with PdfPages(review / "设备复查.pdf") as pdf:
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.06, 0.957, "设备复查", fontsize=18, weight="bold")
        figure.text(0.06, 0.935, f"图纸：{Path(args.drawing).name if args.drawing else run.name}　　程序读出，未经人工复查", fontsize=9, color="#4b5563")
        y = 0.905
        for line in ["怎么复查（不用数台数）：", "① 后面每一格是一种设备：看小图和类型对不对，在「设备复查表.csv」的“复查”栏填 对 / 不是设备 / 正确的类型。台数是程序按图数的，类型对了台数就对。",
                     "② 看叠图：认出的设备都涂了颜色。图上明明是设备却没上色的地方，就是漏了，在表最后写一句在哪。", "③ 看本页下方“对不上的地方”。"]:
            for k, piece in enumerate(wrap_keep(line, 58)):
                figure.text(0.06 if k == 0 else 0.08, y, piece, fontsize=9.5)
                y -= 0.018
            y -= 0.004
        y -= 0.01
        figure.text(0.06, y, "按视图的台数（程序读出）", fontsize=12.5, weight="bold")
        y -= 0.026
        per_view: dict[str, Counter] = defaultdict(Counter)
        for r in equipment["records"]:
            per_view[r["view_id"]][r["type"]] += 1
        for view_id, counts in sorted(per_view.items(), key=lambda kv: -sum(kv[1].values())):
            warehouse = "、".join(f"{LABEL_ZH.get(t, t)} {counts[t]}" for t in WAREHOUSE_TYPES if counts.get(t))
            other = counts.get("OTHER_EQUIPMENT", 0)
            note = "（与另一视图重复，不另计）" if view_id in copies else ("（剖面/立面/详图，不计台数）" if view_id in skipped else "")
            text = f"{title.get(view_id, view_id)}{note}：仓储设备 {warehouse or '无'}" + (f"；其他设备 {other}（可能含生产设备）" if other else "")
            for k, piece in enumerate(wrap_keep(text, 62)):
                figure.text(0.07 if k == 0 else 0.09, y, piece, fontsize=9)
                y -= 0.017
            if y < 0.32:
                break
        y -= 0.012
        figure.text(0.06, y, f"对不上的地方（{len(checks)} 处）", fontsize=12.5, weight="bold")
        y -= 0.026
        for c in checks[:12] or ["没有"]:
            for k, piece in enumerate(wrap_keep("· " + c, 62)):
                figure.text(0.07 if k == 0 else 0.09, y, piece, fontsize=9)
                y -= 0.017
        figure.text(0.06, 0.03, "说明：这是读设备的复查页，不是最终结果。台数由程序按图形数出，没有人工清点；类型由图上文字或 AI 看图判断。", fontsize=8, color="#4b5563")
        pdf.savefig(figure)
        pyplot.close(figure)
        for start in range(0, len(items), TILES_PER_PAGE):
            figure, axes = pyplot.subplots(4, 3, figsize=(8.27, 11.69))
            figure.suptitle("每种设备（看小图和类型对不对）", fontsize=13, x=0.06, ha="left", y=0.985)
            for axis, it in zip(axes.flat, items[start:start + TILES_PER_PAGE]):
                _tile(axis, doc, _block_of(inventory, equipment, it["family"], it["sample_ref"]) if it["drawn_as"] in BLOCK_DRAWN else None)
                label = f"{it['id']}  {it['type_zh']}{('：' + it['name'][:14]) if it['name'] else ''}\n{count_text(it)}　{it['size_mm'][0]}×{it['size_mm'][1]} mm\n{it['basis']}"
                axis.set_title(label, fontsize=8, loc="left")
            for axis in list(axes.flat)[len(items[start:start + TILES_PER_PAGE]):]:
                axis.axis("off")
            figure.tight_layout(rect=(0, 0, 1, 0.97))
            pdf.savefig(figure)
            pyplot.close(figure)
        index = _index(run, review)
        for view_id, counts in sorted(per_view.items(), key=lambda kv: -sum(kv[1].values())):
            if view_id in skipped or (views.get(view_id) or {}).get("role") not in {"plan", None}:
                continue
            records = [r for r in equipment["records"] if r["view_id"] == view_id]
            w = (views.get(view_id) or {}).get("window")
            if not w:
                continue
            figure = pyplot.figure(figsize=(11.69, 8.27))
            axis = figure.add_axes([0.01, 0.06, 0.98, 0.88])
            _overlay(axis, index, records, w)
            figure.text(0.01, 0.965, f"{title.get(view_id, view_id)}：认出的设备涂色，灰线是原图。图上是设备却没上色的地方＝漏了", fontsize=11)
            handles = [patches.Patch(color=COLOURS.get(t, "#6b7280"), label=f"{LABEL_ZH.get(t, t)} {n}") for t, n in counts.most_common()]
            figure.legend(handles=handles, loc="lower center", ncol=min(len(handles), 6), fontsize=8, frameon=False)
            pdf.savefig(figure)
            pyplot.close(figure)
        index.con.close()
    with (review / "设备复查表.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "类型（程序）", "名称", "依据", "台数（不含重复视图）", "单台长×宽 mm", "图块", "复查（对 / 不是设备 / 正确类型）"])
        for it in items:
            w.writerow([it["id"], it["type_zh"], it["name"], it["basis"], count_text(it), f"{it['size_mm'][0]}×{it['size_mm'][1]}", it["family"], ""])
        w.writerow(["漏了的地方", "", "", "", "", "", "", ""])
    (review / "review_items.json").write_text(json.dumps({"made_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "items": items},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"review": str(review), "kinds": len(items), "checks": len(checks)}, ensure_ascii=False))
    return 0


def apply(args: argparse.Namespace) -> int:
    run = Path(args.run).resolve()
    review = run / "review"
    equipment, views, _ = _load(run)
    items = {it["id"]: it for it in json.loads((review / "review_items.json").read_text(encoding="utf-8"))["items"]}
    decisions, missed = {}, ""
    with Path(args.table).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or row[0] == "编号":
                continue
            if row[0] == "漏了的地方":
                missed = " ".join(x for x in row[1:] if x).strip()
                continue
            decisions[row[0]] = (row[7] if len(row) > 7 else "").strip()
    by_family = {it["family"]: it for it in items.values()}
    copies = not_counted(equipment, views)
    # "对" on a kind the first judgement left open accepts the second look's suggestion shown to the person
    archive_path = review / "second_look" / "archive.json"
    suggested = {}
    if archive_path.is_file():
        for family, entry in json.loads(archive_path.read_text(encoding="utf-8")).items():
            votes = Counter(a["type"] for a in entry["asks"] if a.get("type"))
            if votes and votes.most_common(1)[0][1] >= 2:
                top = votes.most_common(1)[0][0]
                suggested[family] = (top, next((a["name"] for a in entry["asks"] if a["type"] == top and a["name"]), ""))
    rows, unknown = [], []
    for it in items.values():
        d = decisions.get(it["id"], "")
        if d == "对" and it["type"] == "UNRECOGNISED" and it["family"] in suggested:
            (kind, name), source = suggested[it["family"]], "人工确认系统建议"
        elif d in {"", "对"}:
            kind, name, source = it["type"], it["name"], "已人工核对" if d == "对" else "系统识别（未核对）"
        elif d == "不是设备":
            continue
        else:
            kind = ZH_TO_TYPE.get(d, "OTHER_EQUIPMENT")
            name, source = ("" if d in ZH_TO_TYPE else d), "人工修正"
        if kind == "UNRECOGNISED":
            unknown.append(it["id"])
            continue
        records = [r for r in equipment["records"] if r["family"] == it["family"] and r["view_id"] not in copies] if it["type"] != "UNRECOGNISED" else []
        count = len(records) if records else it["count"]  # an unrecognised kind has no records: its occurrences are the count
        rows.append({"id": it["id"], "type": kind, "type_zh": LABEL_ZH.get(kind, kind), "name": name, "count": count, "size_mm": it["size_mm"], "source": source})
    rows += repeat_rows(run, decisions)
    totals = Counter()
    for r in rows:
        totals[(r["type_zh"], r["name"])] += r["count"]
    lines = ["# 设备结果（复查后）", "", f"复查人：{args.reviewer or '未记录'}　自报用时：{args.minutes if args.minutes is not None else '未记录'} 分钟　"
             f"复查了 {sum(1 for v in decisions.values() if v)} / {len(items)} 种", "",
             "| 设备类型 | 名称 | 台数 |", "|---|---|---|"]
    lines += [f"| {t} | {n} | {c} |" for (t, n), c in totals.most_common()]
    lines += ["", "| 编号 | 类型 | 名称 | 台数 | 单台长×宽 mm | 来源 |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['type_zh']} | {r['name']} | {r['count']} | {r['size_mm'][0]}×{r['size_mm'][1]} | {r['source']} |" for r in rows]
    if unknown:
        lines += ["", "仍没认出、复查人也没填类型的：" + "、".join(unknown)]
    lines += ["", f"复查人写的漏了的地方：{missed or '无'}", "", "台数由程序按平面图上的图形数出（同一套图画两遍的只算一套，剖面/立面/详图不计），没有人工清点。"]
    (review / "设备结果（复查后）.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (review / "设备结果（复查后）.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "类型", "名称", "台数", "单台长×宽 mm", "来源"])
        for r in rows:
            w.writerow([r["id"], r["type_zh"], r["name"], r["count"], f"{r['size_mm'][0]}×{r['size_mm'][1]}", r["source"]])
    print(json.dumps({"kinds_kept": len(rows), "unknown": unknown}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("make")
    p.add_argument("--run", required=True)
    p.add_argument("--drawing", help="the customer drawing's file name, shown on the first page")
    p = sub.add_parser("apply")
    p.add_argument("--run", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--reviewer")
    p.add_argument("--minutes", type=float)
    p = sub.add_parser("repeat")
    p.add_argument("--run", required=True)
    p = sub.add_parser("dwg")
    p.add_argument("--run", required=True)
    p.add_argument("--area", help="trial_package out dir: the building outline is drawn too")
    p.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)
    if args.cmd == "repeat":
        result = repeats(Path(args.run).resolve())
        print(json.dumps([(g["id"], len(g["places"]), (g.get("suggestion") or {}).get("type")) for v in result["views"] for g in v["groups"]], ensure_ascii=False))
        return 0
    return {"make": make, "apply": apply}.get(args.cmd, lambda a: review_dwg(a))(args)




# ---------------------------------------------------------------- a second look at kinds the first judgement left open
SECOND_LOOK_MIN_MM = 1000.0  # kinds smaller than this (symbols, marks, small parts) are not asked again and not put before a person
SECOND_LOOK_GUIDANCE = """你在看一张仓储物流 CAD 平面图里的一种图形。左图是这种图形本身；右图是它在原图里的一处位置（红框）和周围的原图线条。
下面给出它的实际尺寸、出现次数和附近文字。请判断它是什么，只能从下面选一个：
RACK 货架（含俯视的货架格、成排货架、货架的侧面或立面画法）
STACKER 堆垛机（含堆垛机轨道、巷道）
PALLET_CONV_CHAIN 托盘链条输送机
PALLET_CONV_ROLLER 托盘滚筒输送机
CONVEYOR_UNKNOWN_SUBTYPE 输送机但看不出子类（含弯道、转弯段、转台、移载段）
BOX_CONVEYOR 箱式输送机（窄的料箱输送线）
BOX_LIFT 提升机
ROBOT_ARM 机械臂
OTHER_EQUIPMENT 其他物流设备（穿梭车、四向车、AGV、叉车、工作台、移载机、叠盘机等，在 name 写名称）
NOT_EQUIPMENT 不是设备（柱子、墙、门窗、楼梯、灯、标注、编号圈、托盘或货物轮廓、地面标记、区域框）
COMPONENT 设备的零件（立柱、横梁、电机、传感器、护栏）
UNSURE 实在看不出
不要用工具，只看图和文字。只输出 JSON：{"type": "...", "name": "...", "reason": "一句话", "wrong_if": "什么情况下这个判断是错的，一句话"}"""


def _family_occurrence(inventory: dict[str, Any], family: str) -> dict[str, Any] | None:
    from cad_to_layout.zero_manual.equipment import normalise_anonymous

    if "_normalised" not in inventory:
        inventory["_normalised"] = normalise_anonymous(inventory)["occurrences"]
    hits = [o for o in inventory["_normalised"] if o["original_block"] == family and o.get("bbox")]
    return hits[len(hits) // 2] if hits else None


def second_look(run: Path, items: list[dict[str, Any]], inventory: dict[str, Any], doc: Any, index: Any) -> dict[str, dict[str, Any]]:
    """Kinds left unrecognised and at least SECOND_LOOK_MIN_MM long are shown to the AI again, alone, with the real
    drawing around one occurrence and a wider list of answers; asked 3 times, an answer given at least twice is
    kept as a suggestion for the person (never counted by itself).  Answers are archived and replayed."""

    from cad_to_layout.zero_manual.constants import SPLIT_JUDGE_MAJORITY, SPLIT_JUDGE_REPEATS
    from cad_to_layout.zero_manual.equipment import parse_tiles  # noqa: F401  (same JSON tolerance as the first judgement)
    from cad_to_layout.zero_manual.llm_judge import external_transport
    from cad_to_layout.zero_manual.agent_reader import _json_from

    folder = run / "review" / "second_look"
    folder.mkdir(parents=True, exist_ok=True)
    archive_path = folder / "archive.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.is_file() else {}
    pyplot = _matplotlib()
    transport = None
    texts = inventory["texts"]
    out = {}
    for it in items:
        if it["type"] != "UNRECOGNISED" or max(it["size_mm"]) < SECOND_LOOK_MIN_MM:
            continue
        family = it["family"]
        if family not in archive:
            occ = _family_occurrence(inventory, family)
            if occ is None:
                continue
            b = occ["bbox"]
            half = 4.0 * max(b[2] - b[0], b[3] - b[1], 1.0)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            window = [cx - half, cy - half, cx + half, cy + half]
            figure, (left, right) = pyplot.subplots(1, 2, figsize=(10, 5))
            _tile(left, doc, _block_of(inventory, {}, family, None))
            for ln in index.select(window, limit=40000):
                p = ln["p"]
                if len(p) >= 2:
                    right.plot([q[0] for q in p], [q[1] for q in p], color="#374151", linewidth=0.3)
            from matplotlib import patches

            right.add_patch(patches.Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], edgecolor="#dc2626", facecolor="none", linewidth=2))
            right.set_xlim(window[0], window[2])
            right.set_ylim(window[1], window[3])
            right.set_aspect("equal")
            right.axis("off")
            image = folder / f"{it['id']}.png"
            figure.savefig(image, facecolor="white", dpi=110)
            pyplot.close(figure)
            near = sorted({" ".join(t["text"].split())[:20] for t in texts if window[0] <= t["xy"][0] <= window[2] and window[1] <= t["xy"][1] <= window[3]})[:8]
            prompt = SECOND_LOOK_GUIDANCE + f"\n\n尺寸：{it['size_mm'][0]} × {it['size_mm'][1]} mm；出现 {it['count']} 次；附近文字：{'；'.join(near) or '无'}"
            transport = transport or external_transport()
            asks = []
            for n in range(1, SPLIT_JUDGE_REPEATS + 1):
                call = folder / f"{it['id']}_ask{n}"
                call.mkdir(exist_ok=True)
                raw = transport(prompt, image, call)
                value = _json_from(raw.get("text", "")) or {}
                asks.append({"type": value.get("type"), "name": str(value.get("name") or "")[:30], "reason": str(value.get("reason") or "")[:120],
                             "wrong_if": str(value.get("wrong_if") or "")[:120], "provider": raw.get("provider"), "model": raw.get("model")})
            archive[family] = {"asks": asks, "prompt_sha": __import__("hashlib").sha256(SECOND_LOOK_GUIDANCE.encode()).hexdigest()}
            archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
        asks = archive[family]["asks"]
        votes = Counter(a["type"] for a in asks if a.get("type"))
        if votes:
            top, n = votes.most_common(1)[0]
            name = next((a["name"] for a in asks if a["type"] == top and a["name"]), "")
            out[it["id"]] = {"type": top if n >= SPLIT_JUDGE_MAJORITY else "UNSURE", "name": name, "votes": f"{n}/{len(asks)}",
                             "reason": next((a["reason"] for a in asks if a["type"] == top), ""),
                             "wrong_if": next((a.get("wrong_if", "") for a in asks if a["type"] == top and a.get("wrong_if")), "")}
    return out


# ---------------------------------------------------------------- the review drawing for a CAD colleague
TYPE_ACI = {"RACK": 1, "STACKER": 7, "PALLET_CONV_CHAIN": 6, "PALLET_CONV_ROLLER": 5, "CONVEYOR_UNKNOWN_SUBTYPE": 4, "BOX_CONVEYOR": 3,
            "BOX_LIFT": 30, "ROBOT_ARM": 210, "OTHER_EQUIPMENT": 8}


def questions(items: list[dict[str, Any]], looks: dict[str, dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """The kinds put before a person: left open by the first judgement, at least 1 m, not called not-equipment the second time."""

    asked = [it for it in items if it["type"] == "UNRECOGNISED" and it["id"] in looks and looks[it["id"]]["type"] not in {"NOT_EQUIPMENT", "COMPONENT"}]
    return sorted(asked, key=lambda it: -it["count"] * max(it["size_mm"]))[:limit]


def review_dwg(args: argparse.Namespace) -> int:
    """A copy of the drawing with the equipment on one layer per type, a numbered label per kind, the questions
    circled, and the building outline: what the review PDF shows, where a CAD user can zoom, measure and switch layers."""

    import ezdxf

    from scripts.write_agent_result_cad import save_dwg, write_result_dxf

    run = Path(args.run).resolve()
    review = run / "review"
    review.mkdir(exist_ok=True)
    equipment, views, inventory = _load(run)
    items = items_of(equipment, views)
    skipped = not_counted(equipment, views)
    doc = ezdxf.readfile(run / "convert/source.dxf")
    index = _index(run, review)
    looks = second_look(run, items, inventory, doc, index)
    index.con.close()
    del doc
    asked = {it["id"] for it in questions(items, looks)}
    layer_of = {t: f"CTLE_设备_{LABEL_ZH.get(t, t)}" for t in TYPE_ACI}
    entities = []
    first: dict[str, dict[str, Any]] = {}
    for r in equipment["records"]:
        if r["view_id"] in skipped:
            continue
        L, W, a = r["length_mm"], r["width_mm"], math.radians(r.get("rotation_deg") or 0)
        cx, cy = r["centre"]
        corners = [[cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)]
                   for dx, dy in ((-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2))]
        entities.append({"layer": layer_of.get(r["type"], layer_of["OTHER_EQUIPMENT"]), "kind": "outline", "points": corners,
                         "width": min(max(min(L, W) * 0.06, 30.0), 120.0), "record": {"ref": r["ref"]}})  # thick enough to stand out from the drawing
        first.setdefault(r["family"], r)
    for it in items:
        r = first.get(it["family"])
        if r is None:
            occ = _family_occurrence(inventory, it["family"])
            if occ is None:
                continue
            b = occ["bbox"]
            r = {"centre": [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]}
        size = max(it["size_mm"])
        height = min(max(size / 4, 300.0), 1500.0)
        name = it["name"] or it["type_zh"]
        if it["id"] in asked:
            look = looks[it["id"]]
            guess = "无法判断" if look["type"] == "UNSURE" else (look["name"] or LABEL_ZH.get(look["type"], look["type"]))
            entities.append({"layer": "CTLE_请确认", "kind": "circle", "at": r["centre"], "radius": size * 0.8 + 500, "record": {"item": it["id"]}})
            entities.append({"layer": "CTLE_请确认", "kind": "text", "at": [r["centre"][0], r["centre"][1] + size * 0.8 + 1500],
                             "text": f"确认 {it['id']}：系统识别为 {guess}，是否正确？（共 {it['count']} 处）", "height": max(height, 600.0), "record": {"item": it["id"]}})
        else:
            entities.append({"layer": "CTLE_设备编号", "kind": "text", "at": r["centre"], "text": f"{it['id']} {name}", "height": height, "record": {"item": it["id"]}})
    if args.area:
        result = json.loads((Path(args.area) / "issue" / "ai_result.json").read_text(encoding="utf-8"))
        b = next((x for x in result["buildings"] if x.get("designated") and x.get("outline")), None)
        if b:
            entities.append({"layer": "CTLE_厂房轮廓", "kind": "outline", "points": b["outline"], "width": 300.0, "record": {"building": b["name"]}})
            xs = [q[0] for q in b["outline"]]
            ys = [q[1] for q in b["outline"]]
            entities.append({"layer": "CTLE_厂房轮廓", "kind": "text", "at": [(min(xs) + max(xs)) / 2, max(ys) + 3000],
                             "text": f"厂房：{b['area_m2']:,.0f} m²，{b.get('length_m', 0):.1f} × {b.get('width_m', 0):.1f} m（系统识别，待核对）", "height": 2000.0,
                             "record": {"building": b["name"]}})
    layers = {**{layer_of[t]: aci for t, aci in TYPE_ACI.items()}, "CTLE_设备编号": 7, "CTLE_请确认": 2, "CTLE_厂房轮廓": 1}
    entities += _repeat_entities(run, layers)
    dxf = review / "核对用.dxf"
    write_result_dxf(run / "convert/source.dxf", entities, dxf, extra_layers=layers)
    out = review / "check.dwg"  # ASCII: AutoCAD scripts do not take this path otherwise
    out.unlink(missing_ok=True)
    saved = save_dwg(dxf, out, args.timeout)
    dxf.unlink(missing_ok=True)
    print(json.dumps({"dwg": str(out), "saved": saved, "entities": len(entities), "questions": sorted(asked)}, ensure_ascii=False))
    return 0


def _repeat_entities(run: Path, layers: dict[str, int]) -> list[dict[str, Any]]:
    """The R groups the review PDF shows (only when the report already counted them; nothing is counted here): each
    group's places on its own layer CTLE_重复图形_R#, the example outlined thick, and the R number with its question."""

    path = run / "review" / "repeat" / "repeat.json"
    if not path.is_file():
        return []
    out = []
    for view in json.loads(path.read_text(encoding="utf-8"))["views"]:
        groups = view["groups"]
        s = groups[0].get("suggestion") or {}
        kind = s.get("type")
        guess = "无法判断" if kind in (None, "UNSURE") else ((s.get("name") or LABEL_ZH.get(kind, kind)) if kind == "OTHER_EQUIPMENT" else LABEL_ZH.get(kind, kind))
        for n, g in enumerate(groups):
            layer = f"CTLE_重复图形_{g['id']}"
            layers[layer] = REPEAT_ACI[n % len(REPEAT_ACI)]
            for c in g["places"]:
                out.append({"layer": layer, "kind": "outline", "points": [[c[0], c[1]], [c[2], c[1]], [c[2], c[3]], [c[0], c[3]]], "record": {"repeat": g["id"]}})
            e = g["example"]
            size = max(e[2] - e[0], e[3] - e[1])
            out.append({"layer": layer, "kind": "outline", "points": [[e[0], e[1]], [e[2], e[1]], [e[2], e[3]], [e[0], e[3]]],
                        "width": min(max(size * 0.03, 30.0), 120.0), "record": {"repeat": g["id"], "example": True}})
            if n == 0 and kind == "NOT_EQUIPMENT":
                text = f"{g['id']}：{len(g['places']):,} 处重复图形，系统认为不是设备（未计入），请看一眼"
            elif n == 0:
                text = f"{g['id']}：系统识别为 {guess}，{len(g['places']):,} 处，是否正确？每处算几格？"
            elif kind == "NOT_EQUIPMENT":
                text = f"{g['id']}：另有 {len(g['places']):,} 处相似图形（未计入）"
            else:
                text = f"{g['id']}：另有 {len(g['places']):,} 处相似图形，也是{guess}吗？（未计入）"
            out.append({"layer": layer, "kind": "text", "at": [(e[0] + e[2]) / 2, e[3] + max(size * 0.3, 600.0)], "text": text,
                        "height": min(max(size / 4, 500.0), 1500.0), "record": {"repeat": g["id"]}})
    return out


# ---------------------------------------------------------------- counting by repetition (equipment drawn as loose lines or parts)
REPEAT_COLOURS = ["#dc2626", "#f59e0b", "#7c3aed", "#0d9488"]
REPEAT_ACI = [1, 30, 6, 4]  # the same red / orange / purple / cyan in the review drawing


REPEAT_GUIDANCE = """你在看一张仓储物流 CAD 平面图。左图是一个重复单元：同样的图形在这张平面图上重复出现很多次，排成阵列；右图是它在原图里的一处位置（红框）和周围的原图线条。
请判断这一整片重复排列的东西是什么设备（不是问单元里的某个零件），只能从下面选一个：
RACK 货架（俯视的货架格、成排货架）
STACKER 堆垛机（含堆垛机轨道、巷道）
PALLET_CONV_CHAIN 托盘链条输送机
PALLET_CONV_ROLLER 托盘滚筒输送机
CONVEYOR_UNKNOWN_SUBTYPE 输送机但看不出子类
BOX_CONVEYOR 箱式输送机（窄的料箱输送线）
BOX_LIFT 提升机
ROBOT_ARM 机械臂
OTHER_EQUIPMENT 其他物流设备（在 name 写名称）
NOT_EQUIPMENT 不是设备（柱网、墙、门窗、楼梯、表格、图框、标注、地面标记）
UNSURE 实在看不出
不要用工具，只看图和文字。只输出 JSON：{"type": "...", "name": "...", "reason": "一句话", "wrong_if": "什么情况下这个判断是错的，一句话"}"""


def _record_box(r: dict[str, Any]) -> list[float]:
    half = max(r["length_mm"], r["width_mm"]) / 2
    return [r["centre"][0] - half, r["centre"][1] - half, r["centre"][0] + half, r["centre"][1] + half]


def repeats(run: Path, index: Any = None) -> dict[str, Any]:
    """Groups of repeated linework on every counted plan (`repeat_count.count_view`), each group numbered R1, R2...
    The first group of a plan is the program's own example, the AI names it (3 asks, a suggestion only); later groups
    are questions.  A plan whose first group mostly lies on equipment already counted from blocks gets no group, so
    nothing is counted twice.  Saved in review/repeat/repeat.json and reused."""

    from cad_to_layout.zero_manual.constants import REPEAT_ALREADY_COUNTED_SHARE
    from cad_to_layout.zero_manual.repeat_count import count_view

    folder = run / "review" / "repeat"
    path = folder / "repeat.json"
    if path.is_file():
        out = json.loads(path.read_text(encoding="utf-8"))
        # a name the AI never answered (network down, every ask failed) is asked again; the counts are kept
        failed = [v for v in out["views"] if not any(a.get("type") for a in (v["groups"][0].get("suggestion") or {}).get("asks", []))
                  or (v["groups"][0].get("suggestion") or {}).get("guidance") != _guidance_sha()]  # or asked with other words
        if failed:
            _, _, inventory = _load(run)
            own_index = index is None
            index = index or _index(run, run / "review")
            for view in failed:
                view["groups"][0]["suggestion"] = _repeat_suggestion(folder, view["groups"][0], index, inventory)
            if own_index:
                index.con.close()
            path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out
    folder.mkdir(parents=True, exist_ok=True)
    equipment, views, inventory = _load(run)
    skipped = not_counted(equipment, views)
    own_index = index is None
    index = index or _index(run, run / "review")
    out: dict[str, Any] = {"views": [], "skipped_already_counted": []}
    number = 0
    for view_id, v in views.items():
        if view_id in skipped or not v.get("window"):
            continue
        result = count_view(index, v["window"])
        if not result["groups"]:
            continue
        boxes = [_record_box(r) for r in equipment["records"] if r["view_id"] == view_id]
        first = result["groups"][0]["places"]
        on_counted = sum(1 for c in first if any(b[0] <= (c[0] + c[2]) / 2 <= b[2] and b[1] <= (c[1] + c[3]) / 2 <= b[3] for b in boxes))
        if on_counted >= REPEAT_ALREADY_COUNTED_SHARE * len(first):
            out["skipped_already_counted"].append({"view_id": view_id, "places": len(first), "on_counted_equipment": on_counted})
            continue
        for g in result["groups"]:
            number += 1
            g["id"] = f"R{number}"
        out["views"].append({"view_id": view_id, "seconds": result["seconds"], "groups": result["groups"]})
    for view in out["views"]:
        view["groups"][0]["suggestion"] = _repeat_suggestion(folder, view["groups"][0], index, inventory)
    if own_index:
        index.con.close()
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _repeat_suggestion(folder: Path, group: dict[str, Any], index: Any, inventory: dict[str, Any]) -> dict[str, Any]:
    """The AI's guess for what the program's example is: the example alone and in its surroundings, asked 3 times."""

    from matplotlib import patches

    from cad_to_layout.zero_manual.agent_reader import _json_from
    from cad_to_layout.zero_manual.constants import SPLIT_JUDGE_MAJORITY, SPLIT_JUDGE_REPEATS
    from cad_to_layout.zero_manual.llm_judge import external_transport

    pyplot = _matplotlib()
    b = group["example"]
    half = 3.0 * max(b[2] - b[0], b[3] - b[1])
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    window = [cx - half, cy - half, cx + half, cy + half]
    figure, (left, right) = pyplot.subplots(1, 2, figsize=(10, 5))
    for axis, win in ((left, b), (right, window)):
        for ln in index.select(win, limit=40000):
            p = ln["p"]
            if axis is left and not all(b[0] <= x <= b[2] and b[1] <= y <= b[3] for x, y in p):
                continue
            axis.plot([q[0] for q in p], [q[1] for q in p], color="#111827" if axis is left else "#374151", linewidth=0.6 if axis is left else 0.3)
        axis.set_xlim(win[0], win[2])
        axis.set_ylim(win[1], win[3])
        axis.set_aspect("equal")
        axis.axis("off")
    right.add_patch(patches.Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], edgecolor="#dc2626", facecolor="none", linewidth=2))
    image = folder / f"{group['id']}.png"
    figure.savefig(image, facecolor="white", dpi=110)
    pyplot.close(figure)
    near = sorted({" ".join(t["text"].split())[:20] for t in inventory["texts"] if window[0] <= t["xy"][0] <= window[2] and window[1] <= t["xy"][1] <= window[3]})[:8]
    size = [round(b[2] - b[0]), round(b[3] - b[1])]
    prompt = REPEAT_GUIDANCE + f"\n\n单元尺寸：{size[0]} × {size[1]} mm；同样的图形在这张平面图上重复出现 {len(group['places'])} 次；附近文字：{'；'.join(near) or '无'}"
    transport = external_transport()
    asks = []
    for n in range(1, SPLIT_JUDGE_REPEATS + 1):
        call = folder / f"{group['id']}_ask{n}"
        call.mkdir(exist_ok=True)
        raw = transport(prompt, image, call)
        value = _json_from(raw.get("text", "")) or {}
        asks.append({"type": value.get("type"), "name": str(value.get("name") or "")[:30], "reason": str(value.get("reason") or "")[:120]})
    votes = Counter(a["type"] for a in asks if a.get("type"))
    if not votes:
        return {"type": "UNSURE", "name": "", "votes": f"0/{len(asks)}", "asks": asks, "guidance": _guidance_sha()}
    top, n = votes.most_common(1)[0]
    return {"type": top if n >= SPLIT_JUDGE_MAJORITY else "UNSURE", "name": next((a["name"] for a in asks if a["type"] == top and a["name"]), ""),
            "votes": f"{n}/{len(asks)}", "asks": asks, "guidance": _guidance_sha()}


def _guidance_sha() -> str:
    import hashlib

    return hashlib.sha256(REPEAT_GUIDANCE.encode("utf-8")).hexdigest()[:12]


def per_place(decision: str) -> tuple[str, int | None]:
    """'对，每处2格' -> ('对', 2); '货架 ×2' -> ('货架', 2); '是' -> ('是', None).  A place found by repetition can hold more
    than one unit (the example can straddle two back-to-back rows); the person, who sees the example, says how many."""

    import re

    m = re.search(r"(?:每处|每个红框|每个|每框|×|x|X|\*)\s*(\d+)\s*(?:格|台|段|个|处)?", decision)
    if not m:
        return decision.strip(), None
    return (decision[:m.start()] + decision[m.end():]).strip(" ，,；;、"), int(m.group(1))


def repeat_rows(run: Path, decisions: dict[str, str]) -> list[dict[str, Any]]:
    """Counted groups after the reply: on a plan's first group 对 = the suggestion, a type name = that type, 不是 =
    dropped; a later group counts (as its plan's first group) only on 是 / 对; unanswered later groups are not counted.
    Units per place: as the person says (每处 N 格), a later group taking its first group's number unless it says its
    own; 1 when nobody said (and the source says so)."""

    path = run / "review" / "repeat" / "repeat.json"
    if not path.is_file():
        return []
    rows = []
    for view in json.loads(path.read_text(encoding="utf-8"))["views"]:
        first = view["groups"][0]
        s = first.get("suggestion") or {}
        d, per = per_place(decisions.get(first["id"], ""))
        if d.startswith("是") and d[1:] in ZH_TO_TYPE:  # "是货架"
            d = d[1:]
        if d in {"不是", "不是设备"} or (s.get("type") == "NOT_EQUIPMENT" and d in {"", "对", "是"}):
            continue  # the AI's "not equipment" counts only when the person names the equipment
        if d in {"", "对", "是"}:
            kind, name = s.get("type", "UNSURE"), s.get("name", "")
            source = "已人工核对" if d or per else "系统识别（未核对）"
        else:
            kind, name, source = ZH_TO_TYPE.get(d, "OTHER_EQUIPMENT"), ("" if d in ZH_TO_TYPE else d), "人工修正"
        b = first["example"]
        note = "（重复图形计数" + ("，每处按 1 计、未确认）" if per is None else f"，每处 {per}）")
        rows.append({"id": first["id"], "type": kind, "type_zh": LABEL_ZH.get(kind, kind), "name": name, "count": len(first["places"]) * (per or 1),
                     "per_place": per or 1, "size_mm": [round(b[2] - b[0]), round(b[3] - b[1])], "source": source + note})
        for g in view["groups"][1:]:
            answer, own = per_place(decisions.get(g["id"], ""))
            if answer in {"是", "对"}:
                n = own or per or 1
                rows.append({**rows[-1], "id": g["id"], "count": len(g["places"]) * n, "per_place": n,
                             "source": f"人工确认（重复图形计数，每处 {n}）"})
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
