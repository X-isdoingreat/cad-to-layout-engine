"""Human confirmation round trip in CAD: issue a DWG with numbered marks, read a person's saved DWG back.

  issue:  py -3.11 -X utf8 -m scripts.confirm_cad issue --work <agent work dir> [--other-work <dir> ...] \
              --source-dxf <converted copy> --customer-dwg <customer.dwg> --out <dir>
          writes <out>/to_confirm.dwg (result layers + marks), 待确认清单.md, items.json, issued.json and the AI result
          archive (ai_result_archive.json + its sha256), all before anyone opens the drawing.
  apply:  py -3.11 -X utf8 -m scripts.confirm_cad apply --out <dir> [--returned-dwg <the person's saved DWG>] [--passed] [--confirmer NAME --minutes N]
          --passed: the reviewer answered "通过" for the result page (marks left untouched keep the AI's choice);
          without a returned drawing, --passed alone applies that answer to the issued drawing.
          reads the returned drawing (a copy, saved as DXF by AutoCAD 2025), applies the decisions and writes
          final.json and 最终结果.md: every number labelled AI 读出 / 人工确认 / 人工修改（未标记处） / 待确认.

The customer drawing is never opened by AutoCAD; the returned drawing is read from a copy.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual import agent_reader, confirmation as C
from scripts.write_agent_result_cad import ACCORE, LAYERS, _accore, _lisp_path, result_entities, save_dwg, write_result_dxf


OUTLINE_LAYER = "CTLE_厂房轮廓"  # the machine's outline of the designated building, red
CAVEAT = "仓库面积按指定厂房外墙外边线围成的占地面积计（多层只算指定楼层，默认首层），为默认口径，可按需要调整；需要按存储/作业分区计面积时另做。"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def issue(args: argparse.Namespace) -> int:
    work, out = Path(args.work).resolve(), Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    read = json.loads((work / "agent_read.json").read_text(encoding="utf-8"))
    archive = out / "ai_result_archive.json"
    shutil.copyfile(work / "agent_read.json", archive)  # the AI result as it was before anyone confirmed anything
    answer = read.get("answer") or {}
    m = float(answer.get("unit_m") or 1.0)
    faces = json.loads((work / "faces.json").read_text(encoding="utf-8")) if (work / "faces.json").is_file() else {}
    found = agent_reader.findings(work, answer, read.get("checks", []))
    others = [json.loads((Path(o) / "agent_read.json").read_text(encoding="utf-8")) for o in args.other_work or []]
    from cad_to_layout.zero_manual import agent_tools

    idx = agent_tools.Index(work / agent_tools.INDEX.name)
    suffixes = json.loads((Path(agent_tools.__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8")).get("zone_suffixes", [])
    items = C.build_items(found, read, faces, others, texts=idx.texts, suffixes=suffixes)
    idx.con.close()
    entities = result_entities(work, read)
    if C.AREA_ONLY:  # only the building outline to check, on a layer of its own (zones, columns are not part of the trial)
        entities = [dict(e, layer=OUTLINE_LAYER) for e in entities if e["layer"] in {"CTLE_BUILDING", "CTLE_BUILDING_LABEL"}]
    entities += C.mark_entities(items, m)
    dxf = out / "to_confirm.dxf"
    manifest = write_result_dxf(Path(args.source_dxf), entities, dxf, extra_layers={**C.CONFIRM_LAYERS, OUTLINE_LAYER: 1})
    labels = {e["record"]["item"]: e["handle"] for e in manifest if e["record"].get("role") == "label"}
    issued = {"labels": labels, "entities": {e["handle"]: e["layer"] for e in manifest},
              "column_building": {e["handle"]: e["record"].get("building") for e in manifest if e["layer"] == C.COLUMN_LAYER},
              "ai_result_archive": archive.name, "ai_result_sha256": _sha(archive), "unit_m": m,
              "customer_dwg_sha256": _sha(Path(args.customer_dwg)), "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    dwg = out / "to_confirm.dwg"
    dwg.unlink(missing_ok=True)
    issued["dwg"] = save_dwg(dxf, dwg, args.timeout)
    issued["dwg_sha256"] = _sha(dwg) if dwg.is_file() else None
    (out / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "issued.json").write_text(json.dumps(issued, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = checklist(items, Path(args.customer_dwg).name)
    (out / "待确认清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    preview = C.apply(read, items, issued, {"entities": dict(issued["entities"]), "closed_outlines": [], "open_outlines": [], "human_columns": [],
                                            "columns": []}, m)
    (out / "ai_result.json").write_text(json.dumps(preview, ensure_ascii=False, indent=1), encoding="utf-8")  # the result page before review
    print(json.dumps({"items": len(items), "dwg": issued["dwg"], "by_kind": {k: sum(1 for i in items if i["kind"] == k) for k in {i["kind"] for i in items}}},
                     ensure_ascii=False))
    return 0


STEP = {
    "yes_no": f"要 / 不要：点一下编号文字选中它，在上方功能区的「图层」下拉框里改成「{C.LAYER_YES}」（绿色）或「{C.LAYER_NO}」（红色）。这些图层图里已经建好，下拉框里直接选。",
    "outline": f"厂房轮廓不对：在图层「{C.LAYER_HUMAN_OUTLINE}」（青色）用多段线 PL 沿外墙外边线描一整圈，打开对象捕捉点拐角，最后输入 C 闭合；描好后把核对轮廓的那个编号也改到「{C.LAYER_YES}」（以人工轮廓为准）。重描了整栋，这栋楼的其他轮廓编号（包括“算不算这块”）都不用再管。",
    "zone_area": f"区域没算出面积：能看出它的范围，就在图层「{C.LAYER_HUMAN_ZONE}」（蓝色）沿边界描一圈闭合多段线，并把编号改到「{C.LAYER_YES}」；看不出范围或它不是功能区，就把编号改到「{C.LAYER_NO}」。",
    "category": "功能区类别：把编号改到「CTLE_类别_存储」「CTLE_类别_作业」或「CTLE_类别_其他」（黄色）。",
    "view": "视图：先处理视图编号。同一楼层的几个方案只能选一个「要」；选了「不要」的视图，里面的其他编号都不用再处理。",
}
KIND_STEPS = {"view_count": "view", "zone_category": "category", "zone_no_area": "zone_area", "designated_outline_review": "outline",
              "designated_missing": "outline", "outline_inner_line": "outline", "dimension": "outline", "remeasure": "outline"}


def checklist(items: list[dict[str, Any]], drawing: str) -> list[str]:
    """The list for the CAD colleague: only the steps this drawing needs, views first, plain words."""

    needed = ["yes_no"] + sorted({KIND_STEPS[i["kind"]] for i in items if i["kind"] in KIND_STEPS},
                                 key=["view", "outline", "zone_area", "category"].index)
    if C.AREA_ONLY:
        lines = ["# 复查清单", "", f"图纸：{drawing}", "",
                 "面积、长、宽都是程序按红线算出来的：红线对，数字就对。所以复查只看红线，不用管机器是怎么判断的。", "",
                 f"一、在 AutoCAD 打开 to_confirm.dwg，红线在图层「{OUTLINE_LAYER}」（可以只开这一层和墙线所在的层）。沿红线放大走一圈，看三件事：",
                 "   1. 红线贴的是外墙最外面那条线，不是里侧线或轴线；",
                 "   2. 没多圈进去东西：凹进去的空地、院子、旁边别的小楼不在里面；",
                 "   3. 没漏掉东西：和主楼连着、共用外墙的附属房间包在里面。",
                 "   想再保险，可以用 DIST 量两个外墙角的距离，和结果里的长、宽对一下，或对一下图上标的总尺寸。",
                 "二、紫色编号 C01、C02 … 是机器自己觉得可疑的位置，走一圈时顺便看（可用「查找」搜编号）。", "",
                 "- 都对：直接回复“通过”，并告诉我们看了几分钟。", "- 有不对的：按下面的操作改，存盘发回，并说明其余编号是否通过、用了几分钟。", ""]
    else:
        lines = ["# 待确认清单", "", f"图纸：{drawing}", "", "在 AutoCAD 打开 to_confirm.dwg。紫色文字是要你看的地方，每处有一个编号（C01、C02 …，可用「查找」搜编号）。", ""]
    lines += [f"{n}. {STEP[k]}" for n, k in enumerate(needed, 1)]
    lines += ["", "看不准的编号不要动，保持紫色＝待确认，结果里会如实标出。" + ("（回复“通过”时，没动的编号按机器的结果算。）" if C.AREA_ONLY else ""),
              "改完后直接保存，不要改文件名，把文件发回，并告诉我们：从打开文件到保存一共用了几分钟。", "",
              "| 编号 | 视图 | 建筑/区域 | 问题 | 影响面积（m²） |", "|---|---|---|---|---|"]
    order = items  # already numbered with the views first
    for it in order:
        impact = float(it.get("impact_m2") or 0)
        shown = f"{impact:,.0f}" if impact > 0 else ("—" if it["kind"] == "view_count" else "未算出")
        view = it.get("view") or ("所有视图（同名区域只问一次）" if it["kind"] == "zone_category" else ("—" if it.get("building") else ""))
        lines.append(f"| {it['id']} | {view} | {it.get('building') or it.get('zone') or ''} | {it['question']} | {shown} |")
    return lines


def read_back(dxf: Path, ids: set[str]) -> dict[str, Any]:
    doc = ezdxf.readfile(dxf)
    entities, closed, opened, human_columns, columns, label_by_text = {}, [], [], [], [], {}
    for e in doc.modelspace():
        layer = e.dxf.layer
        if not layer.upper().startswith("CTLE_"):
            continue
        entities[e.dxf.handle] = layer
        kind = e.dxftype()
        if kind == "MTEXT":
            first = e.text.split("\\P")[0].split(" ")[0]
            if first in ids:
                label_by_text[first] = e.dxf.handle
        if kind == "LWPOLYLINE" and layer in (C.LAYER_HUMAN_OUTLINE, C.LAYER_HUMAN_ZONE):
            pts = [list(p[:2]) for p in e.get_points("xy")]
            if e.closed and len(pts) >= 3:
                closed.append({"handle": e.dxf.handle, "layer": layer, "points": pts + [pts[0]]})
            else:
                opened.append({"handle": e.dxf.handle, "layer": layer})
        if kind in {"CIRCLE", "POINT"}:
            p = e.dxf.center if kind == "CIRCLE" else e.dxf.location
            at = [p.x, p.y]
            if layer == C.LAYER_HUMAN_COLUMN:
                human_columns.append(at)
            elif layer == C.COLUMN_LAYER:
                columns.append(at)
    return {"entities": entities, "closed_outlines": closed, "open_outlines": opened, "human_columns": human_columns, "columns": columns,
            "label_by_text": label_by_text}


def apply_cmd(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    issued = json.loads((out / "issued.json").read_text(encoding="utf-8"))
    items = json.loads((out / "items.json").read_text(encoding="utf-8"))
    archive = out / issued["ai_result_archive"]
    if _sha(archive) != issued["ai_result_sha256"]:
        raise SystemExit("the AI result archive changed after the drawing was issued")
    read = json.loads(archive.read_text(encoding="utf-8"))
    if not args.returned_dwg and not args.passed:
        raise SystemExit("give --returned-dwg, or --passed when the reviewer answered 通过 without changing the drawing")
    returned_dwg = Path(args.returned_dwg).resolve() if args.returned_dwg else out / "to_confirm.dwg"  # 通过 alone: the drawing as issued
    copy = out / "returned_copy.dwg"
    shutil.copyfile(returned_dwg, copy)
    from cad_to_layout import cad_runtime

    dxf = cad_runtime.dwg_to_dxf(copy, out / "returned.dxf", args.timeout)
    back = read_back(dxf, {it["id"] for it in items})
    # labels are found by handle; if AutoCAD renumbered handles, by the item number at the start of the label text
    for item_id, handle in list(issued["labels"].items()):
        if handle not in back["entities"] and item_id in back["label_by_text"]:
            new = back["label_by_text"][item_id]
            back["entities"][handle] = back["entities"].pop(new)
    result = C.apply(read, items, issued, back, float(issued["unit_m"]), passed=args.passed)
    result.update({"confirmer": args.confirmer, "minutes_reported": args.minutes, "returned_dwg_sha256": _sha(returned_dwg),
                   "drawing_returned": bool(args.returned_dwg), "overall_pass": bool(args.passed), "marks_shown": [it["id"] for it in items],
                   "ai_result_sha256": issued["ai_result_sha256"], "applied_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                   "items_issued": len(items), "items_handled": sum(1 for d in result["decisions"].values() if d not in {"pending", "deleted"})})
    (out / "final.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 人工确认后的结果", "",
             f"确认人：{args.confirmer or '未记录'}　自报用时：{args.minutes if args.minutes is not None else '未记录'} 分钟　"
             f"编号 {result['items_issued']} 处，已处理 {result['items_handled']} 处，图元改动 {result['operations']} 次（{result['operations_note']}）", "",
             "| 建筑 | 面积 m² | 长 × 宽 m | 柱子 | 轮廓来源 | 柱子来源 | 状态 |", "|---|---|---|---|---|---|---|"]
    if C.AREA_ONLY:
        lines[0] = "# 人工复查后的结果"
        lines[2] = (f"复查人：{args.confirmer or '未记录'}　自报用时：{args.minutes if args.minutes is not None else '未记录'} 分钟　"
                    f"{'整体回复通过' if args.passed else '未整体通过'}；{'交回了改过的图' if args.returned_dwg else '没有改图'}；标出 {len(items)} 处")
        lines[4:6] = ["| 厂房 | 总面积 m² | 长 × 宽 m | 来源 | 状态 |", "|---|---|---|---|---|"]
        for b in result["buildings"]:
            lines.append(f"| {b['name']} | {b['area_m2']:,.0f} | {b.get('length_m', '')} × {b.get('width_m', '')} | {b['outline_source']} | {b['status']} |")
        lines += ["", CAVEAT]
    for b in ([] if C.AREA_ONLY else result["buildings"]):
        lines.append(f"| {b['name']}{'（指定）' if b['designated'] else ''} | {b['area_m2']:,.0f} | {b.get('length_m', '')} × {b.get('width_m', '')} | "
                     f"{b.get('columns', '')} | {b['outline_source']} | {b.get('columns_source') or ''} | {b['status']}"
                     + (f"（{'、'.join(b['pending_items'])}）" if b["pending_items"] else "") + " |")
    if result.get("views"):
        lines += ["", "## 功能区面积（按视图）", "", "| 视图 | 是否计入 | 存储 m² | 作业 m² | 合计 m² | 状态 |", "|---|---|---|---|---|---|"]
        for v in result["views"]:
            lines.append(f"| {v['view']} | {v['counted']} | {v['storage_m2']:,.0f} | {v['operation_m2']:,.0f} | {v['storage_and_operation_m2']:,.0f} | {v['status']}"
                         + (f"（待确认：{'、'.join(map(str, v['pending_zones'][:6]))}）" if v["pending_zones"] else "") + " |")
    if result["problems"]:
        lines += ["", "## 没有采用的人工改动", ""] + [f"- {p.get('item') or p.get('building') or p.get('handle')}：{p['problem']}" for p in result["problems"]]
    if result["unmarked_edits"]:
        lines += ["", "## 人工修改（未标记处）", ""] + [f"- {n}" for n in result["unmarked_edits"]]
    (out / "最终结果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("items_issued", "items_handled", "operations", "pending_items", "unmarked_edits")}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("issue")
    p.add_argument("--work", required=True)
    p.add_argument("--other-work", nargs="*")
    p.add_argument("--source-dxf", required=True)
    p.add_argument("--customer-dwg", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=900.0)
    p = sub.add_parser("apply")
    p.add_argument("--out", required=True)
    p.add_argument("--returned-dwg")
    p.add_argument("--passed", action="store_true", help="the reviewer answered 通过 for the result page")
    p.add_argument("--confirmer")
    p.add_argument("--minutes", type=float)
    p.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    from cad_to_layout import cad_runtime

    try:
        cad_runtime.load()
    except cad_runtime.CadRuntimeError as exc:
        raise SystemExit(str(exc))
    return issue(args) if args.cmd == "issue" else apply_cmd(args)


if __name__ == "__main__":
    raise SystemExit(main())
