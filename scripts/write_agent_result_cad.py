"""Write an agent read into CAD: result layers on a copy of the drawing, saved as DWG by AutoCAD 2025, plus a same-window triptych.

The result file is the converted copy of the customer drawing with these layers added:

  CTLE_BUILDING        outline of every reported building (designated one first), program-measured
  CTLE_BUILDING_LABEL  name, area, length x width at the building
  CTLE_SEMI_OUTDOOR    covered but unenclosed parts listed separately (not in the building area)
  CTLE_COLUMN          a circle at every counted column (the tool's count for the building's faces + places the agent accepted)
  CTLE_ZONE_STORAGE / CTLE_ZONE_OPERATION / CTLE_ZONE_OTHER   functional areas, with name, category and area

Every added entity is listed with its source record in result_layers.json.  The triptych is rendered by
AutoCAD 2025 Core Console (/readonly, one ZOOM window): the customer drawing (a copy), the result layers
alone (read from the final DWG with the other layers frozen) and the final DWG with all layers.
The customer file is never opened by AutoCAD; its hash is checked before and after.

  py -3.11 -X utf8 -m scripts.write_agent_result_cad --work <agent work dir> --source-dxf <converted copy> \
      --customer-dwg <customer.dwg> --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import ezdxf

LAYERS = {"CTLE_BUILDING": 1, "CTLE_BUILDING_LABEL": 1, "CTLE_SEMI_OUTDOOR": 2, "CTLE_COLUMN": 5,
          "CTLE_ZONE_STORAGE": 3, "CTLE_ZONE_OPERATION": 4, "CTLE_ZONE_OTHER": 8}
ZONE_LAYER = {"storage": "CTLE_ZONE_STORAGE", "operation": "CTLE_ZONE_OPERATION"}
CATEGORY_CN = {"storage": "存储", "operation": "作业", "production": "生产", "office_living": "办公生活", "other": "其他",
               "unlabelled": "未标注", "to_confirm": "待确认"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _num(v: float) -> str:
    return f"{v:.6f}"


def _lisp_path(path: Path) -> str:
    return '"' + str(path.resolve()).replace("\\", "/") + '"'


def result_entities(work: Path, agent_read: dict[str, Any]) -> list[dict[str, Any]]:
    """What goes into the result layers, each with its record: plain data, so it can be tested without CAD."""

    answer = agent_read.get("answer") or {}
    m = float(answer.get("unit_m") or 1.0)
    faces = json.loads((work / "faces.json").read_text(encoding="utf-8")) if (work / "faces.json").is_file() else {}
    runs = json.loads((work / "columns.json").read_text(encoding="utf-8")) if (work / "columns.json").is_file() else {}
    checks = {c.get("name"): c for c in agent_read.get("checks", [])}
    out: list[dict[str, Any]] = []
    buildings = sorted(answer.get("buildings", []), key=lambda b: not b.get("designated"))
    for b in buildings:
        c = checks.get(b.get("name"), {})
        if not c.get("outline"):
            continue  # nothing measured by the program: not drawn
        size = c.get("with_decided_parts") or c.get("remeasured") or {}
        record = {"building": b.get("name"), "designated": bool(b.get("designated")), "program_status": c.get("status")}
        out.append({"layer": "CTLE_BUILDING", "kind": "outline", "points": c["outline"], "width": 0.6 / m, "record": record})
        xs = [p[0] for p in c["outline"]]
        ys = [p[1] for p in c["outline"]]
        label = (f"{b.get('name')}{'（指定）' if b.get('designated') else ''}\\P{size.get('area_m2', 0):.0f} m2\\P"
                 f"{size.get('length_m', 0):.1f} m x {size.get('width_m', 0):.1f} m")
        out.append({"layer": "CTLE_BUILDING_LABEL", "kind": "text", "at": [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2], "text": label,
                    "height": max(max(xs) - min(xs), max(ys) - min(ys)) / 60, "record": record})
        faces_used = set(b.get("faces", [])) | set(c.get("decided_parts_left_out", []))
        run = runs.get("+".join(sorted(faces_used))) or runs.get("+".join(sorted(b.get("faces", []))))
        if run:
            from shapely.geometry import Point, Polygon

            reach = Polygon(c["outline"]).buffer(1.5 / m)
            columns = [q for q in run.get("centres", []) if reach.contains(Point(q))]
            columns += [list(q) for q in ((b.get("columns") or {}).get("accepted_to_check") or [])]
            for q in columns:
                out.append({"layer": "CTLE_COLUMN", "kind": "circle", "at": q, "radius": 0.5 / m, "record": {"building": b.get("name")}})
    for s in answer.get("semi_outdoor") or []:
        ids = [f for f in s.get("faces", []) if f in faces]
        for f in ids:
            out.append({"layer": "CTLE_SEMI_OUTDOOR", "kind": "outline", "points": faces[f]["outline"], "record": {"semi_outdoor": s.get("name"), "face": f}})
    for z in answer.get("zones") or []:
        face = faces.get(z.get("face"))
        if not face:
            continue  # no closed boundary: listed in the table, not drawn
        layer = ZONE_LAYER.get(z.get("category"), "CTLE_ZONE_OTHER")
        record = {"zone": z.get("name"), "view": z.get("view"), "category": z.get("category"), "face": z.get("face"), "area_m2": face["area_m2"]}
        out.append({"layer": layer, "kind": "outline", "points": face["outline"], "record": record})
        xs = [p[0] for p in face["outline"]]
        ys = [p[1] for p in face["outline"]]
        out.append({"layer": layer, "kind": "text", "at": face.get("centroid") or [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2],
                    "text": f"{z.get('name')}\\P{CATEGORY_CN.get(z.get('category'), z.get('category'))} {face['area_m2']:.0f} m2",
                    "height": max(min(max(xs) - min(xs), max(ys) - min(ys)) / 12, 0.3 / m), "record": record})
    return out


def write_result_dxf(source_dxf: Path, entities: list[dict[str, Any]], target: Path, extra_layers: dict[str, int] | None = None) -> list[dict[str, Any]]:
    doc = ezdxf.readfile(source_dxf)
    for name, colour in {**LAYERS, **(extra_layers or {})}.items():
        if name not in doc.layers:
            doc.layers.add(name, color=colour)
    if "CTLE_TEXT" not in doc.styles:  # a TrueType font every Windows AutoCAD has: the drawing's own SHX fonts may lack Latin digits
        doc.styles.add("CTLE_TEXT", font="msyh.ttc")
    msp = doc.modelspace()
    manifest = []
    for e in entities:
        attribs = {"layer": e["layer"]}
        if e["kind"] == "outline":
            entity = msp.add_lwpolyline([tuple(p[:2]) for p in e["points"]], close=True, dxfattribs={**attribs, "const_width": e.get("width", 0)})
        elif e["kind"] == "circle":
            entity = msp.add_circle(tuple(e["at"][:2]), e["radius"], dxfattribs=attribs)
        else:
            entity = msp.add_mtext(e["text"], dxfattribs={**attribs, "char_height": e["height"], "style": "CTLE_TEXT"})
            entity.set_location(tuple(e["at"][:2]), attachment_point=5)
        manifest.append({"handle": entity.dxf.handle, "layer": e["layer"], "kind": e["kind"], "record": e["record"]})
    doc.saveas(target)
    return manifest



def save_dwg(dxf: Path, dwg: Path, timeout: float) -> dict[str, Any]:
    """Save a DXF as DWG with the machine's configured CAD runtime (cad_to_layout.cad_runtime)."""

    from cad_to_layout import cad_runtime

    return cad_runtime.dxf_to_dwg(dxf, dwg, timeout)


def render(dwg: Path, window: list[float], png: Path, timeout: float, only_result_layers: bool = False) -> dict[str, Any]:
    """A picture of one window, by the configured CAD runtime; the drawing must not change."""

    from cad_to_layout import cad_runtime

    before = _sha(dwg)
    result = cad_runtime.render(dwg, window, png, timeout, only_result_layers)
    if _sha(dwg) != before:
        raise RuntimeError(f"render session changed {dwg.name}")
    return result


def triptych(pngs: list[Path], titles: list[str], out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    images = [Image.open(p).convert("RGB") for p in pngs]
    h = max(i.size[1] for i in images)
    images = [i.resize((int(i.size[0] * h / i.size[1]), h)) for i in images]
    bar = 60
    canvas = Image.new("RGB", (sum(i.size[0] for i in images) + 20 * (len(images) - 1), h + bar), "white")
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(canvas)
    x = 0
    for image, title in zip(images, titles):
        canvas.paste(image, (x, bar))
        draw.text((x + 10, 12), title, fill="black", font=font)
        x += image.size[0] + 20
    canvas.save(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--source-dxf", required=True, help="the converted copy of the customer drawing the agent read")
    parser.add_argument("--customer-dwg", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    work, out = Path(args.work).resolve(), Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    customer = Path(args.customer_dwg).resolve()
    customer_sha = _sha(customer)
    agent_read = json.loads((work / "agent_read.json").read_text(encoding="utf-8"))
    entities = result_entities(work, agent_read)
    result_dxf, result_dwg = out / "result.dxf", out / "result.dwg"
    manifest = write_result_dxf(Path(args.source_dxf), entities, result_dxf)
    result_dwg.unlink(missing_ok=True)
    saved = save_dwg(result_dxf, result_dwg, args.timeout)
    source_copy = out / "source_copy.dwg"
    shutil.copyfile(customer, source_copy)
    os.chmod(source_copy, 0o666)
    m = float((agent_read.get("answer") or {}).get("unit_m") or 1.0)
    windows = []
    for e in entities:
        if e["layer"] == "CTLE_BUILDING":
            xs = [p[0] for p in e["points"]]
            ys = [p[1] for p in e["points"]]
            pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.08 + 5 / m
            windows.append((e["record"]["building"], [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]))
    renders = []
    for n, (name, window) in enumerate(windows[: 1 if not windows else len(windows)]):
        tag = f"b{n + 1}"
        r = [render(source_copy, window, out / f"{tag}_source.png", args.timeout),
             render(result_dwg, window, out / f"{tag}_result.png", args.timeout, only_result_layers=True),
             render(result_dwg, window, out / f"{tag}_overlay.png", args.timeout)]
        if all(x["exists"] for x in r):
            triptych([out / f"{tag}_{k}.png" for k in ("source", "result", "overlay")], ["原图（客户图副本）", "结果图层（取自成品 DWG）", "叠加（成品 DWG 全部图层）"], out / f"{tag}_triptych.png")
        renders.append({"building": name, "window": window, "renders": r, "triptych": f"{tag}_triptych.png"})
    source_copy.unlink(missing_ok=True)
    receipt = {"customer_dwg_sha256": customer_sha, "customer_dwg_unchanged": _sha(customer) == customer_sha, "result_dwg": saved,
               "result_dwg_sha256": _sha(result_dwg) if result_dwg.is_file() else None, "renderer": "AutoCAD 2025 Core Console /readonly, one ZOOM window per building",
               "entities_by_layer": {k: sum(1 for x in manifest if x["layer"] == k) for k in LAYERS}, "renders": renders}
    (out / "result_layers.json").write_text(json.dumps({"entities": manifest}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "result_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("customer_dwg_unchanged", "result_dwg", "entities_by_layer")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
