"""The one zero-manual reading command (stages P1 so far): convert -> inventory -> units -> views -> split.

Every stage writes its result next to a run manifest with hashes so a later run
can be compared or replayed.  No stage takes a drawing-specific option.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from .inventory import INVENTORY_SCHEMA, build_inventory, write_inventory
from .llm_judge import Transport, external_transport
from .units import infer_unit
from .views import segment_views
from .views_split import split_regions
from .equipment import equipment_markdown, recognise
from .warehouse_geometry import collect_structure, measure_views, summary_markdown

RUN_SCHEMA = "ctle-zero-manual-run-1.0"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def convert_to_dxf(source: Path, out_dir: Path) -> tuple[Path, dict[str, Any]]:
    """DWG -> ASCII DXF through the ODA File Converter; a DXF input is copied through."""

    out_dir.mkdir(parents=True, exist_ok=True)
    if source.suffix.casefold() == ".dxf":
        target = out_dir / source.name
        if not target.exists():
            shutil.copyfile(source, target)
        return target, {"converter": "none (DXF input)"}
    from cad_to_layout.oda_complex_reader import OdaConverterConfig, discover_oda_file_converter
    import subprocess

    converter = discover_oda_file_converter()
    if converter is None:
        raise RuntimeError("ODA File Converter not found; cannot read DWG")
    staging = out_dir / "oda_in"
    staging.mkdir(exist_ok=True)
    staged = staging / "source.dwg"
    shutil.copyfile(source, staged)
    config = OdaConverterConfig(executable=converter, output_version="ACAD2018", output_type="DXF", recursive=False, audit=False)
    completed = subprocess.run(config.command(staged, out_dir), capture_output=True, text=True, timeout=3600)
    candidates = sorted(out_dir.glob("*.dxf"))
    if completed.returncode != 0 or len(candidates) != 1:
        raise RuntimeError(f"ODA conversion failed: rc={completed.returncode} dxf={len(candidates)}")
    return candidates[0], {"converter": str(converter), "returncode": completed.returncode}


def run_read(source: str | Path, out_dir: str | Path, *, allow_external_llm: bool = False,
             transport: Transport | None = None, replay_dir: str | Path | None = None,
             inventory_cache: str | Path | None = None, replay_types: str | Path | None = None) -> dict[str, Any]:
    source = Path(source).resolve(strict=True)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    manifest: dict[str, Any] = {
        "schema": RUN_SCHEMA, "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "source": {"path": str(source), "sha256": _sha(source), "size_bytes": source.stat().st_size},
        "machine": platform.node(), "allow_external_llm": bool(allow_external_llm), "stages": {},
    }
    dxf, conversion = convert_to_dxf(source, out / "convert")
    manifest["stages"]["convert"] = {**conversion, "dxf": str(dxf), "dxf_sha256": _sha(dxf)}

    cached = Path(inventory_cache) if inventory_cache else None
    if cached is not None and cached.is_file():
        inventory = json.loads(cached.read_text(encoding="utf-8"))
        if inventory.get("source", {}).get("dxf_sha256") != manifest["stages"]["convert"]["dxf_sha256"]:
            raise RuntimeError("inventory cache belongs to a different DXF")
        inventory_sha = _sha(cached)
        shutil.copyfile(cached, out / "inventory.json")
    else:
        inventory = build_inventory(dxf)
        inventory_sha = write_inventory(inventory, out / "inventory.json")
    manifest["stages"]["inventory"] = {"schema": INVENTORY_SCHEMA, "sha256": inventory_sha,
                                       "counts": {k: len(v) for k, v in inventory.items() if isinstance(v, list)}}

    unit = infer_unit(inventory)
    manifest["stages"]["units"] = {"sha256": _json(out / "units.json", unit), **{k: unit[k] for k in ("unit", "status")}}

    segmented = segment_views(inventory)
    manifest["stages"]["views"] = {"sha256": _json(out / "views_segmented.json", segmented), "count": len(segmented["views"])}

    judge = transport if transport is not None else (external_transport() if allow_external_llm else None)
    final = split_regions(inventory, segmented, work_dir=out / "split", transport=judge, allow_external=allow_external_llm,
                          replay_dir=Path(replay_dir) if replay_dir else None)
    views_out = {"views": final["views"], "split_receipts": final["split_receipts"], "method": final["method"]}
    manifest["stages"]["split"] = {"sha256": _json(out / "views.json", views_out), "count": len(final["views"]),
                                   "statuses": sorted({r["status"] for r in final["split_receipts"]})}
    import ezdxf

    doc = ezdxf.readfile(dxf)
    structure = collect_structure(doc)
    geometry = measure_views(structure, final["views"], unit, inventory.get("dimensions", []))
    manifest["stages"]["warehouse_geometry"] = {
        "sha256": _json(out / "warehouse_geometry.json", {"unit": unit, "structure_layers": structure.layers, "views": geometry}),
        "views_with_geometry": sum(1 for g in geometry if g.get("status") not in {"no_building_geometry", "unit_unknown"}),
        "statuses": sorted({g.get("status") for g in geometry}),
    }
    (out / "warehouse_geometry.md").write_text(summary_markdown(geometry, unit), encoding="utf-8")

    equipment = recognise(inventory, final["views"], unit, doc, out / "equipment", judge, allow_external_llm,
                          Path(replay_types) if replay_types else None, opened=segmented.get("opened_containers"), dxf=dxf)
    manifest["stages"]["equipment"] = {
        "sha256": _json(out / "equipment.json", equipment),
        "stage_counts": equipment.get("stage_counts"),
        "records": len(equipment.get("records", [])),
        "unrecognised_families": len(equipment.get("unrecognised", [])),
    }
    (out / "equipment.md").write_text(equipment_markdown(equipment, final["views"]), encoding="utf-8")
    manifest["elapsed_seconds"] = round(time.time() - started, 1)
    _json(out / "run_manifest.json", manifest)
    return manifest


__all__ = ["run_read", "convert_to_dxf"]
