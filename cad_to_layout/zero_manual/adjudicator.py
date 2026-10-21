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


__all__ = ["adjudicate_read", "apply_verdicts", "discrepancies", "ensure", "run", "validate_verdicts"]
