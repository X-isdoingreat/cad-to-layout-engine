"""Counting by repetition on a drawing built here: racks drawn as loose lines (no blocks), back-to-back rows mirrored,
one shorter bay at the west end of every row."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ezdxf

from cad_to_layout.zero_manual import agent_tools
from cad_to_layout.zero_manual.repeat_count import count_view

PITCH, DEPTH, POST = 2800.0, 1000.0, 100.0
BAYS, PAIRS = 12, 5  # 10 rows: the 10 short end bays are enough for a question (REPEAT_MIN_COPIES)


def _bay(msp, x: float, y: float, width: float, mirrored: bool) -> None:
    """One bay: a post at its west end (front and back), two beams, and a pallet stop near the aisle side."""

    for py in (y, y + DEPTH - POST):
        msp.add_lwpolyline([(x, py), (x + POST, py), (x + POST, py + POST), (x, py + POST)], close=True)
    for by in (y + POST / 2, y + DEPTH - POST / 2):
        msp.add_line((x + POST, by), (x + width, by))
    stop = y + 150 if not mirrored else y + DEPTH - 150  # the aisle side: front of one row, back of its mirror
    msp.add_line((x + 600, stop), (x + width - 600, stop))


def _drawing(path: Path, racks: bool = True) -> None:
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    if racks:
        for pair in range(PAIRS):
            for row in range(2):
                y = pair * (2 * DEPTH + 3000) + row * DEPTH
                _bay(msp, -1400.0, y, 1400.0, bool(row))  # the short west-end bay
                for k in range(BAYS):
                    _bay(msp, k * PITCH, y, PITCH, bool(row))
                for py in (y, y + DEPTH - POST):  # the post closing the row
                    msp.add_lwpolyline([(BAYS * PITCH, py), (BAYS * PITCH + POST, py), (BAYS * PITCH + POST, py + POST), (BAYS * PITCH, py + POST)], close=True)
    msp.add_line((-5000, -3000), (40000, -3000))  # something that does not repeat
    doc.saveas(path)


class RepeatCountTest(unittest.TestCase):
    def _run(self, racks: bool):
        with tempfile.TemporaryDirectory() as tmp:
            dxf, idx = Path(tmp) / "d.dxf", Path(tmp) / "index.sqlite"
            _drawing(dxf, racks)
            agent_tools.build_index(str(dxf), idx)
            index = agent_tools.Index(idx)
            try:
                return count_view(index, [-6000, -5000, 40000, 30000])
            finally:
                index.con.close()
                agent_tools.close_all()

    def test_program_finds_every_full_bay_mirrored_rows_included(self) -> None:
        first = self._run(True)["groups"][0]
        self.assertEqual(first["by"], "program")
        e = first["example"]
        self.assertAlmostEqual(e[2] - e[0], PITCH, delta=1)
        # the example may straddle two back-to-back rows; the person, who sees it, says how many bays one place is:
        # the rows it touches.  Places x that number is every full bay.
        rows = [pair * (2 * DEPTH + 3000) + row * DEPTH for pair in range(PAIRS) for row in range(2)]
        per_place = sum(1 for y in rows if min(e[3], y + DEPTH) - max(e[1], y) > 0.2 * DEPTH)
        self.assertIn(per_place, (1, 2))
        self.assertEqual(len(first["places"]) * per_place, BAYS * PAIRS * 2)

    def test_short_end_bays_come_back_as_a_question_not_a_count(self) -> None:
        groups = self._run(True)["groups"]
        follow = [g for g in groups if g["by"] == "question"]
        self.assertTrue(any(len(g["places"]) == PAIRS * 2 for g in follow), [(g["id"], len(g["places"])) for g in groups])

    def test_nothing_repeating_gives_no_group(self) -> None:
        self.assertEqual(self._run(False)["groups"], [])


if __name__ == "__main__":
    unittest.main()
