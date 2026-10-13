from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ezdxf

from cad_to_layout.zero_manual.expand import iter_world_items
from cad_to_layout.zero_manual.inventory import build_inventory
from cad_to_layout.zero_manual.units import infer_unit
from cad_to_layout.zero_manual.views import segment_views


def _room_block(doc, name: str) -> None:
    block = doc.blocks.new(name)
    # layer "0" linework inside the block inherits the INSERT's layer
    block.add_lwpolyline([(0, 0), (10000, 0), (10000, 6000), (0, 6000)], close=True)
    block.add_text("设备", dxfattribs={"insert": (100, 100), "height": 250})


def _nested(doc) -> None:
    inner = doc.blocks.new("INNER")
    inner.add_line((0, 0), (1000, 0))
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (500, 0), dxfattribs={"rotation": 90})


class ExpandTests(unittest.TestCase):
    def test_nested_world_coordinates_and_layer_inheritance(self) -> None:
        doc = ezdxf.new("R2018")
        doc.layers.add("WALLS")
        _nested(doc)
        doc.modelspace().add_blockref("OUTER", (10000, 20000), dxfattribs={"layer": "WALLS"})
        items = list(iter_world_items(doc))
        line = next(i for i in items if i.dxftype == "LINE")
        start = line.matrix.transform(line.entity.dxf.start)
        end = line.matrix.transform(line.entity.dxf.end)
        self.assertAlmostEqual(start.x, 10500.0)
        self.assertAlmostEqual(start.y, 20000.0)
        self.assertAlmostEqual(end.x, 10500.0)
        self.assertAlmostEqual(end.y, 21000.0)
        self.assertEqual(line.layer, "WALLS")
        self.assertEqual(line.depth, 2)
        self.assertEqual(line.ref.count("/"), 2)

    def test_recursive_block_does_not_loop(self) -> None:
        doc = ezdxf.new("R2018")
        block = doc.blocks.new("SELF")
        block.add_line((0, 0), (1, 1))
        block.add_blockref("SELF", (5, 5))
        doc.modelspace().add_blockref("SELF", (0, 0))
        items = list(iter_world_items(doc))
        self.assertLess(len(items), 10)


class InventoryUnitViewTests(unittest.TestCase):
    def _save(self, doc) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "d.dxf"
        doc.saveas(path)
        return path

    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_side_by_side_plans_become_separate_views_with_titles(self) -> None:
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 0
        msp = doc.modelspace()
        _room_block(doc, "ROOM")
        for index, x0 in enumerate((0, 500000)):
            for k in range(6):
                msp.add_blockref("ROOM", (x0 + k * 12000, 0))
            msp.add_text(f"{index + 1}号库平面布置图", dxfattribs={"insert": (x0, 9000), "height": 800})
            msp.add_linear_dim(base=(x0, -2000), p1=(x0, 0), p2=(x0 + 6000, 0)).render()
            msp.add_linear_dim(base=(x0, -3000), p1=(x0, 0), p2=(x0 + 8400, 0)).render()
        inv = build_inventory(self._save(doc))
        unit = infer_unit(inv)
        self.assertEqual(unit["unit"], "mm")
        self.assertEqual(unit["status"], "inferred_from_dimensions")
        views = segment_views(inv)["views"]
        self.assertEqual(len(views), 2)
        self.assertEqual({v["role"] for v in views}, {"plan"})
        self.assertEqual(sorted(v["title"]["text"] for v in views), ["1号库平面布置图", "2号库平面布置图"])

    def test_unknown_unit_without_evidence(self) -> None:
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 0
        doc.modelspace().add_line((0, 0), (10, 10))
        inv = build_inventory(self._save(doc))
        self.assertEqual(infer_unit(inv)["status"], "unknown")

    def test_declared_unit_wins(self) -> None:
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_line((0, 0), (10, 10))
        inv = build_inventory(self._save(doc))
        self.assertEqual(infer_unit(inv), {"unit": "m", "status": "declared", "evidence": ["HEADER:$INSUNITS=6"]})

    def test_main_viewport_is_largest_model_area_with_content(self) -> None:
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        _room_block(doc, "ROOM")
        for k in range(15):
            msp.add_blockref("ROOM", (k * 12000, 0))
        layout = doc.layouts.new("Sheet")
        # large on paper but showing an empty tiny model area (title block)
        layout.add_viewport(center=(200, 150), size=(400, 300), view_center_point=(-5000, -5000), view_height=100)
        # the plan
        layout.add_viewport(center=(100, 100), size=(150, 60), view_center_point=(90000, 3000), view_height=40000)
        inv = build_inventory(self._save(doc))
        main = [v for v in segment_views(inv)["views"] if v.get("layout_role") == "main"]
        self.assertEqual(len(main), 1)
        self.assertLess(main[0]["window"][0], 0.0 + 1e-6 + 90000)
        self.assertGreater(main[0]["window"][2] - main[0]["window"][0], 90000)


if __name__ == "__main__":
    unittest.main()
