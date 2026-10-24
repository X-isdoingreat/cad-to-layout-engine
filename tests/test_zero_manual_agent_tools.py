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
        self.assertEqual([(c["dimension_m"], c["differs_pct"], c["flagged"]) for c in short], [(60.0, -10.0, True)])  # an outline measured too short is caught

    def test_dimensions_are_found_by_length_over_the_whole_drawing_and_lists_are_paged(self) -> None:
        # the 60 m total lies outside the window the agent chose: no window searches the whole drawing, by length
        got = T.cmd_dimensions(argparse.Namespace(box=[], unit_m=None, limit=10, offset=0, match=None, length_m=60.0))
        self.assertEqual(([d["length_m"] for d in got["dimensions"]], got["count"]), ([60.0], 1))
        none = T.cmd_dimensions(argparse.Namespace(box=[100000, 100000, 200000, 200000], unit_m=None, limit=10, offset=0, match=None, length_m=60.0))
        self.assertEqual(none["count"], 0)
        bad = T.cmd_dimensions(argparse.Namespace(box=[1, 2, 3], unit_m=None, limit=10, offset=0, match=None, length_m=None))
        self.assertIn("error", bad)
        # a long list is never cut silently: the count, the page and how to get the rest
        first = T.cmd_texts(argparse.Namespace(match="区", window=None, limit=2, offset=0))
        self.assertEqual((first["count"], first["shown"], len(first["texts"])), (3, 2, 2))
        self.assertIn("--offset 2", first["note"])
        rest = T.cmd_texts(argparse.Namespace(match="区", window=None, limit=2, offset=2))
        self.assertEqual((rest["shown"], "note" in rest), (1, False))
        page = T.cmd_window(argparse.Namespace(box=[-2000, -2000, 62000, 26000], layers=["COL"], limit=5, offset=60))
        self.assertEqual((page["items_count"], page["items_offset"], len(page["items"])), (64, 60, 4))  # 21 columns × 3 pieces + the partition

    def test_a_total_dimension_drawn_beside_the_building_is_still_compared(self) -> None:
        # the drawing's 60 m total is drawn 3 m below the wall; the outer wall 0.4 m beyond the axis is within the wall allowance
        far = T.dimension_check([[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]], 0.001)
        self.assertEqual([(c["dimension_m"], c["flagged"]) for c in far], [(60.0, False)])
        wall = T.dimension_check([[-200, 0], [60200, 0], [60200, 24000], [-200, 24000], [-200, 0]], 0.001)
        self.assertEqual([(c["outline_extent_m"], c["flagged"]) for c in wall], [(60.4, False)])
        elsewhere = T.dimension_check([[0, 100000], [60000, 100000], [60000, 124000], [0, 124000], [0, 100000]], 0.001)
        self.assertEqual(elsewhere, [])  # 100 m away: another plan's dimension

    def test_area_notes_near_the_outline_are_compared_and_parts_are_not(self) -> None:
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (60000, 0), (60000, 24000), (0, 24000)], close=True, dxfattribs={"layer": "WALL"})
        msp.add_text("本层面积1500.00平方米", dxfattribs={"insert": (40000, -6000), "height": 500})  # 9 m below the plan
        msp.add_text("防火分区1", dxfattribs={"insert": (65000, 12000), "height": 500})
        msp.add_text("900m²", dxfattribs={"insert": (65000, 11000), "height": 500})  # a compartment, labelled beside it
        msp.add_text("45.5m²", dxfattribs={"insert": (10000, 12000), "height": 300})  # a room
        msp.add_text("面积：1440M2", dxfattribs={"insert": (10000, 30000), "height": 500})  # 6 m above: the nearest note, compared, matches
        msp.add_text("本层面积1452.00平方米", dxfattribs={"insert": (40000, 33000), "height": 500})  # 9 m above: farther than the nearest + 2 m, another plan's
        doc.saveas("notes.dxf")
        T.build_index("notes.dxf", Path("notes.sqlite"))
        idx = T.Index(Path("notes.sqlite"))
        try:
            got = T.stated_area_check([[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]], 1440.0, 0.001, idx)
        finally:
            idx.con.close()
        self.assertEqual([(c["value_m2"], c["flagged"]) for c in got["compared"]], [(1500.0, True), (1440.0, False)])  # both 6 m away: a tie
        self.assertEqual(got["not_compared"], {"part_of_floor": 1, "other_size": 1, "farther_note": 1})

    def test_trace_lists_outline_candidates_and_ranks_them_by_the_drawing_s_own_numbers(self) -> None:
        # a 60 x 24 m hall: the wall layer has an 8 m gate in the south wall and a corridor wall 3 m inside it; the drafter's
        # own area polyline lies on an AREA layer; the plan's area and its 60 m total are written on the drawing
        T.close_all()
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for a, b in [((0, 0), (26000, 0)), ((34000, 0), (60000, 0)), ((60000, 0), (60000, 24000)), ((60000, 24000), (0, 24000)), ((0, 24000), (0, 0))]:
            msp.add_line(a, b, dxfattribs={"layer": "WALL"})
        msp.add_line((0, 3000), (60000, 3000), dxfattribs={"layer": "WALL"})  # the corridor wall a trace dives to at the gate
        msp.add_lwpolyline([(0, 0), (60000, 0), (60000, 24000), (0, 24000)], close=True, dxfattribs={"layer": "AREA"})
        msp.add_lwpolyline([(100000, 0), (130000, 0), (130000, 20000), (100000, 20000)], close=True, dxfattribs={"layer": "AREA"})  # another building
        for i in range(40):  # hatching: short pieces that must not drown the walls
            msp.add_line((5000 + i * 1200, 10000), (5000 + i * 1200 + 300, 10300), dxfattribs={"layer": "HATCH"})
        msp.add_text("本层面积1440.00平方米", dxfattribs={"insert": (30000, -6000), "height": 500})
        msp.add_linear_dim(base=(0, -3000), p1=(0, 0), p2=(60000, 0), dxfattribs={"layer": "DIM"}).render()
        doc.saveas("trace.dxf")
        T.build_index("trace.dxf")
        got = T.cmd_trace(argparse.Namespace(box=[-5000, -8000, 65000, 30000], seed=[30000, 12000], layers=None, unit_m=None,
                                             min_line_m=T.TRACE_MIN_LINE_M, close_gaps_m=list(T.TRACE_CLOSE_GAPS_M)))
        best = got["candidates"][0]
        self.assertTrue(best["made_by"].startswith("closed polyline on layer AREA"))
        self.assertEqual((best["area_m2"], best["matches_drawing"]), (1440.0, 2))  # the area note and the 60 m total
        self.assertEqual(sorted(a["kind"] for a in best["against_drawing"] if a["match"]), ["dimension", "stated_area"])
        self.assertTrue(any("close_gaps_m=6" in s for s in best["same_outline_from"]))  # the wall trace with the gate closed is the same outline
        dived = [c for c in got["candidates"] if "close_gaps_m=2" in c["made_by"] and "WALL" in c["made_by"]]
        self.assertEqual((len(dived), dived[0]["matches_drawing"] < 2), (1, True))  # the trace through the open gate matches fewer numbers
        self.assertTrue(all(c["area_m2"] < 2000 for c in got["candidates"]))  # the other building does not contain the seed
        faces = json.loads(T.FACES.read_text(encoding="utf-8"))
        self.assertTrue(faces[best["id"]]["made_by"].startswith("trace:"))

    def test_shallow_notches_are_marked_but_an_l_shape_is_not(self) -> None:
        # a 100 x 40 m outline whose trace cut four notches 3 m deep and 12 m wide into the south wall (144 m², 3.6%)
        notched = [[0, 0]]
        for k in range(4):
            x = 10000 + k * 22000
            notched += [[x, 0], [x, 3000], [x + 12000, 3000], [x + 12000, 0]]
        notched += [[100000, 0], [100000, 40000], [0, 40000], [0, 0]]
        got = T.notch_check(notched, 0.001)
        self.assertEqual((got["notches"], got["filled_m2"], got["flagged"]), (4, 144.0, True))
        self.assertEqual(len(got["polygons"]), 4)
        l_shape = [[0, 0], [100000, 0], [100000, 20000], [50000, 20000], [50000, 60000], [0, 60000], [0, 0]]
        self.assertFalse(T.notch_check(l_shape, 0.001)["flagged"])  # a re-entrant corner 50 m wide is the building's shape

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
