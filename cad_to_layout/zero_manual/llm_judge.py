"""Automatic judge for region splitting: an LLM chooses among program-proposed cuts.

The judge is called by the product flow with a fixed prompt template; nobody
writes a prompt for a particular drawing.  Every request (image + prompt) and
raw response is stored as-is, the call is repeated and answers must agree, and a
saved receipt can be replayed without any LLM call.  External calls happen only
when ``allow_external`` is true (customer drawing content leaves the machine).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .constants import SPLIT_JUDGE_MAJORITY, SPLIT_JUDGE_REPEATS
from .split import PROMPT_TEMPLATE, PROMPT_TEMPLATE_ID, parse_answer, prompt_sha256

JUDGE_RECEIPT_SCHEMA = "ctle-zero-manual-judge-receipt-1.0"


class Transport(Protocol):
    def __call__(self, prompt: str, image: Path, work_dir: Path) -> dict[str, Any]: ...


def run_cli(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """The reading agent's runner: a hung CLI becomes a failed call (returncode -1) and the judge records it as invalid."""

    from .agent_reader import run_cli as _run

    return _run(command, **kwargs)


def codex_exec_transport(executable: str = "codex", timeout_seconds: float = 600.0) -> Transport:
    """External transport: ``codex exec`` with the image attached, prompt on stdin, no tools."""

    def call(prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        work_dir = work_dir.resolve()
        image = image.resolve()
        answer = work_dir / "answer.txt"
        command = [executable, "-a", "never", "-s", "read-only", "exec", "--skip-git-repo-check",
                   "-o", str(answer), "-i", str(image), "-"]  # "-" = prompt from stdin
        completed = run_cli(command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout_seconds, cwd=str(work_dir))
        (work_dir / "events.jsonl").write_text(completed.stdout, encoding="utf-8")
        (work_dir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        text = answer.read_text(encoding="utf-8") if answer.is_file() else ""
        model, tokens, error = None, None, None
        lines = completed.stderr.splitlines()
        for i, line in enumerate(lines):
            if line.strip().lower() == "tokens used" and i + 1 < len(lines):  # the CLI prints "tokens used" then the count
                digits = lines[i + 1].strip().replace(",", "")
                tokens = int(digits) if digits.isdigit() else None
            elif line.startswith("model:") and model is None:  # the header of the transcript
                model = line.split(":", 1)[1].strip() or None
            elif line.startswith("ERROR") or line.startswith("[timeout"):  # e.g. the plan's usage limit (seen 2026-10-20)
                error = line.strip()
        return {"text": text, "returncode": completed.returncode, "provider": "openai-codex-cli", "model": model or "unknown", "tokens_used": tokens, "error": error}

    return call


def claude_exec_transport(executable: str = "claude", model: str = "sonnet", timeout_seconds: float = 600.0) -> Transport:
    """External transport: Claude Code headless; it opens the image with its Read tool, prompt on stdin, no other tools."""

    def call(prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        work_dir = work_dir.resolve()
        image = image.resolve()
        full = f"先用 Read 工具打开这张图片再回答：{image}\n\n{prompt}"
        command = [executable, "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
                   "--allowedTools", "Read", "--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit"]
        from .agent_reader import subscription_env

        completed = run_cli(command, input=full, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout_seconds, cwd=str(image.parent), env=subscription_env())
        (work_dir / "events.jsonl").write_text(completed.stdout, encoding="utf-8")
        (work_dir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        try:
            event = json.loads(completed.stdout)
        except json.JSONDecodeError:
            event = {}
        usage = event.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")) or None
        text = str(event.get("result") or "")
        (work_dir / "answer.txt").write_text(text, encoding="utf-8")
        error = text if event.get("is_error") else None  # Claude Code reports failures (e.g. the plan's limit) as an error result
        if error is None and completed.returncode != 0:
            error = next((ln.strip() for ln in reversed(completed.stderr.splitlines()) if ln.strip()), f"exit {completed.returncode}")
        return {"text": "" if error else text, "returncode": completed.returncode, "provider": "anthropic-claude-code", "model": model, "tokens_used": tokens, "error": error}

    return call


def external_transport() -> Transport:
    """The judge chosen by the same switch as the reading agent (`agent_reader.AGENT_BACKEND`: claude by default | codex | api)."""

    from .agent_reader import AGENT_BACKEND

    if AGENT_BACKEND == "api":
        from .api_agent import api_transport

        return api_transport()
    return codex_exec_transport() if AGENT_BACKEND == "codex" else claude_exec_transport()


@dataclass
class JudgeResult:
    status: str  # agreed | disagreed | invalid | skipped
    answer: dict[str, Any] | None
    receipt: dict[str, Any]


def judge_split(*, image: Path, work_dir: Path, transport: Transport | None, allow_external: bool, facts: str = "",
                repeats: int = SPLIT_JUDGE_REPEATS, equivalence: Callable[[dict[str, Any]], str] | None = None) -> JudgeResult:
    """``equivalence`` maps an answer to a key; answers with equal keys count as the same answer
    (the caller passes the resulting partition, so equivalent cut lines agree)."""

    work_dir.mkdir(parents=True, exist_ok=True)
    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    prompt = PROMPT_TEMPLATE + facts
    base = {
        "schema": JUDGE_RECEIPT_SCHEMA, "prompt_template_id": PROMPT_TEMPLATE_ID, "prompt_sha256": prompt_sha256(),
        "facts": facts, "facts_sha256": hashlib.sha256(facts.encode("utf-8")).hexdigest(),
        "image": {"path": str(image), "sha256": image_sha}, "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "external_data_egress": bool(allow_external and transport is not None), "calls": [],
    }
    if transport is None or not allow_external:
        receipt = {**base, "status": "skipped", "reason": "external LLM not allowed or no transport"}
        (work_dir / "judge_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
        return JudgeResult("skipped", None, receipt)
    (work_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    answers: list[dict[str, Any] | None] = []
    for index in range(1, repeats + 1):
        call_dir = work_dir / f"call-{index}"
        call_dir.mkdir(exist_ok=True)
        raw = transport(prompt, image, call_dir)
        parsed = parse_answer(raw.get("text", ""))
        answers.append(parsed)
        base["calls"].append({"index": index, "provider": raw.get("provider"), "model": raw.get("model"), "error": raw.get("error"),
                              "returncode": raw.get("returncode"), "raw_text_sha256": hashlib.sha256(str(raw.get("text", "")).encode("utf-8")).hexdigest(),
                              "parsed": parsed})
    status, adopted, size = _tally(answers, equivalence)
    receipt = {**base, "status": status, "answer": adopted,
               "agreement": {"repeats": repeats, "valid": sum(a is not None for a in answers), "majority_size": size, "required": SPLIT_JUDGE_MAJORITY,
                             "by": "partition" if equivalence is not None else "count_and_ids"}}
    (work_dir / "judge_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    return JudgeResult(status, receipt["answer"], receipt)


def _tally(answers: list[dict[str, Any] | None], equivalence: Callable[[dict[str, Any]], str] | None) -> tuple[str, dict[str, Any] | None, int]:
    valid = [a for a in answers if a is not None]
    keyed: dict[str, list[dict[str, Any]]] = {}
    for a in valid:
        key = equivalence(a) if equivalence is not None else json.dumps([a["drawing_count"], a["cut_ids"]])
        keyed.setdefault(key, []).append(a)
    best = max(keyed.values(), key=len) if keyed else []
    if not valid:
        return "invalid", None, 0
    if len(best) >= SPLIT_JUDGE_MAJORITY:
        return "agreed", best[0], len(best)
    return "disagreed", None, len(best)


def replay_judge(receipt_path: Path, image: Path, equivalence: Callable[[dict[str, Any]], str] | None = None,
                 facts: str | None = None) -> JudgeResult:
    """Reuse a saved receipt; the image and prompt must be the same bytes.

    The saved raw answers are re-tallied with the current agreement rule (a
    deterministic step), so no LLM is called.
    """

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("prompt_sha256") != prompt_sha256():
        return JudgeResult("invalid", None, {**receipt, "replay_error": "prompt template changed"})
    if receipt.get("image", {}).get("sha256") != hashlib.sha256(image.read_bytes()).hexdigest():
        return JudgeResult("invalid", None, {**receipt, "replay_error": "image changed"})
    if facts is not None and receipt.get("facts_sha256") != hashlib.sha256(facts.encode("utf-8")).hexdigest():
        return JudgeResult("invalid", None, {**receipt, "replay_error": "facts changed"})
    calls = receipt.get("calls") or []
    if not calls:
        return JudgeResult(receipt.get("status", "invalid"), receipt.get("answer"), {**receipt, "replayed": True})
    status, answer, size = _tally([c.get("parsed") for c in calls], equivalence)
    return JudgeResult(status, answer, {**receipt, "replayed": True, "status": status, "answer": answer,
                                        "agreement": {**(receipt.get("agreement") or {}), "majority_size": size,
                                                      "by": "partition" if equivalence is not None else "count_and_ids"}})


__all__ = ["JudgeResult", "Transport", "claude_exec_transport", "codex_exec_transport", "external_transport", "judge_split", "replay_judge"]
