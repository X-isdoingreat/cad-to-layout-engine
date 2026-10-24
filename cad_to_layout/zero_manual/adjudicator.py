"""Adjudication: a second agent judges the program's discrepancies, the program decides what may change.

2026-10-16: "最终一个定夺的AI是很有必要的…程序算的明显有问题…就直接用新的".  The program lists every
discrepancy mechanically, in three columns (图上写的 / AI 读出的 / 程序复算的); a second agent call sees only
that list and the building's window, may only answer "outline right / outline wrong (with re-measured faces) /
cannot decide", and the program replaces the building's numbers only when its own measurement of those faces
matches a drawing-stated number that was in dispute.  Every verdict and every replacement is shown to the
reviewer with its reason; nothing is hidden by an AI confirmation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import agent_reader, agent_tools

PROMPT_PATH = Path(__file__).with_name("adjudicator_prompt_v3.md")  # v3 (2026-10-16): trace candidates, whole-drawing dimension search, paged lists
PROMPT_ID = "adjudicator_prompt_v3"
ADJUDICATED_KINDS = ("stated_area", "dimension", "notches", "remeasure")
VERDICTS = ("outline_right", "outline_wrong", "cannot_decide")
ADJUDICATOR_MAX_TOOL_CALLS = 12  # the floor of the budget written into the prompt; the CLI has no turn limit, so the count is recorded and reported
# 2026-10-16 ("12 次看不完 16 个凹口"): the budget grows with the work — per discrepancy and per notch to inspect — up to a ceiling
ADJUDICATOR_BUDGET_BASE = 8
ADJUDICATOR_BUDGET_PER_DISCREPANCY = 4
ADJUDICATOR_BUDGET_PER_NOTCH = 1
ADJUDICATOR_BUDGET_MAX = 40
ADJUDICATOR_MODEL = os.environ.get("CTLE_ADJUDICATOR_MODEL", "sonnet")  # same model as the read; opus tried 2026-10-16 (17–38 万 token a call), 2026-10-17: keep sonnet
ADJUDICATOR_OVERLAP_MIN = 0.5  # a re-measured outline must cover this share of the old one: the same building, not another
KIND_CN = {"stated_area": "面积注记", "dimension": "总尺寸", "notches": "浅凹口", "remeasure": "复算不符"}
VERDICT_CN = {"outline_right": "认为轮廓对", "outline_wrong": "认为轮廓错", "cannot_decide": "没把握，交人"}


def _measured(check: dict[str, Any]) -> dict[str, Any]:
    return check.get("with_decided_parts") or check.get("remeasured") or {}


def discrepancies(answer: dict[str, Any], checks: list[dict[str, Any]], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The program's findings that pit a drawing-stated or re-measured number against the read, in three columns."""

    by_name = {b.get("name"): b for b in answer.get("buildings", [])}
    checks_by = {c.get("name"): c for c in checks}
    out = []
    for f in found:
        if f["kind"] not in ADJUDICATED_KINDS:
            continue
        b = by_name.get(f.get("building")) or {}
        c = checks_by.get(f.get("building")) or {}
        d: dict[str, Any] = {"id": f"D{len(out) + 1}", "kind": f["kind"], "building": f.get("building"), "text": f["text"], "at": f.get("at"),
                             "ai_read": f"面积 {float(b.get('area_m2') or 0):,.2f} m²，长 {float(b.get('length_m') or 0):g} m × 宽 {float(b.get('width_m') or 0):g} m"}
        if f["kind"] == "stated_area":
            s = next((s for s in (c.get("stated_area_check") or {}).get("compared", []) if f"「{s['text']}」" in f["text"]), None)
            if s is None:
                continue
            at = s.get("at") or [0, 0]
            d.update({"drawing_says": f"「{s['text']}」= {s['value_m2']:,.2f} m²（文字在 ({at[0]:.0f}, {at[1]:.0f})）", "stated_text": s["text"], "stated_value_m2": s["value_m2"],
                      "program": f"按轮廓量出 {s['measured_m2']:,.2f} m²（相差 {s['differs_pct']:+.2f}%）"})
        elif f["kind"] == "dimension":
            g = next((g for g in (c.get("drawing_says") or {}).get("dimensions", []) if f"图上{g['side']}向" in f["text"]), None)
            if g is None:
                continue
            # where those dimensions are drawn: the reviewer AI must look at them, not search for the number
            spots = [r.get("at") for r in (c.get("dimension_check") or []) if r.get("dimension_m") in g["dimensions_m"] and r.get("at")][:4]
            where = "、".join(f"({x:.0f}, {y:.0f})" for x, y in spots)
            d.update({"drawing_says": f"{g['side']}向端到端总尺寸 {' / '.join(f'{v:g}' for v in g['dimensions_m'])} m" + (f"（标注画在 {where}）" if where else ""),
                      "side": g["side"], "dimensions_m": g["dimensions_m"], "program": f"轮廓{g['side']}向的长度 {g['outline_extent_m']} m"})
        elif f["kind"] == "notches":
            nc = c.get("notch_check") or {}
            spots = [p[0] for p in (nc.get("polygons") or [])][:6]
            d.update({"drawing_says": "—（图上没有可对照的数）",
                      "program": f"轮廓有 {nc.get('notches')} 处向楼内的浅凹口，合计 {nc.get('filled_m2')} m²（占 {100 * float(nc.get('share') or 0):.1f}%）；"
                                 f"凹口位置例如 {'、'.join(f'({x:.0f}, {y:.0f})' for x, y in spots)}"})
        else:  # remeasure
            got = _measured(c)
            d.update({"drawing_says": "—", "program": (f"按所给面重新量：{got.get('area_m2')} m²，长 {got.get('length_m')} m × 宽 {got.get('width_m')} m" if got
                                                      else f"无法重新量（{c.get('reason')}）")})
        out.append(d)
    return out


def tool_budget(checks: list[dict[str, Any]], disc: list[dict[str, Any]]) -> int:
    """Tool calls allowed for the second look: a base, more per discrepancy and per notch to inspect, within a floor and a ceiling."""

    names = {d["building"] for d in disc}
    notches = sum(int((c.get("notch_check") or {}).get("notches") or 0) for c in checks if c.get("name") in names)
    budget = ADJUDICATOR_BUDGET_BASE + ADJUDICATOR_BUDGET_PER_DISCREPANCY * len(disc) + ADJUDICATOR_BUDGET_PER_NOTCH * notches
    return max(ADJUDICATOR_MAX_TOOL_CALLS, min(ADJUDICATOR_BUDGET_MAX, budget))


def _prompt(answer: dict[str, Any], checks: list[dict[str, Any]], disc: list[dict[str, Any]], budget: int | None = None) -> str:
    import sys

    python = '"' + sys.executable + '"'
    budget = budget or tool_budget(checks, disc)
    text = PROMPT_PATH.read_text(encoding="utf-8").replace("{PY}", python).replace("{MAX_TOOL_CALLS}", str(budget))
    by_name = {b.get("name"): b for b in answer.get("buildings", [])}
    lines = ["", "## 你知道的", "", f"- 图纸单位：每图纸单位 {answer.get('unit_m')} 米（--unit-m 用这个值）。"]
    for name in dict.fromkeys(d["building"] for d in disc):
        c = next((c for c in checks if c.get("name") == name), {})
        b = by_name.get(name) or {}
        got = _measured(c)
        if c.get("outline"):
            xs = [p[0] for p in c["outline"]]
            ys = [p[1] for p in c["outline"]]
            pad = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys))
            window = f"{min(xs) - pad:.0f} {min(ys) - pad:.0f} {max(xs) + pad:.0f} {max(ys) + pad:.0f}"
        else:
            window = "未知"
        lines.append(f"- 这栋楼「{name}」：当前轮廓来自面 {' '.join(b.get('faces') or [])}（render 时加 --faces 可画出）；程序按轮廓量出面积 "
                     f"{float(got.get('area_m2') or 0):,.2f} m²，长 {got.get('length_m')} m × 宽 {got.get('width_m')} m；轮廓外包窗口（图纸坐标，已外扩 10%）：{window}。")
    lines += ["", "## 矛盾清单（图上写的 / AI 读出的 / 程序复算的）", ""]
    for d in disc:
        lines.append(f"{d['id']} [{KIND_CN[d['kind']]}] 楼「{d['building']}」：图上写的：{d['drawing_says']}；AI 读出的：{d['ai_read']}；程序复算的：{d['program']}。")
    return text + "\n".join(lines) + "\n"


def validate_verdicts(parsed: dict[str, Any] | None, disc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One verdict per discrepancy, in the discrepancy order; anything not answered or not in the three answers is 'cannot_decide'."""

    given = {}
    for v in (parsed or {}).get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("discrepancy"), str):
            given.setdefault(v["discrepancy"], v)
    out = []
    for d in disc:
        v = given.get(d["id"]) or {}
        verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "cannot_decide"
        why = str(v.get("why") or "").strip()
        if not v:
            why = "定夺没有回答这一条"
        elif v.get("verdict") not in VERDICTS:
            why = f"定夺的回答不在三种之内（{v.get('verdict')}）：" + why
        faces = [f for f in (v.get("faces") or []) if isinstance(f, str)] if verdict != "outline_right" else []  # a cannot_decide may name a candidate
        evidence = [str(e) for e in (v.get("evidence") or [])][:12]
        out.append({"discrepancy": d["id"], "kind": d["kind"], "building": d["building"], "text": d["text"], "verdict": verdict, "why": why,
                    "faces": faces, "measured_by_agent": v.get("measured") if verdict == "outline_wrong" else None, "evidence": evidence, "applied": None,
                    "candidate": None})
    return out


def _candidate(faces_cited: list[str], faces: dict[str, Any], m: float, disc_b: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A face the adjudicator drew but that is not adopted: measured by the program and drawn for the reviewer, with its distance to the drawing's numbers."""

    ids = [f for f in faces_cited if f in faces]
    if not ids or len(ids) != len(faces_cited) or not m:
        return None
    got = agent_tools.measure_faces(ids, faces, m)
    if "error" in got:
        return None
    against = []
    for d in disc_b:
        if d["kind"] == "stated_area":
            against.append(f"与图上写的 {d['stated_value_m2']:,.2f} m² 相差 {100 * (got['area_m2'] - d['stated_value_m2']) / d['stated_value_m2']:+.2f}%")
        elif d["kind"] == "dimension":
            mine = got["length_m"] if d["side"] == "长" else got["width_m"]
            against.append(f"{d['side']}向 {mine} m，图上总尺寸 {' / '.join(f'{v:g}' for v in d['dimensions_m'])} m")
    return {"faces": ids, "area_m2": got["area_m2"], "length_m": got["length_m"], "width_m": got["width_m"], "outline": got["outline"],
            "against_drawing": against}


def _matches(got: dict[str, Any], disc_b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which drawing-stated numbers in dispute the re-measurement agrees with (same tolerances as the checks)."""

    hits = []
    for d in disc_b:
        if d["kind"] == "stated_area":
            v = float(d["stated_value_m2"])
            if abs(got["area_m2"] - v) <= agent_tools.STATED_AREA_TOLERANCE_FRACTION * v:
                hits.append({"discrepancy": d["id"], "kind": "stated_area", "drawing": d["stated_text"], "drawing_value": v, "new_value": got["area_m2"],
                             "differs_pct": round(100 * (got["area_m2"] - v) / v, 2)})
        elif d["kind"] == "dimension":
            mine = got["length_m"] if d["side"] == "长" else got["width_m"]
            for v in d["dimensions_m"]:
                if abs(mine - v) <= max(agent_tools.DIMENSION_TOLERANCE_FRACTION * v, agent_tools.DIMENSION_WALL_ALLOWANCE_M):
                    hits.append({"discrepancy": d["id"], "kind": "dimension", "drawing": f"{d['side']}向总尺寸 {v:g} m", "drawing_value": v, "new_value": mine,
                                 "differs_pct": round(100 * (mine - v) / v, 2)})
                    break
    return hits


def apply_verdicts(work: Path, answer: dict[str, Any], checks: list[dict[str, Any]], record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace a building's numbers only when the program's own measurement of the agent's faces matches a disputed drawing number."""

    from shapely.geometry import Polygon

    faces_path = work / agent_tools.FACES.name
    faces = json.loads(faces_path.read_text(encoding="utf-8")) if faces_path.is_file() else {}
    m = float(answer.get("unit_m") or 0)
    disc, verdicts = record["discrepancies"], record["verdicts"]
    overrides, refused = [], []
    new_answer = copy.deepcopy(answer)
    for name in dict.fromkeys(d["building"] for d in disc):
        check = next((c for c in checks if c.get("name") == name), {})
        old = Polygon(check["outline"]).buffer(0) if check.get("outline") else None
        disc_b = [d for d in disc if d["building"] == name]
        building = next((b for b in new_answer.get("buildings", []) if b.get("name") == name), None)
        for v in [v for v in verdicts if v["building"] == name and v["verdict"] == "outline_wrong"]:
            ids = [f for f in v["faces"] if f in faces]
            if not ids or len(ids) != len(v["faces"]) or not m or old is None or building is None:
                v["applied"] = "没有改判：给出的面编号不存在，或原轮廓/单位缺失"
                refused.append({"discrepancy": v["discrepancy"], "why": v["applied"]})
                continue
            got = agent_tools.measure_faces(ids, faces, m)
            if "error" in got:
                v["applied"] = f"没有改判：{got['error']}"
                refused.append({"discrepancy": v["discrepancy"], "why": v["applied"]})
                continue
            new = Polygon(got["outline"]).buffer(0)
            overlap = new.intersection(old).area / max(old.area, 1e-12)
            numbers = {k: got[k] for k in ("area_m2", "length_m", "width_m", "orientation_deg")}
            if overlap < ADJUDICATOR_OVERLAP_MIN:
                v["applied"] = f"没有改判：重量的轮廓只盖住原轮廓的 {100 * overlap:.0f}%，不是同一栋楼"
                refused.append({"discrepancy": v["discrepancy"], "why": v["applied"], "measured": numbers})
                continue
            matched = _matches(got, disc_b)
            if not matched:
                v["applied"] = (f"没有改判：重量得 {got['area_m2']:,.2f} m²，长 {got['length_m']} m × 宽 {got['width_m']} m，与图上写的数仍对不上")
                refused.append({"discrepancy": v["discrepancy"], "why": v["applied"], "measured": numbers})
                continue
            before = {k: _measured(check).get(k) for k in ("area_m2", "length_m", "width_m")}
            v["applied"] = "改判采用：重量结果与图上写的数一致"
            overrides.append({"building": name, "discrepancy": v["discrepancy"], "faces_before": list(building.get("faces") or []), "faces_after": ids,
                              "before": before, "after": numbers, "matched": matched, "overlap_with_old": round(overlap, 3),
                              "outline_before": check["outline"]})
            building.update({"faces": ids, "area_m2": got["area_m2"], "length_m": got["length_m"], "width_m": got["width_m"], "orientation_deg": got["orientation_deg"],
                             "adjudicated": {"from_faces": overrides[-1]["faces_before"], "before": before, "matched": matched}})
            break
    for v in verdicts:
        if v["applied"] is None:
            v["applied"] = "不改数字" if v["verdict"] != "outline_wrong" else "没有改判：这栋楼已按另一条改判"
        if v["faces"] and not v["applied"].startswith("改判采用"):  # drawn but not adopted: shown to the reviewer as a candidate, never used
            v["candidate"] = _candidate(v["faces"], faces, m, [d for d in disc if d["building"] == v["building"]])
    record.update({"overrides": overrides, "refused": refused})
    return new_answer, record


def run(work: Path, answer: dict[str, Any], checks: list[dict[str, Any]], found: list[dict[str, Any]], timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """The agent call on the discrepancy list; no discrepancy, no call."""

    disc = discrepancies(answer, checks, found)
    budget = tool_budget(checks, disc)
    record: dict[str, Any] = {"prompt_id": PROMPT_ID, "prompt_sha256": hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(), "discrepancies": disc,
                              "max_tool_calls": budget}
    if not disc:
        record.update({"status": "no_discrepancy", "verdicts": [], "overrides": [], "refused": []})
        return record
    prompt = _prompt(answer, checks, disc, budget)
    done = agent_reader.run_prompt(work, prompt, tag="_adjudicate", timeout_seconds=timeout_seconds, answer_key="verdicts", model=ADJUDICATOR_MODEL)
    record.update({"status": "answered" if done["answer"] else "no_answer", "verdicts": validate_verdicts(done["answer"], disc),
                   "minutes": done["minutes"], "tokens_used": done["tokens_used"], "tool_calls": done["tool_calls"],
                   "over_budget": (done["tool_calls"] or 0) > budget, "cost_usd_equivalent": done.get("cost_usd_equivalent"),
                   "backend": agent_reader.AGENT_BACKEND, "model": done.get("model"),
                   "notes": str((done["answer"] or {}).get("notes") or "")})
    return record


def adjudicate_read(work: Path, read: dict[str, Any], timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """Run the step on a finished read: the answer, checks and remaining feedback are updated only when a number was replaced."""

    answer = read.get("answer") or {}
    if not answer.get("buildings"):
        read["adjudication"] = {"status": "no_building", "discrepancies": [], "verdicts": [], "overrides": [], "refused": []}
        return read
    # the checks are recomputed when the index is there: they are deterministic, and a read saved before a check
    # existed (2026-10-16: area notes, totals, notches) would otherwise show no discrepancy
    checks = agent_reader.verify(work, answer) if (work / agent_tools.INDEX.name).is_file() else (read.get("checks") or [])
    read["checks"] = checks
    found = agent_reader.findings(work, answer, checks)
    record = run(work, answer, checks, found, timeout_seconds)
    if record["status"] == "answered":
        new_answer, record = apply_verdicts(work, answer, checks, record)
        if record["overrides"]:
            read["answer_before_adjudication"] = answer
            read["answer"] = new_answer
            read["checks"] = agent_reader.verify(work, new_answer)
            read["remaining_feedback"] = agent_reader.feedback(work, new_answer, read["checks"])
    read["adjudication"] = record
    return read


def ensure(work: Path, force: bool = False, timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """Adjudicate a saved read (agent_read.json) that has not been adjudicated yet; the file is rewritten."""

    path = Path(work) / "agent_read.json"
    read = json.loads(path.read_text(encoding="utf-8"))
    if "adjudication" in read and not force:
        return read["adjudication"]
    if force:
        if read.get("answer_before_adjudication"):
            read["answer"] = read.pop("answer_before_adjudication")
            read["checks"] = agent_reader.verify(Path(work), read["answer"])
        read.pop("adjudication", None)
    read = adjudicate_read(Path(work), read, timeout_seconds)
    path.write_text(json.dumps(read, ensure_ascii=False, indent=1), encoding="utf-8")
    return read["adjudication"]


# ---------------------------------------------------------------- two readers by different methods, one reviewer (2026-10-23)
# Target cross_method_reading_20261023: reader A traces the outer wall, reader B infers the envelope from the axis grid and the
# columns, blind to A.  The program compares the two, measures its own axis/wall geometry round the building, computes where the
# two outlines differ, and only then asks the reviewer AI - which may adopt A, adopt B, draw its own face (adopted only when the
# program's re-measurement matches a drawing-stated number) or leave it undecided (both numbers shown).  The result carries a
# machine-readable status: matches_drawing / two_methods_agree / undecided / failed, plus the source of the adopted number.
PAIR_PROMPT_PATH = Path(__file__).with_name("adjudicator_prompt_v4.md")
PAIR_PROMPT_ID = "adjudicator_prompt_v4"
DECISIONS = ("adopt_a", "adopt_b", "both_wrong", "undecided")
STATUS_CN = {"matches_drawing": "与图上数字一致", "two_methods_agree": "两种方法一致", "undecided": "待定", "failed": "读不出"}
SOURCE_CN = {"a": "甲（沿外墙描）", "b": "乙（按结构推）", "reviewer": "复查 AI 重描"}
METHOD_CN = {"wall": "沿外墙描", "structure": "按轴网、柱网推"}


def _designated_of(read: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The designated building and its check row of a valid read that measured an outline; else None."""

    if not read or not read.get("valid", bool((read.get("answer") or {}).get("buildings"))):
        return None
    answer = read.get("answer") or {}
    b = next((b for b in answer.get("buildings") or [] if b.get("designated")), None)
    c = next((c for c in read.get("checks") or [] if b and c.get("name") == b.get("name") and c.get("outline")), None)
    return (b, c) if b and c else None


_POINT = re.compile(r"(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)")


def designation_point(text: str | None) -> tuple[float, float] | None:
    """The drawing coordinates named in the designation ("图纸坐标 (515010, 261242) 处的那栋"), if any."""

    hit = _POINT.search(text or "")
    return (float(hit.group(1)), float(hit.group(2))) if hit else None


def designation_on_wall(text: str | None, outlines: dict[str, Any], m: float) -> dict[str, Any] | None:
    """The designated point lying on a reader's outline edge (within `DESIGNATION_ON_WALL_M`).

    A point on a wall two buildings or two spans share does not say which side was meant (2026-10-24: the point sat on the
    partition between the north and south spans; one reader took the north span, the other the whole hall).  Noted on the
    result; when the two readers differ, the state stays undecided with both numbers instead of a reviewer's choice.
    """

    from .constants import DESIGNATION_ON_WALL_M

    pt = designation_point(text)
    if pt is None:
        return None
    from shapely.geometry import Point, Polygon

    p = Point(pt)
    dist = {k: round(Polygon(o).exterior.distance(p) * m, 2) for k, o in outlines.items() if o and len(o) >= 4}
    on = {k: d for k, d in dist.items() if d <= DESIGNATION_ON_WALL_M}
    if not on:
        return None
    where = "、".join(f"{SOURCE_CN[k]}的轮廓边线 {d:g} m" for k, d in on.items())
    return {"point": list(pt), "distance_m": dist, "threshold_m": DESIGNATION_ON_WALL_M,
            "message_zh": f"指定点 ({pt[0]:g}, {pt[1]:g}) 距{where}，落在墙上；这道墙两侧哪一边才是指定的楼，指定里没有说明"}


def numbers_agree(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Area, length and width all within TWO_METHOD_AGREE_FRACTION of the larger value."""

    from .constants import TWO_METHOD_AGREE_FRACTION

    for k in ("area_m2", "length_m", "width_m"):
        x, y = float(a.get(k) or 0), float(b.get(k) or 0)
        if abs(x - y) > TWO_METHOD_AGREE_FRACTION * max(x, y, 1e-9):
            return False
    return True


def difference_region(outline_a: list[Any], outline_b: list[Any], m: float) -> dict[str, Any]:
    """Where the two outlines differ: the pieces of their symmetric difference, largest first, each with its side, area and place."""

    from shapely.geometry import Polygon

    pa, pb = Polygon(outline_a).buffer(0), Polygon(outline_b).buffer(0)
    pieces = []
    for only_in, geom in (("a", pa.difference(pb)), ("b", pb.difference(pa))):
        for g in getattr(geom, "geoms", [geom]):
            if g.is_empty or g.geom_type != "Polygon":
                continue
            x0, y0, x1, y1 = g.bounds
            pieces.append({"only_in": only_in, "area_m2": round(g.area * m * m, 1), "at": [round(g.representative_point().x, 3), round(g.representative_point().y, 3)],
                           "bbox": [round(v, 3) for v in (x0, y0, x1, y1)], "outline": [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords]})
    pieces.sort(key=lambda p: -p["area_m2"])
    in_a = round(sum(p["area_m2"] for p in pieces if p["only_in"] == "a"), 1)
    in_b = round(sum(p["area_m2"] for p in pieces if p["only_in"] == "b"), 1)
    words = "；".join(f"{'甲有乙无' if p['only_in'] == 'a' else '乙有甲无'}的一块约 {p['area_m2']:,.0f} m²，在 ({p['at'][0]:.0f}, {p['at'][1]:.0f}) 附近" for p in pieces[:4])
    return {"difference_m2": round(in_a + in_b, 1), "in_a_not_b_m2": in_a, "in_b_not_a_m2": in_b, "pieces": pieces[:8], "description": words or "两条轮廓重合"}


def import_face(work: Path, outline: list[Any], m: float, made_by: str) -> str:
    """Store another read's outline as a face in this work folder, so the reviewer can render and measure it here."""

    from shapely.geometry import Polygon

    path = work / agent_tools.FACES.name
    faces = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    poly = Polygon(outline).buffer(0)
    long_side, short_side, angle = agent_tools._mrr(poly)
    fid = f"F{len(faces) + 1}"
    faces[fid] = {"area_m2": round(poly.area * m * m, 2), "length_m": round(long_side * m, 2), "width_m": round(short_side * m, 2), "orientation_deg": round(angle, 2),
                  "centroid": [round(poly.centroid.x, 3), round(poly.centroid.y, 3)], "texts_inside": [], "outline": [[round(x, 3), round(y, 3)] for x, y in poly.exterior.coords],
                  "layers_used": [], "made_by": made_by}
    path.write_text(json.dumps(faces, ensure_ascii=False), encoding="utf-8")
    return fid


def drawing_matches(check: dict[str, Any]) -> list[dict[str, Any]]:
    """Drawing-stated numbers the measured outline agrees with: an area note within tolerance, an end-to-end dimension within tolerance."""

    hits = []
    for s in (check.get("stated_area_check") or {}).get("compared") or []:
        if not s.get("flagged"):
            hits.append({"kind": "stated_area", "drawing": s.get("text"), "drawing_value": s.get("value_m2"), "differs_pct": s.get("differs_pct")})
    for d in check.get("dimension_check") or []:
        if not d.get("flagged") and d.get("dimension_m"):
            hits.append({"kind": "dimension", "drawing": f"总尺寸 {d['dimension_m']:g} m", "drawing_value": d["dimension_m"], "differs_pct": d.get("differs_pct")})
    return hits


def program_geometry(work: Path, dxf: str | None, outlines: list[list[Any]], m: float) -> dict[str, Any]:
    """The program's own axis/wall measurement round the building (cached in the work folder; no AI)."""

    cache = work / "program_geometry.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    if not dxf or not Path(dxf).is_file():
        return {"status": "no_dxf", "message_zh": "没有 DXF，程序几何未算"}
    try:
        import ezdxf

        from . import warehouse_geometry as wg

        doc = ezdxf.readfile(dxf)
        out = wg.measure_building(wg.collect_structure(doc), outlines, m, [])
        out["status"] = "measured"
    except Exception as exc:  # the reference is optional: a drawing the structure walker cannot read leaves it out, visibly
        out = {"status": "error", "message_zh": f"程序几何未算：{type(exc).__name__}: {str(exc)[:200]}"}
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _geometry_words(g: dict[str, Any]) -> str:
    if g.get("status") != "measured":
        return g.get("message_zh") or "程序几何未算"
    parts = []
    ax, wall = g.get("axis") or {}, g.get("wall") or {}
    parts.append(f"轴网：{'首末轴距 ' + str(ax.get('length_m')) + ' m × ' + str(ax.get('width_m')) + ' m' if ax.get('status') == 'found' else (ax.get('message_zh') or ax.get('status'))}")
    parts.append(f"墙线闭合外廓：{str(wall.get('length_m')) + ' m × ' + str(wall.get('width_m')) + ' m，' + str(wall.get('area_m2')) + ' m²' if wall.get('status') == 'found' else (wall.get('message_zh') or wall.get('status'))}")
    return "；".join(parts)


def _risk_words(answer: dict[str, Any]) -> str:
    rows = [r for r in answer.get("risk_review") or [] if isinstance(r, dict)]
    if not rows:
        return "（没有写 risk_review）"
    return "\n".join(f"    - {r.get('id')}：{'查过' if r.get('checked') else '没查'}；{str(r.get('what_seen') or '')[:160]}；若错影响约 {r.get('if_wrong_effect_m2') or 0} m²" for r in rows)


def _reader_words(label: str, read: dict[str, Any], b: dict[str, Any], c: dict[str, Any], face_ids: list[str]) -> list[str]:
    answer = read.get("answer") or {}
    got = _measured(c)
    lines = [f"- {label}（方法：{METHOD_CN.get(read.get('method'), read.get('method'))}）：面 {' '.join(face_ids)}；面积 {float(got.get('area_m2') or 0):,.2f} m²，"
             f"长 {got.get('length_m')} m × 宽 {got.get('width_m')} m。理由：{str(b.get('evidence') or '')[:400]}"]
    if answer.get("structure"):
        lines.append(f"  结构推算：{json.dumps(answer['structure'], ensure_ascii=False)[:300]}")
    if answer.get("method_available") is False:
        lines.append("  （这张图上该方法不可用，退回沿外墙描）")
    for u in (answer.get("unresolved") or [])[:5]:
        lines.append(f"  拿不准：{str(u.get('item') or '')[:80]}——{str(u.get('why') or '')[:160]}")
    lines.append("  容易出错的地方的核查：")
    lines.append(_risk_words(answer))
    return lines


def pair_discrepancies(read_a: dict[str, Any], read_b: dict[str, Any] | None, pair: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A's program discrepancies, then B's (renumbered), then the two-method difference when the readers disagree."""

    out = []
    for label, read in (("甲", read_a), ("乙", read_b)):
        if not read or not (read.get("answer") or {}).get("buildings"):
            continue
        answer, checks = read["answer"], read.get("checks") or []
        for d in discrepancies(answer, checks, agent_reader.findings(Path(read.get("_work") or "."), answer, checks) if read.get("_found") is None else read["_found"]):
            d = dict(d, id=f"D{len(out) + 1}", reader=label, text=f"{label}的轮廓：" + d["text"])
            out.append(d)
    if pair and pair.get("difference_m2"):
        a, b = pair["a"], pair["b"]
        out.append({"id": f"D{len(out) + 1}", "kind": "two_methods", "building": pair["building"], "reader": "甲乙",
                    "text": f"两种方法的结果不一致：甲 {a['area_m2']:,.2f} m²（{a['length_m']} × {a['width_m']} m），乙 {b['area_m2']:,.2f} m²（{b['length_m']} × {b['width_m']} m）；{pair['region']['description']}。",
                    "at": (pair["region"]["pieces"] or [{}])[0].get("at"), "drawing_says": "—（两种读法之差，不是图上的数）",
                    "ai_read": f"甲 {a['area_m2']:,.2f} m² / 乙 {b['area_m2']:,.2f} m²", "program": f"差异区域合计 {pair['difference_m2']:,.0f} m²：{pair['region']['description']}"})
    return out


def _pair_prompt(read_a: dict[str, Any], read_b: dict[str, Any] | None, a: tuple | None, b: tuple | None, b_face: str | None, disc: list[dict[str, Any]],
                 geometry: dict[str, Any], region: dict[str, Any] | None, budget: int) -> str:
    import sys

    python = '"' + sys.executable + '"'
    text = (PAIR_PROMPT_PATH.read_text(encoding="utf-8").replace("{PY}", python).replace("{MAX_TOOL_CALLS}", str(budget))
            .replace("{RISK_CHECKLIST}", agent_reader.RISK_CHECKLIST_PATH.read_text(encoding="utf-8").strip()))
    m = (read_a.get("answer") or {}).get("unit_m") or (read_b or {}).get("answer", {}).get("unit_m")
    lines = ["", "## 你知道的", "", f"- 图纸单位：每图纸单位 {m} 米（--unit-m 用这个值）。"]
    outlines = [c["outline"] for pair in (a, b) if pair for c in [pair[1]]]
    if outlines:
        xs = [p[0] for o in outlines for p in o]
        ys = [p[1] for o in outlines for p in o]
        pad = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys))
        lines.append(f"- 这栋楼的外包窗口（图纸坐标，已外扩 10%）：{min(xs) - pad:.0f} {min(ys) - pad:.0f} {max(xs) + pad:.0f} {max(ys) + pad:.0f}")
    if a:
        lines += _reader_words("甲", read_a, a[0], a[1], list(a[0].get("faces") or []))
    else:
        lines.append("- 甲：没有有效结果（没读出指定厂房，或没答完易错清单）。")
    if b and read_b:
        lines += _reader_words("乙", read_b, b[0], b[1], [b_face] if b_face else [])
    else:
        lines.append("- 乙：没有有效结果（没读出指定厂房，或没答完易错清单）。")
    lines.append(f"- 程序按图层自己算的（不经 AI）：{_geometry_words(geometry)}")
    if region:
        lines.append(f"- 两条轮廓的差异区域（程序算的）：{region['description']}；合计 {region['difference_m2']:,.0f} m²。")
    lines += ["", "## 矛盾清单（图上写的 / AI 读出的 / 程序复算的）", ""]
    if not disc:
        lines.append("（程序没有发现与图上数字的矛盾）")
    for d in disc:
        lines.append(f"{d['id']} [{KIND_CN.get(d['kind'], d['kind'])}] 楼「{d['building']}」：图上写的：{d['drawing_says']}；AI 读出的：{d['ai_read']}；程序复算的：{d['program']}。")
    return text + "\n".join(lines) + "\n"


def validate_decision(parsed: dict[str, Any] | None, disc: list[dict[str, Any]], a_ok: bool, b_ok: bool) -> dict[str, Any]:
    """The reviewer's decision, forced into the four answers; a side that has no valid read cannot be adopted."""

    p = parsed or {}
    decision = p.get("decision") if p.get("decision") in DECISIONS else "undecided"
    why = str(p.get("why") or "").strip()
    if not parsed:
        why = "复查没有回答"
    elif p.get("decision") not in DECISIONS:
        why = f"复查的回答不在四种之内（{p.get('decision')}）：" + why
    if (decision == "adopt_a" and not a_ok) or (decision == "adopt_b" and not b_ok):
        why = f"复查想采用{'甲' if decision == 'adopt_a' else '乙'}，但该读图没有有效结果：" + why
        decision = "undecided"
    return {"decision": decision, "why": why, "faces": [f for f in (p.get("faces") or []) if isinstance(f, str)] if decision == "both_wrong" else [],
            "measured_by_agent": p.get("measured") if decision == "both_wrong" else None, "evidence": [str(e) for e in (p.get("evidence") or [])][:16],
            "wrong_if": str(p.get("wrong_if") or "")[:300], "verdicts": validate_verdicts(p, disc), "notes": str(p.get("notes") or "")[:600]}


KIND_CN["two_methods"] = "两法不一致"


def adjudicate_pair(work_a: Path, read_a: dict[str, Any], read_b: dict[str, Any] | None, timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """Compare reader A (work_a) with reader B, ask the reviewer when they differ or a drawing number disagrees, and write the result state into read_a.

    read_a comes back as the final read: `answer`/`checks` replaced when B's or the reviewer's outline is adopted (the old answer kept
    as `answer_before_adjudication`), `adjudication` with the whole record, and `result_status` with the machine-readable state."""

    import copy as _copy

    from .constants import TWO_METHOD_AGREE_FRACTION

    work_a = Path(work_a)
    m = float((read_a.get("answer") or {}).get("unit_m") or ((read_b or {}).get("answer") or {}).get("unit_m") or 0)
    if (work_a / agent_tools.FACES.name).is_file() and (read_a.get("answer") or {}).get("buildings"):
        read_a["checks"] = agent_reader.verify(work_a, read_a["answer"])  # deterministic; the drawing checks need the index, the measurement only the faces
    a, b = _designated_of(read_a), _designated_of(read_b)
    record: dict[str, Any] = {"prompt_id": PAIR_PROMPT_ID, "prompt_sha256": hashlib.sha256(PAIR_PROMPT_PATH.read_bytes()).hexdigest(), "kind": "pair",
                              "a_valid": bool(a), "b_valid": bool(b), "b_method_available": bool(((read_b or {}).get("answer") or {}).get("method_available", True)) if read_b else False}
    summary = lambda pair: ({"area_m2": _measured(pair[1]).get("area_m2"), "length_m": _measured(pair[1]).get("length_m"), "width_m": _measured(pair[1]).get("width_m"),
                             "outline": pair[1].get("outline"), "name": pair[0].get("name")} if pair else None)
    status: dict[str, Any] = {"a": summary(a), "b": summary(b), "a_method": read_a.get("method", "wall"), "b_method": (read_b or {}).get("method"),
                              "a_valid": bool(a), "b_valid": bool(b), "agree_tolerance": TWO_METHOD_AGREE_FRACTION}
    if not a and not b:
        record.update({"status": "no_building", "discrepancies": [], "verdicts": [], "overrides": [], "refused": [], "decision": None})
        status.update({"status_code": "failed", "status_zh": STATUS_CN["failed"], "source": None, "source_zh": None,
                       "why": "两种读法都没有读出指定厂房，或没有答完易错清单", "difference_m2": None, "difference_region": None, "drawing_matches": []})
        read_a.update({"adjudication": record, "result_status": status})
        return read_a
    if not a and b and read_b:  # only B read the building: B's answer becomes this folder's answer (its face imported here)
        read_a["answer_before_adjudication"] = read_a.get("answer")
        fid = import_face(work_a, b[1]["outline"], m, "imported from reader B (structure)")
        new = _copy.deepcopy(read_b["answer"])
        for bb in new.get("buildings") or []:
            if bb.get("designated"):
                bb["faces"] = [fid]
        read_a["answer"] = new
        read_a["checks"] = agent_reader.verify(work_a, new)
        a, b_face = _designated_of(dict(read_a, valid=True)), fid
        only = "b"
    else:
        b_face = import_face(work_a, b[1]["outline"], m, "imported from reader B (structure)") if b else None
        only = None if b else "a"
    region = difference_region(a[1]["outline"], b[1]["outline"], m) if (a and b and only is None) else None
    agree = bool(a and b and only is None and record["b_method_available"] and numbers_agree(_measured(a[1]), _measured(b[1])))
    on_wall = designation_on_wall(read_a.get("designation_input") or (read_b or {}).get("designation_input"),
                                  {"a": a[1]["outline"] if a else None, "b": b[1]["outline"] if (b and only is None) else None}, m)
    record["designation_on_wall"] = on_wall
    pair_info = {"building": a[0].get("name"), "a": _measured(a[1]), "b": _measured(b[1]), "region": region, "difference_m2": region["difference_m2"]} if region and not agree else None
    read_a["_found"] = agent_reader.findings(work_a, read_a["answer"], read_a["checks"])
    if read_b:
        read_b["_found"] = read_b.get("_found") or (agent_reader.findings(Path(read_b["_work"]), read_b["answer"], read_b.get("checks") or []) if read_b.get("_work") else [])
    disc = pair_discrepancies(read_a, read_b if only is None else None, pair_info)
    read_a.pop("_found", None)
    geometry = program_geometry(work_a, read_a.get("dxf") or (read_b or {}).get("dxf"), [c["outline"] for pair in (a, b) if pair for c in [pair[1]]], m)
    record.update({"discrepancies": disc, "program_geometry": {k: geometry.get(k) for k in ("status", "message_zh", "axis", "wall") if k in geometry}, "difference_region": region})
    budget = tool_budget(read_a["checks"], disc)
    record["max_tool_calls"] = budget
    needs_call = bool(disc)  # agreement without any drawing discrepancy: no reviewer call (target rule 5)
    if not needs_call:
        record.update({"status": "no_discrepancy", "decision": "adopt_a", "why": "两种方法一致，程序也没有发现与图上数字的矛盾" if agree else "只有一种方法有结果，程序没有发现与图上数字的矛盾",
                       "verdicts": [], "overrides": [], "refused": [], "evidence": [], "wrong_if": ""})
    else:
        prompt = _pair_prompt(read_a, read_b if only is None else None, a, b if only is None else None, b_face, disc, geometry, region, budget)
        if on_wall:
            prompt += (f"\n## 程序提示：指定含糊\n\n{on_wall['message_zh']}。两人若取了这道墙的不同侧，那不是读图错误，是指定没说清：请在 notes 里写明墙两侧各是什么"
                       "（楼名、跨、功能），决定照常给出，程序会把两个数并列、状态记为待定。\n")
        done = agent_reader.run_prompt(work_a, prompt, tag="_adjudicate", timeout_seconds=timeout_seconds, answer_key="decision", model=ADJUDICATOR_MODEL)
        dec = validate_decision(done["answer"], disc, bool(a), bool(b and only is None))
        record.update({"status": "answered" if done["answer"] else "no_answer", **dec, "minutes": done["minutes"], "tokens_used": done["tokens_used"],
                       "tool_calls": done["tool_calls"], "over_budget": (done["tool_calls"] or 0) > budget, "cost_usd_equivalent": done.get("cost_usd_equivalent"),
                       "backend": agent_reader.AGENT_BACKEND, "model": done.get("model"), "overrides": [], "refused": []})
    # apply the decision: which outline is the result, and what the program can say about it
    source = "b" if only == "b" else "a"
    final_answer, final_check = read_a["answer"], a[1]
    decision = record["decision"]
    if decision == "adopt_b" and b and only is None:
        new = _copy.deepcopy(read_a["answer"])
        for bb in new.get("buildings") or []:
            if bb.get("designated"):
                bb.update({"faces": [b_face], "area_m2": _measured(b[1]).get("area_m2"), "length_m": _measured(b[1]).get("length_m"), "width_m": _measured(b[1]).get("width_m"),
                           "evidence": f"复查采用乙（{METHOD_CN.get(read_b.get('method'))}）的轮廓：{record.get('why')}", "method": read_b.get("method")})
        final_answer, source = new, "b"
    elif decision == "both_wrong":
        faces_path = work_a / agent_tools.FACES.name
        faces = json.loads(faces_path.read_text(encoding="utf-8")) if faces_path.is_file() else {}
        ids = [f for f in record["faces"] if f in faces]
        trial = _copy.deepcopy(read_a["answer"])
        adopted = False
        if ids and len(ids) == len(record["faces"]):
            for bb in trial.get("buildings") or []:
                if bb.get("designated"):
                    got = agent_tools.measure_faces(ids, faces, m)
                    if "error" not in got:
                        bb.update({"faces": ids, "area_m2": got["area_m2"], "length_m": got["length_m"], "width_m": got["width_m"]})
            trial_checks = agent_reader.verify(work_a, trial)
            tc = next((c for c in trial_checks if c.get("designated") and c.get("outline")), None)
            hits = drawing_matches(tc) if tc else []
            if tc and hits:
                final_answer, final_check, source, adopted = trial, tc, "reviewer", True
                record["overrides"] = [{"building": tc.get("name"), "faces_after": ids, "before": _measured(a[1]), "after": _measured(tc), "matched": hits, "outline_before": a[1]["outline"]}]
                record["applied"] = "复查重描采用：复测结果与图上写的数一致"
            else:
                record["applied"] = "复查重描未采用：复测结果与图上写的数对不上，转为待定；重描的面只作候选"
                record["candidate"] = _candidate(ids, faces, m, []) if ids else None
        else:
            record["applied"] = "复查重描未采用：给出的面编号不存在，转为待定"
        if not adopted:
            record["decision"] = decision = "undecided"
    if final_answer is not read_a["answer"]:
        read_a["answer_before_adjudication"] = read_a["answer"]
        read_a["answer"] = final_answer
        read_a["checks"] = agent_reader.verify(work_a, final_answer)
        final_check = next((c for c in read_a["checks"] if c.get("designated") and c.get("outline")), final_check)
    read_a["remaining_feedback"] = agent_reader.feedback(work_a, read_a["answer"], read_a["checks"])
    hits = drawing_matches(final_check)
    if decision in ("adopt_a", "adopt_b", "both_wrong") and hits:
        code = "matches_drawing"
    elif agree:
        code = "two_methods_agree"
    else:
        code = "undecided"
    why = record.get("why")
    if on_wall and a and b and only is None and not agree:  # the two readers took different ranges round an ambiguous point: not the reviewer's to decide
        code = "undecided"
        why = f"指定含糊：{on_wall['message_zh']}；两种读法取了不同的范围，程序不定夺，两个数并列。复查意见仅供参考：{record.get('why') or ''}"
    final_m = _measured(final_check)
    status.update({"status_code": code, "status_zh": STATUS_CN[code], "source": source, "source_zh": SOURCE_CN[source], "decision": decision, "why": why,
                   "designation_note": on_wall["message_zh"] if on_wall else None,
                   "wrong_if": record.get("wrong_if"), "evidence": record.get("evidence"), "agree": agree, "drawing_matches": hits,
                   "final": {"area_m2": final_m.get("area_m2"), "length_m": final_m.get("length_m"), "width_m": final_m.get("width_m")},
                   "difference_m2": region["difference_m2"] if region else None, "difference_region": region["description"] if region else None,
                   "difference_pieces": (region or {}).get("pieces", [])[:4], "program_geometry": _geometry_words(geometry),
                   "risk_review": (read_a["answer"].get("risk_review") if source != "b" else (read_b or {}).get("answer", {}).get("risk_review")) or [],
                   "undecided_numbers": ([{"source": "a", "source_zh": SOURCE_CN["a"], **{k: status["a"][k] for k in ("area_m2", "length_m", "width_m")}}] if a and status["a"] else [])
                                        + ([{"source": "b", "source_zh": SOURCE_CN["b"], **{k: status["b"][k] for k in ("area_m2", "length_m", "width_m")}}] if b and status["b"] and only is None else [])
                                        if code == "undecided" else []})
    read_a.update({"adjudication": record, "result_status": status})
    return read_a


def ensure_pair(works: list[Path], force: bool = False, timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """Adjudicate the saved reads of the two methods (works[0] = A, works[1] = B if present); works[0]/agent_read.json is rewritten."""

    path_a = Path(works[0]) / "agent_read.json"
    read_a = json.loads(path_a.read_text(encoding="utf-8"))
    if read_a.get("result_status") and not force:
        return read_a["result_status"]
    if force and read_a.get("answer_before_adjudication"):
        read_a["answer"] = read_a.pop("answer_before_adjudication")
        read_a.pop("adjudication", None)
        read_a.pop("result_status", None)
    read_b = None
    if len(works) > 1 and (Path(works[1]) / "agent_read.json").is_file():
        read_b = json.loads((Path(works[1]) / "agent_read.json").read_text(encoding="utf-8"))
        read_b["_work"] = str(works[1])
    read_a = adjudicate_pair(Path(works[0]), read_a, read_b, timeout_seconds)
    path_a.write_text(json.dumps(read_a, ensure_ascii=False, indent=1), encoding="utf-8")
    return read_a["result_status"]


__all__ = ["adjudicate_pair", "adjudicate_read", "apply_verdicts", "difference_region", "discrepancies", "drawing_matches", "ensure", "ensure_pair",
           "numbers_agree", "run", "validate_decision", "validate_verdicts"]
