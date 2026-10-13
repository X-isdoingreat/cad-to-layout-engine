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
