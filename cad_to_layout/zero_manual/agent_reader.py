"""Agent reading: an LLM agent reads the drawing with deterministic tools, the program checks it.

The agent (Codex CLI, current model) works in its own directory on an index of the
drawing, may run only the tools in ``agent_tools`` and view the pictures they draw,
and returns JSON.  The program then re-measures every building from the faces the
agent cites; a building whose reported numbers differ from the re-measurement is
flagged.  Prompt, answer, tokens and time are kept for audit and replay.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import agent_tools

PROMPT_PATH = Path(__file__).with_name("agent_reader_prompt_v11.md")  # v11 (2026-10-16): `trace` candidates held against the drawing's own numbers; paged lists
PROMPT_ID = "agent_reader_prompt_v11"
SERVICE_TIER = "fast"  # 2026-10-05: faster responses (uses the plan's allowance faster)
AGENT_BACKEND = os.environ.get("CTLE_AGENT", "claude").lower()  # claude | codex: the one switch for the reader, the adjudicator and the equipment judge (2026-10-06: Claude current; 2026-10-20: same process on both)
CLAUDE_MODEL = os.environ.get("CTLE_CLAUDE_MODEL", "sonnet")
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _shape_key(face: dict[str, Any]) -> tuple:
    """The same face built twice (by repeated faces/around calls) gets a new id each time; this key ties the copies."""

    c = face.get("centroid") or [0, 0]
    return (round(float(face.get("area_m2") or 0), 1), round(float(c[0]), 1), round(float(c[1]), 1))


def _json_from(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def prepare(dxf: Path, work: Path) -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=True)
    # built in its own process: reading a big drawing takes 1-2 GB that Python would otherwise keep for the whole run
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", "cad_to_layout.zero_manual.agent_tools", "index", str(Path(dxf).resolve())],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(work), env=env)
    if done.returncode != 0:
        raise RuntimeError(f"index build failed: {done.stderr[-2000:]}")
    info = json.loads(done.stdout.strip().splitlines()[-1])
    (work / "faces.json").unlink(missing_ok=True)
    return info


def run_agent(work: Path, designation: str | None, executable: str = "codex", timeout_seconds: float = 3600.0,
              extra: str | None = None, tag: str = "") -> dict[str, Any]:
    work = work.resolve()
    python = '"' + sys.executable + '"'  # the agent's sandbox may not see the py launcher
    prompt = PROMPT_PATH.read_text(encoding="utf-8").replace("{PY}", python) + "\n\n## 本次指定的建筑\n\n" + (designation or "未指定（没有给出指定）") + "\n"
    if extra:
        prompt += "\n" + extra
    return run_prompt(work, prompt, tag=tag, timeout_seconds=timeout_seconds, executable=executable)


def run_prompt(work: Path, prompt: str, tag: str = "", timeout_seconds: float = 3600.0, executable: str = "codex",
               answer_key: str = "buildings", model: str | None = None) -> dict[str, Any]:
    """One unattended agent call with the given prompt in the work directory; prompt, transcript and answer are kept.

    `model` (Claude backend only) overrides CLAUDE_MODEL for this call; the model used is in the result.
    """

    work = work.resolve()
    (work / f"prompt{tag}.txt").write_text(prompt, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    started = time.time()
    if AGENT_BACKEND == "claude":
        return _run_claude(work, prompt, env, started, timeout_seconds, tag, answer_key, model or CLAUDE_MODEL)
    return _run_codex(work, prompt, env, started, timeout_seconds, tag, answer_key, executable)


UNATTENDED_NOTE = ("这是一个无人值守的自动读图任务：不要向用户提问、不要等待确认、不要写计划、不要改代码或提交；"
                   "其他说明文件里关于汇报、计划、日志的要求不适用于本任务。只用提示里给的工具读图，最后一条回复只输出 JSON。")
CLAUDE_SYSTEM_NOTE = UNATTENDED_NOTE  # Claude gets it as a system prompt
CODEX_NOTE = UNATTENDED_NOTE + "工具画出的图片要等那条命令返回后再打开，不要和命令并行去看。"  # Codex gets it at the top of the prompt (2026-10-20: it opened pictures before they were drawn)


def run_cli(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` that turns a hung CLI into a failed result (returncode -1, partial output kept) instead of an exception."""

    try:
        return subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        def text(value: Any) -> str:
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        return subprocess.CompletedProcess(command, -1, text(exc.stdout), text(exc.stderr) + f"\n[timeout after {exc.timeout:.0f} s]\n")


def _run_codex(work: Path, prompt: str, env: dict[str, str], started: float, timeout_seconds: float, tag: str,
               answer_key: str = "buildings", executable: str = "codex") -> dict[str, Any]:
    """The same read with Codex CLI (`codex exec`, the account's current model); the same record keys as the Claude run."""

    command = [executable, "-a", "never", "-s", "workspace-write", "-c", "model_reasoning_effort=xhigh", "-c", f"service_tier={SERVICE_TIER}",
               "exec", "--skip-git-repo-check", "-o", str(work / f"answer{tag}.txt"), "-"]
    completed = run_cli(command, input=CODEX_NOTE + "\n\n" + prompt, capture_output=True, text=True, encoding="utf-8",
                        timeout=timeout_seconds, cwd=str(work), env=env)
    (work / f"events{tag}.txt").write_text(completed.stdout, encoding="utf-8")
    (work / f"stderr{tag}.txt").write_text(completed.stderr, encoding="utf-8")
    tokens, model, tool_calls = None, None, 0
    lines = completed.stderr.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "tokens used" and i + 1 < len(lines):  # the CLI prints "tokens used" then the count
            digits = lines[i + 1].strip().replace(",", "")
            tokens = int(digits) if digits.isdigit() else tokens
        elif line.startswith("model:") and model is None:  # the header of the transcript
            model = line.split(":", 1)[1].strip() or None
        elif line.strip() == "exec" and i + 1 < len(lines) and "agent_tools" in lines[i + 1]:  # one tool command run
            tool_calls += 1
    answer_path = work / f"answer{tag}.txt"
    text = answer_path.read_text(encoding="utf-8") if answer_path.is_file() else ""
    answer, earlier = _json_from(text), answer_from_transcript(completed.stderr, answer_key)
    from_earlier = answer is None and earlier is not None
    answer = answer if answer is not None else earlier  # the JSON was given, then followed by another message
    return {"returncode": completed.returncode, "minutes": round((time.time() - started) / 60, 1), "tokens_used": tokens,
            "tokens_detail": {}, "cost_usd_equivalent": None, "plan_usage_seen": [], "tool_calls": tool_calls, "model": model,
            "answer": answer, "answer_from_earlier_message": from_earlier, "raw_answer": text}


def answer_from_transcript(stderr: str, key: str = "buildings") -> dict[str, Any] | None:
    """The last line of a Codex transcript that is the answer JSON (it has `key`, e.g. "buildings")."""

    found = None
    for line in stderr.splitlines():
        if "{" not in line:
            continue
        value = _json_from(line)
        if value is not None and key in value:
            found = value
    return found


def _run_claude(work: Path, prompt: str, env: dict[str, str], started: float, timeout_seconds: float, tag: str,
                answer_key: str = "buildings", model: str | None = None) -> dict[str, Any]:
    """The same read with Claude Code headless (Claude Max plan); records tokens and the plan's usage before and after."""

    model = model or CLAUDE_MODEL
    command = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--strict-mcp-config", "--allowedTools", "Bash", "Read", "Glob", "Grep", "--disallowedTools", "Write", "Edit", "NotebookEdit",
               "--append-system-prompt", CLAUDE_SYSTEM_NOTE]
    completed = run_cli(command, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, cwd=str(work), env=env)
    (work / f"events{tag}.txt").write_text(completed.stdout, encoding="utf-8")
    (work / f"stderr{tag}.txt").write_text(completed.stderr, encoding="utf-8")
    text, usage, limits, cost, tool_calls = "", {}, [], None, 0
    answer, earlier = None, answer_from_events(completed.stdout, answer_key)
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "rate_limit_event":
            windows = (event.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            limits.append({k: (v or {}).get("utilization") for k, v in windows.items()})
        elif event.get("type") == "assistant":
            tool_calls += sum(1 for c in (event.get("message") or {}).get("content", []) if c.get("type") == "tool_use")
        elif event.get("type") == "result":
            text, usage, cost = event.get("result") or "", event.get("usage") or {}, event.get("total_cost_usd")
    (work / f"answer{tag}.txt").write_text(text, encoding="utf-8")
    answer = _json_from(text)
    from_earlier = answer is None and earlier is not None
    answer = answer if answer is not None else earlier  # the JSON was given, then followed by another message (seen 2026-10-09)
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")) or None
    return {"returncode": completed.returncode, "minutes": round((time.time() - started) / 60, 1), "tokens_used": tokens,
            "tokens_detail": usage, "cost_usd_equivalent": cost, "plan_usage_seen": limits, "tool_calls": tool_calls, "model": model,
            "answer": answer, "answer_from_earlier_message": from_earlier, "raw_answer": text}


def answer_from_events(stream: str, key: str = "buildings") -> dict[str, Any] | None:
    """The last assistant message in a Claude stream-json transcript that holds the answer JSON (it has `key`, e.g. "buildings")."""

    found = None
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        for c in (event.get("message") or {}).get("content", []):
            value = _json_from(c.get("text") or "") if c.get("type") == "text" else None
            if value is not None and key in value:
                found = value
    return found


def verify(work: Path, answer: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-measure every reported building from the faces it cites."""

    faces_path = work / "faces.json"
    faces = json.loads(faces_path.read_text(encoding="utf-8")) if faces_path.is_file() else {}
    m = answer.get("unit_m")
    checks = []
    for b in answer.get("buildings", []):
        ids = [f for f in b.get("faces", []) if f in faces]
        row = {"name": b.get("name"), "designated": b.get("designated"), "faces": b.get("faces", [])}
        if not ids or not m or len(ids) != len(b.get("faces", [])):
            row.update({"status": "cannot_verify", "reason": "missing faces or unit"})
        else:
            got = agent_tools.measure_faces(ids, faces, float(m))
            if "error" in got:
                row.update({"status": "cannot_verify", "reason": got["error"]})
            else:
                ok = all(abs(float(b.get(k) or 0) - got[k]) <= 0.01 * max(got[k], 1e-9) for k in ("area_m2", "length_m", "width_m"))
                row.update({"status": "matches_remeasurement" if ok else "differs_from_remeasurement", "remeasured": {k: got[k] for k in ("area_m2", "length_m", "width_m", "orientation_deg")},
                            "outline": got["outline"]})
        if row.get("status") == "matches_remeasurement":
            from shapely.geometry import Polygon

            core = Polygon(row["outline"])
            decided = {d.get("face") for d in answer.get("neighbour_decisions", [])} | set(ids)
            decided_shapes = {_shape_key(faces[f]) for f in decided if f in faces}
            unchecked, seen = [], set()
            for fid, face in faces.items():
                if fid in decided or _shape_key(face) in decided_shapes or _shape_key(face) in seen:
                    continue
                seen.add(_shape_key(face))
                poly = Polygon(face["outline"])
                if (poly.is_valid and poly.boundary.intersection(core.boundary).length * float(m) >= 2.0
                        and poly.intersection(core).area <= 0.1 * min(poly.area, core.area)):  # touching from outside, not overlapping
                    unchecked.append(fid)
            row["unchecked_neighbours"] = unchecked
            # The agent's own decisions, applied by the program: every touching face it judged part of the
            # building is merged in (an agent may decide "part of the building" and still leave it out of measure).
            joined = [d.get("face") for d in answer.get("neighbour_decisions", [])
                      if d.get("building") == b.get("name") and d.get("decision") == "part_of_building" and d.get("face") in faces]
            outside = [f for f in joined if f not in ids and not core.buffer(1e-6).contains(Polygon(faces[f]["outline"]))]
            if outside:
                merged = agent_tools.measure_faces(ids + outside, faces, float(m))
                if "error" not in merged:
                    row["decided_parts_left_out"] = outside
                    row["with_decided_parts"] = {k: merged[k] for k in ("area_m2", "length_m", "width_m", "orientation_deg")}
                    row["outline"] = merged["outline"]
                    row["status"] = "measured_with_decided_parts"
        row["columns_check"] = _columns_check(work, b, row)
        if row.get("outline") and m and (work / agent_tools.INDEX.name).is_file():
            # what the drawing itself says against the measured outline: end-to-end dimensions, written area notes;
            # and the outline's own shape: shallow notches an automatic trace cuts where the outer wall has an opening
            idx = agent_tools.Index(work / agent_tools.INDEX.name)
            row["dimension_check"] = agent_tools.dimension_check(row["outline"], float(m), idx)
            measured = float((row.get("with_decided_parts") or row.get("remeasured") or {}).get("area_m2") or 0)
            row["stated_area_check"] = agent_tools.stated_area_check(row["outline"], measured, float(m), idx)
            row["notch_check"] = agent_tools.notch_check(row["outline"], float(m))
            row["drawing_says"] = drawing_says(row, float(m))
            idx.con.close()
        checks.append(row)
    return checks


def _dimension_directions(rows: list[dict[str, Any]], outline: list[list[float]], m: float) -> list[dict[str, Any]]:
    """End-to-end dimensions grouped by direction (along the outline's length or its width), distinct values each."""

    from shapely.geometry import Polygon

    rect = Polygon(outline).minimum_rotated_rectangle
    corners = list(rect.exterior.coords) if rect.geom_type == "Polygon" else []
    if len(corners) < 3:
        return []
    sides = sorted((math.dist(corners[i], corners[i + 1]) * m for i in range(2)), reverse=True)
    groups: dict[str, dict[str, Any]] = {}
    for d in rows:
        # the outline extent in the dimension's direction says which side it measures
        along = "长" if abs(d["outline_extent_m"] - sides[0]) <= abs(d["outline_extent_m"] - sides[1]) else "宽"
        g = groups.setdefault(along, {"side": along, "outline_extent_m": d["outline_extent_m"], "dimensions_m": [], "flagged": False, "at": d.get("at")})
        if d["dimension_m"] not in g["dimensions_m"]:
            g["dimensions_m"].append(d["dimension_m"])
        g["flagged"] = g["flagged"] or bool(d.get("flagged"))
    for g in groups.values():
        g["dimensions_m"].sort()
    return [groups[k] for k in ("长", "宽") if k in groups]


def drawing_says(row: dict[str, Any], m: float) -> dict[str, Any]:
    """What the drawing itself states about this building, beside the measured numbers: total dimensions and area notes."""

    dims = _dimension_directions(row.get("dimension_check") or [], row["outline"], m)
    areas = [{"text": s["text"], "value_m2": s["value_m2"], "differs_pct": s["differs_pct"], "flagged": s["flagged"]}
             for s in (row.get("stated_area_check") or {}).get("compared", [])]
    return {"dimensions": dims, "areas": areas}


def verify_zones(work: Path, answer: dict[str, Any]) -> dict[str, Any]:
    """Re-measure every reported zone from its face and recompute the storage/operation totals of each view."""

    faces_path = work / agent_tools.FACES.name
    faces = json.loads(faces_path.read_text(encoding="utf-8")) if faces_path.is_file() else {}
    rows, totals = [], {}
    for z in answer.get("zones") or []:
        face = faces.get(z.get("face"))
        row = {"view": z.get("view"), "name": z.get("name"), "category": z.get("category"), "face": z.get("face")}
        if face is None:
            row["status"] = "no_face"
        else:
            row["remeasured_m2"] = face["area_m2"]
            row["status"] = "matches" if abs(float(z.get("area_m2") or 0) - face["area_m2"]) <= 0.01 * max(face["area_m2"], 1e-9) else "differs"
            t = totals.setdefault(str(z.get("view")), {"storage_m2": 0.0, "operation_m2": 0.0})
            if z.get("category") == "storage":
                t["storage_m2"] += face["area_m2"]
            elif z.get("category") == "operation":
                t["operation_m2"] += face["area_m2"]
        rows.append(row)
    for t in totals.values():
        t["storage_and_operation_m2"] = round(t["storage_m2"] + t["operation_m2"], 2)
        t["storage_m2"], t["operation_m2"] = round(t["storage_m2"], 2), round(t["operation_m2"], 2)
    reported = {str(z.get("view")): z for z in answer.get("zone_totals") or []}
    flags = [f"view {v}: reported {reported[v].get('storage_and_operation_m2')} m2, recomputed {t['storage_and_operation_m2']} m2"
             for v, t in totals.items() if v in reported and abs(float(reported[v].get("storage_and_operation_m2") or 0) - t["storage_and_operation_m2"]) > 0.01 * max(t["storage_and_operation_m2"], 1.0)]
    used = [z.get("face") for z in answer.get("zones") or [] if z.get("face")]
    shared = sorted({f for f in used if used.count(f) > 1})
    return {"zones": rows, "recomputed_totals": totals, "flags": flags + [f"face {f} cited by several zones" for f in shared]}


BUILDING_NAME = re.compile(r"厂房|车间|仓库|库房|用房|餐厅|食堂|宿舍|办公楼|综合楼|配电房|泵房|门卫|warehouse|factory|plant|building", re.I)
NO_BUILDING_IMPACT_M2 = 100000  # giving up on every building is worse than any measured outline
OUTSIDE_EDGE_MIN_COLUMNS = 5  # this many of the building's own columns just outside its outline: the outline is on an inner line
OUTSIDE_EDGE_MIN_SHARE = 0.1  # ... and at least this share of the building's columns
MAX_REVISIONS = 3  # rounds of sending the program's findings back
ADJUDICATE = True  # after the kept answer: discrepancies with the drawing's own numbers go to the adjudicator (adjudicator.py)
TRIAL_SCOPE = True  # 2026-10-07: read only the designated building and the named zones; other buildings and columns are out
GRID_REACH_M = 2.0  # outer walls stand on (or within this of) the outermost column line
REVISION_HEADER = """## 程序核对发现的问题（请修正）

你上一轮的答案在下面。程序重新量了你给出的面，并按图核对，发现下列问题。请继续用同样的工具，在同一个工作目录里（面编号仍然有效）逐条处理：
该改的改正（重新 measure / column_instances / zones），确实不该改的在 unresolved 里写明理由。然后按同样的格式输出完整的最终 JSON（不是只输出改动）。
"""


IMPACT = re.compile(r"影响约 (\d+) m²")


def _severity(finding: str) -> int:
    """How much a finding matters: the area it would change (written into it as 影响约 N m²), else 1."""

    hit = IMPACT.search(finding)
    return max(int(hit.group(1)), 1) if hit else 1


def _view_centre(answer: dict[str, Any]) -> tuple[float, float] | None:
    for v in answer.get("views") or []:
        w = v.get("window")
        if isinstance(w, list) and len(w) == 4:
            return ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    return None


def findings(work: Path, answer: dict[str, Any], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Problems the program finds in an answer, each with its kind, building, the area it may change, where it is and its shape.

    `text` is written for the agent (revision rounds); the other fields let the same problems be marked in the
    delivered drawing for a person to confirm (see confirmation.py).
    """

    from shapely.geometry import MultiPoint, Point, Polygon

    out: list[dict[str, Any]] = []

    def add(kind: str, building: str | None, text: str, impact: float = 0.0, at: Any = None, geometry: Any = None) -> None:
        out.append({"kind": kind, "building": building, "text": text, "impact_m2": round(float(impact or 0), 1),
                    "at": [round(at[0], 3), round(at[1], 3)] if at is not None else None, "geometry": geometry})

    def coords(poly: Any) -> list[Any]:
        """Each piece as its outline, or {"shell", "holes"} when it has holes (a frame round a building)."""

        parts = [g for g in getattr(poly, "geoms", [poly]) if not g.is_empty and g.geom_type == "Polygon"]
        out_parts = []
        for g in parts:
            shell = [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords]
            holes = [[[round(x, 3), round(y, 3)] for x, y in r.coords] for r in g.interiors]
            out_parts.append({"shell": shell, "holes": holes} if holes else shell)
        return out_parts

    m = float(answer.get("unit_m") or 0) or None
    faces_path = work / agent_tools.FACES.name
    faces = json.loads(faces_path.read_text(encoding="utf-8")) if faces_path.is_file() else {}
    runs_path = work / agent_tools.COLUMNS.name
    runs = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.is_file() else {}
    by_name = {b.get("name"): b for b in answer.get("buildings", [])}
    outlines = []
    for c in checks:
        name = c.get("name")
        if TRIAL_SCOPE and not c.get("designated"):
            continue  # trial scope (2026-10-07): only the designated building is read and checked
        centre = None
        if c.get("outline"):
            p = Polygon(c["outline"]).buffer(0)
            centre = (p.representative_point().x, p.representative_point().y) if not p.is_empty else None
        if c.get("status") in {"cannot_verify", "differs_from_remeasurement"}:
            size = float(by_name.get(name, {}).get("area_m2") or 0)
            impact = max(size * 0.05, 100)
            add("remeasure", name, f"建筑「{name}」：{c.get('status')}（{c.get('reason', '报告的面积/长宽与程序按所给面重新量的不一致')}；影响约 {impact:.0f} m²）。",
                impact, centre)
        for p in ([] if TRIAL_SCOPE else (c.get("columns_check") or {}).get("problems", [])):
            add("columns", name, f"建筑「{name}」柱子：{p}。", 0.0, centre)
        reported_columns = ((by_name.get(name) or {}).get("columns") or {}).get("count")
        if not TRIAL_SCOPE and (c.get("columns_check") or {}).get("status") == "no_tool_count" and (c.get("designated") or reported_columns):
            add("bookkeeping", name, f"建筑「{name}」：没有用这栋楼最终的面运行 column_instances。", 0.0, centre)
        size = float((c.get("with_decided_parts") or c.get("remeasured") or {}).get("area_m2") or 0)
        dims = (c.get("drawing_says") or {}).get("dimensions")
        if dims is None and c.get("outline") and m:  # a check made before drawing_says existed
            dims = _dimension_directions(c.get("dimension_check") or [], c["outline"], m)
        for g in dims or []:  # one mark per side, whatever number of dimensions measure it (a total is often drawn twice)
            if g["flagged"]:
                worst = max(g["dimensions_m"], key=lambda v: abs(g["outline_extent_m"] - v))
                pct = 100 * (g["outline_extent_m"] - worst) / worst
                impact = abs(pct) / 100 * size
                # either side may be the wrong one: an outline traced short, or a dimension that measures more than the building
                why = ("轮廓可能漏了一段，也可能这个尺寸量的不只是这栋楼（如含坡道、雨篷的外廓）" if worst > g["outline_extent_m"]
                       else "轮廓可能多圈进了东西，也可能这个尺寸量的是轴线而外墙在轴线之外")
                add("dimension", name, f"建筑「{name}」：图上{g['side']}向的端到端总尺寸标注 {' / '.join(f'{v:g}' for v in g['dimensions_m'])} m，"
                                       f"轮廓同方向的长度 {g['outline_extent_m']} m，相差 {pct:.1f}%；{why}（影响约 {impact:.0f} m²）。", impact, centre)
        for s in (c.get("stated_area_check") or {}).get("compared", []):
            if s.get("flagged"):
                impact = abs(s["measured_m2"] - s["value_m2"])
                add("stated_area", name, f"建筑「{name}」：图上写着「{s['text']}」（{s['value_m2']:,.2f} m²），程序按轮廓量出 {s['measured_m2']:,.2f} m²，"
                                         f"相差 {s['differs_pct']}%（影响约 {impact:.0f} m²）。", impact, centre)
        nc = c.get("notch_check") or {}
        if nc.get("flagged"):
            add("notches", name, f"建筑「{name}」：轮廓有 {nc['notches']} 处向楼内的浅凹口，合计约 {nc['filled_m2']:.0f} m²（占 {100 * nc['share']:.1f}%）。"
                                 f"自动描边在外墙有缺口处会走进楼内；这些凹口是真的凹进去，还是外墙其实是连续的？", nc["filled_m2"], centre,
                {"polygons": nc["polygons"]})
        if c.get("unchecked_neighbours"):
            add("bookkeeping", name, f"建筑「{name}」：相邻面 {', '.join(c['unchecked_neighbours'][:12])} 没有写进 neighbour_decisions。", 0.0, centre)
        if not c.get("outline") or not m:
            continue
        core = Polygon(c["outline"])
        outlines.append(core)
        cited = set(by_name.get(name, {}).get("faces", [])) | set(c.get("decided_parts_left_out", []))
        run = runs.get("+".join(sorted(cited))) or runs.get("+".join(sorted(by_name.get(name, {}).get("faces", []))))
        centres = [Point(q) for q in (run or {}).get("centres", []) + (run or {}).get("outside_positions", [])]
        if len(centres) < 10:
            continue  # no column grid to reason with
        own = [q for q in centres if core.buffer(GRID_REACH_M / m).contains(q)]
        hugging = [q for q in own if not core.contains(q)]
        # a few columns at corners or steps fall just outside any traced line; a whole side of them means an inner line
        if len(hugging) >= max(OUTSIDE_EDGE_MIN_COLUMNS, OUTSIDE_EDGE_MIN_SHARE * len(own)):
            # the strip left out: each column stands for one bay of wall, as deep as it stands outside (+ the wall beyond it)
            gaps = sorted(min(q.distance(o) for o in own if o is not q) * m for q in own)
            bay = gaps[len(gaps) // 2] if gaps else 0.0
            strip = sum((core.exterior.distance(q) * m + 0.3) * bay for q in hugging)
            mid = MultiPoint(hugging).centroid
            add("outline_inner_line", name,
                f"建筑「{name}」：有 {len(hugging)} 根这栋楼的柱子落在轮廓外 {GRID_REACH_M} m 以内（影响约 {strip:.0f} m²；例如 "
                + "、".join(f"({q.x:.1f}, {q.y:.1f})" for q in hugging[:4])
                + "）。按 GB/T 50353 建筑面积按外墙外边线计，柱子在外墙上时轮廓应把柱子包在里面：这一段轮廓走的是里面一条线，"
                  "请改用外墙最外侧那条线（envelope 或换图层/窗口重做 faces）后重新 measure。",
                strip, (mid.x, mid.y), {"points": [[round(q.x, 3), round(q.y, 3)] for q in hugging]})
        if len(own) >= 3:
            beyond = core.difference(MultiPoint(own).convex_hull.buffer(GRID_REACH_M / m))
            if beyond.area * m * m > 0.02 * core.area * m * m:
                big = max(getattr(beyond, "geoms", [beyond]), key=lambda g: g.area)
                add("outline_beyond_grid", name,
                    f"建筑「{name}」：轮廓有 {beyond.area * m * m:.0f} m² 在柱网最外一圈柱子以外 {GRID_REACH_M} m 之外（影响约 {beyond.area * m * m:.0f} m²）。外墙通常就在最外一圈柱子上；"
                    f"这部分可能是外侧场地、道路边带、雨棚或另一栋楼。确认它在连续外墙之内，否则不并入。",
                    beyond.area * m * m, (big.representative_point().x, big.representative_point().y), {"polygons": coords(beyond)})
        cited_shapes = {_shape_key(faces[f]) for f in cited if f in faces}
        seen = set()
        for fid, face in faces.items():
            if _shape_key(face) in seen:
                continue  # a copy of a face already looked at
            seen.add(_shape_key(face))
            if _shape_key(face) in cited_shapes:
                fid = next(f for f in cited if f in faces and _shape_key(faces[f]) == _shape_key(face))
            poly = Polygon(face["outline"])
            poly = poly if poly.is_valid else poly.buffer(0)  # faces traced along touching walls can self-touch
            if poly.is_empty or poly.area <= 0:
                continue
            # columns standing inside the face, not on the wall it shares with the building
            inside = sum(1 for q in centres if poly.buffer(0.5 / m).contains(q) and core.exterior.distance(q) * m > GRID_REACH_M)
            area = float(face.get("area_m2") or 0)
            at = (poly.representative_point().x, poly.representative_point().y)
            if fid in cited:
                if inside == 0 and poly.area >= 0.05 * core.area:
                    add("outline_face_without_columns", name,
                        f"建筑「{name}」包含的面 {fid}（{face.get('area_m2')} m²，影响约 {area:.0f} m²）里没有这栋楼柱网的任何一根柱子：确认它在连续外墙之内，"
                        f"否则它是外侧场地、通道或半室外，不并入。", area, at, {"polygons": coords(poly), "face": fid})
            elif (inside >= 3 and poly.boundary.intersection(core.boundary).length * m >= 2.0
                  and poly.intersection(core).area <= 0.1 * min(poly.area, core.area)):  # a neighbour, not a face overlapping the building
                add("outline_annex_left_out", name,
                    f"建筑「{name}」：与它共墙的面 {fid}（{face.get('area_m2')} m²，影响约 {area:.0f} m²）里有这栋楼柱网的 {inside} 根柱子，却没有算进楼。"
                    f"同一柱网、共用外墙的附跨按 GB/T 50353 属于这栋楼；只有看到敞开的外边（没有外墙）才是半室外。", area, at, {"polygons": coords(poly), "face": fid})
    status = ((answer.get("designation") or {}).get("status") or "")
    if TRIAL_SCOPE and status in {"not_found", "not_unique"}:
        add("designated_missing", None, f"指定的厂房{'没有找到' if status == 'not_found' else '对应了不止一栋楼'}：请在「CTLE_人工轮廓」层描出要改造的那栋厂房。",
            NO_BUILDING_IMPACT_M2, _view_centre(answer))
    idx_path = work / agent_tools.INDEX.name
    if not TRIAL_SCOPE and idx_path.is_file() and m:  # also with no building reported: names on the drawing then show what was given up
        idx = agent_tools.Index(idx_path)
        listed = " ".join(str(b.get("name", "")) for b in answer.get("buildings", [])) + " " + json.dumps(answer.get("unresolved", []), ensure_ascii=False)
        missing_texts = [t for t in idx.texts if len(t["t"].strip()) <= 20 and BUILDING_NAME.search(t["t"])
                         and not any(o.buffer(1.0 / m).contains(Point(t["x"], t["y"])) for o in outlines) and t["t"].strip() not in listed]
        idx.con.close()
        missing = sorted({t["t"].strip() for t in missing_texts})
        where = {t["t"].strip(): (t["x"], t["y"]) for t in missing_texts}
        if missing and not outlines:
            add("no_building", None, f"你没有报告任何建筑，但图上有 {len(missing)} 个建筑名称（{'、'.join(missing[:12])}；影响约 {NO_BUILDING_IMPACT_M2} m²）。"
                                     "外墙线不完整、混有道路或设备线时，也要用 faces / envelope 量出最可能的外轮廓并报告，把不确定的地方写进 unresolved，"
                                     "不能整栋不报。", NO_BUILDING_IMPACT_M2, where[missing[0]], {"names": [[n, list(where[n])] for n in missing]})
        elif missing:
            add("building_not_reported", None, "图上这些建筑名称不在你报告的任何一栋楼里，也没有在 unresolved 里说明：" + "、".join(missing[:30])
                + "。图上画出的每一栋建筑都要报告（指定的排最前）；不是建筑或不在图上的写进 unresolved。", 0.0, where[missing[0]],
                {"names": [[n, list(where[n])] for n in missing]})
    return out


def feedback(work: Path, answer: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    """Problems the program finds in an answer, in words the agent can act on.  Empty: nothing to revise."""

    return [f["text"] for f in findings(work, answer, checks)]


def _columns_check(work: Path, building: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The reported column count against the tool's own count for the same faces."""

    path = work / agent_tools.COLUMNS.name
    runs = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    reported = building.get("columns") or {}
    faces = set(building.get("faces", [])) | set(row.get("decided_parts_left_out", []))
    run = runs.get("+".join(sorted(faces))) or runs.get("+".join(sorted(building.get("faces", []))))
    if run is None:
        return {"status": "no_tool_count", "reason": "column_instances was not run on this building's faces"}
    accepted = reported.get("accepted_to_check") or []
    expected = int(run.get("columns_in_faces") or 0) + len(accepted)
    out = {"tool_counted": run.get("columns_in_faces"), "accepted_to_check": len(accepted), "reported": reported.get("count"),
           "same_columns_outside_faces": run.get("same_columns_outside_faces", 0), "to_check": run.get("to_check_on_grid", 0),
           "rejected_to_check": len(reported.get("rejected_to_check") or [])}
    problems = []
    if reported.get("count") != expected:
        problems.append(f"reported count {reported.get('count')} differs from tool count plus accepted places ({expected})")
    if out["same_columns_outside_faces"] and not str(reported.get("outside_columns_decision") or "").strip():
        problems.append(f"{out['same_columns_outside_faces']} same columns outside the faces without a decision")
    if out["to_check"] > out["accepted_to_check"] + out["rejected_to_check"]:
        problems.append(f"{out['to_check'] - out['accepted_to_check'] - out['rejected_to_check']} places to check left undecided")
    out["status"] = "consistent" if not problems else "flagged"
    out["problems"] = problems
    return out


def best_round(work: Path, answers: list[dict[str, Any]]) -> int:
    """Index of the answer to keep: fewest findings on the designated building, then fewest overall (area findings weigh 10).

    A revision can also make an answer worse (a correct outline traded for a wrong one), so the last answer is not kept blindly."""

    def score(i: int) -> tuple[int, int, int]:
        a = answers[i]
        found = feedback(work, a, verify(work, a))
        names = {b.get("name") for b in a.get("buildings", []) if b.get("designated")}
        on_designated = sum(_severity(p) for p in found if any(f"「{n}」" in p for n in names) or "你没有报告任何建筑" in p)
        return (on_designated, sum(_severity(p) for p in found), -i)

    return min(range(len(answers)), key=score)


def reselect(work: Path) -> dict[str, Any]:
    """Apply the current best-round rule to a finished read (agent_read.json) without calling the agent again."""

    path = work / "agent_read.json"
    read = json.loads(path.read_text(encoding="utf-8"))
    rounds = read.get("revisions") or ([{"answer_before": read["first_answer"]}] if read.get("first_answer") else [])
    if not rounds:
        return read
    answers = [v["answer_before"] for v in rounds] + [read["last_answer"] if "last_answer" in read else read["answer"]]
    best = best_round(work, answers)
    read["last_answer"] = answers[-1]
    read["kept_round"] = best
    read["answer"] = answers[best]
    read["checks"] = verify(work, read["answer"])
    read["zone_check"] = verify_zones(work, read["answer"])
    read["remaining_feedback"] = feedback(work, read["answer"], read["checks"])
    path.write_text(json.dumps(read, ensure_ascii=False, indent=1), encoding="utf-8")
    return read


def read_once(work: Path, designation: str | None, info: dict[str, Any] | None = None, revise: bool = True) -> dict[str, Any]:
    """One agent read with the program's check; if the check finds problems, one revision round with them."""

    run = run_agent(work, designation)
    answer = run["answer"] or {}
    checks = verify(work, answer) if answer else []
    result = {"schema": "ctle-agent-read-1.0", "prompt_id": PROMPT_ID, "prompt_sha256": hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
              "designation_input": designation, "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "index": info,
              "minutes": run["minutes"], "tokens_used": run["tokens_used"], "returncode": run["returncode"], "answer_parsed": bool(run["answer"]),
              "backend": AGENT_BACKEND, "model": run.get("model"),
              "plan_usage_seen": [run.get("plan_usage_seen")], "cost_usd_equivalent": run.get("cost_usd_equivalent"),
              "answer_from_earlier_message": bool(run.get("answer_from_earlier_message"))}
    problems = feedback(work, answer, checks) if answer else []
    rounds = []
    for n in range(1, (MAX_REVISIONS if revise else 0) + 1):
        if not answer or not problems:
            break
        extra = (REVISION_HEADER + "\n### 问题\n\n" + "\n".join(f"{k}. {p}" for k, p in enumerate(problems, 1))
                 + "\n\n### 你上一轮的答案\n\n" + json.dumps(answer, ensure_ascii=False) + "\n")
        again = run_agent(work, designation, extra=extra, tag=f"_rev{n}")
        rounds.append({"round": n, "answer_before": answer, "feedback": problems, "minutes": again["minutes"], "tokens_used": again["tokens_used"],
                       "parsed": bool(again["answer"])})
        result["minutes"] = round(result["minutes"] + again["minutes"], 1)
        result["tokens_used"] = (result["tokens_used"] or 0) + (again["tokens_used"] or 0)
        result["plan_usage_seen"].append(again.get("plan_usage_seen"))
        if again.get("cost_usd_equivalent") is not None:
            result["cost_usd_equivalent"] = (result["cost_usd_equivalent"] or 0) + again["cost_usd_equivalent"]
        if not again["answer"]:
            break
        answer = again["answer"]
        checks = verify(work, answer)
        problems = feedback(work, answer, checks)
    if rounds:
        result.update({"revisions": rounds, "first_answer": rounds[0]["answer_before"], "feedback": rounds[0]["feedback"], "last_answer": answer})
        best = best_round(work, [v["answer_before"] for v in rounds] + [answer])
        result["kept_round"] = best  # 0 = the first answer, n = after revision n
        if best != len(rounds):
            answer = rounds[best]["answer_before"]
            checks = verify(work, answer)
    result.update({"answer": answer, "checks": checks, "zone_check": verify_zones(work, answer) if answer else {},
                   "remaining_feedback": feedback(work, answer, checks) if answer else []})
    if ADJUDICATE and answer:  # 2026-10-16: the program's discrepancies go to a second, narrowly scoped agent call
        from . import adjudicator

        result = adjudicator.adjudicate_read(work, result)
    return result


def read_with_agent(dxf: Path, work: Path, designation: str | None) -> dict[str, Any]:
    info = prepare(dxf, work)
    result = read_once(work, designation, info)
    result["dxf"] = str(dxf)
    (work / "agent_read.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


__all__ = ["feedback", "findings", "prepare", "read_once", "read_with_agent", "run_agent", "run_prompt", "verify", "verify_zones"]
