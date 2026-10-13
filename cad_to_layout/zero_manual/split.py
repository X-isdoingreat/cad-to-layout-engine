"""Split an unsegmented region into drawings: program proposes cut lines, an
automatic judge (LLM) only chooses among them, the program cuts and checks.

The judge never emits coordinates.  It sees a thumbnail of the region (drawing
objects, closed shapes, the largest texts) with numbered candidate cut lines, plus
program-generated facts (line positions, object counts on each side, texts), and
answers how many drawings it sees and which numbered lines separate them.  The
program applies only those lines, requires every resulting piece to contain
drawing geometry (text-only pieces are refused), and reports the region as
unsplit when anything fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .constants import SPLIT_IGNORE_SPAN_FRACTION, SPLIT_MIN_GAP_MEDIAN_FACTOR, SPLIT_MIN_PIECE_MEMBERS


def is_graphic(ref: str) -> bool:
    """Drawing geometry (block occurrences, closed outlines); texts are hints, never content."""

    return not ref.startswith("T:")


def _gaps(intervals: list[tuple[float, float]], lo: float, hi: float, minimum: float) -> list[tuple[float, float]]:
    """Blank strips of at least `minimum` between lo and hi not covered by any interval."""

    merged: list[list[float]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = []
    cursor = lo
    for a, b in merged:
        if a - cursor >= minimum and cursor > lo:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    return gaps


def content_boxes(boxes: list[tuple[str, list[float]]], window: list[float], axis: str | None = None) -> list[tuple[str, list[float]]]:
    """Boxes that count as drawing content: not spanning most of the window (containers, borders)."""

    width, height = window[2] - window[0], window[3] - window[1]
    kept = []
    for ref, b in boxes:
        wide = (b[2] - b[0]) > width * SPLIT_IGNORE_SPAN_FRACTION
        tall = (b[3] - b[1]) > height * SPLIT_IGNORE_SPAN_FRACTION
        if axis == "x" and wide:
            continue
        if axis == "y" and tall:
            continue
        if axis is None and (wide or tall):
            continue
        kept.append((ref, b))
    return kept


def candidate_cuts(boxes: list[tuple[str, list[float]]], window: list[float]) -> list[dict[str, Any]]:
    """Numbered blank strips crossing the whole window, horizontal (y) and vertical (x).

    Only drawing geometry blocks a strip (a title sitting in a gap does not).  Strips
    with no geometry between them separate the same groups; only the widest is kept.
    """

    boxes = [(ref, b) for ref, b in boxes if is_graphic(ref)]
    sizes = [max(b[2] - b[0], b[3] - b[1]) for _, b in boxes if b[2] > b[0] or b[3] > b[1]]
    if not sizes:
        return []
    sizes.sort()
    median = sizes[len(sizes) // 2]
    minimum = median * SPLIT_MIN_GAP_MEDIAN_FACTOR
    cuts: list[dict[str, Any]] = []
    for axis, lo, hi in (("y", window[1], window[3]), ("x", window[0], window[2])):
        index = (1, 3) if axis == "y" else (0, 2)
        span_limit = (hi - lo) * SPLIT_IGNORE_SPAN_FRACTION
        intervals = [(b[index[0]], b[index[1]]) for _, b in boxes if b[index[1]] - b[index[0]] <= span_limit]
        for a, b in _gaps(intervals, lo, hi, minimum):
            cuts.append({"axis": axis, "from": round(a, 3), "to": round(b, 3), "at": round((a + b) / 2.0, 3), "width": round(b - a, 3)})
    distinct: list[dict[str, Any]] = []
    for axis in ("y", "x"):
        index = (1, 3) if axis == "y" else (0, 2)
        centres = [(b[index[0]] + b[index[1]]) / 2.0 for _, b in content_boxes(boxes, window, axis)]
        run: list[dict[str, Any]] = []
        for cut in sorted((c for c in cuts if c["axis"] == axis), key=lambda c: c["at"]):
            if run and not any(run[-1]["at"] < value < cut["at"] for value in centres):
                run.append(cut)
                continue
            if run:
                distinct.append(max(run, key=lambda c: c["width"]))
            run = [cut]
        if run:
            distinct.append(max(run, key=lambda c: c["width"]))
    distinct.sort(key=lambda c: (c["axis"], c["at"]))
    for number, cut in enumerate(distinct, start=1):
        cut["id"] = number
    return distinct


def apply_cuts(boxes: list[tuple[str, list[float]]], window: list[float], cuts: list[dict[str, Any]], chosen: list[int]) -> list[dict[str, Any]] | None:
    """Cut the window by the chosen lines into a grid; every piece must hold drawing geometry."""

    by_id = {c["id"]: c for c in cuts}
    if any(i not in by_id for i in chosen):
        return None
    xs = sorted({window[0], window[2], *[by_id[i]["at"] for i in chosen if by_id[i]["axis"] == "x"]})
    ys = sorted({window[1], window[3], *[by_id[i]["at"] for i in chosen if by_id[i]["axis"] == "y"]})
    pieces = []
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            members = [ref for ref, b in boxes if x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1]
            if not members:
                continue
            # a piece of only text (notes, tables of text) is not a drawing
            if sum(1 for ref in members if is_graphic(ref)) < SPLIT_MIN_PIECE_MEMBERS:
                return None
            member_set = set(members)
            # the piece window comes from content-sized members (titles included), clipped to
            # the cell, so a long object crossing the region cannot drag a piece over its neighbours
            bx = [b for ref, b in content_boxes(boxes, window) if ref in member_set] or [b for ref, b in boxes if ref in member_set]
            pieces.append({"window": [round(max(x0, min(b[0] for b in bx)), 3), round(max(y0, min(b[1] for b in bx)), 3),
                                      round(min(x1, max(b[2] for b in bx)), 3), round(min(y1, max(b[3] for b in bx)), 3)],
                           "member_count": len(members), "members": sorted(members)})
    return pieces if len(pieces) >= 2 else None


def region_texts(texts: list[dict[str, Any]], window: list[float], limit: int = 25) -> list[dict[str, Any]]:
    """The largest distinct texts inside the window (titles, zone names), for the judge."""

    inside = [t for t in texts if window[0] <= t["xy"][0] <= window[2] and window[1] <= t["xy"][1] <= window[3] and t["text"].strip()]
    inside.sort(key=lambda t: -float(t.get("height") or 0.0))
    seen: set[str] = set()
    chosen = []
    for t in inside:
        key = t["text"].strip()
        if key in seen or len(key) > 40:
            continue
        seen.add(key)
        chosen.append(t)
        if len(chosen) >= limit:
            break
    return chosen


def _side_counts(boxes: list[tuple[str, list[float]]], cut: dict[str, Any]) -> tuple[int, int]:
    index = (1, 3) if cut["axis"] == "y" else (0, 2)
    before = after = 0
    for ref, b in boxes:
        if not is_graphic(ref):
            continue
        if (b[index[0]] + b[index[1]]) / 2.0 < cut["at"]:
            before += 1
        else:
            after += 1
    return before, after


def render_thumbnail(boxes: list[tuple[str, list[float]]], window: list[float], cuts: list[dict[str, Any]], path: Path,
                     texts: list[dict[str, Any]] | None = None) -> str:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import patches, pyplot

    matplotlib.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    width, height = window[2] - window[0], window[3] - window[1]
    figure, axis = pyplot.subplots(figsize=(12, max(5.0, 12 * height / max(width, 1))), dpi=110)
    for ref, b in content_boxes(boxes, window):
        if not is_graphic(ref):
            continue
        w, h = max(b[2] - b[0], width * 0.003), max(b[3] - b[1], height * 0.003)
        if ref.startswith("C:"):
            axis.add_patch(patches.Rectangle((b[0], b[1]), w, h, linewidth=0, facecolor="#9ca3af", alpha=0.45, zorder=0))
        elif ref.startswith("P:"):
            axis.add_patch(patches.Rectangle((b[0], b[1]), w, h, linewidth=0.8, edgecolor="#2563eb", facecolor="none"))
        else:
            axis.add_patch(patches.Rectangle((b[0], b[1]), w, h, linewidth=0, facecolor="#1f2937"))
    for t in texts or []:
        axis.text(t["xy"][0], t["xy"][1], t["text"], color="#dc2626", fontsize=9, clip_on=True)
    for cut in cuts:
        if cut["axis"] == "y":
            axis.axhspan(cut["from"], cut["to"], color="#f59e0b", alpha=0.25)
            axis.axhline(cut["at"], color="#b45309", linewidth=1.5, linestyle="--")
            axis.text(window[0], cut["at"], f" {cut['id']}", color="#b45309", fontsize=18, weight="bold", va="center")
        else:
            axis.axvspan(cut["from"], cut["to"], color="#f59e0b", alpha=0.25)
            axis.axvline(cut["at"], color="#b45309", linewidth=1.5, linestyle="--")
            axis.text(cut["at"], window[3], f"{cut['id']}", color="#b45309", fontsize=18, weight="bold", ha="center", va="bottom")
    axis.set_xlim(window[0], window[2])
    axis.set_ylim(window[1], window[3])
    axis.set_aspect("equal")
    axis.axis("off")
    figure.savefig(path, bbox_inches="tight", facecolor="white")
    pyplot.close(figure)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def region_facts(boxes: list[tuple[str, list[float]]], window: list[float], cuts: list[dict[str, Any]],
                 texts: list[dict[str, Any]]) -> str:
    """Program-generated facts appended to the fixed prompt (no human writes them)."""

    width, height = window[2] - window[0], window[3] - window[1]
    graphic = sum(1 for ref, _ in boxes if is_graphic(ref))
    lines = [f"Region size: {width:.0f} x {height:.0f} drawing units; {graphic} drawing objects (texts excluded)."]
    lines.append("Candidate cut lines:")
    for cut in cuts:
        before, after = _side_counts(boxes, cut)
        span = height if cut["axis"] == "y" else width
        origin = window[1] if cut["axis"] == "y" else window[0]
        where = (cut["at"] - origin) / max(span, 1.0)
        direction = "horizontal" if cut["axis"] == "y" else "vertical"
        side = ("below", "above") if cut["axis"] == "y" else ("left of it", "right of it")
        lines.append(
            f"- line {cut['id']}: {direction}, at {where:.0%} of the region, blank strip "
            f"{cut['width'] / max(span, 1.0):.1%} of the region wide; {before} drawing objects {side[0]}, {after} {side[1]}."
        )
    if texts:
        lines.append("Largest texts in the region (red in the image), position as % of width/height from the bottom-left corner:")
        for t in texts:
            px = (t["xy"][0] - window[0]) / max(width, 1.0)
            py = (t["xy"][1] - window[1]) / max(height, 1.0)
            lines.append(f"- \"{t['text']}\" at ({px:.0%}, {py:.0%})")
    return "\n".join(lines)


PROMPT_TEMPLATE_ID = "split_judge_v3"
PROMPT_TEMPLATE = (
    "Do not use any tools, files or directories; answer only from the attached image and the facts below. "
    "The image shows one region of a CAD model space. Dark filled rectangles are drawing objects (block references); "
    "light grey areas are large blocks whose inside is drawn with plain lines (for example racks); "
    "blue outlines are closed shapes; red words are the largest texts (titles, zone names, notes). "
    "Orange strips with numbers are blank strips that cross the whole region; they are the only allowed cut lines. "
    "Decide how many separate drawings (plan views) this region contains. A separate drawing is a group of drawing "
    "objects that belongs together, usually with its own title or zone names. Text-only areas (notes, legends, tables "
    "of text) are not drawings and must not be cut off on their own. Every resulting piece must be a whole drawing; "
    "never cut a drawing through its middle. If you are not sure, answer one drawing. "
    "Answer with JSON only: {\"drawing_count\": <int>, \"cut_ids\": [<int>, ...], \"reason\": \"<short>\"}. "
    "If the region is one drawing, answer {\"drawing_count\": 1, \"cut_ids\": [], \"reason\": \"...\"}.\n\nFacts:\n"
)


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def partition_key(pieces: list[dict[str, Any]] | None) -> str:
    """Hashable identity of a split result: which members end up together (line numbers do not matter)."""

    if pieces is None:
        return "invalid"
    return json.dumps(sorted(sorted(piece["members"]) for piece in pieces))


def parse_answer(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(value.get("drawing_count"), int) or not isinstance(value.get("cut_ids"), list):
        return None
    if any(not isinstance(i, int) for i in value["cut_ids"]):
        return None
    return {"drawing_count": value["drawing_count"], "cut_ids": sorted(set(value["cut_ids"])), "reason": str(value.get("reason", ""))[:300]}


__all__ = [
    "PROMPT_TEMPLATE",
    "PROMPT_TEMPLATE_ID",
    "apply_cuts",
    "candidate_cuts",
    "is_graphic",
    "parse_answer",
    "partition_key",
    "prompt_sha256",
    "region_facts",
    "region_texts",
    "render_thumbnail",
]
