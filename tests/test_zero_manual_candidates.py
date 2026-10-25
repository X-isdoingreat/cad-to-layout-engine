"""Program-only candidates for equipment that is not "one block = one machine".

Synthetic drawings only; every expectation is reproducible without an LLM except
the block types, which a fake judge supplies.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual.candidates import outline_cell_groups, regular_arrays
from cad_to_layout.zero_manual.equipment import build_families, recognise, rule_decision, text_type
from cad_to_layout.zero_manual.inventory import build_inventory

VIEWS = [{"view_id": "V:all", "window": [-50000, -50000, 200000, 200000], "source": "model_space_cluster"}]
UNIT = {"unit": "mm", "status": "declared"}


def _rect(space, x: float, y: float, w: float, h: float, layer: str = "0") -> None:
    space.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer})


def _text(space, value: str, x: float, y: float) -> None:
    space.add_text(value, dxfattribs={"insert": (x, y), "height": 200})


def _new() -> Any:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    return doc


class Judge:
    """Answers by block name."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: set[str] = set()

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        tiles = []
        for line in prompt.split("Facts per tile:")[1].strip().splitlines():
            name = re.search(r"block name: (\S+)", line).group(1)
            self.asked.add(name)
            tiles.append({"id": line.split(":", 1)[0], "type": self.answers.get(name, "UNSURE"), "reason": "fake"})
        return {"text": json.dumps({"tiles": tiles}), "provider": "fake", "model": "fake"}


class CandidateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _recognise(self, doc: Any, answers: dict[str, str] | None = None, opened: list[str] | None = None) -> dict[str, Any]:
        path = self.root / "d.dxf"
        doc.saveas(path)
        inventory = build_inventory(path)
        self.judge = Judge(answers or {})
        if opened == ["<top level>"]:
            opened = [o["ref"] for o in inventory["occurrences"] if o["depth"] == 0]
        return recognise(inventory, VIEWS, UNIT, ezdxf.readfile(path), self.root / "w", self.judge, True, opened=opened)


class RegularArrayTests(CandidateCase):
    def _nested(self, outer_name: str) -> Any:
        doc = _new()
        _rect(doc.blocks.new("bay"), 0, 0, 500, 600)
        row = doc.blocks.new("row")
        for i in range(10):
            row.add_blockref("bay", (i * 560, 0))
        outer = doc.blocks.new(outer_name)
        _rect(outer, -100, -100, 6000, 2200)
        outer.add_blockref("row", (0, 0))
        outer.add_blockref("row", (0, 1400))
        msp = doc.modelspace()
        msp.add_blockref(outer_name, (0, 0))
        msp.add_blockref(outer_name, (0, 20000))
        return doc

    def test_a_row_of_packed_cells_is_counted_as_rows_and_cells_and_the_llm_typed_outer_block_is_not(self) -> None:
        # 2026-10-24: the row block is judged as one unit; its cells are never asked about alone (they inherit)
        result = self._recognise(self._nested("outer"), {"row": "RACK", "outer": "OTHER_EQUIPMENT"})
        self.assertEqual(self.judge.asked, {"row", "outer"})
        kinds = Counter((r["type"], r["family"], r["drawn_as"], r.get("cells")) for r in result["records"])
        self.assertEqual(kinds, {("RACK", "row", "row", 10): 4})
        self.assertEqual(result["families"]["bay"]["decision"]["basis"], "row_structure")
        self.assertEqual(result["cell_occurrences_counted_through_rows"], 40)
        self.assertEqual([(r["rows_counted"], r["cells_per_row"], r["cells_total"], r["type"]) for r in result["rows"]], [(4, 10, 40, "RACK")])
        self.assertEqual(len(result["assemblies_not_counted"]), 2)
        self.assertEqual(sorted(a["count"] for a in result["regular_arrays"] if a["family"] == "bay"), [10, 10, 10, 10])
        self.assertTrue(all(r["status"] == "pending_independent_count_and_spot_check" for r in result["records"]))

    def test_a_text_named_outer_machine_keeps_its_rows_uncounted(self) -> None:
        result = self._recognise(self._nested("堆垛机"), {"row": "RACK"})
        kinds = Counter((r["type"], r["family"]) for r in result["records"])
        self.assertEqual(kinds, {("STACKER", "堆垛机"): 2})
        self.assertEqual(result["nested_occurrences_not_recounted"], 4)

    def test_machines_standing_apart_are_not_a_packed_array(self) -> None:
        doc = _new()
        _rect(doc.blocks.new("m"), 0, 0, 600, 3000)
        _rect(doc.blocks.new("n"), 0, 0, 500, 600)
        msp = doc.modelspace()
        for i in range(3):
            msp.add_blockref("m", (i * 2600, 0))       # aisles between them
        for i in range(5):
            msp.add_blockref("n", (i * 560, 10000))    # side by side
        msp.add_blockref("n", (4 * 560 + 3000, 10000))  # a sixth one, off pitch
        path = self.root / "a.dxf"
        doc.saveas(path)
        families = build_families(build_inventory(path), VIEWS, UNIT)
        arrays = regular_arrays(families, 1.0)
        self.assertEqual([(a["family"], a["count"], a["pitch_mm"]) for a in arrays], [("n", 5, 560.0)])


class TopDownJudgementTests(CandidateCase):
    def _drawing(self) -> Any:
        doc = _new()
        _rect(doc.blocks.new("gear"), 0, 0, 800, 600)              # only ever inside the machine
        machine = doc.blocks.new("machine")
        _rect(machine, 0, 0, 3000, 1500)
        machine.add_blockref("gear", (100, 100))
        _rect(doc.blocks.new("inner"), 0, 0, 2000, 900)            # inside a block that is not a machine
        frame = doc.blocks.new("frame")
        _rect(frame, 0, 0, 9000, 5000)
        frame.add_blockref("inner", (500, 500))
        plan = doc.blocks.new("plan")                              # a whole plan placed as one block
        plan.add_blockref("machine", (0, 0))
        plan.add_blockref("frame", (10000, 0))
        doc.modelspace().add_blockref("plan", (0, 0))
        return doc

    def test_parts_of_a_machine_are_not_sent_and_non_machines_are_looked_into(self) -> None:
        result = self._recognise(self._drawing(), {"machine": "BOX_LIFT", "frame": "NOT_EQUIPMENT", "inner": "ROBOT_ARM"}, opened=["<top level>"])
        self.assertEqual(self.judge.asked, {"machine", "frame", "inner"}, "the container and the machine's part are never sent")
        self.assertEqual(Counter(r["family"] for r in result["records"]), {"machine": 1, "inner": 1})
        self.assertEqual(result["families"]["gear"]["decision"]["evidence"][0]["kind"], "only_drawn_inside_a_machine_or_part")
        self.assertEqual(result["families"]["plan"]["decision"]["evidence"][0]["kind"], "opened_as_whole_plan_container")


class BackgroundDrawingTests(CandidateCase):
    def test_once_only_blocks_of_a_pasted_background_are_not_sent(self) -> None:
        doc = _new()
        background = doc.blocks.new("site")
        for i in range(210):                                   # a factory plan: hundreds of one-off blocks
            _rect(doc.blocks.new(f"b{i}"), 0, 0, 1000 + i, 800)
            background.add_blockref(f"b{i}", ((i % 20) * 3000, (i // 20) * 3000))
        _rect(doc.blocks.new("conv"), 0, 0, 3000, 1000)
        for i in range(3):                                     # a block that repeats inside it is still judged
            background.add_blockref("conv", (i * 5000, -5000))
        doc.modelspace().add_blockref("site", (0, 0))
        result = self._recognise(doc, {"conv": "PALLET_CONV_ROLLER"})
        self.assertEqual(self.judge.asked, {"site", "conv"})
        self.assertEqual([(b["families"], b["once_only_not_judged"]) for b in result["background_drawings"]], [(211, 210)])
        self.assertEqual(Counter(r["family"] for r in result["records"]), {"conv": 3})


class OutlineCellTests(CandidateCase):
    def _cells(self, doc: Any, x0: float) -> None:
        msp = doc.modelspace()
        for line in range(2):                       # two lines back to back
            for i in range(7):
                _rect(msp, x0 + line * 600, i * 2000, 600, 2000)
        _rect(msp, x0, 14000, 600, 1000)             # a half cell at the end of a line

    def test_cells_are_grouped_and_typed_only_by_a_text_they_hold(self) -> None:
        doc = _new()
        self._cells(doc, 0)          # pair of lines
        self._cells(doc, 4000)       # a second pair across an aisle
        msp = doc.modelspace()
        _text(msp, "重型货架", 2000, 5000)                # in the aisle, inside the extent of the cells
        for i in range(4):                              # another group, no text: stays untyped
            _rect(msp, 40000 + i * 800, 0, 800, 800)
        _rect(msp, 60000, 0, 600, 600, layer="COLUMN")  # structure layers are never cells
        _rect(msp, 60600, 0, 600, 600, layer="COLUMN")
        _rect(msp, 61200, 0, 600, 600, layer="COLUMN")
        result = self._recognise(doc)
        cells = [r for r in result["records"] if r["drawn_as"] == "outline_cell"]
        self.assertEqual(len(cells), 30)
        self.assertEqual({(r["type"], r["basis"], r["status"]) for r in cells}, {("RACK", "text", "accepted")})
        self.assertEqual(len({r["group"] for r in cells}), 2)
        self.assertEqual(Counter(u["key"] for u in result["candidates_untyped"]), {"cells:800x800": 4})
        self.assertEqual(result["zone_labels"][0]["kind"], "zone_label_inside_group_extent")

    def test_a_label_inside_one_cell_does_not_name_the_whole_group(self) -> None:
        # 2026-10-10: one label in one cell covers one of the group's cells, too few to name them all;
        # the cells stay untyped candidates and the label is listed for the reviewer
        doc = _new()
        self._cells(doc, 0)
        _text(doc.modelspace(), "货架", 100, 3000)
        result = self._recognise(doc)
        self.assertEqual(Counter(r["drawn_as"] for r in result["records"]), Counter())
        self.assertEqual([w["texts"] for w in result["labels_not_used_for_type"]], [["货架"]])

    def test_a_short_run_joins_only_a_cell_size_already_seen_in_a_full_group(self) -> None:
        doc = _new()
        msp = doc.modelspace()
        for i in range(5):
            _rect(msp, 0, i * 2000, 600, 2000)             # a full line
        for i in range(2):
            _rect(msp, 3000, i * 2000, 600, 2000)          # a short run of the same cells beside a column
        _rect(msp, 3000, 4000, 600, 1000)
        for i in range(2):
            _rect(msp, 9000, i * 900, 900, 900)            # two squares of a size no full group has
        path = self.root / "s.dxf"
        doc.saveas(path)
        groups = outline_cell_groups(build_inventory(path)["closed_outlines"], VIEWS, 1.0)
        self.assertEqual(sorted((g["count"], g["cell_size_mm"]) for g in groups), [(3, [2000, 600]), (5, [2000, 600])])

    def test_rectangles_that_only_touch_part_of_an_edge_do_not_join(self) -> None:
        doc = _new()
        msp = doc.modelspace()
        for i in range(3):
            _rect(msp, i * 1000, 0, 1000, 600)
        _rect(msp, 0, 600, 3000, 3000)   # a larger area touching the three cells: not a cell of the group
        path = self.root / "c.dxf"
        doc.saveas(path)
        groups = outline_cell_groups(build_inventory(path)["closed_outlines"], VIEWS, 1.0)
        self.assertEqual([(g["count"], g["cell_size_mm"]) for g in groups], [(3, [1000, 600])])


class LabelledOutlineTests(CandidateCase):
    def test_outline_with_a_machine_name_inside_is_that_machine(self) -> None:
        doc = _new()
        msp = doc.modelspace()
        for i in range(2):
            _rect(msp, 0, i * 3500, 2900, 3400)
            _text(msp, "垂直升降柜", 300, i * 3500 + 1500)
            _text(msp, f"{i + 1}#", 300, i * 3500 + 2500)
        _text(msp, "垂直升降柜，共3台", 8000, 0)            # a quantity note in open space
        _rect(msp, 20000, 0, 3000, 2600)
        _text(msp, "货梯", 23200, 1000)                     # 200 mm outside the outline: never bound by distance
        _rect(msp, 30000, 0, 600, 600)
        _text(msp, "电控柜", 30100, 300)                     # an ignored kind
        result = self._recognise(doc)
        self.assertEqual([(r["type"], r["name"], r["drawn_as"], r["basis"]) for r in result["records"]],
                         [("OTHER_EQUIPMENT", "垂直升降柜", "labelled_outline", "text")] * 2)
        self.assertEqual([(q["stated"], q["counted"], q["status"]) for q in result["stated_quantities"]], [(3, 2, "differs")])
        self.assertEqual([u["text"] for u in result["unbound_labels"]], ["货梯"])

    def test_an_area_name_never_makes_an_outline_a_machine(self) -> None:
        doc = _new()
        msp = doc.modelspace()
        _rect(msp, 0, 0, 6000, 3000)
        _text(msp, "大件货架区", 500, 1000)
        result = self._recognise(doc)
        self.assertEqual(result["records"], [])
        self.assertEqual([u["reason"] for u in result["unbound_labels"]], ["area_name_without_equal_members"])

    def test_llm_typed_blocks_inside_a_named_outline_are_its_parts(self) -> None:
        doc = _new()
        _rect(doc.blocks.new("frame"), 0, 0, 1200, 800)
        _rect(doc.blocks.new("堆垛机"), 0, 0, 2000, 800)
        msp = doc.modelspace()
        _rect(msp, 0, 0, 3000, 2600)
        _text(msp, "货梯", 1000, 2000)
        msp.add_blockref("frame", (200, 200))              # typed by the judge only: a part of the lift
        msp.add_blockref("堆垛机", (200, 1100))             # named by text: still counted
        msp.add_blockref("frame", (10000, 0))              # outside the outline: counted
        result = self._recognise(doc, {"frame": "OTHER_EQUIPMENT"})
        self.assertEqual(Counter((r["family"], r["drawn_as"]) for r in result["records"]),
                         {("outline:货梯", "labelled_outline"): 1, ("堆垛机", "block"): 1, ("frame", "block"): 1})
        self.assertEqual(result["parts_inside_named_outlines_not_recounted"], 1)

    def test_outline_holding_equal_blocks_is_a_zone_and_names_them(self) -> None:
        doc = _new()
        _rect(doc.blocks.new("mod"), 0, 0, 4100, 1300)
        msp = doc.modelspace()
        _rect(msp, -500, -500, 30000, 8000)
        for i in range(5):
            msp.add_blockref("mod", (i * 4100, 0))
        _text(msp, "三层阁楼式货架", 5000, 4000)
        result = self._recognise(doc)
        self.assertEqual(Counter((r["type"], r["drawn_as"], r["basis"]) for r in result["records"]), {("RACK", "block", "text"): 5})
        self.assertEqual(result["families"]["mod"]["decision"]["evidence"][0]["kind"], "zone_label_inside_outline")


class NumberSeriesTests(CandidateCase):
    def test_distinct_numbers_are_units_and_take_the_type_of_the_machine_that_holds_them(self) -> None:
        doc = _new()
        _rect(doc.blocks.new("链条输送机"), 0, 0, 3000, 1300)
        giant = doc.blocks.new("giant")                       # lanes drawn as lines inside one big block
        giant.add_line((0, 0), (40000, 0))
        giant.add_line((0, 1300), (40000, 1300))
        msp = doc.modelspace()
        msp.add_blockref("giant", (0, 10000))
        for i in range(6):
            msp.add_blockref("链条输送机", (i * 5000, 0))
            msp.add_text(str(5201 + i), dxfattribs={"insert": (i * 5000 + 1000, 500), "height": 300, "layer": "NO"})
            msp.add_text(str(5301 + i), dxfattribs={"insert": (i * 5000 + 1000, 10500), "height": 300, "layer": "NO"})
        for i in range(12):                                  # repeated values are dimensions, not equipment numbers
            msp.add_text("8000", dxfattribs={"insert": (i * 3000, -5000), "height": 300, "layer": "DIMTXT"})
        result = self._recognise(doc, {"giant": "NOT_EQUIPMENT"})
        self.assertEqual([(s["layer"], s["count"], s["distinct"]) for s in result["number_series"]], [("NO", 12, 12)])
        typed = [u for u in result["numbered_units"] if u["type"]]
        self.assertEqual(sorted(u["number"] for u in typed), [str(5201 + i) for i in range(6)])
        self.assertEqual({(u["type"], u["basis"]) for u in typed}, {("PALLET_CONV_CHAIN", "text")})
        self.assertEqual(Counter(u["drawn_as"] for u in result["candidates_untyped"]), {"number_tag": 6})
        self.assertEqual(result["summary_by_number"]["V:all"]["UNTYPED"]["untyped"], 6)
        self.assertEqual(len(result["records"]), 6, "numbers never add machines to the body count")


class TextRuleTests(unittest.TestCase):
    def test_words(self) -> None:
        self.assertIsNone(text_type("track"))
        self.assertEqual(text_type("RACK-01")["type"], "RACK")
        self.assertEqual(text_type("码垛机器人")["type"], "ROBOT_ARM")
        self.assertEqual(text_type("顶升移栽机"), {"type": "OTHER_EQUIPMENT", "name": "顶升移栽", "term": "顶升移栽", "zone_only": False})
        self.assertTrue(text_type("大件货架区")["zone_only"])
        self.assertIsNone(text_type("电控柜"))
        self.assertIsNone(text_type("链条输送机与滚筒输送机"))

    def _family(self, name: str, size: tuple[float, float]) -> dict[str, Any]:
        return {"family": name, "block_names": [name], "size_mm": size, "layers": Counter({"0": 1}), "texts_inside": [], "attribs": Counter(),
                "occurrences": [{"ref": "I:1"}]}

    def test_a_roller_conveyor_too_narrow_for_a_pallet_is_a_box_conveyor(self) -> None:
        self.assertEqual(rule_decision(self._family("辊道机", (2000.0, 780.0)))["type"], "BOX_CONVEYOR")
        self.assertEqual(rule_decision(self._family("辊道机", (2000.0, 1300.0)))["type"], "PALLET_CONV_ROLLER")
        self.assertEqual(rule_decision(self._family("托盘辊道机", (2000.0, 780.0)))["type"], "PALLET_CONV_ROLLER")


if __name__ == "__main__":
    unittest.main()


class AnonymousBlocksTest(unittest.TestCase):
    def test_copies_of_an_assembly_share_one_family_and_stay_candidates(self) -> None:
        from cad_to_layout.zero_manual.equipment import normalise_anonymous

        blocks = [{"name": "*U1", "entity_types": {"INSERT": 2}, "signature": "bay"},
                  {"name": "*U2", "entity_types": {"INSERT": 2}, "signature": "bay"},
                  {"name": "*U3", "entity_types": {"INSERT": 2}, "signature": "bay"},
                  {"name": "*U7", "entity_types": {"LINE": 4}, "signature": "beam"},
                  {"name": "*U8", "entity_types": {"LINE": 4}, "signature": "beam"}]
        occ = [{"ref": "A", "block": "*U1", "original_block": "*U1", "parent_ref": None},
               {"ref": "A1", "block": "Upright", "original_block": "Upright", "parent_ref": "A"},
               {"ref": "A2", "block": "*U7", "original_block": "*U7", "parent_ref": "A"},
               {"ref": "B", "block": "*U2", "original_block": "*U2", "parent_ref": None},
               {"ref": "B1", "block": "Upright", "original_block": "Upright", "parent_ref": "B"},
               {"ref": "B2", "block": "*U8", "original_block": "*U8", "parent_ref": "B"},
               {"ref": "C", "block": "*U3", "original_block": "*U3", "parent_ref": None},
               {"ref": "C1", "block": "Pallet", "original_block": "Pallet", "parent_ref": "C"},
               {"ref": "C2", "block": "Pallet", "original_block": "Pallet", "parent_ref": "C"},
               {"ref": "D", "block": "*U9", "original_block": "Dyn", "parent_ref": None}]
        by = {o["ref"]: o for o in normalise_anonymous({"blocks": blocks, "occurrences": occ})["occurrences"]}
        self.assertEqual(by["A"]["original_block"], by["B"]["original_block"])  # same shape, same parts (beams grouped too)
        self.assertNotEqual(by["A"]["original_block"], by["C"]["original_block"])  # same outline, other parts
        self.assertEqual(by["A1"]["parent_ref"], "A")  # the assembly stays the parent of its parts
        self.assertEqual(by["D"]["original_block"], "Dyn")  # a dynamic block's variant keeps its block


class DuplicateViewsTest(unittest.TestCase):
    def test_a_copied_set_is_reported_and_a_different_plan_is_not(self) -> None:
        from cad_to_layout.zero_manual.equipment import duplicate_views

        views = [{"view_id": "A", "title": {"text": "首层平面图"}}, {"view_id": "B", "title": {"text": "首层平面图"}},
                 {"view_id": "C", "title": {"text": "二层平面图"}}]
        rec = lambda v, f, n: [{"view_id": v, "family": f, "type": "RACK"}] * n  # noqa: E731
        records = rec("A", "r", 100) + rec("B", "r", 98) + rec("C", "r", 100)
        self.assertEqual([(c["view_id"], c["same_as"]) for c in duplicate_views(records, views)], [("B", "A")])
        records = rec("A", "r", 100) + rec("B", "r", 40)
        self.assertEqual(duplicate_views(records, views), [])

