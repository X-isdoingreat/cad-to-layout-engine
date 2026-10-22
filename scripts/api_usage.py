"""What one drawing cost on the API backend: every usage*.json under a read's output folder, summed and priced.

  py -3.11 -X utf8 -m scripts.api_usage <output folder> [--prices prices.json]

Each API call (the building read, the adjudication, every equipment judge call) leaves a usage file with its input,
output, cache-write and cache-read tokens. This adds them up per step and prices the total at the model that ran, and,
with --prices, at other models' list prices: {"name": {"input": $/M, "output": $/M, "cache_write": $/M, "cache_read": $/M}}.
Other models count tokens with their own tokenizers, so their figures are estimates of the same work, not quotes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cad_to_layout.zero_manual.api_agent import PRICES, USAGE_KEYS, cost_usd  # noqa: E402


def step_of(path: Path, out: Path) -> str:
    rel = path.relative_to(out).as_posix()
    if "_adjudicate" in path.name:
        return "定夺"
    if rel.startswith("area/"):
        return "厂房读图"
    if "/split/" in "/" + rel or "split" in path.parts:
        return "设备：视图切分判官"
    if "type_judge" in path.parts:
        return "设备：类型判官"
    return "其他"


def collect(out: Path) -> dict:
    steps: dict[str, dict] = {}
    models, errors = set(), 0
    for path in sorted(out.rglob("usage*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("backend") != "api":
            continue
        step = steps.setdefault(step_of(path, out), {"calls": 0, "turns": 0, "tool_calls": 0, **{k: 0 for k in USAGE_KEYS}})
        step["calls"] += 1
        step["turns"] += int(data.get("turns") or 0)
        step["tool_calls"] += int(data.get("tool_calls") or 0)
        for k in USAGE_KEYS:
            step[k] += int((data.get("usage") or {}).get(k) or 0)
        models.add(data.get("model"))
        errors += bool(data.get("error"))
    total = {k: sum(s[k] for s in steps.values()) for k in ("calls", "turns", "tool_calls", *USAGE_KEYS)}
    return {"folder": str(out), "models": sorted(m for m in models if m), "failed_calls": errors, "steps": steps, "total": total}


def priced(usage: dict, price: dict) -> float:
    rates = (price["input"], price["output"], price.get("cache_write", price["input"]), price.get("cache_read", price["input"]))
    return round(sum(usage.get(k, 0) * r for k, r in zip(USAGE_KEYS, rates)) / 1e6, 4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--prices", help="JSON of other models' list prices (US$ per million tokens)")
    args = parser.parse_args(argv)
    report = collect(Path(args.out).resolve())
    for name, step in report["steps"].items():
        step["cost_usd"] = {m: cost_usd(m, step) for m in report["models"]}
    report["total"]["cost_usd"] = {m: cost_usd(m, report["total"]) for m in report["models"]}
    report["claude_list_prices"] = {m: cost_usd(m, report["total"]) for m in PRICES}
    if args.prices:
        table = json.loads(Path(args.prices).read_text(encoding="utf-8"))
        report["other_models_estimate"] = {name: priced(report["total"], p) for name, p in table.items() if not name.startswith("_")}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
