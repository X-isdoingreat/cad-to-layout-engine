"""The program's own AI agent: the Anthropic Messages API called directly (CTLE_AGENT=api), no Claude Code or Codex CLI.

The same read as the other two backends: the same prompt, the same deterministic tools, the same record. The model gets
two tools only - ``agent_tools`` (one subcommand of the drawing tools, run in the work directory) and ``view_image``
(a picture those tools drew, sent back as an image) - so it can look at and measure the drawing and nothing else.
Every turn's usage is summed (input, output, cache write, cache read) and written next to the answer, so the cost of a
drawing can be read back for any model price.

Configuration (environment): ANTHROPIC_API_KEY (required), CTLE_API_MODEL (default claude-sonnet-5-5),
CTLE_API_EFFORT (default xhigh, as Claude Code), ANTHROPIC_BASE_URL (a relay; server-side fallback is then left off).
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MODEL_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
DEFAULT_MODEL = "claude-sonnet-5-5"  # the reads were tuned on Sonnet (2026-10-17: keep sonnet)
EFFORT = os.environ.get("CTLE_API_EFFORT", "xhigh")  # Claude Code's default effort, so the API read thinks as the CLI read does
MAX_TOKENS = 64000  # streamed, so no HTTP timeout; one turn is a few tool calls or the final JSON
MAX_RETRIES = 4  # the SDK retries 429 / 5xx / connection errors with backoff
TOOL_TIMEOUT_SECONDS = 600.0
TOOL_OUTPUT_MAX_CHARS = 30000  # what Claude Code keeps of one command's output; the tools page their lists anyway
IMAGE_MAX_EDGE_PX = 2576  # the high-resolution image limit of current Sonnet / Opus
MAX_CONTEXT_TOKENS = int(os.environ.get("CTLE_API_MAX_CONTEXT_TOKENS", "800000"))  # stop before the 1M window; no compaction here
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# US$ per million tokens: input, output, cache write (5 min), cache read (Anthropic list prices, cached 2026-10-06)
PRICES = {"claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20), "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01), "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30)}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
DENIED_SUBCOMMANDS = {"index"}  # building the index reads a file by path; the program does it before the agent starts

API_NOTE = ("本任务里，提示中写的每条 agent_tools 命令都用 agent_tools 工具执行：args 填命令里 agent_tools 之后的各段，"
            "例如 [\"render\", \"0\", \"0\", \"100\", \"100\", \"a.png\", \"--faces\", \"F1\"]；画出的图片用 view_image 工具打开（path 填文件名），"
            "要等画图那次调用返回以后再打开。")

TOOLS = [
    {"name": "agent_tools",
     "description": "Run one subcommand of the drawing tools (cad_to_layout.zero_manual.agent_tools) in the work directory "
                    "and return its JSON output. args = the words after `agent_tools` in the prompt's commands.",
     "input_schema": {"type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                      "required": ["args"], "additionalProperties": False}},
    {"name": "view_image",
     "description": "Open a picture drawn by the render subcommand (a file in the work directory) and look at it.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
]


def api_model(model: str | None = None) -> str:
    """The model id to call: CTLE_API_MODEL when set, else the CLI alias (sonnet / opus / haiku) mapped to the current id."""

    return os.environ.get("CTLE_API_MODEL") or MODEL_ALIASES.get(model or "", model or DEFAULT_MODEL)


def client():
    import anthropic

    return anthropic.Anthropic(max_retries=MAX_RETRIES, timeout=TOOL_TIMEOUT_SECONDS)


def _create(api: Any, model: str, messages: list[Any], system: str | None = None, tools: list[dict[str, Any]] | None = None) -> Any:
    """One streamed request; prompt caching on the growing conversation; server-side fallback on a refusal (Claude API only)."""

    params: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages, "thinking": {"type": "adaptive"},
                              "output_config": {"effort": EFFORT}, "cache_control": {"type": "ephemeral"}}
    if system:
        params["system"] = system
    if tools:
        params["tools"] = tools
    if not os.environ.get("ANTHROPIC_BASE_URL") and model.startswith(("claude-sonnet-5-5", "claude-opus-5")):
        with api.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **params) as stream:
            return stream.get_final_message()
    with api.messages.stream(**params) as stream:
        return stream.get_final_message()


def _add_usage(total: dict[str, int], usage: Any) -> int:
    """Add one response's usage to the running total; return this request's whole input (the context size)."""

    seen = 0
    for key in USAGE_KEYS:
        n = int(getattr(usage, key, 0) or 0)
        total[key] = total.get(key, 0) + n
        if key != "output_tokens":
            seen += n
    return seen


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return round(sum(usage.get(k, 0) * p for k, p in zip(USAGE_KEYS, price)) / 1e6, 4)


def _subprocess_env(work: Path) -> dict[str, str]:
    from .agent_reader import PACKAGE_ROOT

    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}  # the tools need no key
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _inside(work: Path, name: str) -> Path | None:
    """A relative path that stays inside the work directory, or None."""

    if not name or Path(name).is_absolute():
        return None
    path = (work / name).resolve()
    return path if path == work or work in path.parents else None


def run_tool(work: Path, name: str, data: Any) -> tuple[Any, bool]:
    """Run one tool call; returns (tool_result content, is_error). Invalid input is answered as an error, never run."""

    from .agent_reader import run_cli

    if name == "agent_tools":
        args = data.get("args") if isinstance(data, dict) else None
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            return "args must be a non-empty list of strings", True
        if args[0] in DENIED_SUBCOMMANDS:
            return f"subcommand {args[0]!r} is not available", True
        if args[0] == "render" and len(args) >= 6 and _inside(work, args[5]) is None:
            return "render writes only inside the work directory: give a file name such as a.png", True
        done = run_cli([sys.executable, "-X", "utf8", "-m", "cad_to_layout.zero_manual.agent_tools", *args], capture_output=True,
                       text=True, encoding="utf-8", timeout=TOOL_TIMEOUT_SECONDS, cwd=str(work), env=_subprocess_env(work))
        text = done.stdout if done.returncode == 0 else (done.stdout + "\n" + done.stderr[-4000:]).strip()
        if len(text) > TOOL_OUTPUT_MAX_CHARS:
            text = text[:TOOL_OUTPUT_MAX_CHARS] + f"\n[output cut at {TOOL_OUTPUT_MAX_CHARS} characters; narrow the window or use --offset]"
        return text or "(no output)", done.returncode != 0
    if name == "view_image":
        path = _inside(work, data.get("path", "") if isinstance(data, dict) else "")
        if path is None or path.suffix.lower() not in (".png", ".jpg", ".jpeg") or not path.is_file():
            return "no such picture in the work directory (draw it with render first, and wait for that call to return)", True
        from PIL import Image

        image = Image.open(path)
        if max(image.size) > IMAGE_MAX_EDGE_PX:
            image.thumbnail((IMAGE_MAX_EDGE_PX, IMAGE_MAX_EDGE_PX))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii")}}], False
    return f"unknown tool {name!r}", True


def _text(response: Any) -> str:
    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def run_api(work: Path, prompt: str, started: float, timeout_seconds: float, tag: str = "", answer_key: str = "buildings",
            model: str | None = None, api: Any = None) -> dict[str, Any]:
    """The read as a tool loop over the Messages API; the same record keys as the Claude Code and Codex runs."""

    from .agent_reader import UNATTENDED_NOTE, _json_from

    model = api_model(model)
    usage: dict[str, int] = {k: 0 for k in USAGE_KEYS}
    messages: list[Any] = [{"role": "user", "content": prompt}]
    events, tool_calls, error, returncode, peak, turns, answers = [], 0, None, 0, 0, 0, []
    response = None
    try:
        api = api or client()
    except Exception as exc:  # e.g. the SDK is not installed
        error, returncode = f"{type(exc).__name__}: {exc}", -1
    while error is None:
        if time.time() - started > timeout_seconds:
            error, returncode = f"[timeout after {timeout_seconds:.0f} s]", -1
            break
        try:
            response = _create(api, model, messages, system=UNATTENDED_NOTE + API_NOTE, tools=TOOLS)
        except Exception as exc:  # API errors after the SDK's retries, auth, network: a failed call, not a crash
            error, returncode = f"{type(exc).__name__}: {exc}", -1
            break
        turns += 1
        size = _add_usage(usage, response.usage)
        peak = max(peak, size)
        events.append(response.to_dict())
        messages.append({"role": "assistant", "content": response.content})  # whole content: thinking blocks go back unchanged
        if _text(response):
            answers.append(_text(response))
        if response.stop_reason == "refusal":
            error, returncode = f"refusal: {getattr(response, 'stop_details', None)}", -1
            break
        if response.stop_reason == "max_tokens":
            error, returncode = f"max_tokens reached ({MAX_TOKENS}) in one turn", -1
            break
        calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            break  # end_turn: the last message is the answer
        if size > MAX_CONTEXT_TOKENS:
            error, returncode = f"context {size} tokens > {MAX_CONTEXT_TOKENS}: stopped before the window fills", -1
            break
        results = []
        for call in calls:
            content, is_error = run_tool(work, call.name, call.input)
            tool_calls += 1
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, **({"is_error": True} if is_error else {})})
            events.append({"tool": call.name, "input": call.input, "is_error": is_error,
                           "result": content if isinstance(content, str) else "[image]"})
        messages.append({"role": "user", "content": results})  # all results of one turn in one message
    text = _text(response) if response is not None else ""
    (work / f"events{tag}.txt").write_text("\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in events), encoding="utf-8")
    (work / f"stderr{tag}.txt").write_text((error or "") + "\n", encoding="utf-8")
    (work / f"answer{tag}.txt").write_text(text, encoding="utf-8")
    cost = cost_usd(model, usage)
    summary = {"backend": "api", "provider": "anthropic-api", "model": model, "effort": EFFORT, "turns": turns, "tool_calls": tool_calls,
               "usage": usage, "peak_context_tokens": peak, "cost_usd": cost, "minutes": round((time.time() - started) / 60, 1), "error": error}
    (work / f"usage{tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    answer = _json_from(text)
    earlier = None
    for said in answers:  # the answer JSON given in an earlier message, then followed by another message
        value = _json_from(said)
        if value is not None and answer_key in value:
            earlier = value
    from_earlier = answer is None and earlier is not None
    answer = answer if answer is not None else earlier
    total = sum(usage.values()) or None
    return {"returncode": returncode, "minutes": summary["minutes"], "tokens_used": total, "tokens_detail": usage,
            "cost_usd_equivalent": cost, "plan_usage_seen": [], "tool_calls": tool_calls, "model": model,
            "answer": answer, "answer_from_earlier_message": from_earlier, "raw_answer": text}


def api_transport(model: str | None = None, timeout_seconds: float = 600.0, api: Any = None):
    """Equipment judge over the Messages API: the picture and the prompt in one request, no tools."""

    def call(prompt: str, image: Path, work_dir: Path) -> dict[str, Any]:
        work_dir.mkdir(parents=True, exist_ok=True)
        name = api_model(model)
        usage: dict[str, int] = {k: 0 for k in USAGE_KEYS}
        text, error, returncode = "", None, 0
        started = time.time()
        try:
            content, _ = run_tool(image.resolve().parent, "view_image", {"path": image.name})
            if not isinstance(content, list):
                raise FileNotFoundError(content)
            response = _create(api or client(), name, [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}])
            _add_usage(usage, response.usage)
            (work_dir / "events.jsonl").write_text(json.dumps(response.to_dict(), ensure_ascii=False, default=str), encoding="utf-8")
            text = _text(response)
            if response.stop_reason in ("refusal", "max_tokens"):
                error, text, returncode = f"{response.stop_reason}: {getattr(response, 'stop_details', None)}", "", -1
        except Exception as exc:  # API errors after retries, network, auth: the judge records the call as invalid
            error, returncode = f"{type(exc).__name__}: {exc}", -1
        (work_dir / "answer.txt").write_text(text, encoding="utf-8")
        (work_dir / "stderr.txt").write_text((error or "") + "\n", encoding="utf-8")
        cost = cost_usd(name, usage)
        (work_dir / "usage.json").write_text(json.dumps({"backend": "api", "provider": "anthropic-api", "model": name, "effort": EFFORT, "turns": 1,
                                                         "tool_calls": 0, "usage": usage, "cost_usd": cost, "error": error,
                                                         "minutes": round((time.time() - started) / 60, 2)}, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"text": text, "returncode": returncode, "provider": "anthropic-api", "model": name,
                "tokens_used": sum(usage.values()) or None, "error": error}

    return call


__all__ = ["PRICES", "api_model", "api_transport", "cost_usd", "run_api", "run_tool"]
