from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cad_to_layout.zero_manual.pipeline import run_read
from tests.test_zero_manual_views_split import _four_plans


class FakeTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        self.calls += 1
        cuts = json.loads((work_dir.parent / "candidate_cuts.json").read_text(encoding="utf-8"))
        return {"text": json.dumps({"drawing_count": 4, "cut_ids": [c["id"] for c in cuts]}), "returncode": 0, "provider": "fake", "model": "fake"}


class PipelineTests(unittest.TestCase):
    def test_read_writes_manifest_and_replays_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _four_plans(root / "d.dxf")
            transport = FakeTransport()
            manifest = run_read(root / "d.dxf", root / "run1", allow_external_llm=True, transport=transport)
            self.assertEqual(manifest["stages"]["split"]["count"], 4)
            self.assertEqual(manifest["stages"]["units"]["status"], "unknown")
            self.assertEqual(transport.calls, 3)
            views = json.loads((root / "run1" / "views.json").read_text(encoding="utf-8"))
            self.assertEqual({v["source"] for v in views["views"]}, {"judged_split"})
            replay = run_read(root / "d.dxf", root / "run2", allow_external_llm=False, replay_dir=root / "run1" / "split")
            self.assertEqual(replay["stages"]["split"]["count"], 4)
            self.assertFalse(replay["allow_external_llm"])
            self.assertEqual(manifest["stages"]["inventory"]["sha256"], replay["stages"]["inventory"]["sha256"])

    def test_external_llm_off_keeps_region_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _four_plans(root / "d.dxf")
            manifest = run_read(root / "d.dxf", root / "run", allow_external_llm=False)
            self.assertEqual(manifest["stages"]["split"]["count"], 1)
            self.assertEqual(manifest["stages"]["split"]["statuses"], ["unsplit"])


if __name__ == "__main__":
    unittest.main()
