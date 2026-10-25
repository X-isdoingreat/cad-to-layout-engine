"""The closer look (细看, 2026-10-25): an AI agent with the drawing tools looks for itself at a block family.

The first look (``equipment.judge_shapes``) shows the AI one picture and one line of facts per block and takes its answer
twice; it cannot ask back, zoom in or look next door.  What the two votes cannot settle, and the kinds that decide a large
part of the count (rows, families with many copies) whatever the votes, go to the closer look: the agent gets the program's
whole state about every block family (``blocks.json``), the line index of the drawing and the tools ``block``, ``near``,
``compare``, ``render_block`` (``agent_tools``), and decides after looking.  Its evidence list, the commands it ran and the
pictures it drew are kept; its answer is archived per family and replayed under the same prompt.

Counting and measuring stay with the program; the agent only names what a block is (CEO 2026-10-25).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from .constants import CLOSER_LOOK_MAX_TOOL_CALLS, CLOSER_LOOK_TIMEOUT_SECONDS, HIGH_COUNT_KIND_MIN

PROMPT_PATH = Path(__file__).with_name("closer_look_prompt_v1.md")
PROMPT_ID = "closer_look_prompt_v1"
ARCHIVE_NAME = "closer_look_archive.json"
STATE_NAME = "blocks.json"
STATE_SCHEMA = "ctle-closer-look-state-1.0"
# (work directory, family, prompt) -> {"answer": the agent's JSON or None, "tool_calls", "minutes", "cost_usd_equivalent", ...}
Looker = Callable[[Path, str, str], dict[str, Any]]


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()


def needs_look(key: str, fam: dict[str, Any], judged: dict[str, Any] | None, rows: dict[str, dict[str, Any]],
               follower_of: dict[str, str]) -> str | None:
    """Why this family gets a closer look, or None: the first look's votes differ or say UNSURE; it is the leader of a kind of
    row; or it (its kind) has at least HIGH_COUNT_KIND_MIN copies -- the kinds that decide a large part of the count are looked
    at whatever the votes."""

    if judged is not None:
        if judged.get("status") == "disagreed":
            return "first_look_disagreed"
        if judged.get("status") == "agreed" and judged.get("type") == "UNSURE":
            return "first_look_unsure"
    if key in rows and key not in follower_of:
        return "row"
    copies = (rows.get(key) or {}).get("kind_occurrences") or len(fam["occurrences"])
    if copies >= HIGH_COUNT_KIND_MIN:
        return "many_copies"
    return None


def write_state(work: Path, families: dict[str, dict[str, Any]], decisions: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]],
                arrays: list[dict[str, Any]], scale: float, views: list[dict[str, Any]], follower_of: dict[str, str],
                types_zh: dict[str, str]) -> dict[str, Any]:
    """blocks.json: everything the program knows about every block family, for the agent's tools (never a decision)."""

    array_info: dict[str, dict[str, Any]] = {}
    for a in arrays:
        if a["count"] > array_info.get(a["family"], {}).get("count", 0):
            array_info[a["family"]] = a
    fams = {}
    for key, fam in families.items():
        d = decisions.get(key)
        first = None
        if d is not None:
            first = {k: d.get(k) for k in ("type", "basis", "name", "votes", "reasons", "wrong_if", "reason", "via", "same_kind_as")}
        row = rows.get(key)
        fams[key] = {
            "size_mm": [round(v) for v in fam["size_mm"]], "copies": len(fam["occurrences"]), "views": dict(fam["views"]),
            "parents": dict(fam["parents"].most_common(5)), "children": dict(fam["children"].most_common(8)),
            "texts_inside": list(fam["texts_inside"]), "layers": [layer for layer, _ in fam["layers"].most_common(3)],
            "row": ({k: row.get(k) for k in ("cell_family", "cells_per_row", "groups", "pitch_mm", "cell_size_mm")}
                    | {"also_drawn_by": [f["family"] for f in row.get("also_drawn_by", [])]}) if row else None,
            "follows": follower_of.get(key),
            "array": {k: array_info[key][k] for k in ("count", "pitch_mm", "along")} if key in array_info else None,
            "first_look": first,
            "occurrences": [{"ref": o["ref"], "view": o["view_id"], "bbox": [round(v, 1) for v in o["bbox"]]} for o in fam["occurrences"]],
        }
    state = {"schema": STATE_SCHEMA, "metres_per_unit": scale / 1000.0, "types": types_zh,
             "views": [{"view_id": v.get("view_id"), "window": v.get("window"),
                        "title": (v.get("title") or {}).get("text") if isinstance(v.get("title"), dict) else v.get("title")} for v in views],
             "families": fams}
    work.mkdir(parents=True, exist_ok=True)
    (work / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def safe_tag(key: str) -> str:
    """A file-name-safe tag for one family: its name cleaned plus a short hash (two names may clean to the same text)."""

    return "_" + re.sub(r"[^0-9A-Za-z_\-]", "_", key)[:30] + "_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:6]


def build_prompt(key: str, family: dict[str, Any], tag: str, python: str) -> str:
    first = family.get("first_look") or {}
    votes = list(first.get("votes") or [])
    reasons = list(first.get("reasons") or [])
    if votes:
        parts = []
        for i, vote in enumerate(votes):
            reason = reasons[i] if i < len(reasons) and reasons[i] else ""
            parts.append(f"第 {i + 1} 次 {vote}" + (f"（{reason[:160]}）" if reason else ""))
        first_text = "第一遍看图（两张不同排布的截图各问一次）的答案：" + "；".join(parts) + "。"
        if first.get("wrong_if"):
            first_text += f"它自己说会错的情形：{str(first['wrong_if'])[:160]}。"
    elif first.get("type"):
        first_text = f"程序按规则或文字已判为 {first['type']}（依据 {first.get('basis')}）。"
    else:
        first_text = "第一遍看图没有给出答案。"
    size = family["size_mm"]
    return (PROMPT_PATH.read_text(encoding="utf-8").replace("{PY}", python).replace("{FAMILY}", key).replace("{TAG}", tag)
            .replace("{FIRST_LOOK}", first_text).replace("{SIZE}", f"{size[0]} × {size[1]} mm").replace("{COPIES}", str(family["copies"]))
            .replace("{BUDGET}", str(CLOSER_LOOK_MAX_TOOL_CALLS)))


def run_look(work: Path, key: str, prompt: str) -> dict[str, Any]:
    """One unattended agent run on the chosen backend (claude | codex | api, the one switch), in the shared work directory."""

    from .agent_reader import run_prompt

    result = run_prompt(work, prompt, tag=safe_tag(key), timeout_seconds=CLOSER_LOOK_TIMEOUT_SECONDS, answer_key="type")
    return {"answer": result.get("answer"), **{k: result.get(k) for k in ("tool_calls", "minutes", "cost_usd_equivalent", "tokens_used", "model", "returncode")}}


def decision_from(entry: dict[str, Any], key: str, first: dict[str, Any] | None, types: set[str]) -> dict[str, Any]:
    """The decision record made from one archived look; without a usable answer, only `closer_look_error` is returned."""

    answer = entry.get("answer") if isinstance(entry.get("answer"), dict) else None
    kind = (answer or {}).get("type")
    meta = {"why": entry.get("why"), "tool_calls": entry.get("tool_calls"), "minutes": entry.get("minutes"),
            "cost_usd": entry.get("cost_usd_equivalent"), "model": entry.get("model"), "prompt_id": entry.get("prompt_id")}
    if answer is None or kind not in types:
        return {"closer_look_error": "no answer" if answer is None else f"answer type {kind!r} is not a known type", **meta}
    looked = [{"command": str(l.get("command") or "")[:200], "seen": str(l.get("seen") or "")[:300]}
              for l in (answer.get("looked_at") or []) if isinstance(l, dict)][:40]
    first_type = (first or {}).get("type")
    base = {"basis": "agent_look", "stage": "agent", "name": str(answer.get("name") or "")[:40], "reason": str(answer.get("reason") or "")[:400],
            "wrong_if": str(answer.get("wrong_if") or "")[:200], "looked_at": looked,
            "first_look": {k: (first or {}).get(k) for k in ("type", "votes", "reason", "basis")}, **meta}
    if kind == "UNSURE":
        return {**base, "type": "UNRECOGNISED", "reason": "agent_unsure", "agent_reason": base["reason"],
                "what_would_settle_it": str(answer.get("what_would_settle_it") or "")[:300], "agrees_with_first_look": first_type == "UNRECOGNISED"}
    return {**base, "type": kind, "agrees_with_first_look": kind == first_type}


def closer_look(work: Path, dxf: Path | None, wanted: dict[str, str], families: dict[str, dict[str, Any]], decisions: dict[str, dict[str, Any]],
                rows: dict[str, dict[str, Any]], arrays: list[dict[str, Any]], scale: float, views: list[dict[str, Any]],
                follower_of: dict[str, str], types_zh: dict[str, str], types: set[str], looker: Looker | None = None,
                replay: Path | None = None) -> dict[str, dict[str, Any]]:
    """Look at every family in `wanted` ({family: why}); returns {family: decision or {"closer_look_error": ...}}.

    The agent's answers are archived in the work directory and replayed from there (or from `replay`, an earlier run's
    archive) when the prompt is unchanged; the drawing's line index is built once, in the work directory."""

    work.mkdir(parents=True, exist_ok=True)
    archive_path = work / ARCHIVE_NAME
    archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.is_file() else {}
    if replay is not None and replay.is_file():
        for k, v in json.loads(replay.read_text(encoding="utf-8")).items():
            archive.setdefault(k, v)
    sha = prompt_sha256()
    state = write_state(work, families, decisions, rows, arrays, scale, views, follower_of, types_zh)
    out: dict[str, dict[str, Any]] = {}
    fresh = [k for k in wanted if not (k in archive and archive[k].get("prompt_sha256") == sha)]
    if fresh and looker is None:  # the real agent needs the line index of the drawing for its pictures
        if dxf is None:
            return {k: {"closer_look_error": "no drawing file: the line index for the closer look cannot be built"} for k in wanted}
        if not (work / "index.sqlite").is_file():
            from .agent_reader import prepare

            prepare(Path(dxf), work)
    python = '"' + sys.executable + '"'
    for key, why in wanted.items():
        entry = archive.get(key)
        if entry is None or entry.get("prompt_sha256") != sha:
            prompt = build_prompt(key, state["families"][key], safe_tag(key), python)
            try:
                result = (looker or run_look)(work, key, prompt)
            except Exception as exc:  # a failed look is recorded, never a crash of the whole read
                result = {"answer": None, "error": f"{type(exc).__name__}: {exc}"}
            entry = {"prompt_sha256": sha, "prompt_id": PROMPT_ID, "why": why, **result}
            archive[key] = entry
            archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
        out[key] = decision_from({**entry, "why": why}, key, decisions.get(key), types)
    return out


__all__ = ["ARCHIVE_NAME", "PROMPT_ID", "STATE_NAME", "build_prompt", "closer_look", "decision_from", "needs_look", "prompt_sha256", "run_look", "safe_tag", "write_state"]
