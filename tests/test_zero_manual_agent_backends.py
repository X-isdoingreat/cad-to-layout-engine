"""The two AI backends of the reading agent (Claude Code, Codex CLI) run the same process: same note, same record keys,
same tool count, same answer recovery; the equipment judge follows the same switch."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cad_to_layout.zero_manual import agent_reader, llm_judge

CODEX_STDERR = """OpenAI Codex v0.154.0
--------
workdir: C:\\work
model: gpt-5.6-terra
provider: openai
approval: never
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
--------
user
这是一个无人值守的自动读图任务
    python -X utf8 -m cad_to_layout.zero_manual.agent_tools overview
    python -X utf8 -m cad_to_layout.zero_manual.agent_tools texts --match 正则

exec
powershell -Command "& python -X utf8 -m cad_to_layout.zero_manual.agent_tools overview"
 succeeded in 10ms:
{"views": 1}

exec
powershell -Command "& python -X utf8 -m cad_to_layout.zero_manual.agent_tools measure F1 --unit-m 0.001"
 succeeded in 10ms:
{"faces": ["F1"], "area_m2": 3603.18}

exec
powershell -Command "Get-ChildItem"
 succeeded in 10ms:
answer.txt

codex
{"buildings": [{"name": "A", "faces": ["F1"], "area_m2": 3603.18}], "unresolved": []}
tokens used
105,891
"""

CLAUDE_STDOUT = "\n".join(json.dumps(e) for e in [
    {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.2}}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "tool_use", "name": "Bash"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"buildings": [{"name": "A"}]}'}]}},
    {"type": "result", "result": '{"buildings": [{"name": "A", "faces": ["F1"], "area_m2": 3603.18}], "unresolved": []}',
     "usage": {"input_tokens": 100, "output_tokens": 50}, "total_cost_usd": 0.01},
])


def fake_run(stdout: str, stderr: str, answer_file_text: str | None):
    calls = []

    def run(command, **kwargs):
        calls.append({"command": command, "input": kwargs.get("input"), "cwd": kwargs.get("cwd")})
        if "-o" in command and answer_file_text is not None:
            Path(command[command.index("-o") + 1]).write_text(answer_file_text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=stderr)

    return run, calls


class AgentBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_and_claude_runs_return_the_same_record(self):
        answer = '{"buildings": [{"name": "A", "faces": ["F1"], "area_m2": 3603.18}], "unresolved": []}'
        run, calls = fake_run("", CODEX_STDERR, answer)
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "codex"), mock.patch.object(agent_reader.subprocess, "run", run):
            codex = agent_reader.run_prompt(self.work, "PROMPT", tag="_t")
        self.assertEqual(calls[0]["command"][0], "codex")
        self.assertIn("exec", calls[0]["command"])
        self.assertTrue(calls[0]["input"].startswith(agent_reader.CODEX_NOTE), "the unattended note goes first")
        self.assertTrue(calls[0]["input"].endswith("PROMPT"))
        self.assertEqual(codex["model"], "gpt-5.6-terra")
        self.assertEqual(codex["tool_calls"], 2, "only the agent_tools commands, not the prompt echo or other commands")
        self.assertEqual(codex["tokens_used"], 105891)
        self.assertEqual(codex["answer"]["buildings"][0]["area_m2"], 3603.18)
        self.assertFalse(codex["answer_from_earlier_message"])
        self.assertTrue((self.work / "events_t.txt").is_file() and (self.work / "stderr_t.txt").is_file() and (self.work / "prompt_t.txt").is_file())

        run, calls = fake_run(CLAUDE_STDOUT, "", None)
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "claude"), mock.patch.object(agent_reader.subprocess, "run", run):
            claude = agent_reader.run_prompt(self.work, "PROMPT", tag="_c")
        self.assertEqual(calls[0]["command"][:2], ["claude", "-p"])
        self.assertIn(agent_reader.CLAUDE_SYSTEM_NOTE, calls[0]["command"])
        self.assertEqual(calls[0]["input"], "PROMPT")
        self.assertEqual(claude["model"], agent_reader.CLAUDE_MODEL)
        self.assertEqual(claude["tool_calls"], 2)
        self.assertEqual(claude["tokens_used"], 150)
        self.assertEqual(claude["answer"]["buildings"][0]["area_m2"], 3603.18)

        self.assertEqual(set(codex), set(claude), "both backends fill the same record")

    def test_codex_answer_is_recovered_from_the_transcript_when_the_last_message_is_prose(self):
        run, _ = fake_run("", CODEX_STDERR, "我已经完成了，上面是答案。")
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "codex"), mock.patch.object(agent_reader.subprocess, "run", run):
            got = agent_reader.run_prompt(self.work, "PROMPT")
        self.assertEqual(got["answer"]["buildings"][0]["name"], "A")
        self.assertTrue(got["answer_from_earlier_message"])

        run, _ = fake_run("", CODEX_STDERR, "没有答案")
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "codex"), mock.patch.object(agent_reader.subprocess, "run", run):
            got = agent_reader.run_prompt(self.work, "PROMPT", answer_key="verdicts")
        self.assertIsNone(got["answer"], "a transcript JSON without the asked key is not the answer")

    def test_a_hung_cli_is_a_failed_call_on_both_backends_not_a_crash(self):
        def hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1), output="partial", stderr="model: gpt-5.6-terra\n")

        for backend in ("codex", "claude"):
            with mock.patch.object(agent_reader, "AGENT_BACKEND", backend), mock.patch.object(agent_reader.subprocess, "run", hang):
                got = agent_reader.run_prompt(self.work, "PROMPT", tag=f"_{backend}", timeout_seconds=7)
            self.assertEqual(got["returncode"], -1, backend)
            self.assertIsNone(got["answer"], backend)
            self.assertIn("[timeout after 7 s]", (self.work / f"stderr_{backend}.txt").read_text(encoding="utf-8"), backend)

        image = self.work / "tile.png"
        image.write_bytes(b"png")
        (self.work / "call").mkdir()
        for transport in (llm_judge.codex_exec_transport(timeout_seconds=7), llm_judge.claude_exec_transport(timeout_seconds=7)):
            with mock.patch.object(agent_reader.subprocess, "run", hang):
                raw = transport("PROMPT", image, self.work / "call")
            self.assertEqual(raw["returncode"], -1)
            self.assertEqual(raw["text"], "")
            result = llm_judge.judge_split(image=image, work_dir=self.work / "judge", transport=lambda *a: raw, allow_external=True)
            self.assertEqual(result.status, "invalid")
            self.assertEqual([c["returncode"] for c in result.receipt["calls"]], [-1] * llm_judge.SPLIT_JUDGE_REPEATS)

    def test_judge_transports_record_model_and_the_cli_error_on_both_backends(self):
        image = self.work / "tile.png"
        image.write_bytes(b"png")
        limit = "ERROR: You've hit your usage limit. Upgrade to Plus to continue using Codex, or try again at Nov 19th, 2026 2:47 PM."
        run, calls = fake_run("", "model: gpt-5.6-terra\n--------\nuser\nPROMPT\n" + limit + "\n", None)
        with mock.patch.object(agent_reader.subprocess, "run", run):
            raw = llm_judge.codex_exec_transport()("PROMPT", image, self.work)
        self.assertIn("-i", calls[0]["command"])
        self.assertEqual((raw["model"], raw["error"], raw["text"]), ("gpt-5.6-terra", limit, ""))

        ok = json.dumps({"type": "result", "is_error": False, "result": '{"drawing_count": 1, "cut_ids": []}', "usage": {"input_tokens": 10, "output_tokens": 5}})
        run, calls = fake_run(ok, "", None)
        with mock.patch.object(agent_reader.subprocess, "run", run):
            raw = llm_judge.claude_exec_transport()("PROMPT", image, self.work)
        self.assertEqual(calls[0]["command"][:2], ["claude", "-p"])
        self.assertEqual((raw["model"], raw["error"], raw["tokens_used"]), ("sonnet", None, 15))
        self.assertIn("drawing_count", raw["text"])

        failed = json.dumps({"type": "result", "is_error": True, "result": "Claude Max usage limit reached", "usage": {}})
        run, _ = fake_run(failed, "", None)
        with mock.patch.object(agent_reader.subprocess, "run", run):
            raw = llm_judge.claude_exec_transport()("PROMPT", image, self.work)
        self.assertEqual((raw["error"], raw["text"]), ("Claude Max usage limit reached", ""))
        result = llm_judge.judge_split(image=image, work_dir=self.work / "judge", transport=lambda *a: raw, allow_external=True)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.receipt["calls"][0]["error"], "Claude Max usage limit reached")

    def test_the_equipment_judge_follows_the_reader_switch(self):
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "codex"):
            self.assertIn("codex_exec_transport", llm_judge.external_transport().__qualname__)
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "claude"):
            self.assertIn("claude_exec_transport", llm_judge.external_transport().__qualname__)

    def test_the_default_backend_is_claude_and_the_switch_is_case_insensitive(self):
        env = {k: v for k, v in os.environ.items() if k != "CTLE_AGENT"}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(importlib.reload(agent_reader).AGENT_BACKEND, "claude")
            with mock.patch.dict(os.environ, {**env, "CTLE_AGENT": "Codex"}, clear=True):
                self.assertEqual(importlib.reload(agent_reader).AGENT_BACKEND, "codex")
        finally:
            importlib.reload(agent_reader)


if __name__ == "__main__":
    unittest.main()
