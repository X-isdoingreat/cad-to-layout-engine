"""Read one drawing end to end: the building's size, the warehouse equipment, the report and the review drawings.

  py -3.11 -X utf8 -m scripts.read_drawing --dwg <drawing.dwg> --building "<which building>" --out <folder>
      [--no-area] [--no-equipment]

  --building   the building to measure: its name as written on the drawing, or "图纸坐标 (x, y) 处的那栋" (required
               unless --no-area; the program never picks a building by itself)
Writes into <folder>:
  图纸识别报告.pdf (+ _附录.pdf)   the report: summary, building size, equipment, questions for the reviewer
  area/                           building read and its review drawing (area/发给复查人员/to_confirm.dwg)
  equipment/                      equipment read; equipment/review/check.dwg is the equipment review drawing
The drawing itself is never changed. Drawing content is sent to the configured AI service (CTLE_AGENT, default Claude).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str]) -> None:
    print("::", " ".join(args[3:6]), flush=True)
    subprocess.run([sys.executable, "-X", "utf8", *args], cwd=str(ROOT), check=True)


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
    dwg = str(Path(args.dwg).resolve())
    report = ["-m", "scripts.drawing_report", "--drawing", Path(dwg).name, "--out", str(out / "图纸识别报告.pdf")]
    if not args.no_area:
        _run(["-m", "scripts.trial_package", "run", "--dwg", dwg, "--designation", args.building, "--out", str(out / "area")])
        report += ["--area", str(out / "area")]
    if not args.no_equipment:
        _run(["-m", "scripts.read_equipment", dwg, "--out", str(out / "equipment")])
        _run(["scripts/equipment_review.py", "make", "--run", str(out / "equipment"), "--drawing", Path(dwg).name])
        _run(["scripts/equipment_review.py", "dwg", "--run", str(out / "equipment")] + ([] if args.no_area else ["--area", str(out / "area")]))
        report += ["--equipment", str(out / "equipment")]
    _run(report)
    print(f"done: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
