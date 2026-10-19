"""Human confirmation: items from findings and disagreements, and reading a person's decisions back (no CAD needed)."""

from __future__ import annotations

import unittest

from cad_to_layout.zero_manual import confirmation as C

A = [[0, 0], [100, 0], [100, 40], [0, 40], [0, 0]]  # building A with a 10 m strip on top (y 30-40) that may not belong
STRIP = [[0, 30], [100, 30], [100, 40], [0, 40], [0, 30]]
B = [[200, 0], [250, 0], [250, 20], [200, 20], [200, 0]]


def read(outline_a=A) -> dict:
    return {"answer": {"unit_m": 1.0, "buildings": [{"name": "A", "designated": True}, {"name": "B"}],
                       "zones": [{"name": "备件区", "face": "F9", "category": "to_confirm", "view": "1F"}]},
            "checks": [{"name": "A", "outline": outline_a}, {"name": "B", "outline": B}]}


FOUND = [{"kind": "outline_beyond_grid", "building": "A", "text": "...", "impact_m2": 1000.0, "at": [50, 35], "geometry": {"polygons": [STRIP]}},
         {"kind": "bookkeeping", "building": "A", "text": "...", "impact_m2": 0.0, "at": [50, 20], "geometry": None}]
FACES = {"F9": {"outline": [[210, 5], [220, 5], [220, 15], [210, 15], [210, 5]], "area_m2": 100.0}}


def issue(items):
    labels = {it["id"]: f"H{k}" for k, it in enumerate(items)}
    entities = {h: C.LAYER_LABEL for h in labels.values()}
    entities.update({"COL1": C.COLUMN_LAYER, "COL2": C.COLUMN_LAYER})
    return {"labels": labels, "entities": entities, "column_building": {"COL1": "A", "COL2": "A"}}


def returned(issued, moves=None, deleted=(), outlines=(), open_outlines=(), human_columns=(), columns=((10, 10), (90, 10))):
    entities = dict(issued["entities"])
    for item_handle, layer in (moves or {}).items():
        entities[item_handle] = layer
    for h in deleted:
        entities.pop(h, None)
    closed = [{"handle": f"O{k}", "layer": C.LAYER_HUMAN_OUTLINE, "points": p} for k, p in enumerate(outlines)]
    for o in closed:
        entities[o["handle"]] = o["layer"]
    return {"entities": entities, "closed_outlines": closed, "open_outlines": [{"handle": "X", "layer": C.LAYER_HUMAN_OUTLINE} for _ in open_outlines],
            "human_columns": [list(q) for q in human_columns], "columns": [list(q) for q in columns]}


class ConfirmationTest(unittest.TestCase):
    """The full-target behaviour (2026-10-07: building + named zones); the 2026-10-09 trial is AreaOnlyTest."""

    def setUp(self) -> None:
        C.AREA_ONLY = False
        self.addCleanup(setattr, C, "AREA_ONLY", True)
        self.items = C.build_items(FOUND, read(), FACES)
        self.issued = issue(self.items)
        self.ids = {it["kind"]: it["id"] for it in self.items}

    def test_items_leave_out_bookkeeping_and_add_unsure_zones(self) -> None:
        self.assertEqual(sorted(self.ids), ["designated_outline_review", "outline_beyond_grid", "zone_category"])  # the main outline is always looked at
        self.assertEqual([i["kind"] for i in self.items[:2]], ["designated_outline_review", "outline_beyond_grid"])  # largest effect first; the whole outline leads

    def test_no_removes_the_strip_and_labels_the_number_human_confirmed(self) -> None:
        h = self.issued["labels"][self.ids["outline_beyond_grid"]]
        review = self.issued["labels"][self.ids["designated_outline_review"]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, {h: C.LAYER_NO, review: C.LAYER_YES}), 1.0)
        a = out["buildings"][0]
        self.assertEqual((a["name"], a["area_m2"], a["outline_source"], a["status"]), ("A", 3000.0, "人工确认", "完成"))
        self.assertEqual([x["name"] for x in out["buildings"]], ["A"])  # trial scope: the other building is not reported

    def test_unhandled_and_deleted_marks_stay_pending(self) -> None:
        out = C.apply(read(), self.items, self.issued, returned(self.issued), 1.0)
        self.assertEqual(out["buildings"][0]["status"], "待确认")
        h = self.issued["labels"][self.ids["outline_beyond_grid"]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, deleted=[h]), 1.0)
        self.assertEqual(out["buildings"][0]["status"], "待确认")
        self.assertTrue(any("编号被删除" in p["problem"] for p in out["problems"]))

    def test_drawn_outline_answers_outline_questions(self) -> None:
        drawn = [[0, 0], [100, 0], [100, 31], [0, 31], [0, 0]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, outlines=[drawn]), 1.0)
        a = out["buildings"][0]
        self.assertEqual((a["area_m2"], a["outline_source"], a["status"]), (3100.0, "人工确认", "完成"))

    def test_edit_on_an_unmarked_building_is_labelled_so(self) -> None:
        drawn = [[200, 0], [252, 0], [252, 20], [200, 20], [200, 0]]
        C.TRIAL_SCOPE = False  # the general behaviour, all buildings reported
        self.addCleanup(setattr, C, "TRIAL_SCOPE", True)
        out = C.apply(read(), self.items, self.issued, returned(self.issued, outlines=[drawn]), 1.0)
        b = next(x for x in out["buildings"] if x["name"] == "B")
        self.assertEqual(b["outline_source"], "人工修改（未标记处）")
        self.assertIn("B", out["unmarked_edits"])

    def test_ambiguous_and_open_outlines_are_reported_not_used(self) -> None:
        across = [[50, 0], [240, 0], [240, 20], [50, 20], [50, 0]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, outlines=[across], open_outlines=[1]), 1.0)
        self.assertTrue(any("同时盖住几栋楼" in p["problem"] for p in out["problems"]))
        self.assertTrue(any("没有闭合" in p["problem"] for p in out["problems"]))
        self.assertEqual(out["buildings"][0]["area_m2"], 4000.0)  # unchanged

    def test_zone_category_and_column_edits(self) -> None:
        hz = self.issued["labels"][self.ids["zone_category"]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, {hz: "CTLE_类别_存储"}, columns=[(10, 10)], human_columns=[(50, 10)]), 1.0)
        self.assertEqual(out["zones"][0]["category"], "storage")
        a = out["buildings"][0]
        self.assertEqual((a["columns"], a["columns_source"]), (2, "人工修改（未标记处）"))  # no column item was issued

    def test_a_frame_shaped_disagreement_keeps_its_hole(self) -> None:
        inner = [[10, 5], [90, 5], [90, 35], [10, 35], [10, 5]]
        items = C.build_items([], read(), {}, [read(inner)])
        items = [i for i in items if i["kind"] == "reads_extra"]
        self.assertEqual(len(items), 1)
        issued = issue(items)
        out = C.apply(read(), items, issued, returned(issued, {issued["labels"][items[0]["id"]]: C.LAYER_NO}), 1.0)
        self.assertEqual(out["buildings"][0]["area_m2"], 2400.0)  # the frame round the inner outline removed, not the whole building

    def test_column_counts_that_differ_between_reads_are_marked_and_edits_count_as_confirmed(self) -> None:
        mine, other = read(), read()
        mine["answer"]["buildings"][0]["columns"] = {"count": 3}
        other["answer"]["buildings"][0]["columns"] = {"count": 2}
        self.assertEqual([i for i in C.build_items([], mine, {}, [other]) if i["kind"] == "reads_columns"], [])  # trial scope: no column marks
        items = [i for i in C.disagreement_items(mine, [other], 1.0) if i["kind"] == "reads_columns"]
        for n, i in enumerate(items):
            i["id"] = f"C{n + 1:02d}"
        self.assertEqual([i["building"] for i in items], ["A"])
        issued = issue(items)
        out = C.apply(mine, items, issued, returned(issued, {issued["labels"][items[0]["id"]]: C.LAYER_YES}, columns=[(10, 10)]), 1.0)
        a = out["buildings"][0]
        self.assertEqual((a["columns"], a["columns_source"], a["status"]), (1, "人工确认", "完成"))

    def test_views_are_marked_and_totals_follow_the_choices(self) -> None:
        r = read()
        r["answer"]["views"] = [{"name": "1F", "role": "floor plan", "window": [190, -5, 260, 25]}, {"name": "B方案", "role": "scheme", "window": [400, 0, 500, 50]}]
        r["answer"]["zones"] = [{"view": "1F", "name": "存储区", "face": "F8", "area_m2": 300.0, "category": "storage"},
                                {"view": "1F", "name": "备件区", "face": "F9", "area_m2": 100.0, "category": "to_confirm"},
                                {"view": "B方案", "name": "存储区", "face": "F7", "area_m2": 500.0, "category": "storage"}]
        faces = dict(FACES, F8={"outline": [[200, 0], [230, 0], [230, 10], [200, 10], [200, 0]], "area_m2": 300.0},
                     F7={"outline": [[400, 0], [450, 0], [450, 10], [400, 10], [400, 0]], "area_m2": 500.0})
        items = C.build_items([], r, faces)
        kinds = sorted(i["kind"] for i in items)
        self.assertEqual(kinds, ["designated_outline_review", "view_count", "view_count", "zone_category"])
        issued = issue(items)
        moves = {}
        for i in items:
            if i["kind"] == "view_count":
                moves[issued["labels"][i["id"]]] = C.LAYER_YES if i["view"] == "1F" else C.LAYER_NO
            else:
                moves[issued["labels"][i["id"]]] = "CTLE_类别_存储"
        out = C.apply(r, items, issued, returned(issued, moves), 1.0)
        views = {v["view"]: v for v in out["views"]}
        self.assertEqual((views["1F"]["counted"], views["1F"]["storage_m2"], views["1F"]["status"]), ("计入", 400.0, "完成"))
        self.assertEqual(views["B方案"]["counted"], "不计入")
        unhandled = C.apply(r, items, issued, returned(issued), 1.0)
        self.assertEqual({v["view"]: v["status"] for v in unhandled["views"]}, {"1F": "待确认", "B方案": "待确认"})

    def test_saying_the_outline_is_wrong_without_redrawing_keeps_it_pending(self) -> None:
        review = self.issued["labels"][self.ids["designated_outline_review"]]
        strip = self.issued["labels"][self.ids["outline_beyond_grid"]]
        out = C.apply(read(), self.items, self.issued, returned(self.issued, {review: C.LAYER_NO, strip: C.LAYER_YES}), 1.0)
        self.assertEqual(out["buildings"][0]["status"], "待确认")
        self.assertTrue(any("没有在「CTLE_人工轮廓」层重描" in p["problem"] for p in out["problems"]))

    def test_drawing_number_and_notch_marks_are_questions_about_the_outline(self) -> None:
        notch = [[40, 0], [60, 0], [60, 3], [40, 3], [40, 0]]
        found = FOUND + [{"kind": "stated_area", "building": "A", "text": "图上写着 4200，量出 4000", "impact_m2": 200.0, "at": [50, 10], "geometry": None},
                         {"kind": "notches", "building": "A", "text": "1 处浅凹口", "impact_m2": 60.0, "at": [50, 1], "geometry": {"polygons": [notch]}}]
        items = C.build_items(found, read(), FACES)
        ids = {it["kind"]: it["id"] for it in items}
        self.assertIn("stated_area", ids)
        self.assertIn("notches", ids)
        self.assertTrue(any(e["kind"] == "outline" and e["record"]["item"] == ids["notches"] for e in C.mark_entities(items, 1.0)))  # the notches are drawn
        issued = issue(items)
        labels = issued["labels"]
        # "yes" to both: the outline stands, confirmed by a person
        yes = C.apply(read(), items, issued, returned(issued, {labels[ids["stated_area"]]: C.LAYER_YES, labels[ids["notches"]]: C.LAYER_YES,
                                                             labels[ids["designated_outline_review"]]: C.LAYER_YES, labels[ids["outline_beyond_grid"]]: C.LAYER_YES}), 1.0)
        self.assertEqual((yes["buildings"][0]["area_m2"], yes["buildings"][0]["outline_source"]), (4000.0, "人工确认"))
        # "no" without a redrawn outline: nothing is changed by guessing, the item stays pending
        no = C.apply(read(), items, issued, returned(issued, {labels[ids["notches"]]: C.LAYER_NO}), 1.0)
        self.assertIn(ids["notches"], no["buildings"][0]["pending_items"])
        self.assertTrue(any("没有在「CTLE_人工轮廓」层重描" in p["problem"] for p in no["problems"]))
        # a redrawn outline answers every outline question, the notch one included
        drawn = [[0, 0], [100, 0], [100, 30], [0, 30], [0, 0]]
        redrawn = C.apply(read(), items, issued, returned(issued, {labels[ids["notches"]]: C.LAYER_NO}, outlines=[drawn]), 1.0)
        self.assertEqual((redrawn["buildings"][0]["area_m2"], redrawn["buildings"][0]["status"]), (3000.0, "完成"))

    def test_default_categories_by_name_and_one_question_per_name(self) -> None:
        self.assertEqual([C.default_category(n) for n in ("飞机轮子存放库", "入库区", "办公区", "备件区", "领料间")],
                         ["storage", "operation", "office_living", None, "operation"])
        r = read()
        r["answer"]["zones"] = [{"view": "V1", "name": "备件区", "face": "F9", "area_m2": 100.0, "category": "to_confirm"},
                                {"view": "V2", "name": "备件区", "face": "F9", "area_m2": 100.0, "category": "to_confirm"},
                                {"view": "V1", "name": "不可用件库", "face": "F9", "area_m2": 100.0, "category": "to_confirm"}]
        items = [i for i in C.build_items([], r, FACES) if i["kind"] == "zone_category"]
        self.assertEqual([i["zone"] for i in items], ["备件区"])  # asked once; the 库 is storage by default
        issued = issue(items)
        out = C.apply(r, items, issued, returned(issued, {issued["labels"][items[0]["id"]]: "CTLE_类别_存储"}), 1.0)
        self.assertEqual(sorted((z["view"], z["zone"], z["category"]) for z in out["zones"]),
                         [("V1", "不可用件库", "storage"), ("V1", "备件区", "storage"), ("V2", "备件区", "storage")])

    def test_a_zone_name_without_area_is_marked_and_a_drawn_boundary_gives_it_one(self) -> None:
        r = read()
        r["answer"]["views"] = [{"name": "1F", "role": "floor plan", "window": [190, -5, 260, 25]}]
        r["answer"]["zones"] = [{"view": "1F", "name": "存储区", "face": "F9", "area_m2": 100.0, "category": "storage"}]
        texts = [{"t": "存储区", "x": 215, "y": 10}, {"t": "整箱存储库", "x": 240, "y": 10}, {"t": "说明", "x": 200, "y": 0}]
        items = [i for i in C.build_items([], r, FACES, texts=texts, suffixes=["区", "库"]) if i["kind"] == "zone_no_area"]
        self.assertEqual([i["zone"] for i in items], ["整箱存储库"])
        issued = issue(items)
        back = returned(issued)
        back["closed_outlines"].append({"handle": "Z1", "layer": C.LAYER_HUMAN_ZONE, "points": [[235, 5], [245, 5], [245, 15], [235, 15], [235, 5]]})
        back["entities"]["Z1"] = C.LAYER_HUMAN_ZONE
        out = C.apply(r, items, issued, back, 1.0)
        z = next(z for z in out["zones"] if z["zone"] == "整箱存储库")
        self.assertEqual((z["area_m2"], z["category"], z["source"]), (100.0, "storage", "人工确认"))
        self.assertEqual(out["buildings"][0]["area_m2"], 4000.0)  # a zone boundary never replaces the building outline

    def test_disagreement_with_another_read_becomes_items(self) -> None:
        other = read([[0, 0], [100, 0], [100, 30], [0, 30], [0, 0]])
        items = C.disagreement_items(read(), [other], 1.0)
        self.assertEqual([(i["kind"], i["building"], i["impact_m2"]) for i in items], [("reads_extra", "A", 1000.0)])


if __name__ == "__main__":
    unittest.main()



class AreaOnlyTest(unittest.TestCase):
    """Trial 2026-10-10 (target rev 22): the total area only; review with an overall 通过 or a redrawn outline."""

    def setUp(self) -> None:
        self.items = C.build_items(FOUND, read(), FACES)
        self.issued = issue(self.items)
        self.ids = {it["kind"]: it["id"] for it in self.items}

    def test_marks_are_about_the_outline_only(self) -> None:
        self.assertEqual(sorted(self.ids), ["designated_outline_review", "outline_beyond_grid"])  # no zone or room marks

    def test_overall_pass_keeps_the_ai_result_and_labels_it(self) -> None:
        out = C.apply(read(), self.items, self.issued, returned(self.issued), 1.0, passed=True)
        a = out["buildings"][0]
        self.assertEqual((a["area_m2"], a["outline_source"], a["status"]), (4000.0, "人工复查通过", "完成"))  # the strip stays: the AI counted it
        self.assertEqual((out["pending_items"], out["zones"], out["views"]), ([], [], []))
        self.assertNotIn("usable_m2", a)  # nothing is subtracted from the total

    def test_untouched_marks_without_pass_stay_pending(self) -> None:
        a = C.apply(read(), self.items, self.issued, returned(self.issued), 1.0)["buildings"][0]
        self.assertEqual((a["outline_source"], a["status"]), ("AI 读出", "待确认"))

    def test_a_redrawn_outline_with_pass_is_a_human_edit(self) -> None:
        drawn = [[0, 0], [100, 0], [100, 30], [0, 30], [0, 0]]
        a = C.apply(read(), self.items, self.issued, returned(self.issued, outlines=[drawn]), 1.0, passed=True)["buildings"][0]
        self.assertEqual((a["area_m2"], a["outline_source"], a["status"]), (3000.0, "人工修改", "完成"))

    def test_the_same_finding_twice_is_one_mark(self) -> None:
        twice = FOUND + [dict(FOUND[0])]
        items = C.build_items(twice, read(), FACES)
        self.assertEqual(sum(1 for i in items if i["kind"] == "outline_beyond_grid"), 1)
