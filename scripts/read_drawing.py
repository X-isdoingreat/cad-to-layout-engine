"""Read one drawing end to end: the building's size, the warehouse equipment, the report and the review drawings.

  py -3.11 -X utf8 -m scripts.read_drawing --dwg <drawing.dwg> --building "<which building>" --out <folder>
      [--no-area] [--no-equipment]

  --building   the building to measure: its name as written on the drawing, or "图纸坐标 (x, y) 处的那栋" (required
               unless --no-area; the program never picks a building by itself)
Writes into <folder>:
  结果.json                        the machine-readable result: the building's numbers with their status (与图上数字一致 /
                                  两种方法一致 / 待定 / 读不出) and source, both methods' numbers, equipment counts, every AI
                                  call's use and cost; a failed run says so and carries no numbers
  图纸识别报告.pdf (+ _附录.pdf)   the report: summary, building size, equipment, questions for the reviewer
  area/                           building read and its optional review drawing (area/发给复查人员/to_confirm.dwg)
  equipment/                      equipment read; equipment/review/check.dwg is the equipment review drawing
The drawing itself is never changed. Drawing content is sent to the configured AI service (CTLE_AGENT, default Claude).
One call runs the whole chain; nobody has to sit at an interactive AI tool.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str]) -> int:
    print("::", " ".join(args[3:6]), flush=True)
    return subprocess.run([sys.executable, "-X", "utf8", *args], cwd=str(ROOT)).returncode


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _load_list(path: Path) -> list:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    if isinstance(value, dict):
        value = value.get("items") or []  # review/review_items.json is {"made_at": ..., "items": [...]}
    return value if isinstance(value, list) else []


def _claude_call_usage(events: Path) -> list[dict]:
    """The `result` event of one Claude Code call (stream-json transcript): tokens, the cost Claude Code states, duration."""

    out, model = [], None
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model") or model
        elif event.get("type") == "result":
            usage = event.get("usage") or {}
            out.append({"part": "设备", "step": "细看" if "closer_look" in events.parts else "复看" if "second_look" in events.parts else "判官", "backend": "claude", "model": model,
                        "minutes": round(float(event.get("duration_ms") or 0) / 60000, 1), "cost_usd_equivalent": event.get("total_cost_usd"),
                        "error": "is_error" if event.get("is_error") else None,
                        "tokens_used": sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))})
    return out


def write_result(out: Path, dwg: Path, building: str | None, area: Path | None, equipment: Path | None, failed: str | None = None) -> Path:
    """`结果.json` for the whole drawing: the area result (its own 结果.json) plus the equipment counts and the equipment AI usage."""

    body = {"schema": "ctle-drawing-result-1.0", "drawing": dwg.name, "designation": building, "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "status_code": "failed" if failed else "ok", "why": failed, "area": None, "equipment": None, "ai_calls": [], "cost_usd_equivalent": 0.0}
    if area is not None:
        a = _load(area / "结果.json")
        body["area"] = {k: a.get(k) for k in ("status_code", "status_zh", "why", "building", "human_review")} if a else {"status_code": "failed", "why": "没有面积结果文件"}
        body["ai_calls"] += [dict(c, part="厂房") for c in a.get("ai_calls") or []]
        if not failed and (a.get("status_code") == "failed" or not a):
            body.update({"status_code": "failed", "why": a.get("why") or "厂房读图失败"})
    if equipment is not None:
        e = _load(equipment / "equipment.json")
        counts = e.get("stage_counts") or {}
        kinds = [{k: it.get(k) for k in ("id", "type", "type_zh", "name", "count", "cells_per_row", "cells", "drawn_by", "basis", "wrong_if")}
                 for it in _load_list(equipment / "review" / "review_items.json")]
        body["equipment"] = {"records": len(e.get("records") or []), "stage_counts": counts, "folder": str(equipment), "kinds": kinds,
                             "llm_all_failed": bool(counts) and counts.get("llm_skipped", 0) > 0 and not any(counts.get(k) for k in ("llm_agreed", "llm_unsure", "llm_disagreed"))}
        usage = []
        for u in sorted(equipment.rglob("usage*.json")):  # the API backend leaves one per call
            d = _load(u)
            if d.get("backend") == "api":
                usage.append({"part": "设备", "step": "判官", "model": d.get("model"), "minutes": d.get("minutes"), "cost_usd_equivalent": d.get("cost_usd"), "error": d.get("error"),
                              "tokens_used": sum(int((d.get("usage") or {}).get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))})
        for ev in sorted(equipment.rglob("events.jsonl")):  # the Claude Code backend leaves one transcript per call
            usage += _claude_call_usage(ev)
        body["ai_calls"] += usage
        if body["equipment"]["llm_all_failed"] and body["status_code"] == "ok":
            body.update({"status_code": "equipment_llm_failed", "why": "设备判官的 AI 调用全部失败：设备只有规则认出的部分，类型未经判官确认"})
    body["cost_usd_equivalent"] = round(sum(float(c.get("cost_usd_equivalent") or 0) for c in body["ai_calls"]), 4)
    path = out / "结果.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dwg", required=True)
    parser.add_argument("--building")
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-area", action="store_true")
    parser.add_argument("--no-equipment", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_area and not args.building:
        raise SystemExit("--building is required (or --no-area): name the building to measure")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dwg = Path(args.dwg).resolve()
    area = None if args.no_area else out / "area"
    equipment = None if args.no_equipment else out / "equipment"
    report = ["-m", "scripts.drawing_report", "--drawing", dwg.name, "--out", str(out / "图纸识别报告.pdf")]
    if area is not None:
        rc = _run(["-m", "scripts.trial_package", "run", "--dwg", str(dwg), "--designation", args.building, "--out", str(area)])
        if rc != 0:  # the building read failed (every AI call refused, or nothing read): say so and stop, no report that looks normal
            path = write_result(out, dwg, args.building, area, None, failed=f"厂房读图失败（退出码 {rc}），见 area/read_*.log 与 area/结果.json")
            print(f"failed: {path}")
            return 3
        report += ["--area", str(area)]
    if equipment is not None:
        for step in (["-m", "scripts.read_equipment", str(dwg), "--out", str(equipment)],
                     ["scripts/equipment_review.py", "make", "--run", str(equipment), "--drawing", dwg.name],
                     ["scripts/equipment_review.py", "dwg", "--run", str(equipment)] + ([] if area is None else ["--area", str(area)])):
            rc = _run(step)
            if rc != 0:
                path = write_result(out, dwg, args.building, area, equipment, failed=f"设备读图失败（退出码 {rc}）：{' '.join(step[:3])}")
                print(f"failed: {path}")
                return 3
        report += ["--equipment", str(equipment)]
    rc = _run(report)
    path = write_result(out, dwg, args.building, area, equipment, failed=None if rc == 0 else f"报告生成失败（退出码 {rc}）")
    print(f"done: {out}" if rc == 0 else f"failed: {path}")
    return 0 if rc == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
