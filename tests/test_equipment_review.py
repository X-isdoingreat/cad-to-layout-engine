"""Equipment review page: which views count, one item per kind (no CAD needed)."""

from __future__ import annotations

import unittest

from scripts.equipment_review import items_of, not_counted


def _views():
    return {"P": {"title": {"text": "首层平面图"}, "role": "plan"}, "Q": {"title": {"text": "首层平面图"}, "role": "plan"},
            "S": {"title": {"text": "剖面图"}, "role": "section"}}


def _record(view, family, kind="RACK"):
    return {"ref": f"{view}{family}", "view_id": view, "type": kind, "type_zh": "货架", "family": family, "basis": "llm_shape",
            "length_mm": 1000.0, "width_mm": 800.0, "rotation_deg": 0.0, "centre": [0, 0]}


class EquipmentReviewTest(unittest.TestCase):
    def test_copies_and_sections_are_not_counted(self) -> None:
        equipment = {"duplicate_views": [{"view_id": "Q", "same_as": "P"}]}
        self.assertEqual(not_counted(equipment, _views()), {"Q", "S"})

    def test_one_item_per_kind_with_the_count_on_counted_views(self) -> None:
        records = [_record("P", "bay")] * 3 + [_record("Q", "bay")] * 3 + [_record("S", "bay")] * 2 + [_record("P", "arm", "ROBOT_ARM")]
        equipment = {"records": records, "duplicate_views": [{"view_id": "Q", "same_as": "P"}],
                     "families": {"bay": {"decision": {"votes": ["RACK", "RACK", "RACK"]}}}, "unrecognised": []}
        items = items_of(equipment, _views())
        self.assertEqual([(i["id"], i["family"], i["count"]) for i in items], [("E01", "bay", 3), ("E02", "arm", 1)])
        self.assertEqual(items[0]["basis"], "AI 看图（3/3 次一致）")


class RepeatReplyTest(unittest.TestCase):
    """A one-sentence reply on groups counted by repeated linework: R-first is the program's own count, later groups
    count only when the person says 是."""

    def _rows(self, decisions):
        import json
        import tempfile
        from pathlib import Path

        from scripts.equipment_review import repeat_rows

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "review" / "repeat").mkdir(parents=True)
            groups = [{"id": "R1", "example": [0, 0, 2820, 990], "places": [[0, 0, 1, 1]] * 1200, "suggestion": {"type": "RACK", "name": "", "votes": "3/3"}},
                      {"id": "R2", "example": [0, 0, 2783, 990], "places": [[0, 0, 1, 1]] * 40},
                      {"id": "R3", "example": [0, 0, 1505, 990], "places": [[0, 0, 1, 1]] * 48}]
            (run / "review" / "repeat" / "repeat.json").write_text(json.dumps({"views": [{"view_id": "P", "groups": groups}]}), encoding="utf-8")
            return [(r["id"], r["type"], r["count"], r["source"]) for r in repeat_rows(run, decisions)]

    def test_unanswered_counts_only_the_programs_own_group(self) -> None:
        self.assertEqual(self._rows({}), [("R1", "RACK", 1200, "系统识别（未核对）（重复图形计数，每处按 1 计、未确认）")])

    def test_reply_confirms_first_group_and_adds_only_the_groups_said_yes(self) -> None:
        rows = self._rows({"R1": "对", "R2": "不是", "R3": "是"})
        self.assertEqual([(i, n) for i, _, n, _ in rows], [("R1", 1200), ("R3", 48)])
        self.assertTrue(all(t == "RACK" for _, t, _, _ in rows))

    def test_reply_can_rename_or_drop_the_first_group(self) -> None:
        self.assertEqual(self._rows({"R1": "托盘输送机（滚筒）"})[0][1], "PALLET_CONV_ROLLER")
        self.assertEqual(self._rows({"R1": "不是", "R3": "是"}), [])

    def test_reply_says_how_many_units_one_place_holds(self) -> None:
        rows = self._rows({"R1": "对，每处2格", "R3": "是"})
        self.assertEqual([(i, n) for i, _, n, _ in rows], [("R1", 2400), ("R3", 96)])  # a later group takes R1's number
        rows = self._rows({"R1": "货架 ×2", "R3": "是，每处1格"})
        self.assertEqual([(i, t, n) for i, t, n, _ in rows], [("R1", "RACK", 2400), ("R3", "RACK", 48)])

    def test_what_the_ai_called_not_equipment_counts_only_when_the_person_names_it(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from scripts.equipment_review import repeat_rows

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "review" / "repeat").mkdir(parents=True)
            groups = [{"id": "R1", "example": [0, 0, 2820, 990], "places": [[0, 0, 1, 1]] * 600,
                       "suggestion": {"type": "NOT_EQUIPMENT", "name": "钢结构支撑", "votes": "3/3"}}]
            (run / "review" / "repeat" / "repeat.json").write_text(json.dumps({"views": [{"view_id": "P", "groups": groups}]}), encoding="utf-8")
            self.assertEqual(repeat_rows(run, {}), [])
            self.assertEqual(repeat_rows(run, {"R1": "对"}), [])
            rows = repeat_rows(run, {"R1": "是货架，每处2格"})
            self.assertEqual([(r["type"], r["count"]) for r in rows], [("RACK", 1200)])

    def test_review_drawing_carries_each_r_group_on_its_own_layer(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from scripts.equipment_review import _repeat_entities

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            self.assertEqual(_repeat_entities(run, {}), [])  # no counting done: nothing drawn
            (run / "review" / "repeat").mkdir(parents=True)
            views = [{"view_id": "A", "groups": [{"id": "R1", "example": [0, 0, 2820, 990], "places": [[0, 0, 2820, 990], [3000, 0, 5820, 990]],
                                                   "suggestion": {"type": "RACK", "votes": "3/3"}},
                                                  {"id": "R2", "example": [0, 2000, 1410, 2990], "places": [[0, 2000, 1410, 2990]]}]},
                     {"view_id": "B", "groups": [{"id": "R3", "example": [0, 0, 100, 100], "places": [[0, 0, 100, 100]],
                                                   "suggestion": {"type": "NOT_EQUIPMENT", "name": "柱网"}},
                                                  {"id": "R4", "example": [0, 200, 100, 300], "places": [[0, 200, 100, 300]]}]}]
            (run / "review" / "repeat" / "repeat.json").write_text(json.dumps({"views": views}), encoding="utf-8")
            layers: dict = {}
            out = _repeat_entities(run, layers)
            self.assertEqual(sorted(layers), ["CTLE_重复图形_R1", "CTLE_重复图形_R2", "CTLE_重复图形_R3", "CTLE_重复图形_R4"])
            texts = {e["record"]["repeat"]: e["text"] for e in out if e["kind"] == "text"}
            self.assertIn("货架", texts["R1"])
            self.assertIn("也是货架吗", texts["R2"])
            self.assertIn("不是设备", texts["R3"])
            self.assertNotIn("NOT_EQUIPMENT", texts["R4"])
            self.assertEqual(sum(1 for e in out if e["layer"] == "CTLE_重复图形_R1" and e["kind"] == "outline"), 3)  # 2 places + the example
