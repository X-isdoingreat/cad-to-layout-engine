"""Agent tools: window index and column counting on a synthetic drawing (metres, 1 unit = 1 mm)."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

import ezdxf

from cad_to_layout.zero_manual import agent_reader
from cad_to_layout.zero_manual import agent_tools as T


def _column(msp, x: float, y: float, layer: str = "COL") -> None:
    # an I-shaped symbol: web 1100 mm and two 700 mm flanges
    msp.add_lwpolyline([(x, y - 550), (x, y + 550)], dxfattribs={"layer": layer})
    msp.add_lwpolyline([(x - 350, y - 550), (x + 350, y - 550)], dxfattribs={"layer": layer})
    msp.add_lwpolyline([(x - 350, y + 550), (x + 350, y + 550)], dxfattribs={"layer": layer})


def _drawing(path: Path) -> None:
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    # building 60 m x 24 m, walls on WALL
    msp.add_lwpolyline([(0, 0), (60000, 0), (60000, 24000), (0, 24000)], close=True, dxfattribs={"layer": "WALL"})
    for x in range(0, 60001, 6000):
        for y in (0, 24000):
            if (x, y) != (30000, 0):  # one place on the south wall holds a door, no column
                _column(msp, x, y)
    # a column whose flange touches a short partition: it lands in another shape group
    msp.add_lwpolyline([(30350, 24550), (31000, 24550)], dxfattribs={"layer": "COL"})
    # the same columns 12 m east of the building: an annex left out of the faces
    for y in (0, 12000, 24000):
        _column(msp, 72000, y)
    # a door leaf at the empty south place: symbol-sized linework, not a column
    msp.add_lwpolyline([(29600, -400), (30400, -400), (30400, 300), (29600, 300)], close=True, dxfattribs={"layer": "DOOR"})
    # the drawing's own dimensions: overall length (end to end) and one bay
    msp.add_linear_dim(base=(0, -3000), p1=(0, 0), p2=(60000, 0), dxfattribs={"layer": "DIM"}).render()
    msp.add_linear_dim(base=(0, -2000), p1=(0, 0), p2=(6000, 0), dxfattribs={"layer": "DIM"}).render()
    # two named areas split by a zone line, and a name with no boundary around it
    msp.add_line((30000, 0), (30000, 24000), dxfattribs={"layer": "ZONE"})
    msp.add_text("存储区", dxfattribs={"insert": (10000, 12000), "height": 500})
    msp.add_text("发货区", dxfattribs={"insert": (45000, 12000), "height": 500})
    msp.add_text("生活区", dxfattribs={"insert": (10000, 30000), "height": 500})
    # a section to the north: a column-top level with its words, and the section title
    msp.add_text("1-1剖面图", dxfattribs={"insert": (10000, 37000), "height": 500})
    msp.add_text("12.000", dxfattribs={"insert": (10000, 45000), "height": 300})
    msp.add_text("柱顶", dxfattribs={"insert": (11500, 45000), "height": 300})
    msp.add_text("C30", dxfattribs={"insert": (30000, 45000), "height": 300})
    doc.saveas(path)


class AgentToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        _drawing(Path("t.dxf"))
        T.build_index("t.dxf")
        T.FACES.write_text(json.dumps({"F1": {"outline": [[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]]}}), encoding="utf-8")

    def tearDown(self) -> None:
        T.close_all()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_window_reads_only_the_window(self) -> None:
        idx = T._load()
        self.assertEqual(idx.count([-1000, -1000, 1000, 1000], ["COL"]), 3)
        self.assertEqual(len(idx.select([71000, -1000, 73000, 25000], ["COL"])), 9)
        self.assertEqual(idx.count([100000, 100000, 200000, 200000]), 0)
        self.assertEqual(idx["insunits"], 4)

    def test_columns_counted_checked_and_outside_reported(self) -> None:
        box = [-2000, -2000, 62000, 26000]
        groups = T.cmd_columns(argparse.Namespace(box=box, layers=None, unit_m=None, max_size=1.5))["symbol_groups"]
        self.assertEqual((groups[0]["size_m"], groups[0]["count"]), ([0.7, 1.1], 20))
        got = T.cmd_column_instances(argparse.Namespace(box=box, groups=[0], faces=["F1"], layers=None, unit_m=None, max_size=1.5))
        # 20 plain symbols + the one touching the partition, found by its template
        self.assertEqual(got["columns_in_faces"], 21)
        self.assertEqual(got["same_columns_outside_faces"], 3)
        # the door place on the south column line is listed to check, not counted
        self.assertTrue(any(abs(x - 30000) < 600 and abs(y) < 600 for x, y in got["to_check_positions"]))
        south = [ln for ln in got["lines_along_long_side"] if ln["columns"] == 10]
        self.assertEqual(len(south[0]["possible_missing"]), 1)
        runs = json.loads(T.COLUMNS.read_text(encoding="utf-8"))
        self.assertEqual(runs["F1"]["columns_in_faces"], 21)

    def test_dimensions_compared_with_outline(self) -> None:
        got = T.cmd_dimensions(argparse.Namespace(box=[-5000, -5000, 65000, 30000], unit_m=None, limit=10))
        self.assertEqual([d["length_m"] for d in got["dimensions"]], [60.0, 6.0])
        check = T.dimension_check([[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]], 0.001)
        self.assertEqual([(c["dimension_m"], c["differs_pct"]) for c in check], [(60.0, 0.0)])  # the bay dimension is not end to end
        short = T.dimension_check([[0, 0], [54000, 0], [54000, 24000], [0, 24000], [0, 0]], 0.001)
        self.assertEqual([(c["dimension_m"], c["differs_pct"]) for c in short], [(60.0, -10.0)])  # an outline measured too short is caught

    def test_elevations_with_words_and_section(self) -> None:
        got = T.cmd_elevations(argparse.Namespace(box=[0, 30000, 40000, 50000], unit_m=None, near_m=5.0, limit=10))
        self.assertEqual(got["count"], 1)
        row = got["elevations"][0]
        self.assertEqual(row["value"], "12.000")
        self.assertEqual([w for w, _ in row["words_next_to_it"]], ["柱顶"])  # C30 is 20 m away
        self.assertEqual(row["nearest_view_title"][0], "1-1剖面图")

    def test_zones_get_their_own_boundary_and_totals_are_recomputed(self) -> None:
        got = T.cmd_zones(argparse.Namespace(box=[-1000, -1000, 61000, 35000], layers=["WALL", "ZONE"], unit_m=None, match=None, gap=1.0, min_area=2.0))
        areas = {z["name"]: z["area_m2"] for z in got["zones"]}
        self.assertEqual(areas, {"存储区": 720.0, "发货区": 720.0})
        self.assertEqual([z["name"] for z in got["names_without_closed_boundary"]], ["生活区"])
        face = {z["name"]: z["face"] for z in got["zones"]}
        answer = {"zones": [{"view": "1F", "name": "存储区", "face": face["存储区"], "area_m2": 720, "category": "storage"},
                            {"view": "1F", "name": "发货区", "face": face["发货区"], "area_m2": 720, "category": "operation"}],
                  "zone_totals": [{"view": "1F", "storage_and_operation_m2": 1500}]}
        check = agent_reader.verify_zones(Path("."), answer)
        self.assertEqual(check["recomputed_totals"]["1F"], {"storage_m2": 720.0, "operation_m2": 720.0, "storage_and_operation_m2": 1440.0})
        self.assertEqual(len(check["flags"]), 1)  # the reported 1500 is caught

    def test_feedback_names_a_left_out_annex_and_unreported_buildings(self) -> None:
        T.FACES.write_text(json.dumps({
            "F1": {"outline": [[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]], "area_m2": 1440.0, "centroid": [30000, 12000]},
            "F2": {"outline": [[60000, 0], [74000, 0], [74000, 24000], [60000, 24000], [60000, 0]], "area_m2": 336.0, "centroid": [67000, 12000]}}), encoding="utf-8")
        box = [-2000, -2000, 62000, 26000]
        T.cmd_column_instances(argparse.Namespace(box=box, groups=[0], faces=["F1"], layers=None, unit_m=None, max_size=1.5))
        answer = {"unit_m": 0.001, "buildings": [{"name": "主厂房", "designated": True, "faces": ["F1"], "area_m2": 1440, "length_m": 60, "width_m": 24,
                                                     "columns": {"count": 21, "outside_columns_decision": "另一栋楼"}}],
                  "neighbour_decisions": [{"building": "主厂房", "face": "F2", "decision": "not_building"}]}
        Path("t2.dxf").unlink(missing_ok=True)
        checks = agent_reader.verify(Path("."), answer)
        problems = agent_reader.feedback(Path("."), answer, checks)
        self.assertTrue(any("F2" in p and "3 根柱子" in p for p in problems), problems)
        self.assertFalse(any("图上这些建筑名称" in p for p in problems))  # no building names on this drawing

    def test_corners_snap_to_the_wall_and_become_a_face(self) -> None:
        got = T.cmd_corners(argparse.Namespace(points=[300, -400, 59500, 300, 60400, 23600, -200, 24300], layers=["WALL"], unit_m=None, snap_m=1.0))
        self.assertEqual(got["unsnapped"], 0)
        self.assertEqual(got["area_m2"], 1440.0)  # snapped exactly onto the 60 x 24 m wall corners
        far = T.cmd_corners(argparse.Namespace(points=[0, 0, 60000, 0, 60000, 30000], layers=["WALL"], unit_m=None, snap_m=1.0))
        self.assertEqual(far["unsnapped"], 1)  # 6 m from any wall: kept as given and reported

    def test_unknown_unit_is_refused(self) -> None:
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 0
        doc.modelspace().add_line((0, 0), (1, 1))
        doc.saveas("u.dxf")
        T.build_index("u.dxf")
        with self.assertRaises(SystemExit):
            T.cmd_columns(argparse.Namespace(box=[0, 0, 1, 1], layers=None, unit_m=None, max_size=1.5))


if __name__ == "__main__":
    unittest.main()


class AnswerFromEventsTest(unittest.TestCase):
    def test_the_answer_given_before_a_last_chatty_message_is_found(self) -> None:
        import json as _json

        from cad_to_layout.zero_manual import agent_reader

        events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": '{"buildings": [{"name": "A"}]}'}]}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "The result is in my previous message."}]}},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}]
        found = agent_reader.answer_from_events("\n".join(_json.dumps(e) for e in events))
        self.assertEqual(found, {"buildings": [{"name": "A"}]})
        self.assertIsNone(agent_reader.answer_from_events(_json.dumps(events[1])))
