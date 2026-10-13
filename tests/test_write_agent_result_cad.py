"""Result layers: what an agent read puts into CAD (no AutoCAD needed)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ezdxf

from scripts.write_agent_result_cad import result_entities, write_result_dxf

SQUARE = [[0, 0], [60000, 0], [60000, 24000], [0, 24000], [0, 0]]
HALF = [[0, 0], [30000, 0], [30000, 24000], [0, 24000], [0, 0]]


class ResultLayersTest(unittest.TestCase):
    def test_layers_follow_the_program_checked_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "faces.json").write_text(json.dumps({"F1": {"outline": SQUARE}, "F2": {"outline": HALF, "area_m2": 720.0, "centroid": [15000, 12000]}}), encoding="utf-8")
            (work / "columns.json").write_text(json.dumps({"F1": {"centres": [[0, 0], [6000, 0], [90000, 0]]}}), encoding="utf-8")
            read = {"answer": {"unit_m": 0.001,
                               "buildings": [{"name": "B", "faces": ["F9"]}, {"name": "A", "designated": True, "faces": ["F1"], "columns": {"accepted_to_check": [[12000, 0]]}}],
                               "zones": [{"name": "存储区", "face": "F2", "category": "storage"}, {"name": "生活区", "face": None, "category": "office_living"}]},
                    "checks": [{"name": "A", "outline": SQUARE, "remeasured": {"area_m2": 1440, "length_m": 60, "width_m": 24}}, {"name": "B", "status": "cannot_verify"}]}
            got = result_entities(work, read)
            self.assertEqual(got[0]["record"]["building"], "A")  # designated first; B was not measured by the program, so not drawn
            self.assertEqual(sum(1 for e in got if e["layer"] == "CTLE_BUILDING"), 1)
            self.assertEqual(sum(1 for e in got if e["layer"] == "CTLE_COLUMN"), 3)  # 2 inside + 1 accepted; the one 30 m away is not
            zones = [e for e in got if e["layer"] == "CTLE_ZONE_STORAGE"]
            self.assertEqual(len(zones), 2)  # outline + label; the zone with no boundary is not drawn
            doc = ezdxf.new()
            doc.saveas(work / "src.dxf")
            manifest = write_result_dxf(work / "src.dxf", got, work / "out.dxf")
            out = ezdxf.readfile(work / "out.dxf")
            self.assertEqual(len(out.modelspace().query('LWPOLYLINE[layer=="CTLE_BUILDING"]')), 1)
            self.assertEqual(len(manifest), len(got))


class StabilityTest(unittest.TestCase):
    def test_disagreeing_runs_are_marked_uncertain(self) -> None:
        from scripts.run_agent_reads import compare

        def read(area: float, columns: int, total: float) -> dict:
            return {"answer": {"buildings": [{"name": "A", "designated": True, "columns": {"count": columns}}]},
                    "checks": [{"name": "A", "outline": SQUARE, "remeasured": {"area_m2": area, "length_m": 60, "width_m": 24}}],
                    "zone_check": {"recomputed_totals": {"1F": {"storage_and_operation_m2": total}}}}

        got = compare([read(1440, 10, 100), read(1441, 10, 100), read(1440, 12, 130)])
        building = got["buildings"][0]
        self.assertFalse(building["area_m2"]["uncertain"])  # within 1 %
        self.assertTrue(building["columns"]["uncertain"])
        self.assertTrue(got["zone_totals"][0]["storage_and_operation_m2"]["uncertain"])
        self.assertEqual(got["uncertain_count"], 2)


if __name__ == "__main__":
    unittest.main()
