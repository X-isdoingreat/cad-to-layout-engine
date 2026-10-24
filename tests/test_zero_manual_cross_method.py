"""Two readers by different methods, one reviewer, machine-readable result states (target cross_method_reading_20261023).

No CAD, no AI: the reviewer call is faked; metres, 1 unit = 1 m."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from cad_to_layout.zero_manual import adjudicator as J
from cad_to_layout.zero_manual import agent_reader
from cad_to_layout.zero_manual import confirmation as C
from cad_to_layout.zero_manual import equipment as E

RECT = [[0, 0], [100, 0], [100, 40], [0, 40], [0, 0]]  # 4000 m², what the drawing states
NOTCHED = [[0, 0], [40, 0], [40, 3], [60, 3], [60, 0], [100, 0], [100, 40], [0, 40], [0, 0]]  # 3940 m²: a 20 × 3 notch
TALLER = [[0, 0], [100, 0], [100, 41], [0, 41], [0, 0]]  # 4100 m²


def _face(outline, area, length, width):
    return {"outline": outline, "area_m2": area, "length_m": length, "width_m": width, "orientation_deg": 0.0, "centroid": [50, 20], "texts_inside": []}


FACES = {"F1": _face(NOTCHED, 3940.0, 100.0, 40.0), "F2": _face(RECT, 4000.0, 100.0, 40.0), "F3": _face(TALLER, 4100.0, 100.0, 41.0)}
RISK = [{"id": rid, "checked": True, "what_seen": "看过", "if_wrong_effect_m2": 0} for rid in agent_reader.RISK_IDS]


def _read(method: str, face: str, outline, area, length, width, risk=RISK) -> dict:
    return {"method": method, "valid": True, "dxf": None,
            "answer": {"unit_m": 1.0, "method": method, "method_available": True, "risk_review": risk,
                       "buildings": [{"name": "A", "designated": True, "faces": [face], "area_m2": area, "length_m": length, "width_m": width, "evidence": "沿外墙"}]},
            "checks": [{"name": "A", "designated": True, "faces": [face], "status": "matches_remeasurement", "outline": outline,
                        "remeasured": {"area_m2": area, "length_m": length, "width_m": width, "orientation_deg": 0.0}}]}


def _fake_verify_with_stated(stated: float):
    """verify() as it runs with an index: the measurement from the faces plus the stated-area comparison (1% tolerance)."""

    def verify(work: Path, answer: dict) -> list[dict]:
        faces = json.loads((Path(work) / "faces.json").read_text(encoding="utf-8"))
        out = []
        for b in answer.get("buildings", []):
            from cad_to_layout.zero_manual import agent_tools

            got = agent_tools.measure_faces(b["faces"], faces, 1.0)
            pct = round(100 * (got["area_m2"] - stated) / stated, 2)
            out.append({"name": b["name"], "designated": b.get("designated"), "faces": b["faces"], "status": "matches_remeasurement", "outline": got["outline"],
                        "remeasured": {k: got[k] for k in ("area_m2", "length_m", "width_m", "orientation_deg")},
                        "stated_area_check": {"compared": [{"text": f"建筑面积{stated:.0f}平方米", "value_m2": stated, "measured_m2": got["area_m2"], "differs_pct": pct,
                                                            "flagged": abs(pct) > 1.0, "at": [50, -5]}], "not_compared": {}},
                        "dimension_check": [], "notch_check": {"notches": 0, "filled_m2": 0, "share": 0, "flagged": False, "polygons": []},
                        "drawing_says": {"dimensions": [], "areas": []}})
        return out

    return verify


class RiskReviewTest(unittest.TestCase):
    def test_every_checklist_item_must_be_answered(self) -> None:
        self.assertEqual(agent_reader.risk_review_missing({"risk_review": RISK}), [])
        short = [r for r in RISK if r["id"] != "R3"] + [{"id": "R3", "checked": "yes"}]  # not a yes/no
        self.assertEqual(agent_reader.risk_review_missing({"risk_review": short}), ["R3"])
        self.assertEqual(agent_reader.risk_review_missing({}), list(agent_reader.RISK_IDS))

    def test_a_missing_item_is_a_finding_the_reader_must_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            answer = {"unit_m": 1.0, "buildings": [{"name": "A", "designated": True}], "risk_review": RISK[:-1]}
            found = agent_reader.findings(Path(tmp), answer, [])
            kinds = [f["kind"] for f in found]
            self.assertIn("risk_review", kinds)
            self.assertIn(agent_reader.RISK_IDS[-1], next(f["text"] for f in found if f["kind"] == "risk_review"))
            self.assertIn("risk_review", C.NOT_FOR_PEOPLE)  # a reader's bookkeeping problem, never a mark for a person

    def test_a_drawing_number_the_reader_already_explained_is_not_sent_back_again(self) -> None:
        finding = {"kind": "dimension", "text": "建筑「A」：图上长向的端到端总尺寸标注 190 m，轮廓同方向的长度 144.6 m，相差 -23.9%"}
        silent = {"unresolved": []}
        spoken = {"unresolved": [{"item": "190 m 标注与轮廓长度不一致", "why": "它是轴线总长，两端落在室外广场"}]}
        self.assertFalse(agent_reader.explained(finding, silent))
        self.assertTrue(agent_reader.explained(finding, spoken))
        self.assertFalse(agent_reader.explained(dict(finding, kind="notches"), spoken))  # only drawing-number findings can be talked away

    def test_both_prompts_carry_the_same_checklist_and_say_blind(self) -> None:
        for method in ("wall", "structure"):
            text = agent_reader.PROMPTS[method][1].read_text(encoding="utf-8")
            self.assertIn("{RISK_CHECKLIST}", text)
            self.assertIn("risk_review", text)
            self.assertIn(f'"method": "{method}"', text)
        self.assertIn("看不到那位同事的任何结果", agent_reader.PROMPTS["structure"][1].read_text(encoding="utf-8"))
        import re

        checklist = agent_reader.RISK_CHECKLIST_PATH.read_text(encoding="utf-8")
        numbers = re.sub(r"GB(?:/T)?\s*\d+", "", checklist)  # the cited national standard is the only number allowed
        self.assertIsNone(re.search(r"\d{4,}", numbers), "a coordinate or a drawing number in the checklist")  # generic situations only: no drawing, coordinate or layer
        self.assertIsNone(re.search(r"\b[A-Z]{1,4}-[A-Z]{2,}\b", checklist), "a layer name in the checklist")
        self.assertNotIn(".dwg", checklist.lower())


class PairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "faces.json").write_text(json.dumps(FACES), encoding="utf-8")

    def test_numbers_agree_within_one_percent(self) -> None:
        self.assertTrue(J.numbers_agree({"area_m2": 4000, "length_m": 100, "width_m": 40}, {"area_m2": 4030, "length_m": 100.5, "width_m": 40.2}))
        self.assertFalse(J.numbers_agree({"area_m2": 4000, "length_m": 100, "width_m": 40}, {"area_m2": 3940, "length_m": 100, "width_m": 40}))

    def test_difference_region_names_where_the_outlines_differ(self) -> None:
        d = J.difference_region(NOTCHED, RECT, 1.0)
        self.assertEqual(d["difference_m2"], 60.0)
        self.assertEqual((d["pieces"][0]["only_in"], d["pieces"][0]["area_m2"]), ("b", 60.0))
        self.assertIn("乙有甲无", d["description"])

    def test_agreement_without_a_drawing_discrepancy_needs_no_reviewer_call(self) -> None:
        a = _read("wall", "F2", RECT, 4000.0, 100.0, 40.0)
        b = _read("structure", "F1", RECT, 4000.0, 100.0, 40.0)
        with mock.patch.object(J.agent_reader, "run_prompt", side_effect=AssertionError("no call expected")):
            out = J.adjudicate_pair(self.work, a, b)
        s = out["result_status"]
        self.assertEqual((s["status_code"], s["source"], out["adjudication"]["status"]), ("two_methods_agree", "a", "no_discrepancy"))
        self.assertEqual(s["final"]["area_m2"], 4000.0)
        faces = json.loads((self.work / "faces.json").read_text(encoding="utf-8"))
        self.assertEqual(len(faces), 4)  # B's outline imported here so the reviewer could have rendered it

    def test_a_disagreement_goes_to_the_reviewer_who_may_adopt_b(self) -> None:
        a = _read("wall", "F1", NOTCHED, 3940.0, 100.0, 40.0)
        b = _read("structure", "F2", RECT, 4000.0, 100.0, 40.0)
        fake = {"answer": {"decision": "adopt_b", "why": "放大看外墙是连续的，凹口是描线钻进了门洞", "evidence": ["render 38 -2 62 6 → 外墙线连续"], "wrong_if": "如果那段墙其实是雨篷边线"},
                "minutes": 0.5, "tokens_used": 1000, "tool_calls": 3, "model": "fake", "returncode": 0}
        with mock.patch.object(J.agent_reader, "run_prompt", return_value=fake) as call:
            out = J.adjudicate_pair(self.work, a, b)
        prompt = call.call_args.args[1]
        self.assertIn("两法不一致", prompt)
        self.assertIn("乙有甲无的一块约 60 m²", prompt)
        self.assertIn("R1：查过", prompt)
        s = out["result_status"]
        self.assertEqual((s["decision"], s["source"], s["final"]["area_m2"]), ("adopt_b", "b", 4000.0))
        self.assertEqual(s["status_code"], "undecided")  # adopted with evidence, but neither a drawing number nor the two methods confirm it: both numbers stay visible
        self.assertEqual([n["area_m2"] for n in s["undecided_numbers"]], [3940.0, 4000.0])
        self.assertEqual(out["answer_before_adjudication"]["buildings"][0]["area_m2"], 3940.0)
        self.assertEqual(out["answer"]["buildings"][0]["area_m2"], 4000.0)
        self.assertEqual(s["wrong_if"], "如果那段墙其实是雨篷边线")

    def test_both_wrong_is_adopted_only_when_the_remeasurement_matches_the_drawing(self) -> None:
        a = _read("wall", "F1", NOTCHED, 3940.0, 100.0, 40.0)
        b = _read("structure", "F3", TALLER, 4100.0, 100.0, 41.0)
        fake_ok = {"answer": {"decision": "both_wrong", "faces": ["F2"], "measured": {"area_m2": 4000, "length_m": 100, "width_m": 40}, "why": "两人都没沿外皮", "evidence": ["corners → F2"]},
                   "minutes": 1, "tokens_used": 1, "tool_calls": 2, "model": "fake", "returncode": 0}
        with mock.patch.object(J.agent_reader, "verify", _fake_verify_with_stated(4000.0)), mock.patch.object(J.agent_reader, "run_prompt", return_value=fake_ok):
            out = J.adjudicate_pair(self.work, a, b)
        s = out["result_status"]
        self.assertEqual((s["status_code"], s["source"], s["final"]["area_m2"]), ("matches_drawing", "reviewer", 4000.0))
        self.assertEqual(len(out["adjudication"]["overrides"]), 1)
        self.assertIn("复查重描采用", out["adjudication"]["applied"])
        # the same verdict with a face that matches nothing becomes undecided, the face a candidate only
        a2 = _read("wall", "F1", NOTCHED, 3940.0, 100.0, 40.0)
        b2 = _read("structure", "F3", TALLER, 4100.0, 100.0, 41.0)
        fake_bad = dict(fake_ok, answer=dict(fake_ok["answer"], faces=["F3"]))
        with mock.patch.object(J.agent_reader, "verify", _fake_verify_with_stated(4000.0)), mock.patch.object(J.agent_reader, "run_prompt", return_value=fake_bad):
            out2 = J.adjudicate_pair(self.work, a2, b2)
        s2 = out2["result_status"]
        self.assertEqual((s2["status_code"], s2["decision"], s2["source"], s2["final"]["area_m2"]), ("undecided", "undecided", "a", 3940.0))
        self.assertIn("对不上", out2["adjudication"]["applied"])
        self.assertEqual(out2["adjudication"]["candidate"]["area_m2"], 4100.0)

    def test_a_drawing_number_that_matches_decides_the_state(self) -> None:
        a = _read("wall", "F2", RECT, 4000.0, 100.0, 40.0)
        b = _read("structure", "F1", NOTCHED, 3940.0, 100.0, 40.0)
        fake = {"answer": {"decision": "adopt_a", "why": "图上写的 4000 与甲一致", "evidence": ["texts → 建筑面积4000平方米"]}, "minutes": 1, "tokens_used": 1, "tool_calls": 1, "model": "fake", "returncode": 0}
        with mock.patch.object(J.agent_reader, "verify", _fake_verify_with_stated(4000.0)), mock.patch.object(J.agent_reader, "run_prompt", return_value=fake):
            out = J.adjudicate_pair(self.work, a, b)
        s = out["result_status"]
        self.assertEqual((s["status_code"], s["source"]), ("matches_drawing", "a"))
        self.assertEqual(s["drawing_matches"][0]["drawing_value"], 4000.0)
        self.assertEqual(s["undecided_numbers"], [])

    def test_no_valid_read_is_a_failed_state_and_an_invalid_second_read_cannot_be_adopted(self) -> None:
        out = J.adjudicate_pair(self.work, {"answer": {}, "valid": False}, {"answer": {}, "valid": False})
        self.assertEqual((out["result_status"]["status_code"], out["adjudication"]["status"]), ("failed", "no_building"))
        a = _read("wall", "F1", NOTCHED, 3940.0, 100.0, 40.0)
        b = dict(_read("structure", "F2", RECT, 4000.0, 100.0, 40.0, risk=[]), valid=False)  # B did not answer the checklist: not a second opinion
        with mock.patch.object(J.agent_reader, "run_prompt", side_effect=AssertionError("no call expected")):
            out = J.adjudicate_pair(self.work, a, b)
        s = out["result_status"]
        self.assertEqual((s["status_code"], s["b_valid"], s["source"]), ("undecided", False, "a"))
        self.assertEqual(J.validate_decision({"decision": "adopt_b"}, [], True, False)["decision"], "undecided")
        self.assertIn("不在四种之内", J.validate_decision({"decision": "maybe"}, [], True, True)["why"])

    def test_the_result_state_reaches_the_result_page_and_a_human_edit_leaves_it(self) -> None:
        read = _read("wall", "F2", RECT, 4000.0, 100.0, 40.0)
        read["result_status"] = {"status_code": "two_methods_agree", "status_zh": "两种方法一致", "source": "a", "source_zh": "甲", "decision": "adopt_a", "why": "一致",
                                 "a": {"area_m2": 4000}, "b": {"area_m2": 4010}, "a_method": "wall", "b_method": "structure", "risk_review": RISK, "undecided_numbers": []}
        issued = {"labels": {}, "entities": {}}
        untouched = {"entities": {}, "closed_outlines": [], "open_outlines": [], "human_columns": [], "columns": []}
        result = C.apply(read, [], issued, untouched, 1.0)
        b = result["buildings"][0]
        self.assertEqual((b["status_code"], b["source"], b["human_review"], b["methods"]["b_method"]), ("two_methods_agree", "a", "可选", "structure"))
        redrawn = dict(untouched, closed_outlines=[{"handle": "H1", "layer": C.LAYER_HUMAN_OUTLINE, "points": TALLER}])
        result = C.apply(read, [], issued, redrawn, 1.0)
        b = result["buildings"][0]
        self.assertEqual((b["status_code"], b["source"], b["area_m2"]), ("human_edited", "human", 4100.0))


class ExtraVotesTest(unittest.TestCase):
    def test_majority_needs_more_than_half_of_all_votes(self) -> None:
        self.assertEqual(E._majority(["RACK", "RACK", "UNSURE"]), ("RACK", 2))
        self.assertEqual(E._majority(["RACK", "UNSURE", "COMPONENT"])[0], None)
        self.assertEqual(E._majority(["RACK", "UNSURE", "COMPONENT", "RACK", "RACK"]), ("RACK", 3))
        self.assertEqual(E._majority(["RACK", "UNSURE", "COMPONENT", "RACK", "UNSURE"])[0], None)

    def test_parse_keeps_wrong_if(self) -> None:
        parsed = E.parse_tiles('{"tiles": [{"id": "T1", "type": "RACK", "reason": "rows", "wrong_if": "painted floor positions"}]}')
        self.assertEqual(parsed["T1"]["wrong_if"], "painted floor positions")

    def test_a_high_count_family_without_a_majority_gets_extra_votes_with_the_earlier_reasons(self) -> None:
        occurrences = [{"ref": f"o{i}", "bbox": [i * 1200.0, 0.0, i * 1200.0 + 1000.0, 500.0], "footprint": [[i * 1200.0, 0.0], [i * 1200.0 + 1000.0, 0.0], [i * 1200.0 + 1000.0, 500.0], [i * 1200.0, 500.0]],
                        "parent_ref": None, "view_id": "V1"} for i in range(60)]
        fam = {"family": "BAY", "size_mm": (1000.0, 500.0), "occurrences": occurrences, "parents": Counter(), "children": Counter(), "views": {"V1"}, "texts_inside": []}
        answers = iter([("RACK", "rows"), ("UNSURE", "plain box"), ("COMPONENT", "a beam"), ("RACK", "the row and pitch say racking"), ("RACK", "agree with the first")])
        prompts = []

        def transport(prompt, image, call_dir):
            t, r = next(answers)
            prompts.append(prompt)
            return {"text": json.dumps({"tiles": [{"id": "T1", "type": t, "reason": r, "wrong_if": "if the boxes are floor markings"}]}), "provider": "fake", "model": "fake"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(E, "render_sheet", return_value="sha"):
            arrays = [{"family": "BAY", "count": 60, "pitch_mm": 1200.0, "along": "length"}]
            out = E.judge_shapes(None, {"BAY": fam}, ["BAY"], [], 1.0, Path(tmp), transport, True, arrays=arrays)
        self.assertEqual(len(prompts), E.SPLIT_JUDGE_REPEATS + 2)
        self.assertIn("arrangement: 60 identical copies", prompts[0])
        self.assertNotIn("earlier judgements: RACK", prompts[2])
        self.assertIn("earlier judgements: RACK (rows) | UNSURE (plain box) | COMPONENT (a beam)", prompts[3])
        j = out["BAY"]
        self.assertEqual((j["status"], j["type"], len(j["votes"])), ("agreed", "RACK", 5))
        self.assertEqual(j["wrong_if"][0], "if the boxes are floor markings")

    def test_a_small_family_is_not_asked_again(self) -> None:
        fam = {"family": "ONE", "size_mm": (1000.0, 500.0), "occurrences": [{"ref": "o1", "bbox": [0.0, 0.0, 1000.0, 500.0], "footprint": [], "parent_ref": None, "view_id": "V1"}],
               "parents": Counter(), "children": Counter(), "views": {"V1"}, "texts_inside": []}
        answers = iter(["RACK", "UNSURE", "COMPONENT"])
        calls = []

        def transport(prompt, image, call_dir):
            calls.append(1)
            return {"text": json.dumps({"tiles": [{"id": "T1", "type": next(answers), "reason": "x"}]}), "provider": "fake", "model": "fake"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(E, "render_sheet", return_value="sha"):
            out = E.judge_shapes(None, {"ONE": fam}, ["ONE"], [], 1.0, Path(tmp), transport, True)
        self.assertEqual((len(calls), out["ONE"]["status"]), (3, "disagreed"))


class ResultFileTest(unittest.TestCase):
    def test_a_failed_run_writes_the_reason_and_no_numbers(self) -> None:
        from scripts.trial_package import result_json

        with tempfile.TemporaryDirectory() as tmp:
            path = result_json(Path(tmp), Path("x.dwg"), "坐标 1, 2", [], None, failed="AI 调用全部失败")
            body = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((body["status_code"], body["building"], body["why"]), ("failed", None, "AI 调用全部失败"))
        self.assertEqual(body["ai_calls"], [])

    def test_the_drawing_result_lists_equipment_kinds_and_counts_claude_code_judge_calls(self) -> None:
        from scripts.read_drawing import write_result

        with tempfile.TemporaryDirectory() as tmp:
            eq = Path(tmp) / "equipment"
            (eq / "review").mkdir(parents=True)
            (eq / "equipment.json").write_text(json.dumps({"records": [1, 2, 3], "stage_counts": {"rules": 1, "llm_agreed": 1}}), encoding="utf-8")
            (eq / "review" / "review_items.json").write_text(json.dumps({"made_at": "t", "items": [
                {"id": "E1", "type": "RACK", "type_zh": "货架", "name": "x", "count": 5, "basis": "llm", "wrong_if": "若是地面划线则不是货架"}]}, ensure_ascii=False), encoding="utf-8")
            ask = eq / "equipment" / "type_judge" / "sheet_001" / "ask_1"
            ask.mkdir(parents=True)
            ask.joinpath("events.jsonl").write_text("\n".join([
                json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": []}}),
                json.dumps({"type": "result", "duration_ms": 6000, "total_cost_usd": 0.07, "is_error": False,
                            "usage": {"input_tokens": 4, "output_tokens": 500, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2000}})]), encoding="utf-8")
            body = json.loads(write_result(Path(tmp), Path("x.dwg"), None, None, eq).read_text(encoding="utf-8"))
        self.assertEqual(body["equipment"]["records"], 3)
        self.assertEqual([(k["id"], k["count"], k["wrong_if"]) for k in body["equipment"]["kinds"]], [("E1", 5, "若是地面划线则不是货架")])
        self.assertEqual([(c["part"], c["step"], c["model"], c["tokens_used"], c["cost_usd_equivalent"]) for c in body["ai_calls"]],
                         [("设备", "判官", "claude-sonnet-5-5", 3504, 0.07)])
        self.assertEqual(body["cost_usd_equivalent"], 0.07)


if __name__ == "__main__":
    unittest.main()
