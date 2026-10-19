"""Count equipment by repetition, the way Bluebeam VisualSearch / AutoCAD COUNT / GQI 设备提量 count from one boxed
example -- except that the program proposes the example itself.

For equipment drawn as loose lines or as parts (no block per unit), a view is searched for linework that repeats at a
fixed spacing in both directions.  One spacing by one spacing is the example cell; its edges go where the example keeps
the most linework whole (a member cut by an edge drops out of the example).  The example is matched over the view,
mirrored and half-turned copies included (back-to-back rows are drawn mirrored).  Several anchors are tried and the
largest count at least two of them agree on is kept.

What the first example leaves out (end bays drawn differently, shorter end bays, things at row ends) becomes up to
REPEAT_MAX_FOLLOW_UPS further examples.  Those are questions for a person ("are these also one?"), never counted on
their own; an example that finds nothing new is not asked.

  python -m cad_to_layout.zero_manual.repeat_count <index.sqlite> <x0,y0,x1,y1 view window> <out.json>
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import constants as C

Segment = tuple[float, float, float, float]


def segments(lines: list[dict[str, Any]]) -> list[Segment]:
    out = []
    for ln in lines:
        p = ln["p"]
        for a, b in zip(p, p[1:]):
            if math.dist(a, b) > 1e-6:
                out.append((a[0], a[1], b[0], b[1]))
    return out


def _key(s: Segment, q: float) -> tuple:
    a, b = (round(s[0] / q), round(s[1] / q)), (round(s[2] / q), round(s[3] / q))
    return (a, b) if a <= b else (b, a)


class Scene:
    """The linework of one view, loaded once: every example in the view is matched against it."""

    def __init__(self, index: Any, window: list[float]):
        self.index = index
        self.window = window
        metres = None
        try:
            row = index.con.execute("SELECT v FROM meta WHERE k = 'metres_per_unit'").fetchone()
            metres = json.loads(row[0]) if row else None
        except Exception:
            metres = None
        self.mm = 1.0 / (1000.0 * metres) if metres else 1.0  # drawing units per millimetre (mm when unknown)
        self.tol = C.REPEAT_MATCH_TOLERANCE_MM * self.mm
        self.segs = segments(index.select(window, limit=3_000_000))
        self.present: set = set()
        for s in self.segs:
            k = _key(s, self.tol)
            for dx in (-1, 0, 1):  # tolerance across bucket borders
                for dy in (-1, 0, 1):
                    self.present.add(((k[0][0] + dx, k[0][1] + dy), (k[1][0] + dx, k[1][1] + dy)))
        self.by_length: dict[int, list[Segment]] = defaultdict(list)
        for s in self.segs:
            self.by_length[round(math.dist(s[:2], s[2:]) / self.tol)].append(s)

    def has(self, s: Segment) -> bool:
        return _key(s, self.tol) in self.present


def match(scene: Scene, box: list[float]) -> dict[str, Any]:
    """Places in the view where the linework inside `box` repeats; each place is where the box's lower-left corner lands."""

    started = time.time()
    inside = [ln for ln in scene.index.select(box) if all(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in ln["p"])]
    rel0 = [(s[0] - box[0], s[1] - box[1], s[2] - box[0], s[3] - box[1]) for s in segments(inside)]
    if not rel0:
        return {"segments": 0, "places": [], "seconds": 0.0}
    w, h = box[2] - box[0], box[3] - box[1]
    variants = [rel0,
                [(w - a, b, w - c, d) for a, b, c, d in rel0],
                [(a, h - b, c, h - d) for a, b, c, d in rel0],
                [(w - a, h - b, w - c, h - d) for a, b, c, d in rel0]]
    hits = []
    for rel in variants:
        anchor = max(rel, key=lambda s: math.dist(s[:2], s[2:]))
        alen = math.dist(anchor[:2], anchor[2:])
        ux, uy = (anchor[2] - anchor[0]) / alen, (anchor[3] - anchor[1]) / alen
        offsets = set()
        bucket = round(alen / scene.tol)
        for s in scene.by_length.get(bucket - 1, []) + scene.by_length.get(bucket, []) + scene.by_length.get(bucket + 1, []):
            L = math.dist(s[:2], s[2:])
            if abs(L - alen) > scene.tol:
                continue
            d = ((s[2] - s[0]) / L, (s[3] - s[1]) / L)
            if abs(d[0] - ux) < 1e-3 and abs(d[1] - uy) < 1e-3:
                offsets.add((round(s[0] - anchor[0], 1), round(s[1] - anchor[1], 1)))
            elif abs(d[0] + ux) < 1e-3 and abs(d[1] + uy) < 1e-3:
                offsets.add((round(s[2] - anchor[0], 1), round(s[3] - anchor[1], 1)))
        lengths = [math.dist(r[:2], r[2:]) for r in rel]
        total = sum(lengths)
        for ox, oy in offsets:
            # weighted by length: the long members (beams, rails, frames) decide; small details may differ
            found = sum(L for r, L in zip(rel, lengths) if scene.has((r[0] + ox, r[1] + oy, r[2] + ox, r[3] + oy)))
            if found >= C.REPEAT_MATCH_SHARE * total:
                hits.append((ox, oy, found / total))
    kept: list[tuple[float, float, float]] = []
    for ox, oy, score in sorted(hits, key=lambda t: -t[2]):  # one place per spot
        if all(abs(ox - kx) > 0.5 * w or abs(oy - ky) > 0.5 * h for kx, ky, _ in kept):
            kept.append((ox, oy, score))
    return {"segments": len(rel0), "places": [[x, y] for x, y, _ in kept], "seconds": round(time.time() - started, 1)}


def spacings(scene: Scene) -> tuple[dict[str, Counter], dict[tuple, list[tuple[float, float]]]]:
    """How often long straight members of one length repeat along x (same y) and along y (same x), weighted by length."""

    votes = {"x": Counter(), "y": Counter()}
    groups: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    step = 10.0 * scene.mm
    for s in scene.segs:
        horizontal, vertical = abs(s[1] - s[3]) < scene.mm, abs(s[0] - s[2]) < scene.mm
        L = math.dist(s[:2], s[2:])
        if (horizontal or vertical) and L >= C.REPEAT_MIN_MEMBER_MM * scene.mm:
            groups[(round(L / step), horizontal)].append((min(s[0], s[2]), min(s[1], s[3])))
    for (length, _), pts in groups.items():
        if len(pts) < C.REPEAT_MIN_COPIES:
            continue
        for axis, (a, b) in (("x", (1, 0)), ("y", (0, 1))):
            lines = defaultdict(list)
            for p in pts:
                lines[round(p[a] / (2 * step))].append(p[b])
            for cs in lines.values():
                cs.sort()
                for u, v in zip(cs, cs[1:]):
                    d = round((v - u) / step) * step
                    if C.REPEAT_MIN_MEMBER_MM * scene.mm <= d <= C.REPEAT_MAX_SPACING_MM * scene.mm:
                        votes[axis][d] += length
    return votes, groups


def place(scene: Scene, cx: float, cy: float, dx: float, dy: float, reach: tuple[float, float] = (0.5, 0.5)) -> list[float] | None:
    """A dx x dy box near (cx, cy), moved by up to reach x its size, whose edges keep the most linework whole."""

    import numpy as np

    near = segments(scene.index.select([cx - dx, cy - dy, cx + 2 * dx, cy + 2 * dy], limit=500000))
    if not near:
        return None
    a = np.array(near)
    x0, x1 = np.minimum(a[:, 0], a[:, 2]), np.maximum(a[:, 0], a[:, 2])
    y0, y1 = np.minimum(a[:, 1], a[:, 3]), np.maximum(a[:, 1], a[:, 3])
    length = np.hypot(a[:, 2] - a[:, 0], a[:, 3] - a[:, 1])
    step = 20.0 * scene.mm
    best = None
    for ox in (np.arange(-reach[0] * dx, reach[0] * dx, step) if reach[0] else [0.0]):
        inx = (x0 >= cx + ox) & (x1 <= cx + ox + dx)
        if not inx.any():
            continue
        for oy in (np.arange(-reach[1] * dy, reach[1] * dy, step) if reach[1] else [0.0]):
            kept = length[inx & (y0 >= cy + oy) & (y1 <= cy + oy + dy)].sum()
            if best is None or kept > best[0]:
                best = (kept, [float(cx + ox), float(cy + oy), float(cx + ox + dx), float(cy + oy + dy)])
    return best[1] if best else None


def _try_cell(scene: Scene, groups: dict, along: str, dx: float, dy: float, seed: int) -> dict[str, Any] | None:
    """Match a dx x dy cell placed at a few anchors; the largest count at least two anchors agree on is the answer."""

    pitch = dx if along == "x" else dy
    a = 0 if along == "x" else 1
    inner = []  # members repeated two spacings either side: the middle of a run, away from ends drawn differently
    for pts in groups.values():
        have = {(round(p[0] / scene.tol), round(p[1] / scene.tol)) for p in pts}

        def repeated(p: tuple[float, float], n: int) -> bool:
            q = list(p)
            q[a] += n * pitch
            k = (round(q[0] / scene.tol), round(q[1] / scene.tol))
            return any((k[0] + e, k[1]) in have or (k[0], k[1] + e) in have for e in (-1, 0, 1))

        inner += [p for p in pts if all(repeated(p, n) for n in (-2, -1, 1, 2))]
    if not inner:
        return None
    inner.sort()
    tries = []
    for cx, cy in random.Random(seed).sample(inner, min(C.REPEAT_ANCHORS, len(inner))):
        box = place(scene, cx, cy, dx, dy)
        if box is not None:
            tries.append({"box": box, "places": match(scene, box)["places"]})
    counts = Counter(len(t["places"]) for t in tries)
    if not counts:
        return None
    # the largest count at least two anchors agree on: an anchor near a run's end finds less; a single large count can
    # come from a cell too generic to mean anything
    agreed = max((n for n in counts if counts[n] >= 2), default=0)
    if agreed < C.REPEAT_MIN_COPIES:
        return None
    chosen = next(t for t in tries if len(t["places"]) == agreed)
    return {"box": chosen["box"], "places": chosen["places"], "along": along, "spacing": [dx, dy],
            "anchors": [len(t["places"]) for t in tries], "agreed_by": counts[agreed]}


def first_example(scene: Scene, seed: int = 0) -> dict[str, Any] | None:
    """The program's own example: one spacing along a row by one unit across it.  Either direction may carry the rows
    (aisles can repeat more often than bays), so both are tried and the smallest cell several anchors agree on wins --
    the finest unit, one bay rather than an aisle of bays."""

    votes, groups = spacings(scene)
    if not votes["x"] or not votes["y"]:
        return None
    cells = set()
    for along, across in (("x", "y"), ("y", "x")):
        pitch = votes[along].most_common(1)[0][0]
        depth = min(d for d, _ in votes[across].most_common(2))
        dx, dy = (pitch, depth) if along == "x" else (depth, pitch)
        if dx * dy / (1e6 * scene.mm * scene.mm) >= C.REPEAT_MIN_CELL_M2:
            cells.add((dx * dy, along, dx, dy))
    for _, along, dx, dy in sorted(cells):
        found = _try_cell(scene, groups, along, dx, dy, seed)
        if found:
            return found
    return None


def _cells(example: dict[str, Any]) -> list[list[float]]:
    b = example["box"]
    w, h = b[2] - b[0], b[3] - b[1]
    return [[x, y, x + w, y + h] for x, y in example["places"]]


def _inside(s: Segment, b: list[float], pad: float) -> bool:
    return min(s[0], s[2]) >= b[0] - pad and max(s[0], s[2]) <= b[2] + pad and min(s[1], s[3]) >= b[1] - pad and max(s[1], s[3]) <= b[3] + pad


def _new(cells: list[list[float]], box: list[float], places: list[list[float]]) -> list[list[float]]:
    w, h = box[2] - box[0], box[3] - box[1]
    out = []
    for x, y in places:
        c = [x, y, x + w, y + h]
        overlap = lambda o: max(0.0, min(c[2], o[2]) - max(c[0], o[0])) * max(0.0, min(c[3], o[3]) - max(c[1], o[1]))  # noqa: E731
        if not any(overlap(o) > 0.5 * min(w * h, (o[2] - o[0]) * (o[3] - o[1])) for o in cells + out):
            out.append(c)
    return out


def _leftover_example(scene: Scene, cells: list[list[float]], along: str, asked: set) -> tuple[list[float], Any] | None:
    """A member along the rows that no found cell holds (e.g. a shorter end beam), boxed out to the neighbouring cell."""

    a, b = (0, 1) if along == "x" else (1, 0)
    unit = cells[0][2 + a] - cells[0][a]
    depth = cells[0][2 + b] - cells[0][b]
    rows = defaultdict(list)
    for c in cells:
        rows[round(c[b] / scene.tol)].append(c)
    groups = defaultdict(list)
    for row in rows.values():
        lo, hi = min(c[a] for c in row) - 2 * unit, max(c[2 + a] for c in row) + 2 * unit
        band = [0.0] * 4
        band[a], band[2 + a], band[b], band[2 + b] = lo, hi, row[0][b], row[0][b] + depth
        for s in segments(scene.index.select(band, limit=200000)):
            L = math.dist(s[:2], s[2:])
            parallel = abs(s[b] - s[2 + b]) < scene.mm  # runs along the rows
            if parallel and 0.3 * unit <= L <= 1.2 * unit and _inside(s, band, scene.mm) and not any(_inside(s, c, scene.mm) for c in row):
                groups[round(L / (10 * scene.mm))].append((min(s[a], s[2 + a]), max(s[a], s[2 + a]), row))
    groups = {L: m for L, m in groups.items() if L not in asked and len(m) >= 2}
    if not groups:
        return None
    L, members = max(groups.items(), key=lambda kv: len(kv[1]))
    asked.add(L)
    s0, s1, row = members[len(members) // 2]
    before = max((c[2 + a] for c in row if c[2 + a] <= s0 + scene.mm), default=None)
    after = min((c[a] for c in row if c[a] >= s1 - scene.mm), default=None)
    box = [0.0] * 4
    box[b], box[2 + b] = row[0][b], row[0][b] + depth
    if s1 - s0 >= 0.85 * unit:  # about one unit long: the grid cell it sits in (a full unit drawn differently)
        phase = row[0][a] % unit
        start = (s0 + s1) / 2 - (((s0 + s1) / 2 - phase) % unit)
        box[a], box[2 + a] = start, start + unit
        return box, L * 10
    margin = 0.05 * unit  # clearly shorter: from its own ends out to the neighbouring found place (a shorter end unit)
    box[a] = before if before is not None and s0 - before < 0.2 * unit else s0 - margin
    box[2 + a] = after if after is not None and after - s1 < 0.2 * unit else s1 + margin
    return box, L * 10


def _gap_example(scene: Scene, cells: list[list[float]], along: str, tried: set) -> list[float] | None:
    """A place that continues a found row by whole units -- a hole inside the row first, then up to two units past
    either end -- and holds linework but no match.  Each row keeps its own offset (mirrored rows sit shifted)."""

    a, b = (0, 1) if along == "x" else (1, 0)
    unit = cells[0][2 + a] - cells[0][a]
    need = 0.5 * scene.index.count(cells[0])
    rows: dict[int, list[list[float]]] = defaultdict(list)
    for c in cells:
        rows[round(c[b] / scene.tol)].append(c)
    holes, ends = [], []
    for row in rows.values():
        row.sort(key=lambda c: c[a])
        starts = []
        for c, d in zip(row, row[1:]):
            if d[a] - c[a] >= 1.5 * unit:  # a hole: continue from both sides
                starts += [(0, c[a] + unit, c), (0, d[a] - unit, c)]
        starts += [(k, row[0][a] - k * unit, row[0]) for k in (1, 2)] + [(k, row[-1][a] + k * unit, row[-1]) for k in (1, 2)]
        for k, p, c in starts:
            box = list(c)
            box[a], box[2 + a] = p, p + unit
            key = (round(p / scene.tol), round(c[b] / scene.tol))
            if key not in tried and _new(cells, box, [[box[0], box[1]]]) and scene.index.count(box) >= need:  # not covered, not empty
                (holes if k == 0 else ends).append((k, key, box))
    for pool in (holes, sorted(ends, key=lambda t: t[0])):
        if pool:
            k = pool[0][0]
            same = [t for t in pool if t[0] == k]
            _, key, box = same[len(same) // 2]
            tried.add(key)
            return box
    return None


def _split(first: list[list[float]], new: list[list[float]], along: str) -> tuple[list[list[float]], list[list[float]]]:
    """New places inside a row the program found (between its first and last place) and the rest (past a row's end,
    or in no row): two different questions -- a bay drawn differently inside a rack, or something at or beyond its end."""

    a, b = (0, 1) if along == "x" else (1, 0)
    rows: dict[tuple, list[float]] = {}
    for c in first:
        key = (round(c[b]), round(c[2 + b]))
        lo, hi = rows.get(key, [c[a], c[2 + a]])
        rows[key] = [min(lo, c[a]), max(hi, c[2 + a])]
    inside, outside = [], []
    for c in new:
        mid = (c[a] + c[2 + a]) / 2
        depth = c[2 + b] - c[b]
        hit = any(min(c[2 + b], k[1]) - max(c[b], k[0]) > 0.5 * depth and lo <= mid <= hi for k, (lo, hi) in rows.items())
        (inside if hit else outside).append(c)
    return inside, outside


def count_view(index: Any, window: list[float], seed: int = 0) -> dict[str, Any]:
    """The groups of repeated linework in one view: group 1 found by the program alone, later groups are questions.
    Each round takes the next follow-up example -- a leftover member, else a hole or a row end; fewer than
    REPEAT_MIN_COPIES new places is not worth a question.  A place may hold more than one unit (the cell can straddle
    two back-to-back rows): how many is asked of the person, who sees the example."""

    started = time.time()
    scene = Scene(index, window)
    first = first_example(scene, seed)
    if first is None:
        return {"window": window, "groups": [], "seconds": round(time.time() - started, 1)}
    cells = _cells(first)
    found = list(cells)
    groups = [{"id": 1, "by": "program", "example": first["box"], "places": found, "spacing": first["spacing"],
               "anchors": first["anchors"], "agreed_by": first["agreed_by"]}]
    asked, tried = set(), set()
    for _ in range(3 * C.REPEAT_MAX_FOLLOW_UPS):
        if len(groups) > C.REPEAT_MAX_FOLLOW_UPS:
            break
        # in this order, the first that finds enough new places: the largest count would favour a too generic example
        got = _leftover_example(scene, cells, first["along"], asked)
        box, member = got if got else (_gap_example(scene, cells, first["along"], tried), None)
        if box is None:
            break
        new = _new(cells, box, match(scene, box)["places"])
        if len(new) < C.REPEAT_MIN_COPIES:
            continue  # a handful of odd places is not worth a question
        cells += new
        w, h = box[2] - box[0], box[3] - box[1]
        for part, where in zip(_split(found, new, first["along"]), ("inside_rows", "row_ends_or_outside")):
            if len(part) < C.REPEAT_MIN_COPIES or len(groups) > C.REPEAT_MAX_FOLLOW_UPS:
                continue
            own = any(abs(c[0] - box[0]) < 0.5 * w and abs(c[1] - box[1]) < 0.5 * h for c in part)
            example = box if own else [part[0][0], part[0][1], part[0][0] + w, part[0][1] + h]
            groups.append({"id": len(groups) + 1, "by": "question", "where": where, "example": example, "places": part, "member_mm": member})
    return {"window": window, "groups": groups, "seconds": round(time.time() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    from .agent_tools import Index

    args = argv if argv is not None else sys.argv[1:]
    result = count_view(Index(args[0]), [float(v) for v in args[1].split(",")])
    Path(args[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"groups": [(g["id"], g["by"], len(g["places"])) for g in result["groups"]], "seconds": result["seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
