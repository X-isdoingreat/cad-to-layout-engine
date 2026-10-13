from __future__ import annotations

import unittest

import ezdxf

from cad_to_layout.zero_manual.warehouse_geometry import collect_structure, layer_role, measure_views

BAYS_X = [8400] * 6          # 50400 along x
BAYS_Y = [9000, 9000, 6000]  # 24000 along y
WALL = 240                    # wall thickness; axes run on the wall centre line


def _building(doc, *, with_walls: bool = True, detail_room: bool = True):
    msp = doc.modelspace()
    for name in ("AXIS", "AXIS_TEXT", "WALL"):
        doc.layers.add(name)
    total_x, total_y = sum(BAYS_X), sum(BAYS_Y)
    xs = [0]
    for b in BAYS_X:
        xs.append(xs[-1] + b)
    ys = [0]
    for b in BAYS_Y:
        ys.append(ys[-1] + b)
    over = 2000
    for i, x in enumerate(xs):
        msp.add_line((x, -over), (x, total_y + over), dxfattribs={"layer": "AXIS", "linetype": "CENTER"})
        msp.add_circle((x, -over - 500), 500, dxfattribs={"layer": "AXIS"})
        msp.add_text(str(i + 1), dxfattribs={"layer": "AXIS_TEXT", "insert": (x - 150, -over - 650), "height": 300})
    for j, y in enumerate(ys):
        msp.add_line((-over, y), (total_x + over, y), dxfattribs={"layer": "AXIS"})
        msp.add_circle((-over - 500, y), 500, dxfattribs={"layer": "AXIS"})
        msp.add_text("ABCD"[j], dxfattribs={"layer": "AXIS_TEXT", "insert": (-over - 650, y - 150), "height": 300})
    msp.add_linear_dim(base=(0, -4000), p1=(0, 0), p2=(total_x, 0), dxfattribs={"layer": "AXIS"}).render()
    msp.add_linear_dim(base=(-4000, 0), p1=(0, 0), p2=(0, total_y), angle=90, dxfattribs={"layer": "AXIS"}).render()
    if with_walls:
        # walls in a nested block on layer "0", inserted on layer WALL
        block = doc.blocks.new("SHELL")
        h = WALL / 2
        outer = [(-h, -h), (total_x + h, -h), (total_x + h, total_y + h), (-h, total_y + h)]
        inner = [(h, h), (total_x - h, h), (total_x - h, total_y - h), (h, total_y - h)]
        # outer and inner faces as separate lines, with two 4000 door gaps in the south wall
        def ring(points, door=False):
            for k in range(4):
                a, b = points[k], points[(k + 1) % 4]
                if door and k == 0:
                    block.add_line(a, (10000, a[1])); block.add_line((14000, a[1]), (30000, a[1])); block.add_line((34000, a[1]), b)
                else:
                    block.add_line(a, b)
        ring(outer, door=True)
        ring(inner, door=True)
        msp.add_blockref("SHELL", (0, 0), dxfattribs={"layer": "WALL"})
        if detail_room:
            # a small detail room drawn beside the building must not become the warehouse
            for a, b in (((60000, 0), (66000, 0)), ((66000, 0), (66000, 4000)), ((66000, 4000), (60000, 4000)), ((60000, 4000), (60000, 0))):
                msp.add_line(a, b, dxfattribs={"layer": "WALL"})
    return doc


def _view(window):
    return {"view_id": "V:test", "window": window, "source": "sheet_frame", "role": "plan", "title": {"text": "首层平面图"}}


class WarehouseGeometryTests(unittest.TestCase):
    def _measure(self, doc, window, unit="mm"):
        items = collect_structure(doc)
        from cad_to_layout.zero_manual.inventory import _dimension  # same reader as the inventory
        from cad_to_layout.zero_manual.expand import iter_world_items

        dims = []
        for it in iter_world_items(doc):
            if it.dxftype == "DIMENSION":
                d = _dimension(it.entity, it.matrix)
                if d:
                    dims.append({"ref": f"D:{it.ref}", **d})
        return measure_views(items, [_view(window)], {"unit": unit, "status": "declared"}, dims)[0]

    def test_layer_roles_from_lexicon(self) -> None:
        self.assertEqual(layer_role("A-WALL"), "wall")
        self.assertEqual(layer_role("墙体"), "wall")
        self.assertEqual(layer_role("AXIS"), "axis")
        self.assertEqual(layer_role("柱"), "column")
        self.assertIsNone(layer_role("0"))

    def test_axis_extent_ignores_overshoot_and_bubbles_and_matches_dimensions(self) -> None:
        doc = _building(ezdxf.new("R2018"))
        row = self._measure(doc, [-10000, -10000, 70000, 40000])
        axis = row["axis"]
        self.assertEqual(axis["status"], "found")
        self.assertAlmostEqual(axis["length_mm"], 50400.0, places=1)
        self.assertAlmostEqual(axis["width_mm"], 24000.0, places=1)
        groups = {round(g["direction_deg"]) % 180: g for g in axis["groups"]}
        self.assertEqual(groups[0]["spacings_mm"], [9000.0, 9000.0, 6000.0])
        self.assertEqual(groups[90]["spacings_mm"], [8400.0] * 6)
        self.assertEqual((groups[90]["first_label"], groups[90]["last_label"]), ("1", "7"))
        self.assertEqual((groups[0]["first_label"], groups[0]["last_label"]), ("A", "D"))
        self.assertTrue(all(g["dimension_check"]["status"] == "matches_drawn_dimension" for g in axis["groups"]))

    def test_exterior_wall_closes_doors_skips_detail_room_and_measures_thickness(self) -> None:
        doc = _building(ezdxf.new("R2018"))
        row = self._measure(doc, [-10000, -10000, 70000, 40000])
        wall = row["wall"]
        self.assertEqual(wall["status"], "found")
        self.assertEqual(wall["wall_thickness_mm"], 240.0)
        self.assertAlmostEqual(wall["length_mm"], 50400 + WALL, delta=5)
        self.assertAlmostEqual(wall["width_mm"], 24000 + WALL, delta=5)
        self.assertTrue(wall["rectangular"])
        self.assertEqual(row["status"], "consistent")

    def test_view_without_walls_reports_one_basis_and_without_anything_reports_none(self) -> None:
        doc = _building(ezdxf.new("R2018"), with_walls=False)
        row = self._measure(doc, [-10000, -10000, 70000, 40000])
        self.assertEqual(row["wall"]["status"], "no_wall_linework")
        self.assertEqual(row["status"], "one_basis_only")
        empty = self._measure(doc, [200000, 200000, 300000, 300000])
        self.assertEqual(empty["status"], "no_building_geometry")

    def test_columns_only_on_wall_layer_are_not_an_outline(self) -> None:
        doc = ezdxf.new("R2018")
        doc.layers.add("WALL")
        msp = doc.modelspace()
        for x in range(0, 50401, 8400):
            for y in (0, 24000):
                msp.add_lwpolyline([(x - 400, y - 400), (x + 400, y - 400), (x + 400, y + 400), (x - 400, y + 400)], close=True, dxfattribs={"layer": "WALL"})
        row = self._measure(doc, [-10000, -10000, 70000, 40000])
        self.assertEqual(row["wall"]["status"], "open_wall_linework")
        self.assertAlmostEqual(row["wall"]["linework_extent_mm"][0], 51200.0, places=1)

    def test_unknown_unit_gives_no_numbers(self) -> None:
        doc = _building(ezdxf.new("R2018"))
        row = self._measure(doc, [-10000, -10000, 70000, 40000], unit=None)
        self.assertEqual(row["status"], "unit_unknown")
        self.assertNotIn("axis", row)

    def test_metre_drawing_is_scaled(self) -> None:
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        doc.layers.add("AXIS")
        for x in (0, 8.4, 16.8):
            msp.add_line((x, -1), (x, 13), dxfattribs={"layer": "AXIS"})
        for y in (0, 12):
            msp.add_line((-1, y), (17.8, y), dxfattribs={"layer": "AXIS"})
        row = self._measure(doc, [-5, -5, 25, 20], unit="m")
        self.assertAlmostEqual(row["axis"]["length_mm"], 16800.0, places=1)
        self.assertAlmostEqual(row["axis"]["width_mm"], 12000.0, places=1)


if __name__ == "__main__":
    unittest.main()
