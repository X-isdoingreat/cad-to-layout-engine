from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cad_to_layout.zero_manual import split as S
from cad_to_layout.zero_manual.llm_judge import judge_split, replay_judge


def _grid(cols: int, rows: int, *, size: float = 10000.0, gap: float = 30000.0, n: int = 12) -> tuple[list[tuple[str, list[float]]], list[float]]:
    """cols x rows plans, each a cloud of n small boxes."""

    boxes: list[tuple[str, list[float]]] = []
    for c in range(cols):
        for r in range(rows):
            x0, y0 = c * (size + gap), r * (size + gap)
            for k in range(n):  # dense: boxes touch, so no gap inside a plan
                x = x0 + (k % 4) * 500
                y = y0 + (k // 4) * 500
                boxes.append((f"I:{c}{r}{k}", [x, y, x + 500, y + 500]))
    xs = [b[0] for _, b in boxes] + [b[2] for _, b in boxes]
    ys = [b[1] for _, b in boxes] + [b[3] for _, b in boxes]
    return boxes, [min(xs), min(ys), max(xs), max(ys)]


class CandidateCutTests(unittest.TestCase):
    def test_two_by_two_gives_one_cut_per_axis(self) -> None:
        boxes, window = _grid(2, 2)
        cuts = S.candidate_cuts(boxes, window)
        self.assertEqual([c["axis"] for c in cuts], ["x", "y"])
        pieces = S.apply_cuts(boxes, window, cuts, [1, 2])
        self.assertEqual(len(pieces), 4)
        self.assertEqual({p["member_count"] for p in pieces}, {12})

    def test_container_spanning_the_region_does_not_block_cuts(self) -> None:
        boxes, window = _grid(2, 1)
        boxes.append(("I:container", [window[0] - 10, window[1] - 10, window[2] + 10, window[3] + 10]))
        cuts = S.candidate_cuts(boxes, window)
        self.assertEqual(len(cuts), 1)

    def test_parallel_strips_between_same_groups_collapse_to_the_widest(self) -> None:
        boxes, window = _grid(2, 1, gap=30000.0)
        # a lone object inside the gap splits it into two strips that separate the same groups? no:
        # it lies between them, so the two strips are distinct (object centre between). Check that.
        boxes.append(("I:lone", [2000 + 15000, 0, 2000 + 15500, 500]))
        cuts = S.candidate_cuts(boxes, window)
        self.assertEqual(len(cuts), 2)
        # but a lone object off to the side does not create a distinct cut
        boxes2, window2 = _grid(2, 1, gap=30000.0)
        cuts2 = S.candidate_cuts(boxes2, window2)
        self.assertEqual(len(cuts2), 1)

    def test_cut_through_a_drawing_is_refused_by_piece_check(self) -> None:
        boxes, window = _grid(1, 1, n=8)
        # force a fake cut in the middle
        cuts = [{"id": 1, "axis": "x", "at": 1000.0, "from": 900.0, "to": 1100.0, "width": 200.0}]
        self.assertIsNone(S.apply_cuts(boxes, window, cuts, [1]))

    def test_unknown_cut_id_is_refused(self) -> None:
        boxes, window = _grid(2, 1)
        cuts = S.candidate_cuts(boxes, window)
        self.assertIsNone(S.apply_cuts(boxes, window, cuts, [99]))


class ParseTests(unittest.TestCase):
    def test_parse_answer(self) -> None:
        self.assertEqual(S.parse_answer('x {"drawing_count": 4, "cut_ids": [7, 2, 2], "reason": "r"} y'), {"drawing_count": 4, "cut_ids": [2, 7], "reason": "r"})
        self.assertIsNone(S.parse_answer("no json"))
        self.assertIsNone(S.parse_answer('{"drawing_count": "4", "cut_ids": []}'))


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "thumb.png"
        self.image.write_bytes(b"\x89PNG fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _transport(self, answers: list[str]):
        calls: list[str] = []

        def call(prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
            calls.append(prompt)
            return {"text": answers[len(calls) - 1], "returncode": 0, "provider": "fake", "model": "fake-1"}

        call.calls = calls  # type: ignore[attr-defined]
        return call

    def test_agreeing_calls_are_adopted_and_replayable(self) -> None:
        transport = self._transport(['{"drawing_count": 4, "cut_ids": [2, 7]}', 'garbage', '{"drawing_count": 4, "cut_ids": [7, 2]}'])
        result = judge_split(image=self.image, work_dir=self.root / "j", transport=transport, allow_external=True)
        self.assertEqual(result.status, "agreed")
        self.assertEqual(result.answer["cut_ids"], [2, 7])
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(all(S.PROMPT_TEMPLATE == p for p in transport.calls), "prompt is the fixed template")
        replay = replay_judge(self.root / "j" / "judge_receipt.json", self.image)
        self.assertEqual(replay.status, "agreed")
        self.assertEqual(replay.answer["cut_ids"], [2, 7])
        self.image.write_bytes(b"changed")
        self.assertEqual(replay_judge(self.root / "j" / "judge_receipt.json", self.image).status, "invalid")

    def test_disagreeing_calls_give_no_answer(self) -> None:
        transport = self._transport(['{"drawing_count": 2, "cut_ids": [4]}', '{"drawing_count": 4, "cut_ids": [2, 7]}', '{"drawing_count": 3, "cut_ids": [7]}'])
        result = judge_split(image=self.image, work_dir=self.root / "j", transport=transport, allow_external=True)
        self.assertEqual(result.status, "disagreed")
        self.assertIsNone(result.answer)

    def test_external_llm_off_by_default_makes_no_call(self) -> None:
        transport = self._transport(['{"drawing_count": 4, "cut_ids": [2, 7]}'] * 3)
        result = judge_split(image=self.image, work_dir=self.root / "j", transport=transport, allow_external=False)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(transport.calls, [])
        receipt = json.loads((self.root / "j" / "judge_receipt.json").read_text(encoding="utf-8"))
        self.assertFalse(receipt["external_data_egress"])


if __name__ == "__main__":
    unittest.main()


class InputQualityTests(unittest.TestCase):
    def test_text_only_piece_is_refused(self) -> None:
        boxes, window = _grid(1, 1)
        notes = [(f"T:n{i}", [30000.0, i * 300.0, 30000.0, i * 300.0]) for i in range(20)]
        region = [*boxes, *notes]
        window = [window[0], window[1], 30001.0, window[3]]
        cuts = S.candidate_cuts(region, window)
        self.assertEqual(cuts, [], "texts neither block nor create cut lines")
        fake = [{"id": 1, "axis": "x", "at": 20000.0, "from": 19000.0, "to": 21000.0, "width": 2000.0}]
        self.assertIsNone(S.apply_cuts(region, window, fake, [1]))

    def test_facts_list_lines_and_titles(self) -> None:
        boxes, window = _grid(2, 1)
        cuts = S.candidate_cuts(boxes, window)
        texts = [{"text": "一层平面图", "xy": [window[0], window[3]], "height": 500.0}]
        facts = S.region_facts(boxes, window, cuts, texts)
        self.assertIn("line 1: vertical", facts)
        self.assertIn("12 drawing objects left of it, 12 right of it", facts)
        self.assertIn('"一层平面图"', facts)
