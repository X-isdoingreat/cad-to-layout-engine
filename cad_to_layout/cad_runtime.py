"""Which CAD program turns DWG into DXF, saves results as DWG and renders pictures: chosen per machine, never guessed.

The choice is written once per machine in ``cad_runtime.json`` at the repository root (or the file named by the
environment variable ``CTLE_CAD_CONFIG``):

  {"backend": "autocad", "accoreconsole": "C:/Program Files/Autodesk/AutoCAD 2024/accoreconsole.exe"}
  {"backend": "oda"}          # no AutoCAD: the free ODA File Converter converts, pictures are drawn by the program

Without that file the program stops and lists the AutoCAD installations it found; it never picks a version by itself.

  py -3.11 -X utf8 -m cad_to_layout.cad_runtime doctor     # what is installed, what is configured, a real round trip

Differences between AutoCAD years are handled here and nowhere else: the DWG format name given to SAVEAS (2018 and
later write "2018"; 2013-2017 write "2013") and the year read from the installation path.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV = "CTLE_CAD_CONFIG"
CONFIG_NAME = "cad_runtime.json"
AUTOCAD_GLOBS = ("C:/Program Files/Autodesk/AutoCAD 20*/accoreconsole.exe", "C:/Program Files/Autodesk/AutoCAD LT 20*/accoreconsole.exe")


class CadRuntimeError(RuntimeError):
    """The machine's CAD choice is missing or does not work; the message says what to do."""


@dataclass(frozen=True)
class Runtime:
    backend: str  # "autocad" | "oda"
    accoreconsole: Path | None = None
    year: int | None = None

    @property
    def label(self) -> str:
        return f"AutoCAD {self.year} ({self.accoreconsole})" if self.backend == "autocad" else "ODA File Converter + program drawing (no AutoCAD)"


def installed_autocads() -> list[dict[str, Any]]:
    found = []
    for pattern in AUTOCAD_GLOBS:
        for path in sorted(glob.glob(pattern)):
            found.append({"accoreconsole": Path(path).as_posix(), "year": _year(Path(path))})
    return found


def _year(path: Path) -> int | None:
    hit = re.search(r"AutoCAD(?: LT)? (20\d\d)", path.as_posix())
    return int(hit.group(1)) if hit else None


def _config_path() -> Path:
    return Path(os.environ[CONFIG_ENV]) if os.environ.get(CONFIG_ENV) else REPO_ROOT / CONFIG_NAME


def load() -> Runtime:
    path = _config_path()
    if not path.is_file():
        found = installed_autocads()
        listing = "\n".join(f"  {f['accoreconsole']}" for f in found) or "  (none found)"
        raise CadRuntimeError(
            f"No CAD runtime configured: create {path} with\n"
            f'  {{"backend": "autocad", "accoreconsole": "<path to accoreconsole.exe>"}}\n'
            f'or {{"backend": "oda"}} to work without AutoCAD.\nAutoCAD installations found:\n{listing}')
    config = json.loads(path.read_text(encoding="utf-8"))
    backend = config.get("backend")
    if backend == "oda":
        return Runtime("oda")
    if backend == "autocad":
        exe = Path(config.get("accoreconsole") or "")
        if not exe.is_file():
            raise CadRuntimeError(f"{path}: accoreconsole not found at {exe}")
        return Runtime("autocad", exe, _year(exe))
    raise CadRuntimeError(f"{path}: backend must be 'autocad' or 'oda', not {backend!r}")


# ---------------------------------------------------------------- AutoCAD Core Console
def _lisp_path(path: Path) -> str:
    return '"' + str(Path(path).resolve()).replace("\\", "/") + '"'


def _num(v: float) -> str:
    return f"{v:.6f}"


def run_script(rt: Runtime, drawing: Path, script: str, scr: Path, *, readonly: bool, timeout: float) -> int:
    if rt.backend != "autocad":
        raise CadRuntimeError("an AutoCAD script was asked for, but this machine is configured without AutoCAD")
    scr.write_text(script, encoding="utf-8", newline="\n")
    command = [str(rt.accoreconsole), "/i", str(drawing), "/s", str(scr)] + (["/readonly"] if readonly else [])
    return subprocess.run(command, capture_output=True, timeout=timeout, cwd=str(scr.parent)).returncode


def _dwg_format(rt: Runtime) -> str:
    return "2018" if (rt.year or 2018) >= 2018 else "2013"


# ---------------------------------------------------------------- ODA File Converter
def _oda(source: Path, out_dir: Path, version: str, kind: str, timeout: float) -> Path:
    from .oda_complex_reader import OdaConverterConfig, discover_oda_file_converter

    converter = discover_oda_file_converter()
    if converter is None:
        raise CadRuntimeError("ODA File Converter not found: install it (free) or set ODA_FILE_CONVERTER to its path")
    with tempfile.TemporaryDirectory(prefix="ctle-oda-", ignore_cleanup_errors=True) as staging:
        staged = Path(staging) / source.name
        shutil.copyfile(source, staged)
        out_dir.mkdir(parents=True, exist_ok=True)
        config = OdaConverterConfig(executable=converter, output_version=version, output_type=kind, recursive=False, audit=False)
        subprocess.run(config.command(staged, out_dir), capture_output=True, timeout=timeout)
    result = out_dir / (source.stem + "." + kind.lower())
    if not result.is_file():
        raise CadRuntimeError(f"ODA File Converter did not write {result.name}")
    return result


# ---------------------------------------------------------------- the three operations the product needs
def dwg_to_dxf(dwg: Path, dxf: Path, timeout: float = 900.0) -> Path:
    """A DXF of a DWG; the DWG itself is never written (a copy is converted)."""

    rt = load()
    dwg, dxf = Path(dwg).resolve(), Path(dxf).resolve()
    dxf.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="ctle-cad-", ignore_cleanup_errors=True) as work:
        copy = Path(work) / "source.dwg"
        shutil.copyfile(dwg, copy)
        os.chmod(copy, 0o666)
        if rt.backend == "autocad":
            run_script(rt, copy, f'(setvar "FILEDIA" 0)\n(command "_.SAVEAS" "DXF" "" {_lisp_path(dxf)})\n_.QUIT\n_Y\n',
                       Path(work) / "to_dxf.scr", readonly=False, timeout=timeout)
        else:
            shutil.move(str(_oda(copy, Path(work) / "out", "ACAD2018", "DXF", timeout)), dxf)
    if not dxf.is_file():
        raise CadRuntimeError(f"{load().label} could not save {dwg.name} as DXF")
    return dxf


def dxf_to_dwg(dxf: Path, dwg: Path, timeout: float = 900.0) -> dict[str, Any]:
    rt = load()
    dxf, dwg = Path(dxf).resolve(), Path(dwg).resolve()
    dwg.unlink(missing_ok=True)
    if rt.backend == "autocad":
        code = run_script(rt, dxf, f'(setvar "FILEDIA" 0)\n(command "_.SAVEAS" "{_dwg_format(rt)}" {_lisp_path(dwg)})\n_.QUIT\n_Y\n',
                          dwg.with_suffix(".save.scr"), readonly=False, timeout=timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="ctle-cad-", ignore_cleanup_errors=True) as work:
            shutil.move(str(_oda(dxf, Path(work) / "out", "ACAD2018", "DWG", timeout)), dwg)
        code = 0
    return {"dwg": dwg.name, "exists": dwg.is_file(), "returncode": code, "runtime": rt.label}


# layers the product adds are drawn in colour by the program renderer; everything else grey
RESULT_COLOURS = {"CTLE_BUILDING": "#dc2626", "CTLE_厂房轮廓": "#dc2626", "CTLE_最终_厂房": "#dc2626", "CTLE_待确认": "#c026d3",
                  "CTLE_待确认_范围": "#c026d3", "CTLE_请确认": "#ca8a04", "CTLE_人工轮廓": "#0891b2"}


def render(drawing: Path, window: list[float], png: Path, timeout: float = 900.0, only_result_layers: bool = False) -> dict[str, Any]:
    """A picture of one window of a DWG; the DWG is opened read-only (AutoCAD) or only read (program drawing)."""

    rt = load()
    drawing, png = Path(drawing).resolve(), Path(png).resolve()
    if rt.backend == "autocad":
        lines = ['(setvar "TILEMODE" 1)', '(setvar "CMDECHO" 0)', '(setvar "FILEDIA" 0)']
        if only_result_layers:
            lines += ['(command "_.-LAYER" "_S" "CTLE_BUILDING" "")', '(command "_.-LAYER" "_F" "*" "_T" "CTLE_*" "")']
        lines += [f'(command "_.ZOOM" "_W" (list {_num(window[0])} {_num(window[1])}) (list {_num(window[2])} {_num(window[3])}))',
                  f'(command "_.PNGOUT" {_lisp_path(png)} "_All" "")', "_.QUIT", "_Y", ""]
        code = run_script(rt, drawing, "\n".join(lines), png.with_suffix(".scr"), readonly=True, timeout=timeout)
        return {"png": png.name, "exists": png.is_file(), "returncode": code, "runtime": rt.label}
    return _program_render(drawing, window, png, timeout, only_result_layers)


def _program_render(drawing: Path, window: list[float], png: Path, timeout: float, only_result_layers: bool) -> dict[str, Any]:
    """Without AutoCAD: lines read from a DXF of the drawing (cached next to it), product layers in colour."""

    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]  # Chinese labels on the picture (2026-10-20)
    from matplotlib import pyplot

    from .zero_manual import agent_tools

    dxf = drawing.with_suffix(".render.dxf")
    if not dxf.is_file() or dxf.stat().st_mtime < drawing.stat().st_mtime:
        dwg_to_dxf(drawing, dxf, timeout) if drawing.suffix.lower() == ".dwg" else shutil.copyfile(drawing, dxf)
    index_path = dxf.with_suffix(".sqlite")
    if not index_path.is_file() or index_path.stat().st_mtime < dxf.stat().st_mtime:
        index_path.unlink(missing_ok=True)
        agent_tools.build_index(str(dxf), index_path)
    index = agent_tools.Index(index_path)
    figure = pyplot.figure(figsize=(12, 12 * (window[3] - window[1]) / max(window[2] - window[0], 1e-9)), dpi=110)
    axis = figure.add_axes([0, 0, 1, 1])
    axis.set_facecolor("white")
    for ln in index.select(window, limit=150000):
        colour = next((c for k, c in RESULT_COLOURS.items() if ln["l"].startswith(k)), None)
        if colour is None and (only_result_layers or ln["l"].upper().startswith("CTLE_")):
            colour = "#2563eb" if ln["l"].upper().startswith("CTLE_") else None
        if colour is None and only_result_layers:
            continue
        p = ln["p"]
        if len(p) >= 2:
            axis.plot([q[0] for q in p], [q[1] for q in p], color=colour or "#9ca3af", linewidth=1.2 if colour else 0.25)
    for t in index.texts:
        if t["l"].upper().startswith("CTLE_") and window[0] <= t["x"] <= window[2] and window[1] <= t["y"] <= window[3]:
            axis.text(t["x"], t["y"], t["t"][:40], fontsize=7, color=RESULT_COLOURS.get(t["l"], "#1d4ed8"))
    index.con.close()
    axis.set_xlim(window[0], window[2])
    axis.set_ylim(window[1], window[3])
    axis.set_aspect("equal")
    axis.axis("off")
    figure.savefig(png, facecolor="white")
    pyplot.close(figure)
    return {"png": png.name, "exists": png.is_file(), "returncode": 0, "runtime": "program drawing (no AutoCAD)"}


# ---------------------------------------------------------------- doctor
def doctor() -> int:
    """Installed AutoCADs, the configured runtime, ODA, and a real round trip DXF -> DWG -> DXF -> picture."""

    import ezdxf

    from .oda_complex_reader import discover_oda_file_converter

    report: dict[str, Any] = {"autocad_installations": installed_autocads(), "oda_file_converter": str(discover_oda_file_converter() or "") or None,
                              "config_file": str(_config_path())}
    try:
        rt = load()
    except CadRuntimeError as exc:
        report.update(status="not_configured", message=str(exc))
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1
    report["runtime"] = rt.label
    with tempfile.TemporaryDirectory(prefix="ctle-doctor-", ignore_cleanup_errors=True) as work:
        work = Path(work)
        doc = ezdxf.new("R2018")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 5000), (0, 5000)], close=True, dxfattribs={"layer": "CTLE_BUILDING"})
        msp.add_line((0, 0), (10000, 5000))
        doc.saveas(work / "probe.dxf")
        steps = {}
        try:
            saved = dxf_to_dwg(work / "probe.dxf", work / "probe.dwg")
            steps["dxf_to_dwg"] = saved["exists"]
            back = dwg_to_dxf(work / "probe.dwg", work / "back.dxf")
            entities = [e.dxftype() for e in ezdxf.readfile(back).modelspace()]
            steps["dwg_to_dxf"] = "LWPOLYLINE" in entities and "LINE" in entities
            steps["render"] = render(work / "probe.dwg", [-1000, -1000, 11000, 6000], work / "probe.png")["exists"]
        except Exception as exc:  # the report says which step failed and why
            steps["error"] = f"{type(exc).__name__}: {exc}"
    report["round_trip"] = steps
    report["status"] = "pass" if steps.get("dxf_to_dwg") and steps.get("dwg_to_dxf") and steps.get("render") else "fail"
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["doctor"]:
        raise SystemExit(doctor())
    print(__doc__)
    raise SystemExit(2)
