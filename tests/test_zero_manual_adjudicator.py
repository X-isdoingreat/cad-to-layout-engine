"""Adjudication without an agent: discrepancies in three columns, and verdicts applied only when the program's own
re-measurement matches a drawing-stated number (no CAD, no AI; metres, 1 unit = 1 m)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cad_to_layout.zero_manual import adjudicator as J
from cad_to_layout.zero_manual import agent_reader
from cad_to_layout.zero_manual import confirmation as C

RECT = [[0, 0], [100, 0], [100, 40], [0, 40], [0, 0]]  # the building as the drawing states it: 4000 m²
NOTCHED = [[0, 0], [40, 0], [40, 3], [60, 3], [60, 0], [100, 0], [100, 40], [0, 40], [0, 0]]  # the automatic trace: a 20 × 3 notch, 3940 m²
TALLER = [[0, 0], [100, 0], [100, 41], [0, 41], [0, 0]]  # 4100 m²: not the stated number
FAR = [[500, 0], [600, 0], [600, 40], [500, 40], [500, 0]]  # another building of the same size


def _face(outline, area, length, width):
    return {"outline": outline, "area_m2": area, "length_m": length, "width_m": width, "orientation_deg": 0.0, "centroid": [50, 20], "texts_inside": []}


FACES = {"F1": _face(NOTCHED, 3940.0, 100.0, 40.0), "F2": _face(RECT, 4000.0, 100.0, 40.0), "F3": _face(TALLER, 4100.0, 100.0, 41.0),
         "F4": _face(FAR, 4000.0, 100.0, 40.0)}


def answer() -> dict:
    return {"unit_m": 1.0, "buildings": [{"name": "A", "designated": True, "faces": ["F1"], "area_m2": 3940.0, "length_m": 100.0, "width_m": 40.0}]}


def checks() -> list[dict]:
    return [{"name": "A", "designated": True, "faces": ["F1"], "status": "matches_remeasurement", "outline": NOTCHED,
             "remeasured": {"area_m2": 3940.0, "length_m": 100.0, "width_m": 40.0, "orientation_deg": 0.0},
             "stated_area_check": {"compared": [{"text": "建筑面积4000平方米", "value_m2": 4000.0, "at": [50, -5], "handle": "T1", "layer": "0", "label": "",
                                                 "measured_m2": 3940.0, "differs_pct": -1.5, "flagged": True}], "not_compared": {}},
             "notch_check": {"notches": 1, "filled_m2": 60.0, "share": 0.015, "flagged": True, "polygons": [[[40, 0], [60, 0], [60, 3], [40, 3], [40, 0]]]},
             "dimension_check": [], "drawing_says": {"dimensions": [], "areas": []}}]


class AdjudicatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "faces.json").write_text(json.dumps(FACES), encoding="utf-8")
        self.found = agent_reader.findings(self.work, answer(), checks())
        self.disc = J.discrepancies(answer(), checks(), self.found)

    def record(self, verdicts: list[dict]) -> dict:
        return {"discrepancies": self.disc, "verdicts": J.validate_verdicts({"verdicts": verdicts}, self.disc)}

    def test_discrepancies_have_the_three_columns(self) -> None:
        self.assertEqual([d["kind"] for d in self.disc], ["stated_area", "notches"])
        area = self.disc[0]
        self.assertEqual((area["id"], area["stated_value_m2"]), ("D1", 4000.0))
        self.assertIn("「建筑面积4000平方米」", area["drawing_says"])
        self.assertIn("3,940", area["ai_read"])
        self.assertIn("3,940", area["program"])
        self.assertIn("没有可对照的数", self.disc[1]["drawing_says"])

    def test_a_remeasurement_that_matches_the_stated_number_replaces_the_numbers(self) -> None:
        new, rec = J.apply_verdicts(self.work, answer(), checks(), self.record([{"discrepancy": "D1", "verdict": "outline_wrong", "faces": ["F2"], "why": "外墙连续"}]))
        self.assertEqual(len(rec["overrides"]), 1)
        b = new["buildings"][0]
        self.assertEqual((b["faces"], b["area_m2"]), (["F2"], 4000.0))
        self.assertEqual(b["adjudicated"]["before"]["area_m2"], 3940.0)
        self.assertEqual(rec["overrides"][0]["matched"][0]["drawing_value"], 4000.0)
        self.assertEqual(rec["verdicts"][0]["applied"], "改判采用：重量结果与图上写的数一致")

    def test_a_remeasurement_that_does_not_match_changes_nothing(self) -> None:
        new, rec = J.apply_verdicts(self.work, answer(), checks(), self.record([{"discrepancy": "D1", "verdict": "outline_wrong", "faces": ["F3"], "why": "..."}]))
        self.assertEqual(rec["overrides"], [])
        self.assertEqual(new["buildings"][0]["area_m2"], 3940.0)
        self.assertIn("对不上", rec["verdicts"][0]["applied"])
        self.assertEqual(rec["refused"][0]["measured"]["area_m2"], 4100.0)

    def test_another_building_of_the_right_size_is_refused(self) -> None:
        new, rec = J.apply_verdicts(self.work, answer(), checks(), self.record([{"discrepancy": "D1", "verdict": "outline_wrong", "faces": ["F4"], "why": "..."}]))
        self.assertEqual(rec["overrides"], [])
        self.assertIn("不是同一栋楼", rec["verdicts"][0]["applied"])

    def test_confirmations_unknown_answers_and_silence_change_nothing(self) -> None:
        rec = self.record([{"discrepancy": "D1", "verdict": "outline_right", "why": "图上的数是轴线面积"}, {"discrepancy": "D2", "verdict": "maybe"}])
        self.assertEqual([v["verdict"] for v in rec["verdicts"]], ["outline_right", "cannot_decide"])
        self.assertIn("不在三种之内", rec["verdicts"][1]["why"])
        new, rec = J.apply_verdicts(self.work, answer(), checks(), rec)
        self.assertEqual((rec["overrides"], new["buildings"][0]["area_m2"]), ([], 3940.0))
        silent = J.validate_verdicts(None, self.disc)
        self.assertTrue(all(v["verdict"] == "cannot_decide" and "没有回答" in v["why"] for v in silent))

    def test_a_face_named_without_a_decision_is_a_candidate_for_the_reviewer_not_a_change(self) -> None:
        new, rec = J.apply_verdicts(self.work, answer(), checks(), self.record([{"discrepancy": "D1", "verdict": "cannot_decide", "faces": ["F3"], "why": "楼梯间算不算不清楚"}]))
        self.assertEqual((rec["overrides"], new["buildings"][0]["area_m2"]), ([], 3940.0))
        cand = rec["verdicts"][0]["candidate"]
        self.assertEqual((cand["faces"], cand["area_m2"]), (["F3"], 4100.0))
        self.assertIn("+2.50%", cand["against_drawing"][0])
        read = {"answer": answer(), "checks": checks(), "adjudication": {"status": "answered", "overrides": [], "verdicts": rec["verdicts"]}}
        items = C.build_items(self.found, read, FACES)
        it = next(x for x in items if x["kind"] == "stated_area")
        self.assertIn("候选轮廓", it["question"])
        self.assertIn(TALLER, it["geometry"]["polygons"])  # drawn beside the mark, in the same purple layer

    def test_the_tool_budget_grows_with_the_work_within_its_bounds(self) -> None:
        # 2 discrepancies, 1 notch: 8 + 2 × 4 + 1 = 17 calls; nothing to look at: the floor; a floor full of notches: the ceiling
        self.assertEqual(J.tool_budget(checks(), self.disc), 17)
        self.assertEqual(J.tool_budget(checks(), []), J.ADJUDICATOR_MAX_TOOL_CALLS)
        many = checks()
        many[0]["notch_check"]["notches"] = 100
        self.assertEqual(J.tool_budget(many, self.disc), J.ADJUDICATOR_BUDGET_MAX)
        self.assertIn("最多 17 次", J._prompt(answer(), checks(), self.disc))

    def test_a_total_dimension_in_dispute_counts_as_a_drawing_number(self) -> None:
        cks = checks()
        cks[0]["stated_area_check"] = {"compared": [], "not_compared": {}}
        cks[0]["notch_check"]["flagged"] = False
        # the drawing's width total is 42.5 m, the traced outline 40 m: 2.5 m beyond the 1 m wall allowance
        cks[0]["drawing_says"] = {"dimensions": [{"side": "宽", "outline_extent_m": 40.0, "dimensions_m": [42.5], "flagged": True, "at": [50, 45]}], "areas": []}
        disc = J.discrepancies(answer(), cks, agent_reader.findings(self.work, answer(), cks))
        self.assertEqual([d["kind"] for d in disc], ["dimension"])
        wide = [[0, 0], [100, 0], [100, 42], [0, 42], [0, 0]]
        faces = dict(FACES, F5=_face(wide, 4200.0, 100.0, 42.0))
        (self.work / "faces.json").write_text(json.dumps(faces), encoding="utf-8")

        def judged(face: str) -> dict:
            return {"discrepancies": disc, "verdicts": J.validate_verdicts({"verdicts": [{"discrepancy": "D1", "verdict": "outline_wrong", "faces": [face]}]}, disc)}

        new, rec = J.apply_verdicts(self.work, answer(), cks, judged("F3"))  # 41 m: still 1.5 m short of the drawn total
        self.assertEqual((rec["overrides"], new["buildings"][0]["width_m"]), ([], 40.0))
        new, rec = J.apply_verdicts(self.work, answer(), cks, judged("F5"))  # 42 m: within max(1 %, 1 m) of 42.5
        self.assertEqual((len(rec["overrides"]), new["buildings"][0]["width_m"]), (1, 42.0))
        self.assertEqual(rec["overrides"][0]["matched"][0]["kind"], "dimension")


class AdjudicatedMarksTest(unittest.TestCase):
    """The reviewer sees every verdict beside its mark and a mark for each replaced number (confirmation.py)."""

    def read_with(self, overrides, verdicts) -> dict:
        return {"answer": {"unit_m": 1.0, "buildings": [{"name": "A", "designated": True, "faces": ["F2"]}]}, "checks": [{"name": "A", "outline": RECT}],
                "adjudication": {"status": "answered", "overrides": overrides, "verdicts": verdicts}}

    def test_a_replaced_number_gets_its_own_mark_with_the_old_outline(self) -> None:
        over = {"building": "A", "discrepancy": "D1", "before": {"area_m2": 3940.0, "length_m": 100.0, "width_m": 40.0}, "after": {"area_m2": 4000.0, "length_m": 100.0, "width_m": 40.0},
                "matched": [{"drawing": "建筑面积4000平方米", "differs_pct": 0.0}], "outline_before": NOTCHED}
        read = self.read_with([over], [{"discrepancy": "D1", "kind": "stated_area", "building": "A", "text": "x", "verdict": "outline_wrong", "why": "外墙连续", "applied": "改判采用：重量结果与图上写的数一致"}])
        items = C.build_items([], read, FACES)
        kinds = {it["kind"]: it for it in items}
        self.assertIn("adjudicated", kinds)
        it = kinds["adjudicated"]
        self.assertEqual(it["geometry"]["polygons"], [NOTCHED])
        self.assertIn("3,940", it["question"])
        self.assertIn("4,000", it["question"])
        self.assertEqual(it["impact_m2"], 60.0)
        issued = {"labels": {x["id"]: f"H{k}" for k, x in enumerate(items)}, "entities": {f"H{k}": C.LAYER_LABEL for k in range(len(items))}, "column_building": {}}
        untouched = {"entities": dict(issued["entities"]), "closed_outlines": [], "open_outlines": [], "human_columns": [], "columns": []}
        preview = C.apply(read, items, issued, untouched, 1.0)
        self.assertEqual((preview["buildings"][0]["outline_source"], preview["buildings"][0]["area_m2"]), ("AI 定夺改判", 4000.0))
        self.assertEqual(preview["buildings"][0]["adjudicated"]["before"]["area_m2"], 3940.0)
        # "no" without a redrawn outline keeps it pending; "yes" to everything is a confirmed number
        label = issued["labels"][it["id"]]
        no = C.apply(read, items, issued, dict(untouched, entities={**issued["entities"], label: C.LAYER_NO}), 1.0)
        self.assertIn(it["id"], no["buildings"][0]["pending_items"])
        passed = C.apply(read, items, issued, untouched, 1.0, passed=True)
        self.assertEqual(passed["buildings"][0]["outline_source"], "人工复查通过")

    def test_a_confirmed_discrepancy_keeps_its_mark_with_the_reason(self) -> None:
        found = [{"kind": "stated_area", "building": "A", "text": "建筑「A」：图上写着「本层面积3800」", "impact_m2": 200.0, "at": [50, 10], "geometry": None}]
        read = self.read_with([], [{"discrepancy": "D1", "kind": "stated_area", "building": "A", "text": found[0]["text"], "verdict": "outline_right",
                                    "why": "图上的数扣掉了中庭", "applied": "不改数字"}])
        items = C.build_items(found, read, FACES)
        it = next(x for x in items if x["kind"] == "stated_area")
        self.assertIn("定夺复核：认为轮廓对——图上的数扣掉了中庭", it["question"])
        self.assertEqual(it["adjudication"]["verdict"], "outline_right")


if __name__ == "__main__":
    unittest.main()
