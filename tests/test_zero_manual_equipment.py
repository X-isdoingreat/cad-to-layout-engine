from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual.equipment import recognise
from cad_to_layout.zero_manual.inventory import build_inventory


def _rect(block, w: float, h: float) -> None:
    block.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)


def _drawing(path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    _rect(doc.blocks.new("bolt"), 100, 100)                       # tiny part
    _rect(doc.blocks.new("堆垛机"), 4000, 1500)                    # named in Chinese
    named = doc.blocks.new("X17")                                  # text inside says chain conveyor
    _rect(named, 3000, 1300)
    named.add_text("链条输送机", dxfattribs={"insert": (100, 100), "height": 100})
    roller = doc.blocks.new("r1")                                  # a part inside the conveyor below
    _rect(roller, 1200, 80)
    conv = doc.blocks.new("QWER")                                  # meaningless name, judged by shape
    _rect(conv, 3000, 1300)
    for k in range(5):
        conv.add_blockref("r1", (50, 100 + k * 200))
    _rect(doc.blocks.new("ZXCV"), 2000, 2000)                      # judges disagree
    _rect(doc.blocks.new("LONG"), 40000, 800)                      # judged BOX_LIFT but 40 m long
    msp = doc.modelspace()
    for i in range(4):
        msp.add_blockref("bolt", (i * 500, -3000))
        msp.add_blockref("堆垛机", (i * 5000, 0))
        msp.add_blockref("X17", (i * 5000, 3000))
        msp.add_blockref("QWER", (i * 5000, 6000))
        msp.add_blockref("ZXCV", (i * 5000, 9000))
    msp.add_blockref("LONG", (0, 12000))
    doc.saveas(path)


class FakeJudge:
    """Answers by facts: QWER -> PALLET_CONV_ROLLER, r1 -> COMPONENT, ZXCV alternates, LONG -> BOX_LIFT."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        self.calls += 1
        tiles = []
        for line in prompt.split("Facts per tile:")[1].strip().splitlines():
            tid = line.split(":", 1)[0]
            name = re.search(r"block name: (\S+)", line).group(1)
            kind = {"QWER": "PALLET_CONV_ROLLER", "r1": "COMPONENT", "LONG": "BOX_LIFT"}.get(name)
            if name == "ZXCV":
                kind = ["BOX_LIFT", "ROBOT_ARM", "UNSURE"][(self.calls - 1) % 3]
            tiles.append({"id": tid, "type": kind or "UNSURE", "reason": "fake"})
        return {"text": json.dumps({"tiles": tiles}), "provider": "fake", "model": "fake"}


class EquipmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _drawing(self.root / "d.dxf")
        self.inventory = build_inventory(self.root / "d.dxf")
        self.doc = ezdxf.readfile(self.root / "d.dxf")
        self.views = [{"view_id": "V:all", "window": [-10000, -10000, 60000, 20000], "source": "model_space_cluster"}]
        self.unit = {"unit": "mm", "status": "declared"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, judge=None, allow=True, replay=None, out="w"):
        return recognise(self.inventory, self.views, self.unit, self.doc, self.root / out, judge, allow, replay)

    def test_rules_first_then_llm_with_checks(self) -> None:
        judge = FakeJudge()
        result = self._run(judge)
        fams = result["families"]
        self.assertEqual(fams["bolt"]["decision"]["type"], "COMPONENT")
        self.assertEqual(fams["堆垛机"]["decision"], {**fams["堆垛机"]["decision"], "type": "STACKER", "basis": "text", "stage": "rules"})
        self.assertEqual(fams["X17"]["decision"]["type"], "PALLET_CONV_CHAIN")
        self.assertEqual(fams["QWER"]["decision"]["basis"], "llm_shape")
        self.assertEqual(fams["QWER"]["decision"]["type"], "PALLET_CONV_ROLLER")
        self.assertEqual(fams["ZXCV"]["decision"]["type"], "UNRECOGNISED")
        self.assertEqual(fams["LONG"]["decision"]["reason"], "size_implausible_for_type")
        self.assertEqual(judge.calls, 3)
        by_type: dict[str, list[dict]] = {}
        for r in result["records"]:
            by_type.setdefault(r["type"], []).append(r)
        self.assertEqual(len(by_type["STACKER"]), 4)
        self.assertEqual(len(by_type["PALLET_CONV_ROLLER"]), 4, "rollers inside the conveyor are not counted")
        self.assertTrue(all(r["status"] == "pending_independent_count_and_spot_check" for r in by_type["PALLET_CONV_ROLLER"]))
        self.assertTrue(all(r["status"] == "accepted" for r in by_type["STACKER"]))
        self.assertEqual(by_type["STACKER"][0]["length_mm"], 4000.0)
        counts = result["summary"]["V:all"]
        self.assertEqual(counts["PALLET_CONV_ROLLER"], {"text_or_geometry": 0, "llm_shape": 4})
        self.assertEqual(counts["STACKER"], {"text_or_geometry": 4, "llm_shape": 0})

    def test_llm_off_leaves_shape_families_unrecognised(self) -> None:
        judge = FakeJudge()
        result = self._run(judge, allow=False)
        self.assertEqual(judge.calls, 0)
        self.assertEqual(result["families"]["QWER"]["decision"]["type"], "UNRECOGNISED")
        self.assertNotIn("PALLET_CONV_ROLLER", result["summary"].get("V:all", {}))

    def test_replay_uses_archive_without_calls(self) -> None:
        first = self._run(FakeJudge())
        archive = self.root / "w" / "type_judge" / "type_judge_archive.json"
        again_judge = FakeJudge()
        second = self._run(again_judge, allow=False, replay=archive, out="w2")
        self.assertEqual(again_judge.calls, 0)
        self.assertEqual(first["summary"], second["summary"])


if __name__ == "__main__":
    unittest.main()
