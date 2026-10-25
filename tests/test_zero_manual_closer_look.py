"""The closer look (2026-10-25): the first look votes twice; what the votes cannot settle, every kind of row and every kind
with many copies goes to an agent with the drawing tools, which decides after looking.  The agent is a stand-in here; the
block-level tools are run for real on the synthetic drawing."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf

from cad_to_layout.zero_manual import agent_tools, closer_look
from cad_to_layout.zero_manual.equipment import equipment_markdown, recognise
from cad_to_layout.zero_manual.inventory import build_inventory
from tests.test_zero_manual_rows import UNIT, VIEWS, Judge, _one_kind_two_blocks


class CountingJudge(Judge):
    def __init__(self, answers: dict[str, str]) -> None:
        super().__init__(answers)
        self.calls = 0

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        self.calls += 1
        return super().__call__(prompt, image, work_dir)


class FakeLooker:
    """Stands in for the agent: records what it was asked, answers from a table (None = the look fails)."""

    def __init__(self, answers: dict[str, dict[str, Any] | None]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.prompts: dict[str, str] = {}

    def __call__(self, work: Path, key: str, prompt: str) -> dict[str, Any]:
        self.asked.append(key)
        self.prompts[key] = prompt
        answer = self.answers.get(key)
        return {"answer": answer, "tool_calls": 4, "minutes": 1.5, "cost_usd_equivalent": 0.2}


def _look(kind: str, seen: int = 2, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "name": "", "reason": f"fake reason for {kind}", "wrong_if": "fake wrong_if",
            "looked_at": [{"command": f"cmd {i}", "seen": f"seen {i}"} for i in range(seen)], **extra}


class CloserLookCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dxf = self.root / "d.dxf"
        _one_kind_two_blocks().saveas(self.dxf)
        self.inventory = build_inventory(self.dxf)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _recognise(self, answers: dict[str, str], looker: Any, work: str = "w", replay: Path | None = None) -> dict[str, Any]:
        self.judge = CountingJudge(answers)
        return recognise(self.inventory, VIEWS, UNIT, ezdxf.readfile(self.dxf), self.root / work, self.judge, True, replay=replay,
                         dxf=self.dxf, looker=looker)


ANSWERS = {"row": "RACK", "pair": "RACK", "row_c": "UNSURE", "row_d": "RACK"}


class CloserLookTests(CloserLookCase):
    def test_two_votes_then_the_unsettled_and_the_heavy_kinds_get_a_closer_look(self) -> None:
        looker = FakeLooker({"row": _look("RACK"), "row_c": _look("BOX_CONVEYOR", 3), "row_d": _look("UNSURE", what_would_settle_it="a section")})
        result = self._recognise(ANSWERS, looker)
        self.assertEqual(self.judge.calls, 2, "one sheet of tiles, asked twice")
        self.assertEqual(looker.asked, ["row_c", "row", "row_d"], "the unsure kind first, then the row leaders (whatever the votes) by copies; pair is neither")
        fams = result["families"]
        d = fams["row"]["decision"]
        self.assertEqual((d["type"], d["basis"], d["stage"], d["why"], d["agrees_with_first_look"]), ("RACK", "agent_look", "agent", "row", True))
        self.assertEqual(d["first_look"]["votes"], ["RACK", "RACK"])
        self.assertEqual((d["tool_calls"], d["cost_usd"], len(d["looked_at"])), (4, 0.2, 2))
        c = fams["row_c"]["decision"]
        self.assertEqual((c["type"], c["why"], c["agrees_with_first_look"], c["first_look"]["type"]), ("BOX_CONVEYOR", "first_look_unsure", False, "UNRECOGNISED"))
        e = fams["row_d"]["decision"]
        self.assertEqual((e["type"], e["reason"], e["what_would_settle_it"]), ("UNRECOGNISED", "agent_unsure", "a section"))
        self.assertEqual((fams["row_b"]["decision"]["basis"], fams["row_b"]["decision"]["via"]), ("row_kind", "agent_look"), "the follower row takes the agent's answer")
        self.assertEqual((fams["cell"]["decision"]["type"], fams["cell"]["decision"]["via"]), ("RACK", "agent_look"))
        self.assertEqual(Counter((r["family"], r["type"], r["basis"]) for r in result["records"] if r["drawn_as"] == "row"),
                         {("row", "RACK", "agent_look"): 7, ("row_b", "RACK", "agent_look"): 4, ("row_c", "BOX_CONVEYOR", "agent_look"): 3})
        self.assertEqual(result["summary"]["V:all"]["RACK"]["llm_shape"], 16, "the agent's decisions count as AI decisions: 11 rows + 5 loose cells")
        self.assertEqual({k: v for k, v in result["stage_counts"].items() if k.startswith("closer_look")},
                         {"closer_look": 3, "closer_look_agreed_first": 1, "closer_look_changed": 1, "closer_look_unsure": 1})
        self.assertIn("细看（带工具的 AI 自己查图）：3 种，其中与第一遍一致 1、改判 1、仍拿不准 1", equipment_markdown(result, VIEWS))
        prompt = looker.prompts["row_c"]
        self.assertIn("查一种图块：**row_c**", prompt)
        self.assertIn("第 1 次 UNSURE（fake）；第 2 次 UNSURE（fake）", prompt)
        self.assertIn("agent_tools near 图块名", prompt)

    def test_the_state_file_holds_the_programs_whole_knowledge_and_the_archive_replays(self) -> None:
        looker = FakeLooker({"row": _look("RACK"), "row_c": _look("RACK"), "row_d": _look("RACK")})
        self._recognise(ANSWERS, looker)
        work = self.root / "w" / "closer_look"
        state = json.loads((work / closer_look.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["metres_per_unit"], 0.001)
        self.assertEqual(state["families"]["row_c"]["first_look"]["votes"], ["UNSURE", "UNSURE"])
        self.assertEqual((state["families"]["row"]["row"]["cells_per_row"], state["families"]["row"]["row"]["also_drawn_by"]), (20, ["row_b"]))
        self.assertEqual(state["families"]["row_b"]["follows"], "row")
        self.assertEqual(len(state["families"]["row_c"]["occurrences"]), 3)
        archive = json.loads((work / closer_look.ARCHIVE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(set(archive), {"row", "row_c", "row_d"})
        self.assertEqual(archive["row_c"]["prompt_sha256"], closer_look.prompt_sha256())

        class NeverCalled:
            def __call__(self, work: Path, key: str, prompt: str) -> dict[str, Any]:
                raise AssertionError(f"the archive should have answered {key}")

        again = self._recognise(ANSWERS, NeverCalled(), work="w2", replay=self.root / "w" / "type_judge" / "type_judge_archive.json")
        self.assertEqual(again["families"]["row_c"]["decision"]["type"], "RACK")
        self.assertEqual(again["stage_counts"]["closer_look"], 3)

    def test_a_failed_look_keeps_the_first_looks_decision(self) -> None:
        looker = FakeLooker({"row": _look("RACK"), "row_c": None, "row_d": {"type": "NOT_A_TYPE"}})
        result = self._recognise(ANSWERS, looker)
        c = result["families"]["row_c"]["decision"]
        self.assertEqual((c["type"], c["basis"], c["reason"], c["closer_look_error"]), ("UNRECOGNISED", "llm_shape", "judge_unsure", "no answer"))
        e = result["families"]["row_d"]["decision"]
        self.assertEqual((e["basis"], e["closer_look_error"]), ("llm_shape", "answer type 'NOT_A_TYPE' is not a known type"), "an unknown type from the agent changes nothing")
        self.assertEqual(result["stage_counts"]["closer_look_failed"], 2)

    def test_without_an_agent_backend_the_look_is_counted_as_unavailable(self) -> None:
        self.judge = CountingJudge(ANSWERS)
        result = recognise(self.inventory, VIEWS, UNIT, ezdxf.readfile(self.dxf), self.root / "w", self.judge, False, dxf=self.dxf)
        self.assertNotIn("closer_look", result["stage_counts"])


class BlockToolTests(CloserLookCase):
    """The block-level tools, run as the agent runs them, on the state the program wrote."""

    def setUp(self) -> None:
        super().setUp()
        self._recognise(ANSWERS, FakeLooker({"row": _look("RACK"), "row_c": _look("RACK"), "row_d": _look("RACK")}))
        self.work = self.root / "w" / "closer_look"
        self.cwd = os.getcwd()
        os.chdir(self.work)
        agent_tools.build_index(str(self.dxf))

    def tearDown(self) -> None:
        agent_tools.close_all()
        os.chdir(self.cwd)
        super().tearDown()

    def _run(self, *argv: str) -> dict[str, Any]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            agent_tools.main(list(argv))
        return json.loads(out.getvalue())

    def test_block_near_compare_and_render_block(self) -> None:
        block = self._run("block", "row_c")
        self.assertEqual((block["copies"], block["row"]["cells_per_row"], block["row"]["pitch_mm"], block["first_look"]["votes"]), (3, 20, 550.0, ["UNSURE", "UNSURE"]))
        near = self._run("near", "row_c", "--radius-m", "20")
        by_name = {r["block"]: r for r in near["families"]}
        self.assertIn("row_d", by_name)
        self.assertAlmostEqual(by_name["row_d"]["nearest_gap_m"], 8.0, places=1, msg="row_c ends at y 41600+400, row_d starts at 50000")
        self.assertEqual(by_name["cell"]["placement"], "inside it")
        self.assertEqual(by_name["row_d"]["first_look"]["type"], "RACK")
        self.assertNotIn("pair", by_name, "the pairs stand more than 20 m away")
        comp = self._run("compare", "row", "row_c")
        self.assertEqual((comp["a"]["row"]["pitch_mm"], comp["b"]["row"]["pitch_mm"]), (500.0, 550.0))
        self.assertEqual(comp["same_structure"], {"cells_per_row": True, "pitch": False, "depth_within_25_percent": True})
        pic = self._run("render_block", "row_c", "look_a.png", "--blocks", "row_d", "--margin-m", "12")
        self.assertTrue((self.work / "look_a.png").is_file())
        self.assertEqual(pic["blocks_in_window"]["row_c"], 3)
        self.assertGreaterEqual(pic["blocks_in_window"]["row_d"], 1)
        with self.assertRaises(SystemExit):
            self._run("render_block", "row_c", "../outside.png")
        listed = self._run("blocks", "--min-copies", "3")
        self.assertEqual([b["block"] for b in listed["blocks"]][:2], ["cell", "beam"])


if __name__ == "__main__":
    unittest.main()
