"""Read one drawing several times with the agent and mark what the runs do not agree on.

Each run gets its own work directory (same index, fresh faces) and is checked by the program as usual.
Then every building (matched across runs by outline overlap), its area, length, width and column count,
and each view's storage + operation total are compared: a value that differs by more than 1 % between runs
(assistant default, as in the acceptance target) is marked uncertain, with all run values listed.
Runs go one after another, never in parallel (memory).

  py -3.11 -X utf8 -m scripts.run_agent_reads --dxf <converted copy> --out <dir> [--runs 3] [--designation "..."]
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon

from cad_to_layout.zero_manual import agent_reader, agent_tools

TOLERANCE = 0.01


def _buildings(read: dict[str, Any]) -> list[dict[str, Any]]:
    checks = {c.get("name"): c for c in read.get("checks", [])}
    out = []
    for b in (read.get("answer") or {}).get("buildings", []):
        c = checks.get(b.get("name"), {})
        if not c.get("outline"):
            continue
        size = c.get("with_decided_parts") or c.get("remeasured") or {}
        out.append({"name": b.get("name"), "designated": bool(b.get("designated")), "poly": Polygon(c["outline"]),
                    "area_m2": size.get("area_m2"), "length_m": size.get("length_m"), "width_m": size.get("width_m"),
                    "columns": (b.get("columns") or {}).get("count")})
    return out


def _spread(values: list[Any]) -> bool:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if len(nums) != len(values) or not nums:
        return len(set(map(str, values))) > 1
    return (max(nums) - min(nums)) > TOLERANCE * max(abs(max(nums)), 1e-9)


def compare(reads: list[dict[str, Any]]) -> dict[str, Any]:
    runs = [_buildings(r) for r in reads]
    rows = []
    for b in runs[0] if runs else []:
        same = [b]
        for other in runs[1:]:
            best = max(other, key=lambda o: o["poly"].intersection(b["poly"]).area / max(o["poly"].union(b["poly"]).area, 1e-12), default=None)
            iou = best["poly"].intersection(b["poly"]).area / max(best["poly"].union(b["poly"]).area, 1e-12) if best else 0.0
            same.append(best if iou >= 0.5 else None)
        row = {"building": b["name"], "designated": b["designated"], "found_in_runs": sum(1 for s in same if s)}
        for key in ("area_m2", "length_m", "width_m", "columns"):
            values = [s[key] if s else None for s in same]
            row[key] = {"runs": values, "uncertain": row["found_in_runs"] < len(same) or _spread(values)}
        rows.append(row)
    extra = [[o["name"] for o in r if not any(o["poly"].intersection(b["poly"]).area > 0.5 * o["poly"].area for b in runs[0])] for r in runs[1:]]
    totals = [{str(v): t for v, t in ((r.get("zone_check") or {}).get("recomputed_totals") or {}).items()} for r in reads]
    if all(len(t) == 1 for t in totals) and totals:  # one view per run: compare it whatever each run called it
        totals = [{"(single view)": next(iter(t.values()))} for t in totals]
    views = sorted({v for t in totals for v in t})
    zone_rows = [{"view": v, "storage_and_operation_m2": {"runs": [t.get(v, {}).get("storage_and_operation_m2") for t in totals],
                                                          "uncertain": _spread([t.get(v, {}).get("storage_and_operation_m2") for t in totals])}} for v in views]
    return {"runs": len(reads), "buildings": rows, "buildings_only_in_later_runs": extra, "zone_totals": zone_rows,
            "tokens_per_run": [r.get("tokens_used") for r in reads], "minutes_per_run": [r.get("minutes") for r in reads],
            "uncertain_count": sum(1 for r in rows for k in ("area_m2", "length_m", "width_m", "columns") if r[k]["uncertain"]) + sum(1 for z in zone_rows if z["storage_and_operation_m2"]["uncertain"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dxf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--designation")
    parser.add_argument("--only", type=int, help="do only run N (own directory and index); runs can then go in parallel")
    parser.add_argument("--compare", action="store_true", help="only compare the finished runs in --out")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.only:  # one run in its own directory with its own index, so several can go at once
        work = out / f"run{args.only}"
        info = agent_reader.prepare(Path(args.dxf), work)
        read = agent_reader.read_once(work, args.designation, info)
        read["dxf"] = str(args.dxf)
        (work / "agent_read.json").write_text(json.dumps(read, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"run {args.only}: {read['minutes']} min, {read['tokens_used']} tokens, parsed={read['answer_parsed']}, "
              f"rounds={len(read.get('revisions', []))}, left {len(read['remaining_feedback'])} findings", flush=True)
        return 0
    if args.compare:
        reads = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("run*/agent_read.json"))]
        result = compare(reads)
        (out / "stability.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("runs", "uncertain_count", "tokens_per_run", "minutes_per_run")}, ensure_ascii=False))
        return 0
    first = out / "run1"
    info = agent_reader.prepare(Path(args.dxf), first)
    reads = []
    for n in range(1, args.runs + 1):
        work = out / f"run{n}"
        if n > 1:
            work.mkdir(exist_ok=True)
            shutil.copyfile(first / agent_tools.INDEX.name, work / agent_tools.INDEX.name)
            (work / agent_tools.FACES.name).unlink(missing_ok=True)
            (work / agent_tools.COLUMNS.name).unlink(missing_ok=True)
        if (work / "agent_read.json").is_file():  # resume: a finished run is kept
            reads.append(json.loads((work / "agent_read.json").read_text(encoding="utf-8")))
            continue
        read = agent_reader.read_once(work, args.designation, info)  # with one revision round on the program's findings
        read["dxf"] = str(args.dxf)
        (work / "agent_read.json").write_text(json.dumps(read, ensure_ascii=False, indent=1), encoding="utf-8")
        reads.append(read)
        print(f"run {n}: {read['minutes']} min, {read['tokens_used']} tokens, parsed={read['answer_parsed']}, revised={'feedback' in read}, "
              f"left {len(read['remaining_feedback'])} findings", flush=True)
    result = compare(reads)
    (out / "stability.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("runs", "uncertain_count", "tokens_per_run", "minutes_per_run")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
