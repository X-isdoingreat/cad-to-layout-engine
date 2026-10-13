"""Drawing unit with evidence: declared header -> dimension agreement -> unit note -> unknown.

A view whose unit is unknown gets no numbers (acceptance target, missing/invalid rule).
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

from .constants import UNIT_MIN_AGREEING_DIMENSIONS, UNIT_MM_INTEGER_SHARE

INSUNITS = {1: "inch", 2: "foot", 4: "mm", 5: "cm", 6: "m"}
_LEXICON = json.loads((Path(__file__).with_name("lexicon_v1.json")).read_text(encoding="utf-8"))
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def infer_unit(inventory: dict[str, Any]) -> dict[str, Any]:
    code = int(inventory.get("header", {}).get("insunits", 0) or 0)
    if code in INSUNITS:
        return {"unit": INSUNITS[code], "status": "declared", "evidence": [f"HEADER:$INSUNITS={code}"]}

    # Dimensions whose displayed number equals the measured length (x DIMLFAC) show
    # the drawing's own numbers; building/equipment dimensions of several hundred
    # to tens of thousands in round steps are millimetre drawings.
    agreeing: list[tuple[str, float]] = []
    for row in inventory.get("dimensions", []):
        measured = float(row["measurement"]) * float(row.get("dimlfac") or 1.0)
        override = str(row.get("text_override") or "").strip()
        if override in {"", "<>"}:
            shown = measured
        elif _NUMBER.match(override):
            shown = float(override)
            if abs(shown - measured) > max(1.0, abs(measured) * 1e-4):
                continue
        else:
            continue
        if shown >= 100.0:
            agreeing.append((row["ref"], shown))
    if len(agreeing) >= UNIT_MIN_AGREEING_DIMENSIONS:
        values = [value for _, value in agreeing]
        integer_share = sum(1 for value in values if abs(value - round(value)) < 0.01) / len(values)
        if statistics.median(values) >= 300.0 and integer_share >= UNIT_MM_INTEGER_SHARE:
            return {
                "unit": "mm",
                "status": "inferred_from_dimensions",
                "evidence": [ref for ref, _ in agreeing[:10]],
                "detail": {"agreeing_dimensions": len(agreeing), "median_value": statistics.median(values), "whole_mm_share": round(integer_share, 3)},
            }

    for unit, notes in _LEXICON["unit_notes"].items():
        for row in inventory.get("texts", []):
            text = row["text"].replace(" ", "")
            if any(note != "mm" and note.replace(" ", "") in text for note in notes):
                return {"unit": unit, "status": "inferred_from_note", "evidence": [row["ref"]]}

    return {"unit": None, "status": "unknown", "evidence": [], "reason": "no declared unit, no agreeing dimensions, no unit note"}


__all__ = ["infer_unit"]
