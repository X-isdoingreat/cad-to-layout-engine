"""Review package: one command per drawing.

  run:     py -3.11 -X utf8 -m scripts.trial_package run --dwg <customer.dwg> [--designation "厂房名或 坐标 x, y"] --out <dir> [--reads 2]
           copy of the customer DWG -> DXF (AutoCAD 2025) -> the agent reads it N times (Claude Code, Codex or the API, by CTLE_AGENT) ->
           <out>/发给复查人员/ : 结果（待复查）.pdf (the machine's result first, with the few unsure places), to_confirm.dwg,
           复查清单.md, 复查说明.pdf
  finish:  py -3.11 -X utf8 -m scripts.trial_package finish --out <dir> [--returned-dwg <saved DWG>] [--passed] --confirmer NAME --minutes N
           --passed alone: the reviewer answered 通过; with --returned-dwg the person's drawing is read back.
           Writes <out>/result/ : 结果.pdf, final.dwg (result layers), final.json, 最终结果.md

Scope (2026-10-09 / 10-10, target rev 22): the designated building's outline, length, width and total area (and
height when a section gives one); rooms inside it, zones, other buildings and columns are not read.
The customer file is never opened by AutoCAD; every CAD step works on copies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import confirm_cad  # noqa: E402
from scripts.write_agent_result_cad import render, save_dwg, triptych  # noqa: E402

GUIDE_IMAGES = ROOT / "docs/trial_guide"
CATEGORY_CN = {"storage": "存储", "operation": "作业", "production": "生产", "office_living": "办公生活", "other": "其他", "unlabelled": "未标注", "to_confirm": "待确认"}


def to_dxf(dwg: Path, out: Path, timeout: float) -> Path:
    from cad_to_layout import cad_runtime

    copy = out / "source_copy.dwg"
    shutil.copyfile(dwg, copy)
    os.chmod(copy, 0o666)
    return cad_runtime.dwg_to_dxf(copy, out / "source.dxf", timeout)


def run(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    customer = Path(args.dwg).resolve()
    reads = out / "reads"
    # Two readers by different methods (target cross_method_reading_20261023): run1 = wall (traces the outer wall), run2 = structure
    # (axis grid, columns, wall thickness), one after another - never two AI sessions at once - and blind to each other.
    methods = [s.strip() for s in (args.methods or ("wall,structure" if args.reads >= 2 else "wall")).split(",") if s.strip()]
    reuse = args.reuse_reads and all((reads / f"run{n}" / "agent_read.json").is_file() for n in range(1, len(methods) + 1))
    dxf = out / "source.dxf" if reuse else to_dxf(customer, out, args.timeout)
    env = dict(os.environ, CTLE_AGENT=os.environ.get("CTLE_AGENT", "claude"), PYTHONPATH=str(ROOT))  # the AI backend is chosen by the caller
    for n, method in ([] if reuse else list(enumerate(methods, 1))):
        cmd = [sys.executable, "-X", "utf8", "-m", "scripts.run_agent_reads", "--dxf", str(dxf), "--out", str(reads), "--only", str(n), "--method", method, "--no-adjudicate"]
        if args.designation:
            cmd += ["--designation", args.designation]
        with open(out / f"read_{n}.log", "w", encoding="utf-8") as log:
            subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    works = [reads / f"run{n}" for n in range(1, len(methods) + 1) if (reads / f"run{n}" / "agent_read.json").is_file()]
    if not works:
        result_json(out, customer, args.designation, [], None, failed="没有一次读图完成（AI 调用失败或超时），见 read_*.log")
        raise SystemExit(3)
    dropped: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    if not args.skip_adjudication:  # the reviewer AI over both reads (no call when they agree and no drawing number disagrees)
        from cad_to_layout.zero_manual import adjudicator

        status = adjudicator.ensure_pair(works, force=args.re_adjudicate, timeout_seconds=args.timeout * 2)
        print(json.dumps({"status": status.get("status_code"), "decision": status.get("decision"), "source": status.get("source"),
                          "difference_m2": status.get("difference_m2"), "a_valid": status.get("a_valid"), "b_valid": status.get("b_valid")}, ensure_ascii=False), flush=True)
        if status.get("status_code") == "failed":
            result_json(out, customer, args.designation, works, None, failed=status.get("why") or "读不出")
            raise SystemExit(3)
    issue_dir = out / "issue"
    confirm_cad.main(["issue", "--work", str(works[0]), *(["--other-work", *map(str, works[1:])] if works[1:] else []),
                      "--source-dxf", str(dxf), "--customer-dwg", str(customer), "--out", str(issue_dir)])
    send = out / "发给复查人员"
    send.mkdir(exist_ok=True)
    items = json.loads((issue_dir / "items.json").read_text(encoding="utf-8"))
    shutil.copyfile(issue_dir / "to_confirm.dwg", send / "to_confirm.dwg")
    shutil.copyfile(issue_dir / "待确认清单.md", send / "复查清单.md")
    guide(send / "复查说明.pdf", items, customer.name)
    preview = json.loads((issue_dir / "ai_result.json").read_text(encoding="utf-8"))
    read = json.loads((issue_dir / "ai_result_archive.json").read_text(encoding="utf-8"))
    pictures = building_pictures(preview, out / "source_copy.dwg", issue_dir / "to_confirm.dwg", issue_dir, float((read.get("answer") or {}).get("unit_m") or 1.0),
                                 args.timeout, "机器读出的轮廓（红线）和没把握的地方（紫色编号），取自 to_confirm.dwg；本图只是示意，判断对错请在 DWG 里放大看")
    area_report(send / "结果（待复查）.pdf", preview, read, items, pictures, customer.name, reviewed=False)
    (out / "package.json").write_text(json.dumps({"customer_dwg": str(customer), "designation": args.designation, "reads": [str(w) for w in works],
                                                  "reads_not_compared": dropped,
                                                  "issue": str(issue_dir), "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    result_json(out, customer, args.designation, works, preview, send=send)
    print(json.dumps({"send_folder": str(send), "reads": len(works), "status": status.get("status_code")}, ensure_ascii=False))
    return 0


STEP_CN = {"wall": "读图甲（沿外墙描）", "structure": "读图乙（按结构推）"}


def ai_calls(works: list[Path]) -> list[dict[str, Any]]:
    """Every AI call of this drawing's area read with its use and cost: the readers (with their revision rounds) and the reviewer."""

    calls = []
    for w in works:
        path = Path(w) / "agent_read.json"
        if not path.is_file():
            continue
        r = json.loads(path.read_text(encoding="utf-8"))
        calls.append({"step": STEP_CN.get(r.get("method"), "读图"), "method": r.get("method"), "backend": r.get("backend"), "model": r.get("model"), "minutes": r.get("minutes"),
                      "tokens_used": r.get("tokens_used"), "cost_usd_equivalent": r.get("cost_usd_equivalent"), "rounds": 1 + len(r.get("revisions") or []),
                      "valid": r.get("valid"), "returncode": r.get("returncode")})
        adj = r.get("adjudication") or {}
        if adj.get("status") in {"answered", "no_answer"}:
            calls.append({"step": "复查 AI", "backend": adj.get("backend"), "model": adj.get("model"), "minutes": adj.get("minutes"), "tokens_used": adj.get("tokens_used"),
                          "cost_usd_equivalent": adj.get("cost_usd_equivalent"), "tool_calls": adj.get("tool_calls"), "decision": adj.get("decision")})
    return calls


def result_json(out: Path, customer: Path, designation: str | None, works: list[Path], preview: dict[str, Any] | None, send: Path | None = None,
                failed: str | None = None) -> Path:
    """The machine-readable result of the area read (`结果.json`): the building's numbers with their status and source, both methods'
    numbers, the difference between them, every AI call's use and cost.  A failed run says so and carries no numbers."""

    calls = ai_calls(works)
    cost = sum(float(c.get("cost_usd_equivalent") or 0) for c in calls)
    b = next((x for x in (preview or {}).get("buildings") or [] if x.get("designated")), None) if preview else None
    body: dict[str, Any] = {"schema": "ctle-area-result-1.0", "drawing": customer.name, "designation": designation,
                            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "ai_calls": calls, "cost_usd_equivalent": round(cost, 4)}
    if failed or b is None:
        body.update({"status_code": "failed", "status_zh": "读不出", "why": failed or "没有读出指定厂房", "building": None})
    else:
        keys = ("name", "area_m2", "length_m", "width_m", "status_code", "status_zh", "source", "source_zh", "decision", "why", "wrong_if", "undecided_numbers",
                "difference_m2", "difference_region", "drawing_matches", "methods", "risk_review", "designation_note", "outline_source", "human_review")
        body.update({"status_code": b.get("status_code") or "undecided", "status_zh": b.get("status_zh") or "待定", "building": {k: b.get(k) for k in keys},
                     "human_review": {"optional": True, "package": str(send) if send else None}})
    path = out / "结果.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def rank_reads(works: list[Path]) -> tuple[list[Path], list[dict[str, Any]]]:
    """The fullest read first (designated building found, then most zones); a read that gave up on what another read did is not compared."""

    def content(w: Path) -> tuple[int, int]:
        a = json.loads((w / "agent_read.json").read_text(encoding="utf-8")).get("answer") or {}
        found = any(b.get("designated") for b in a.get("buildings") or [])
        return int(found), 0 if C_AREA_ONLY() else len(a.get("zones") or [])

    scored = sorted(works, key=lambda w: content(w), reverse=True)
    best = content(scored[0])
    kept, dropped = [scored[0]], []
    for w in scored[1:]:
        c = content(w)
        if c[0] < best[0] or c[1] < 0.3 * best[1]:
            dropped.append({"read": str(w), "why": f"designated found {bool(c[0])}, zones {c[1]} against {best[1]} in the fullest read: it gave up, not a second opinion"})
        else:
            kept.append(w)
    return kept, dropped


def _matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot

    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    return pyplot


def wrap_keep(text: str, width: int) -> list[str]:
    """Wrap Chinese text without splitting a 「…」 name (a layer name broken over two lines invites typing mistakes)."""

    import re

    pieces = re.findall(r"「[^」]*」|约?\s?\d[\d,.]*\s?m²|[A-Za-z0-9_.%×/²\-–]+|.", text)  # names, numbers and Latin words are never split
    lines, cur = [], ""
    for p in pieces:
        if len(cur) + len(p) > width and cur and p not in CLOSING:  # closing punctuation stays on the line it closes
            lines.append(cur)
            cur = ""
        cur += p
    if cur:
        lines.append(cur)
    return lines


CLOSING = set("，。、；：！？）」』”’》")


def guide(path: Path, items: list[dict[str, Any]], drawing: str = "") -> None:
    """One-page how-to for the CAD colleague, with the close-ups from the recorded demo."""

    from matplotlib import image as mpimg
    from matplotlib.backends.backend_pdf import PdfPages

    pyplot = _matplotlib()
    with PdfPages(path) as pdf:
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.06, 0.955, "复查说明" if C_AREA_ONLY() else "确认操作说明（AutoCAD）", fontsize=17, weight="bold")
        steps = [line for line in confirm_cad.checklist(items, drawing)[4:] if line and not line.startswith("|")]
        import textwrap

        y = 0.915
        for line in steps:
            for k, piece in enumerate(wrap_keep(line, 46) or [""]):
                figure.text(0.06 if k == 0 else 0.085, y, piece, fontsize=10)
                y -= 0.024
            y -= 0.01
        pictures = [("guide_select.png", ""), ("guide_layer.png", "")]  # captions are in the pictures (示例图)
        top = y - 0.01
        if top - 0.31 * len(pictures) < 0.02:  # not enough room left: the pictures go on a page of their own
            pdf.savefig(figure)
            pyplot.close(figure)
            figure = pyplot.figure(figsize=(8.27, 11.69))
            figure.text(0.06, 0.955, "示例图（取自演示录像）", fontsize=13, weight="bold")
            top = 0.93
        for name, caption in pictures:
            p = GUIDE_IMAGES / name
            if p.is_file():
                axis = figure.add_axes([0.06, top - 0.27, 0.88, 0.26])
                axis.imshow(mpimg.imread(p))
                axis.axis("off")
                figure.text(0.06, top - 0.285, caption, fontsize=9.5, color="#4b5563")
                top -= 0.31
        pdf.savefig(figure)
        pyplot.close(figure)


def C_AREA_ONLY() -> bool:
    from cad_to_layout.zero_manual import confirmation

    return confirmation.AREA_ONLY


def building_pictures(result: dict[str, Any], source_dwg: Path, result_dwg: Path, folder: Path, m: float, timeout: float,
                      caption: str) -> list[tuple[Path, str]]:
    """The designated building in the customer copy and in the result drawing, same window."""

    b = next((b for b in result["buildings"] if b.get("designated") and b.get("outline")), None) or next((b for b in result["buildings"] if b.get("outline")), None)
    if not b or not result_dwg.is_file():
        return []
    xs = [q[0] for q in b["outline"]]
    ys = [q[1] for q in b["outline"]]
    pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.2 + 5 / m  # wide enough to see a strip or annex the outline left out
    window = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
    shots = [render(source_dwg, window, folder / "b_source.png", timeout), render(result_dwg, window, folder / "b_overlay.png", timeout)]
    if not all(x["exists"] for x in shots):
        return []
    return [(folder / "b_source.png", "原图（客户图副本）"), (folder / "b_overlay.png", caption)]


SHORT = {
    "designated_outline_review": "整栋轮廓：红线是否沿外墙外边线，没多算空地或别的楼，也没漏掉附跨（不对就重描整栋）",
    "reads_extra": "两次读图不一致：这一块算进了楼，对吗（要＝算；不要＝不算）",
    "reads_missing": "两次读图不一致：这一块没算进楼，对吗（要＝并入；不要＝不并入）",
    "outline_beyond_grid": "这一块在柱网以外，算进了楼，对吗（要＝算；不要＝不算）",
    "outline_face_without_columns": "这一块里没有柱子，算进了楼，对吗（要＝算；不要＝不算）",
    "outline_annex_left_out": "这一块与楼共墙，没算进楼，对吗（要＝并入；不要＝不并入）",
    "outline_inner_line": "这一侧轮廓可能走了里线",
    "dimension": "图上总尺寸与轮廓长度对不上（要＝轮廓对；不对就重描）",
    "stated_area": "图上写的面积与量出的面积对不上（要＝轮廓对；不对就重描）",
    "notches": "轮廓上有多处浅凹口（要＝真的凹进去；不是就沿连续外墙重描）",
    "adjudicated": "定夺复核改判了轮廓（要＝新红线对；不对就重描）",
    "remeasure": "程序重新量的结果与读图报告对不上",
    "designated_missing": "指定的厂房没读出来，需要描出轮廓",
    "height": "读出的高度是不是净高",
}


ZONE_WORDS = ("功能区", "存储", "作业面积", "zones", "zone")  # doubts about zones: not part of the trial
TECHNICAL = re.compile(r"\d{4,}|\de\d|\bF\d+|\bx\b|\by\b")  # drawing coordinates and face ids


def drawing_says_rows(says: dict[str, Any]) -> list[tuple[str, float, str]]:
    """The drawing's own numbers beside the measured ones (2026-10-16: 图上写的 / 量出的 side by side, never merged)."""

    rows = []
    dims = says.get("dimensions") or []
    if dims:
        parts = [f"{g['side']} {' / '.join(f'{v:g}' for v in g['dimensions_m'])} 米" + ("（与轮廓不一致，见编号）" if g.get("flagged") else "") for g in dims]
        rows.append(("图上标注的总尺寸：" + "，".join(parts) + "（轴线或外墙尺寸，以图为准）", 10.5, "normal"))
        if len(dims) == 2:
            rect = dims[0]["dimensions_m"][-1] * dims[1]["dimensions_m"][-1]
            rows.append((f"按图上总尺寸的矩形面积：{rect:,.0f} 平方米（长 × 宽，不扣凹凸，供对照）", 10.5, "normal"))
    for a in says.get("areas") or []:
        rows.append((f"图上写的面积：「{a['text']}」＝ {a['value_m2']:,.2f} 平方米" + (f"（与量出的相差 {a['differs_pct']:+.1f}%，见编号）" if a.get("flagged") else "（与量出的一致）"), 10.5, "normal"))
    if not dims and not says.get("areas"):
        rows.append(("图上没有找到这栋楼的总尺寸标注或面积注记，无法对照", 10.5, "normal"))
    wrapped = []
    for text, size, weight in rows:  # a long line of numbers is wrapped, never cut off at the page edge
        for k, piece in enumerate(wrap_keep(text, 60)):
            wrapped.append((("　　" if k else "") + piece, size, weight))
    return wrapped


def adjudication_rows(read: dict[str, Any], building: str, items: list[dict[str, Any]]) -> list[tuple[str, float, str]]:
    """When the adjudication step replaced this building's numbers: what the AI read first, what it became and why (never silent)."""

    over = next((o for o in (read.get("adjudication") or {}).get("overrides") or [] if o.get("building") == building), None)
    if not over:
        return []
    before, hit = over.get("before") or {}, (over.get("matched") or [{}])[0]
    mark = next((it["id"] for it in items if it["kind"] == "adjudicated" and it.get("building") == building), None)
    text = (f"定夺复核改判：AI 原读出 {float(before.get('area_m2') or 0):,.0f} 平方米（长 {before.get('length_m')} × 宽 {before.get('width_m')} 米），"
            f"上面的数是重量后的轮廓，与图上写的「{hit.get('drawing')}」一致（相差 {float(hit.get('differs_pct') or 0):+.2f}%）"
            + (f"；改判前的轮廓画在编号 {mark} 的紫框里" if mark else ""))
    return [(("　　" if k else "") + piece, 10.5, "normal") for k, piece in enumerate(wrap_keep(text, 60))]


def adjudication_summary(read: dict[str, Any]) -> list[str]:
    """One line on what the second AI did with the discrepancies, or that it was not needed / did not answer."""

    adj = read.get("adjudication") or {}
    status = adj.get("status")
    if status in {None, "no_discrepancy", "no_building"}:
        return [] if status != "no_discrepancy" else ["定夺复核：程序没有发现图上数字与轮廓的矛盾，第二个 AI 没有出场。"]
    if status == "no_answer":
        return ["定夺复核：第二个 AI 没有给出有效回答，矛盾全部交人复查。"]
    verdicts = adj.get("verdicts") or []
    right = sum(1 for v in verdicts if v["verdict"] == "outline_right")
    changed = len(adj.get("overrides") or [])
    wrong_kept = sum(1 for v in verdicts if v["verdict"] == "outline_wrong" and str(v.get("applied") or "").startswith("没有改判"))
    unsure = sum(1 for v in verdicts if v["verdict"] == "cannot_decide")
    cost = f"{int(adj.get('tokens_used') or 0):,} token、{adj.get('minutes')} 分钟、{adj.get('tool_calls')} 次工具调用" + ("，超出上限" if adj.get("over_budget") else "")
    text = (f"定夺复核：第二个 AI 看了 {len(verdicts)} 条矛盾——认为轮廓对 {right}、改判 {changed}（只在重量结果与图上写的数一致时才改）、"
            f"认为错但对不上图上的数没改 {wrong_kept}、没把握交人 {unsure}；{cost}。每条的理由写在上面对应编号下。")
    return wrap_keep(text, 66)


def other_floors(answer: dict[str, Any], building: dict[str, Any]) -> list[str]:
    """Other floors of the building the drawing shows (not measured): the read's own list, or plan views on another floor."""

    if isinstance(answer.get("other_floors"), list):
        return [str(f) for f in answer["other_floors"] if f]
    views = answer.get("views") or []
    mine = next((v for v in views if v.get("name") == building.get("view")), {})
    floor = str(mine.get("floor") or "")
    out = []
    for v in views:
        f = str(v.get("floor") or "")
        role = str(v.get("role") or "").lower()
        if f and floor and f != floor and "plan" in role and not any(w in f + str(v.get("name")) for w in ("屋面", "roof", "总平面")):
            out.append(str(v.get("name")))
    return out


def area_report(path: Path, result: dict[str, Any], read: dict[str, Any], items: list[dict[str, Any]], pictures: list[tuple[Path, str]],
                drawing: str, reviewed: bool) -> None:
    """One page per drawing: the designated building's numbers first, the unsure places, the building in the drawing."""

    from matplotlib import image as mpimg
    from matplotlib.backends.backend_pdf import PdfPages

    pyplot = _matplotlib()
    answer = read.get("answer") or {}
    with PdfPages(path) as pdf:
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.06, 0.957, "厂房面积结果（人工复查后）" if reviewed else "厂房面积结果（待复查）", fontsize=17, weight="bold")
        figure.text(0.06, 0.937, f"图纸：{drawing}", fontsize=9, color="#4b5563")
        if reviewed:
            figure.text(0.06, 0.921, f"复查人：{result.get('confirmer') or '未记录'}　用时：{result.get('minutes_reported') if result.get('minutes_reported') is not None else '未记录'} 分钟　"
                                     f"{'整体回复通过' if result.get('overall_pass') else '未整体通过'}　{'交回了改过的图' if result.get('drawing_returned') else '没有改图'}",
                        fontsize=9, color="#4b5563")
        else:
            figure.text(0.06, 0.921, "数字都是按红线算的：红线对，数字就对。请在 DWG 里沿红线走一圈核对（见「复查说明」），都对就回复“通过”。", fontsize=9, color="#4b5563")
        y = 0.888
        designated = [b for b in result["buildings"] if b.get("designated")]
        if (answer.get("designation") or {}).get("status") in {"not_found", "not_unique"}:
            designated = []  # the designation matched no building or several: no number, only the mark (target rev 24, rule 1)
        if not designated:
            why = "图上没有画出厂房外形，或没有指定要改造的厂房：读不出厂房面积。" if (answer.get("designation") or {}).get("status") in {None, "", "not_given"} else "指定的厂房没有读出来（见下面的编号）。"
            figure.text(0.06, y, why, fontsize=12)
            y -= 0.04
        for b in designated:
            src = next((x for x in answer.get("buildings", []) if x.get("name") == b["name"]), {})
            check = next((c for c in read.get("checks") or [] if c.get("name") == b["name"]), {})
            rows = [(f"{b['name']}", 12.5, "bold"),
                    (f"总面积（占地）：{b['area_m2']:,.0f} 平方米　　长 {b.get('length_m', 0):.1f} 米 × 宽 {b.get('width_m', 0):.1f} 米", 12, "normal"),
                    *drawing_says_rows(check.get("drawing_says") or {}),
                    *adjudication_rows(read, b["name"], items),
                    ("净高：需客户提供（本产品不读高度）", 10.5, "normal"),
                    *([(f"图上有其他楼层，未计入：{'、'.join(floors)}", 10.5, "normal")] if (floors := other_floors(answer, src)) else []),
                    (f"来源：{b['outline_source']}　状态：{b['status']}", 9.5, "normal")]
            for text, size, weight in rows:
                figure.text(0.06, y, text, fontsize=size, weight=weight)
                y -= 0.027
        for piece in wrap_keep("口径：" + confirm_cad.CAVEAT, 66):
            figure.text(0.06, y, piece, fontsize=8, color="#b45309")
            y -= 0.016
        y -= 0.02
        figure.text(0.06, y, f"机器没把握的地方（{len(items)} 处）" if items else "机器没有标出没把握的地方", fontsize=12, weight="bold")
        y -= 0.025
        for it in items:
            what = f"「{it['zone']}」" if it.get("zone") else ""
            impact = float(it.get("impact_m2") or 0)
            said = SHORT.get(it["kind"], it["question"])
            if it["kind"] in {"dimension", "stated_area", "notches", "adjudicated"} and it.get("program_text"):  # the numbers say which side: 图上标注 42.0 m，轮廓 42.6 m
                said = it["program_text"].split("：", 1)[-1].split("（影响")[0] + {"notches": "（要＝真的凹进去；不是就沿连续外墙重描）",
                                                                                 "adjudicated": "（要＝新红线对；不对就重描）"}.get(it["kind"], "（要＝轮廓对；不对就重描）")
            line = f"{it['id']}  {what}{said}" + (f"（影响约 {impact:,.0f} m²）" if impact > 0 else "")
            for k, piece in enumerate(wrap_keep(line, 58)):
                figure.text(0.08 if k == 0 else 0.11, y, piece, fontsize=9.5)
                y -= 0.018
            if it.get("adjudication"):  # the second AI's verdict on this mark, beside it: the reviewer sees what was checked and why
                for k, piece in enumerate(wrap_keep("定夺复核：" + it["adjudication"]["said"], 62)[:5]):
                    figure.text(0.11, y, piece, fontsize=8.5, color="#4b5563")
                    y -= 0.016
        for piece in adjudication_summary(read):
            figure.text(0.06, y, piece, fontsize=8.5, color="#4b5563")
            y -= 0.016
        notes = [u for u in answer.get("unresolved") or [] if not any(w in json.dumps(u, ensure_ascii=False) for w in ZONE_WORDS)
                 and "工具" not in str(u.get("item") or "") and not TECHNICAL.search(str(u.get("item") or ""))
                 and not any(w in str(u.get("item") or "") for w in ("高", "标高", "层数", "楼层"))][:4]  # height is not read; floors have their own line  # tool notes and coordinates mean nothing to the reviewer
        if notes:  # the agent's own doubts, as written during the read: often where to look first
            y -= 0.006
            figure.text(0.06, y, "读图时 AI 自己写下的疑点（供参考，不用回复；有问题会体现在上面的编号或轮廓上）：", fontsize=9, color="#4b5563")
            y -= 0.018
            for u in notes:
                for k, piece in enumerate(wrap_keep(str(u.get("item") or ""), 60)[:2]):
                    figure.text(0.08 if k == 0 else 0.095, y, ("· " if k == 0 else "") + piece, fontsize=8.5, color="#4b5563")
                    y -= 0.016
        y -= 0.01
        if pictures:
            span = (y - 0.02) / len(pictures)
            for k, (pic, caption) in enumerate(pictures):
                top = y - k * span
                figure.text(0.04, top - 0.008, caption, fontsize=8.5)
                axis = figure.add_axes([0.03, top - span + 0.004, 0.94, span - 0.024])
                axis.imshow(mpimg.imread(pic))
                axis.axis("off")
        pdf.savefig(figure)
        pyplot.close(figure)


def final_entities(result: dict[str, Any], read: dict[str, Any], faces: dict[str, Any], m: float) -> list[dict[str, Any]]:
    out = []
    for b in result["buildings"]:
        if not b.get("outline"):
            continue
        xs = [p[0] for p in b["outline"]]
        ys = [p[1] for p in b["outline"]]
        out.append({"layer": "CTLE_最终_厂房", "kind": "outline", "points": b["outline"], "width": 0.6 / m, "record": {"building": b["name"], "source": b["outline_source"]}})
        out.append({"layer": "CTLE_最终_厂房", "kind": "text", "at": [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2],
                    "text": f"{b['name']}\\P{b['area_m2']:.0f} m2\\P{b.get('length_m', 0):.1f} m x {b.get('width_m', 0):.1f} m\\P（{b['outline_source']}）",
                    "height": max(max(xs) - min(xs), max(ys) - min(ys)) / 60, "record": {"building": b["name"]}})
    counted_views = {v["view"] for v in result.get("views", []) if v["counted"] != "不计入"}
    by_name = {(z.get("view"), z.get("name")): z for z in (read.get("answer") or {}).get("zones") or []}
    for z in result.get("zones", []):
        src = by_name.get((z["view"], z["zone"]))
        face = faces.get((src or {}).get("face"))
        if not face or not z["counted"] or z["view"] not in counted_views:
            continue
        layer = {"storage": "CTLE_最终_存储", "operation": "CTLE_最终_作业"}.get(z["category"], "CTLE_最终_其他区域")
        out.append({"layer": layer, "kind": "outline", "points": face["outline"], "record": {"zone": z["zone"], "view": z["view"]}})
        c = face.get("centroid") or face["outline"][0]
        out.append({"layer": layer, "kind": "text", "at": c, "text": f"{z['zone']}\\P{CATEGORY_CN.get(z['category'], z['category'])} {float(z['area_m2'] or 0):.0f} m2",
                    "height": 1.2 / m, "record": {"zone": z["zone"]}})
    return out


def finish(args: argparse.Namespace) -> int:
    from scripts.write_agent_result_cad import write_result_dxf

    out = Path(args.out).resolve()
    package = json.loads((out / "package.json").read_text(encoding="utf-8"))
    issue_dir = Path(package["issue"])
    confirm_cad.main(["apply", "--out", str(issue_dir), *(["--returned-dwg", str(Path(args.returned_dwg).resolve())] if args.returned_dwg else []),
                      *(["--passed"] if args.passed else []),
                      *(["--confirmer", args.confirmer] if args.confirmer else []), *(["--minutes", str(args.minutes)] if args.minutes is not None else [])])
    result = json.loads((issue_dir / "final.json").read_text(encoding="utf-8"))
    read = json.loads((issue_dir / "ai_result_archive.json").read_text(encoding="utf-8"))
    work = Path(package["reads"][0])
    faces = json.loads((work / "faces.json").read_text(encoding="utf-8")) if (work / "faces.json").is_file() else {}
    m = float((read.get("answer") or {}).get("unit_m") or 1.0)
    res = out / "result"  # ASCII: AutoCAD scripts cannot take this path otherwise
    res.mkdir(exist_ok=True)
    layers = {"CTLE_最终_厂房": 1, "CTLE_最终_存储": 3, "CTLE_最终_作业": 4, "CTLE_最终_其他区域": 8}
    final_dxf = res / "final.dxf"
    write_result_dxf(issue_dir / "returned.dxf", final_entities(result, read, faces, m), final_dxf, extra_layers=layers)
    final_dwg = res / "final.dwg"
    final_dwg.unlink(missing_ok=True)
    save_dwg(final_dxf, final_dwg, args.timeout)
    shutil.copyfile(issue_dir / "final.json", res / "final.json")
    shutil.copyfile(issue_dir / "最终结果.md", res / "最终结果.md")
    if C_AREA_ONLY():
        items = json.loads((issue_dir / "items.json").read_text(encoding="utf-8"))
        pictures = building_pictures(result, out / "source_copy.dwg", final_dwg, res, m, args.timeout, "复查后的结果叠加在原图上（取自 final.dwg，红线＝厂房轮廓）")
        area_report(res / "结果.pdf", result, read, items, pictures, Path(package["customer_dwg"]).name, reviewed=True)
        print(json.dumps({"result_folder": str(res)}, ensure_ascii=False))
        return 0
    pictures = []
    designated = next((b for b in result["buildings"] if b.get("outline")), None)
    if designated and final_dwg.is_file():
        source_copy = out / "source_copy.dwg"
        xs = [p[0] for p in designated["outline"]]
        ys = [p[1] for p in designated["outline"]]
        pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.08 + 5 / m
        window = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
        r = [render(source_copy, window, res / "b_source.png", args.timeout), render(final_dwg, window, res / "b_overlay.png", args.timeout)]
        if all(x["exists"] for x in r):
            triptych([res / "b_source.png", res / "b_overlay.png"], ["原图（客户图副本）", "确认后的结果叠加在原图上（取自 final.dwg）"], res / "b_compare.png")
            pictures += [(res / "b_source.png", "原图（客户图副本）"), (res / "b_overlay.png", "确认后的结果叠加在原图上（取自 final.dwg，红线＝厂房轮廓）")]
    report(res / "结果.pdf", result, read, pictures, Path(package["customer_dwg"]).name)
    print(json.dumps({"result_folder": str(res)}, ensure_ascii=False))
    return 0


def report(path: Path, result: dict[str, Any], read: dict[str, Any], pictures: list[Path], drawing: str = "") -> None:
    from matplotlib import image as mpimg
    from matplotlib.backends.backend_pdf import PdfPages

    pyplot = _matplotlib()
    answer = read.get("answer") or {}
    with PdfPages(path) as pdf:
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.06, 0.955, "读图结果（人工确认后）", fontsize=17, weight="bold")
        figure.text(0.06, 0.935, f"图纸：{drawing or Path(str(read.get('dxf') or '')).name}", fontsize=9, color="#4b5563")
        figure.text(0.06, 0.918, f"确认人：{result.get('confirmer') or '未记录'}　"
                                 f"用时：{result.get('minutes_reported') if result.get('minutes_reported') is not None else '未记录'} 分钟　"
                                 f"标出 {result['items_issued']} 处，已处理 {result['items_handled']} 处", fontsize=9, color="#4b5563")
        y = 0.89
        figure.text(0.06, y, "一、指定改造的厂房", fontsize=13, weight="bold")
        y -= 0.035
        if not result["buildings"]:
            figure.text(0.08, y, "未指定改造建筑，或没有读出（见待确认）。", fontsize=10.5)
            y -= 0.03
        for b in result["buildings"]:
            designated = next((x for x in answer.get("buildings", []) if x.get("name") == b["name"]), {})
            height = designated.get("clear_height") or "图上无，需客户提供"
            for line in [f"{b['name']}：占地约 {b['area_m2']:,.0f} 平方米，长 {b.get('length_m', 0):.1f} 米 × 宽 {b.get('width_m', 0):.1f} 米（{b['outline_source']}；{b['status']}）",
                         f"高度：{height}"]:
                figure.text(0.08, y, line, fontsize=10.5, wrap=True)
                y -= 0.03
        figure.text(0.06, y - 0.01, "二、功能区面积（按视图，平方米）", fontsize=13, weight="bold")
        y -= 0.045
        views = result.get("views") or []
        if not views:
            figure.text(0.08, y, "图上无功能区说明。", fontsize=10.5)
            y -= 0.03
        for v in views:
            figure.text(0.08, y, f"{v['view']}（{v['counted']}）：存储 {v['storage_m2']:,.0f}，作业 {v['operation_m2']:,.0f}，合计 {v['storage_and_operation_m2']:,.0f}　{v['status']}",
                        fontsize=10.5, wrap=True)
            y -= 0.03
        y -= 0.01
        figure.text(0.06, y, "说明：类别口径按国家标准 GB 51157 默认（名称写明存储/作业的归类，其余待确认）；不同方案、同一层的重复视图不相加。"
                                     "每个数字的来源：AI 读出 / 人工确认 / 人工修改（未标记处） / 待确认。", fontsize=8.5, color="#4b5563", wrap=True)
        y -= 0.07
        if pictures:
            span = (y - 0.04) / len(pictures)
            for k, (p, caption) in enumerate(pictures):
                top = y - k * span
                figure.text(0.04, top - 0.01, caption, fontsize=9)
                axis = figure.add_axes([0.03, top - span + 0.005, 0.94, span - 0.03])
                axis.imshow(mpimg.imread(p))
                axis.axis("off")
        pdf.savefig(figure)
        pyplot.close(figure)
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.06, 0.955, "功能区明细", fontsize=15, weight="bold")
        y = 0.92
        for z in result.get("zones", [])[:60]:
            figure.text(0.06, y, f"{z['view']}｜{z['zone']}｜{CATEGORY_CN.get(z['category'], z['category'])}｜{float(z['area_m2'] or 0):,.0f} m²｜"
                                 f"{'不计入' if not z['counted'] else ''}{z['source']}", fontsize=8.5)
            y -= 0.0145
        pdf.savefig(figure)
        pyplot.close(figure)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--dwg", required=True)
    p.add_argument("--designation")
    p.add_argument("--out", required=True)
    p.add_argument("--reads", type=int, default=2, help="2 (default): the two methods, wall then structure; 1: the wall reader alone")
    p.add_argument("--methods", help="comma-separated reader methods in run order, e.g. wall,structure (overrides --reads)")
    p.add_argument("--reuse-reads", action="store_true", help="keep finished reads and only rebuild the marks and the package")
    p.add_argument("--skip-adjudication", action="store_true", help="do not run the adjudication step on a read that lacks it (no agent call)")
    p.add_argument("--re-adjudicate", action="store_true", help="run the adjudication step again on the kept read (one agent call)")
    p.add_argument("--timeout", type=float, default=900.0)
    p = sub.add_parser("finish")
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
    return run(args) if args.cmd == "run" else finish(args)


if __name__ == "__main__":
    raise SystemExit(main())
