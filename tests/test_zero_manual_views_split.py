from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual.inventory import build_inventory
from cad_to_layout.zero_manual.split import candidate_cuts
from cad_to_layout.zero_manual.views import segment_views
from cad_to_layout.zero_manual.views_split import split_regions


def _four_plans(path: Path) -> None:
    """2x2 dense plans separated by gaps too small for gap clustering but wide enough for cuts."""

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0  # no declared unit
    block = doc.blocks.new("M")
    block.add_lwpolyline([(0, 0), (500, 0), (500, 500), (0, 500)], close=True)
    msp = doc.modelspace()
    for cx, cy, title in ((0, 0, "一层平面图"), (5000, 0, "二层平面图"), (0, 5000, "三层平面图"), (5000, 5000, "四层平面图")):
        for i in range(4):
            for j in range(4):
                msp.add_blockref("M", (cx + i * 500, cy + j * 500))
        msp.add_text(title, dxfattribs={"insert": (cx, cy + 2100), "height": 200})
    doc.saveas(path)


class FakeTransport:
    def __init__(self, answer: str, *answers: str) -> None:
        self.answers = [answer, *answers]
        self.calls = 0

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        self.calls += 1
        return {"text": self.answers[min(self.calls - 1, len(self.answers) - 1)], "returncode": 0, "provider": "fake", "model": "fake"}


class ViewsSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _four_plans(self.root / "d.dxf")
        self.inventory = build_inventory(self.root / "d.dxf")
        self.segmented = segment_views(self.inventory)
        self.assertEqual(len(self.segmented["views"]), 1, "gap clustering keeps the four plans as one region")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cut_ids(self) -> list[int]:
        view = self.segmented["views"][0]
        boxes = [(r, b) for r, b in self.segmented["region_boxes"]]
        return [c["id"] for c in candidate_cuts(boxes, view["window"])]

    def test_agreed_judge_splits_into_four_titled_plans(self) -> None:
        transport = FakeTransport('{"drawing_count": 4, "cut_ids": %s}' % self._cut_ids())
        result = split_regions(self.inventory, self.segmented, work_dir=self.root / "w", transport=transport, allow_external=True)
        pieces = [v for v in result["views"] if v["source"] == "judged_split"]
        self.assertEqual(len(pieces), 4)
        self.assertEqual(sorted(v["title"]["text"] for v in pieces), sorted(["一层平面图", "二层平面图", "三层平面图", "四层平面图"]))
        self.assertEqual(result["split_receipts"][0]["status"], "split")
        self.assertEqual(transport.calls, 3)
        replayed = split_regions(self.inventory, self.segmented, work_dir=self.root / "w2", transport=None, allow_external=False, replay_dir=self.root / "w")
        self.assertEqual(len([v for v in replayed["views"] if v["source"] == "judged_split"]), 4)

    def test_equivalent_cut_lines_count_as_agreement(self) -> None:
        # two parallel blank strips between the same plans: judge names different ids each time
        doc = ezdxf.new("R2018"); doc.header["$INSUNITS"] = 0
        block = doc.blocks.new("M"); block.add_lwpolyline([(0, 0), (500, 0), (500, 500), (0, 500)], close=True)
        msp = doc.modelspace()
        for cx in (0, 6000):
            for i in range(4):
                for j in range(4):
                    msp.add_blockref("M", (cx + i * 500, j * 500))
        msp.add_blockref("M", (3000, 0))  # a stray object in the gap makes two strips
        doc.saveas(self.root / "e.dxf")
        inventory = build_inventory(self.root / "e.dxf")
        segmented = segment_views(inventory)
        view = segmented["views"][0]
        cuts = candidate_cuts([(r, b) for r, b in segmented["region_boxes"]], view["window"])
        self.assertEqual(len(cuts), 2)
        transport = FakeTransport('{"drawing_count": 2, "cut_ids": [1]}', '{"drawing_count": 2, "cut_ids": [2]}', '{"drawing_count": 2, "cut_ids": [1]}')
        result = split_regions(inventory, segmented, work_dir=self.root / "w", transport=transport, allow_external=True)
        self.assertEqual(result["split_receipts"][0]["status"], "split")

    def test_wrong_drawing_count_is_rejected_and_region_stays_whole(self) -> None:
        transport = FakeTransport('{"drawing_count": 3, "cut_ids": %s}' % self._cut_ids())
        result = split_regions(self.inventory, self.segmented, work_dir=self.root / "w", transport=transport, allow_external=True)
        self.assertEqual(len(result["views"]), 1)
        self.assertEqual(result["views"][0]["split_status"], "unsplit_cut_rejected")

    def test_external_llm_disabled_leaves_region_whole_without_calls(self) -> None:
        transport = FakeTransport('{"drawing_count": 4, "cut_ids": [1, 2]}')
        result = split_regions(self.inventory, self.segmented, work_dir=self.root / "w", transport=transport, allow_external=False)
        self.assertEqual(result["views"][0]["split_status"], "unsplit_judge_skipped")
        self.assertEqual(transport.calls, 0)


if __name__ == "__main__":
    unittest.main()
