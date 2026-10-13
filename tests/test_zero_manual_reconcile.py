from __future__ import annotations

import unittest

from cad_to_layout.zero_manual.reconcile import reconcile


class ReconcileTests(unittest.TestCase):
    def test_handle_then_overlap_matching_and_split_counts(self) -> None:
        product = {"records": [
            {"ref": "I:AA/BB", "view_id": "V:1", "type": "STACKER", "basis": "text", "centre": [5, 5], "length_mm": 4, "width_mm": 2},
            {"ref": "I:CC", "view_id": "V:1", "type": "PALLET_CONV_ROLLER", "basis": "llm_shape", "centre": [20, 5], "length_mm": 3, "width_mm": 1},
            {"ref": "I:DD", "view_id": "V:1", "type": "RACK", "basis": "llm_shape", "centre": [40, 5], "length_mm": 3, "width_mm": 1},
            {"ref": "I:EE", "view_id": "V:2", "type": "STACKER", "basis": "text", "centre": [500, 5], "length_mm": 4, "width_mm": 2},
        ]}
        audit = {"views": [{"view_id": "a", "window": [0, 0, 50, 10], "equipment": [
            {"type": "STACKER", "handles": ["AA/BB"]},
            {"type": "PALLET_CONV_ROLLER", "bbox": [18.5, 4.5, 21.5, 5.5]},
            {"type": "BOX_LIFT", "handles": ["ZZ"]},
        ], "unresolved": [{"handles": ["QQ"], "why": "unclear"}]}]}
        views = [{"view_id": "V:1", "window": [0, 0, 50, 10]}, {"view_id": "V:2", "window": [480, 0, 520, 10]}]
        report = reconcile(product, views, audit)["views"][0]
        self.assertEqual(report["product_views"], ["V:1"], "other views are never merged in")
        self.assertEqual(report["by_type"]["STACKER"]["matched_text"], 1)
        self.assertEqual(report["by_type"]["PALLET_CONV_ROLLER"]["matched_llm"], 1)
        self.assertEqual(report["by_type"]["BOX_LIFT"]["missed"], 1)
        self.assertEqual(report["extra_in_product"], {"RACK": {"text_or_geometry": 0, "llm_shape": 1}})
        self.assertEqual(report["audit_unresolved"], 1)

    def test_type_mismatch_is_counted_not_matched(self) -> None:
        product = {"records": [{"ref": "I:BB", "view_id": "V:1", "type": "BOX_LIFT", "basis": "llm_shape", "centre": [5, 5], "length_mm": 4, "width_mm": 2}]}
        audit = {"views": [{"view_id": "a", "window": [0, 0, 50, 10], "equipment": [{"type": "STACKER", "handles": ["BB"]}], "unresolved": []}]}
        report = reconcile(product, [{"view_id": "V:1", "window": [0, 0, 50, 10]}], audit)["views"][0]
        self.assertEqual(report["by_type"]["STACKER"]["type_mismatch"], 1)
        self.assertEqual(report["by_type"]["STACKER"]["matched"], 0)

    def test_module_groups_parts_and_untyped_candidates(self) -> None:
        def rec(ref, kind, x, basis="llm_shape"):
            return {"ref": ref, "view_id": "V:1", "type": kind, "basis": basis, "centre": [x, 5], "length_mm": 1, "width_mm": 1}

        product = {
            "records": [rec("I:ROW1/B1", "RACK", 1), rec("I:ROW1/B2", "RACK", 2), rec("I:ROW1/S", "STACKER", 3, "text"),
                        rec("I:SEG1", "BOX_CONVEYOR", 30, "text"), rec("I:SEG2", "BOX_CONVEYOR", 32, "text"),
                        rec("P:C1", "RACK", 41, "text")],
            "candidates_untyped": [{"ref": "P:C2", "view_id": "V:1", "key": "cells:1x1", "bbox": [41.5, 4.5, 42.5, 5.5], "drawn_as": "outline_cell"}],
        }
        audit = {"views": [{"view_id": "a", "window": [0, 0, 50, 10], "equipment": [
            # a rack row audited as one block holding 3 bays: the product's bays inside that block match, the stacker inside it does not
            {"type": "RACK", "count_unit": "module", "count": 3, "groups": [{"handles": ["ROW1"], "count": 3, "bbox": [0, 4, 4, 6]}]},
            # one lane audited as two listed segments: the second segment is a further part, not an extra machine
            {"type": "BOX_CONVEYOR", "count": 1, "handles": ["SEG1", "SEG2"], "bbox": [29, 4, 33, 6]},
            # rack cells audited by handle: one typed by the product, one found but untyped, one not found at all
            {"type": "RACK", "count_unit": "module", "count": 3, "groups": [{"handles": ["C1", "C2", "C3"], "count": 3, "bbox": [40, 4, 44, 6]}]},
        ], "unresolved": []}]}
        report = reconcile(product, [{"view_id": "V:1", "window": [0, 0, 50, 10]}], audit)["views"][0]
        rack = report["by_type"]["RACK"]
        self.assertEqual((rack["audit"], rack["matched"], rack["missed"]), (6, 3, 3))
        self.assertEqual((rack["missed_but_candidate_untyped"], rack["missed_not_a_candidate"]), (1, 2))
        self.assertEqual(report["by_type"]["BOX_CONVEYOR"]["matched_text"], 1)
        self.assertEqual(report["further_parts_of_audited_units"], 1)
        self.assertEqual(report["extra_in_product"], {"STACKER": {"text_or_geometry": 1, "llm_shape": 0}})

    def test_units_audited_by_their_number_match_the_numbered_units(self) -> None:
        body = {"ref": "I:FRAME", "view_id": "V:1", "type": "PALLET_CONV_CHAIN", "basis": "llm_shape", "centre": [5, 5], "length_mm": 4, "width_mm": 2}
        product = {
            "records": [body],
            "numbered_units": [
                {"number": "5215", "ref": "T:N1", "view_id": "V:1", "xy": [4, 5], "type": "PALLET_CONV_CHAIN", "basis": "llm_shape", "host_ref": "I:FRAME"},
                {"number": "5216", "ref": "T:N2", "view_id": "V:1", "xy": [6, 5], "type": "OTHER_EQUIPMENT", "basis": "llm_shape", "host_ref": "I:FRAME/DS"},
                {"number": "5211", "ref": "T:N3", "view_id": "V:1", "xy": [20, 5], "type": None, "basis": None, "host_ref": None},
            ],
            "candidates_untyped": [{"ref": "T:N3", "view_id": "V:1", "key": "number:x", "bbox": [20, 5, 21, 6], "drawn_as": "number_tag"}],
        }
        audit = {"views": [{"view_id": "a", "window": [0, 0, 50, 10], "equipment": [
            {"type": "PALLET_CONV_CHAIN", "count": 1, "groups": [{"handles": ["N1", "E1", "FRAME"], "bbox": [3, 4, 7, 6]}]},
            {"type": "OTHER:顶升移载机", "count": 1, "groups": [{"handles": ["N2", "E2", "FRAME"], "bbox": [3, 4, 7, 6]}]},
            {"type": "PALLET_CONV_ROLLER", "count": 1, "groups": [{"handles": ["N3", "E3", "GIANT"], "bbox": [19, 4, 22, 7]}]},
        ], "unresolved": []}]}
        report = reconcile(product, [{"view_id": "V:1", "window": [0, 0, 50, 10]}], audit)["views"][0]
        self.assertEqual(report["by_type"]["PALLET_CONV_CHAIN"]["matched_llm"], 1)
        self.assertEqual(report["by_type"]["OTHER_EQUIPMENT"]["matched_llm"], 1)
        roller = report["by_type"]["PALLET_CONV_ROLLER"]
        self.assertEqual((roller["missed"], roller["missed_but_candidate_untyped"]), (1, 1))
        self.assertEqual(report["extra_in_product"], {}, "the body that carries the numbers is not an extra machine")


if __name__ == "__main__":
    unittest.main()
