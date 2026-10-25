"""Rows of cells (2026-10-24): a block holding one kind of cell in a regular array that spans it is a row, judged as one
unit; its cells are never asked about alone; the count is rows and cells.  A long thin block is not a whole-plan container."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual.equipment import equipment_markdown, recognise
from cad_to_layout.zero_manual.inventory import build_inventory
from cad_to_layout.zero_manual.views import segment_views

VIEWS = [{"view_id": "V:all", "window": [-50000, -50000, 200000, 200000], "source": "model_space_cluster"}]
UNIT = {"unit": "mm", "status": "declared"}


def _rect(space, x: float, y: float, w: float, h: float) -> None:
    space.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True)


class Judge:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: set[str] = set()
        self.facts: dict[str, str] = {}

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        tiles = []
        for line in prompt.split("Facts per tile:")[1].strip().splitlines():
            name = re.search(r"block name: (\S+)", line).group(1)
            self.asked.add(name)
            self.facts[name] = line
            tiles.append({"id": line.split(":", 1)[0], "type": self.answers.get(name, "UNSURE"), "reason": "fake", "wrong_if": "fake wrong_if"})
        return {"text": json.dumps({"tiles": tiles}), "provider": "fake", "model": "fake"}


class RowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _recognise(self, doc: Any, answers: dict[str, str]) -> dict[str, Any]:
        path = self.root / "d.dxf"
        doc.saveas(path)
        inventory = build_inventory(path)
        self.judge = Judge(answers)
        return recognise(inventory, VIEWS, UNIT, ezdxf.readfile(path), self.root / "w", self.judge, True)


def _shelving(cell_name: str = "cell", cell_w: float = 450.0) -> Any:
    """A row block of 20 cells (and 21 beams, themselves a regular array) at 500 mm pitch; a pair block holding two rows;
    three pairs, one loose row and five loose cells in model space.  The default cell is smaller than the single-block size
    rule allows; the beam is not, so only the row rule can make it a part."""

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    _rect(doc.blocks.new(cell_name), 0, 0, cell_w, 400)
    _rect(doc.blocks.new("beam"), 0, 0, 520, 60)
    row = doc.blocks.new("row")
    for i in range(20):
        row.add_blockref(cell_name, (i * 500, 0))
        row.add_blockref("beam", (i * 500 - 50, 170))
    row.add_blockref("beam", (20 * 500 - 50, 170))
    pair = doc.blocks.new("pair")
    _rect(pair, -100, -100, 11600, 1500)
    pair.add_blockref("row", (0, 0))
    pair.add_blockref("row", (0, 800))
    msp = doc.modelspace()
    for k in range(3):
        msp.add_blockref("pair", (0, k * 5000))
    msp.add_blockref("row", (0, 20000))
    for i in range(5):
        msp.add_blockref(cell_name, (30000 + i * 2000, 0))
    return doc


class RowRecognitionTests(RowCase):
    def test_the_row_is_judged_whole_and_its_cells_and_parts_are_never_asked(self) -> None:
        result = self._recognise(_shelving(), {"row": "RACK", "pair": "RACK"})
        self.assertEqual(self.judge.asked, {"row", "pair"})
        self.assertIn("THIS BLOCK IS ONE ROW: it holds 20 identical cells (cell, 450 x 400 mm) at 500 mm pitch", self.judge.facts["row"])
        fams = result["families"]
        self.assertEqual((fams["row"]["decision"]["type"], fams["row"]["decision"]["basis"]), ("RACK", "llm_shape"))
        self.assertEqual((fams["cell"]["decision"]["type"], fams["cell"]["decision"]["basis"], fams["cell"]["decision"]["rows"]), ("RACK", "row_structure", ["row"]))
        self.assertEqual(fams["beam"]["decision"]["evidence"][0], {"kind": "part_of_a_row", "row": "row"}, "an array of parts along the row is not the cells")
        self.assertEqual(result["stage_counts"]["cell_size_rule_waived"], 1, "the cell's small size does not decide it: its row does")
        kinds = Counter((r["family"], r["drawn_as"], r.get("cells")) for r in result["records"])
        self.assertEqual(kinds, {("row", "row", 20): 7, ("cell", "loose_cell", None): 5})
        self.assertEqual(result["cell_occurrences_counted_through_rows"], 140)
        self.assertEqual([(r["row_family"], r["cell_family"], r["rows_counted"], r["cells_per_row"], r["cells_total"], r["type_zh"]) for r in result["rows"]],
                         [("row", "cell", 7, 20, 140, "货架")])
        self.assertEqual([a["reason"] for a in result["assemblies_not_counted"]], ["holds_counted_rows"] * 3)
        self.assertEqual(result["summary"]["V:all"]["RACK"], {"text_or_geometry": 0, "llm_shape": 12}, "rows count once each, loose cells once each")
        self.assertIn("成排画的货架：row 7 排，每排 20 格（450×400 mm，节距 500 mm），共 140 格", equipment_markdown(result, VIEWS))

    def test_cells_named_by_a_text_name_their_row_without_a_judge(self) -> None:
        result = self._recognise(_shelving("货架", 600.0), {"pair": "NOT_EQUIPMENT"})
        self.assertNotIn("row", self.judge.asked)
        d = result["families"]["row"]["decision"]
        self.assertEqual((d["type"], d["basis"], d["evidence"][0]["kind"]), ("RACK", "text", "row_of_text_named_cells"))
        self.assertEqual(Counter(r["family"] for r in result["records"]), {"row": 7, "货架": 5})
        self.assertTrue(all(r["status"] == "accepted" for r in result["records"] if r["family"] == "row"))

    def test_an_unrecognised_row_leaves_its_cells_unrecognised_and_not_listed_one_by_one(self) -> None:
        result = self._recognise(_shelving(), {"row": "UNSURE", "pair": "NOT_EQUIPMENT"})
        self.assertEqual(result["families"]["cell"]["decision"]["reason"], "row_not_recognised")
        self.assertEqual(result["records"], [])
        untyped = Counter(c["key"] for c in result["candidates_untyped"])
        self.assertEqual(untyped, {"row": 7, "cell": 5}, "cells inside their rows are not candidates on their own")

    def test_a_conveyor_line_of_segments_is_checked_by_its_segment_size(self) -> None:
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 4
        _rect(doc.blocks.new("seg"), 0, 0, 1000, 600)
        line = doc.blocks.new("line")
        for i in range(70):  # a 70 m line: longer than any single box conveyor
            line.add_blockref("seg", (i * 1000, 0))
        doc.modelspace().add_blockref("line", (0, 0))
        result = self._recognise(doc, {"line": "BOX_CONVEYOR"})
        self.assertEqual(result["families"]["line"]["decision"]["type"], "BOX_CONVEYOR")
        self.assertEqual([(r["family"], r["cells"]) for r in result["records"]], [("line", 70)])


def _one_kind_two_blocks() -> Any:
    """The shelving above plus: `row_b`, the same kind of row drawn with another cell block (20 cells of `cell_b` 450 x 340 mm
    at 500 mm pitch), 4 copies 5.6 m above the loose row; `row_c`, 20 of the same cells at 550 mm pitch (10 % off), 3 copies;
    `row_d`, 20 cells of `cell_d` 450 x 200 mm at 500 mm pitch (half the depth), 2 copies."""

    doc = _shelving()
    msp = doc.modelspace()
    for name, cell, cell_h, pitch, copies, y0 in (("row_b", "cell_b", 340, 500, 4, 26000), ("row_c", "cell", 400, 550, 3, 40000),
                                                  ("row_d", "cell_d", 200, 500, 2, 50000)):
        if cell not in doc.blocks:
            _rect(doc.blocks.new(cell), 0, 0, 450, cell_h)
        block = doc.blocks.new(name)
        for i in range(20):
            block.add_blockref(cell, (i * pitch, 0))
        for k in range(copies):
            msp.add_blockref(name, (0, y0 + k * 800))
    return doc


class RowKindTests(RowCase):
    """One kind of row drawn by two blocks (2026-10-25): the block with more copies is asked once, the facts name the other,
    the other takes the answer; counted as one kind.  A different pitch or depth is another kind."""

    def test_one_kind_of_row_drawn_by_two_blocks_is_asked_once_and_counted_as_one_kind(self) -> None:
        result = self._recognise(_one_kind_two_blocks(), {"row": "RACK", "pair": "RACK", "row_c": "RACK", "row_d": "UNSURE"})
        self.assertEqual(self.judge.asked, {"row", "pair", "row_c", "row_d"}, "row_b is the same kind as row: never asked")
        self.assertIn("The same kind of row is also drawn by block row_b: 4 copies, cells cell_b 450 x 340 mm, the same 20 cells at 500 mm pitch, "
                      "340 mm deep; its nearest copy stands 5.6 m from a copy of this block. One answer covers both blocks", self.judge.facts["row"])
        fams = result["families"]
        d = fams["row_b"]["decision"]
        self.assertEqual((d["type"], d["basis"], d["via"], d["same_kind_as"]), ("RACK", "row_kind", "llm_shape", "row"))
        self.assertIn("row_b is not the same kind of row as row", d["wrong_if"])
        self.assertEqual((fams["cell_b"]["decision"]["type"], fams["cell_b"]["decision"]["via"], fams["cell_b"]["decision"]["rows"]), ("RACK", "llm_shape", ["row_b"]),
                         "the cells of a follower row rest on what decided the leader, not on the follower's own basis")
        self.assertEqual(Counter((r["family"], r["row_kind"]) for r in result["records"] if r["drawn_as"] == "row"),
                         {("row", "row"): 7, ("row_b", "row"): 4, ("row_c", "row_c"): 3})
        kinds = {r["row_family"]: (r["rows_counted"], r["cells_total"], r.get("drawn_by")) for r in result["rows"]}
        self.assertEqual(kinds, {"row": (11, 220, {"row": 7, "row_b": 4}), "row_c": (3, 60, None), "row_d": (0, 0, None)})
        self.assertEqual(result["stage_counts"]["rows_of_same_kind"], 1)
        self.assertIn("成排画的货架：row 11 排，其中图块 row_b 画的 4 排，每排 20 格（450×400 mm，节距 500 mm），共 220 格", equipment_markdown(result, VIEWS))

    def test_a_follower_row_is_unrecognised_with_the_leader_of_its_kind(self) -> None:
        result = self._recognise(_one_kind_two_blocks(), {"row": "UNSURE", "pair": "NOT_EQUIPMENT", "row_c": "RACK", "row_d": "UNSURE"})
        self.assertNotIn("row_b", self.judge.asked)
        self.assertEqual(result["families"]["row_b"]["decision"]["reason"], "row_kind_not_recognised")
        self.assertEqual(result["families"]["cell_b"]["decision"]["reason"], "row_not_recognised")
        self.assertEqual(Counter(r["family"] for r in result["records"]), {"row_c": 3, "cell": 5},
                         "the loose cells follow row_c, the recognised row holding the same cell; nothing of row or row_b is counted")


class ContainerOpeningTests(unittest.TestCase):
    def test_long_thin_blocks_are_not_opened_but_wide_or_mixed_containers_are(self) -> None:
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 4
        _rect(doc.blocks.new("m"), 0, 0, 400, 400)
        _rect(doc.blocks.new("cell"), 0, 0, 600, 485)
        _rect(doc.blocks.new("a"), 0, 0, 1000, 600)
        _rect(doc.blocks.new("b"), 0, 0, 1200, 500)
        _rect(doc.blocks.new("c"), 0, 0, 900, 700)
        row = doc.blocks.new("rowblk")  # 30 m x 0.6 m, one kind of child
        for i in range(50):
            row.add_blockref("cell", (i * 600, 0))
        plan = doc.blocks.new("plan")  # 40 m x 30 m, big both ways
        for i in range(8):
            plan.add_blockref("a", (i * 5000, 0))
            plan.add_blockref("a", (i * 5000, 29000))
        _rect(plan, 0, 0, 40000, 30000)
        mixed = doc.blocks.new("mixed")  # 30 m x 2 m, three kinds of child
        for i in range(10):
            mixed.add_blockref("a", (i * 3000, 0))
            mixed.add_blockref("b", (i * 3000, 700))
            mixed.add_blockref("c", (i * 3000, 1300))
        msp = doc.modelspace()
        for i in range(300):
            msp.add_blockref("m", ((i % 30) * 1000, (i // 30) * 1000))
        msp.add_blockref("rowblk", (0, 20000))
        msp.add_blockref("plan", (0, 40000))
        msp.add_blockref("mixed", (0, 80000))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.dxf"
            doc.saveas(path)
            inventory = build_inventory(path)
        segmented = segment_views(inventory)
        by_ref = {o["ref"]: o["original_block"] for o in inventory["occurrences"]}
        opened = sorted(by_ref[ref] for ref in segmented["opened_containers"])
        self.assertEqual(opened, ["mixed", "plan"])


if __name__ == "__main__":
    unittest.main()
