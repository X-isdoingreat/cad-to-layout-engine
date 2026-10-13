"""Read the warehouse equipment of a drawing (no per-drawing rules; the AI judges shapes the text does not name).

  py -3.11 -X utf8 -m scripts.read_equipment <drawing.dwg|dxf> --out <folder>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cad_to_layout.zero_manual.pipeline import run_read  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--out", required=True)
    parser.add_argument("--replay-types", help="a type_judge_archive.json to replay instead of asking the AI again")
    args = parser.parse_args(argv)
    manifest = run_read(args.source, args.out, allow_external_llm=True, replay_types=args.replay_types)
    print(json.dumps({"out": args.out, "equipment": manifest["stages"]["equipment"], "seconds": manifest["elapsed_seconds"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
