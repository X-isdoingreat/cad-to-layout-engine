"""The third AI backend (CTLE_AGENT=api): the program runs the tool loop over the Messages API itself.

A fake client stands in for the API, so no call leaves the machine: same record as the CLI backends, only the drawing
tools, usage summed and priced, failures recorded instead of crashing, the subscription path never sees an API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cad_to_layout.zero_manual import agent_reader, api_agent, llm_judge

RECORD_KEYS = {"returncode", "minutes", "tokens_used", "tokens_detail", "cost_usd_equivalent", "plan_usage_seen", "tool_calls",
               "model", "answer", "answer_from_earlier_message", "raw_answer"}
ANSWER = '{"buildings": [{"name": "A", "faces": ["F1"], "area_m2": 3603.18}], "unresolved": []}'


def block(kind: str, **fields):
    return SimpleNamespace(type=kind, **fields)


def response(content, stop="end_turn", usage=(100, 20, 0, 0)):
    u = SimpleNamespace(**dict(zip(api_agent.USAGE_KEYS, usage)))
    r = SimpleNamespace(content=content, stop_reason=stop, usage=u, stop_details=None)
    r.to_dict = lambda: {"stop_reason": stop, "content": [vars(b) for b in content]}
    return r


class FakeAPI:
    """Answers each request with the next scripted response and keeps every request's parameters."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.messages = SimpleNamespace(stream=self._stream("messages"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream("beta")))

    def _stream(self, route):
        def stream(**params):
            self.requests.append({"route": route, **params, "messages": list(params["messages"])})
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            manager = mock.MagicMock()
            manager.__enter__.return_value.get_final_message.return_value = reply
            return manager
        return stream


class ApiBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith(("CTLE_API", "ANTHROPIC_"))}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_api(self, api, **kwargs):
        return api_agent.run_api(self.work, "PROMPT", started=kwargs.pop("started", __import__("time").time()),
                                 timeout_seconds=kwargs.pop("timeout_seconds", 60), api=api, **kwargs)

    def test_a_read_is_a_tool_loop_with_the_same_record_as_the_cli_backends(self):
        api = FakeAPI([
            response([block("thinking", thinking="", signature="s1"),
                      block("tool_use", id="t1", name="agent_tools", input={"args": ["overview"]}),
                      block("tool_use", id="t2", name="agent_tools", input={"args": ["texts", "--match", "仓库"]})], "tool_use", (1000, 50, 0, 0)),
            response([block("tool_use", id="t3", name="view_image", input={"path": "a.png"})], "tool_use", (10, 30, 900, 1000)),
            response([block("text", text=ANSWER)], "end_turn", (5, 400, 100, 1900)),
        ])
        ran = []
        with mock.patch.object(api_agent, "run_tool", lambda work, name, data: (ran.append((name, data)) or ("{}", False))):
            got = self.run_api(api, tag="_t")
        self.assertEqual(set(got), RECORD_KEYS)
        self.assertEqual(got["answer"]["buildings"][0]["area_m2"], 3603.18)
        self.assertEqual((got["returncode"], got["tool_calls"], got["model"]), (0, 3, "claude-sonnet-5-5"))
        self.assertEqual([n for n, _ in ran], ["agent_tools", "agent_tools", "view_image"])
        self.assertEqual(got["tokens_detail"], {"input_tokens": 1015, "output_tokens": 480, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2900})
        self.assertAlmostEqual(got["cost_usd_equivalent"], (1015 * 2 + 480 * 10 + 1000 * 2.5 + 2900 * 0.2) / 1e6, places=4)
        first = api.requests[0]
        self.assertEqual(first["route"], "beta", "server-side fallback on the Claude API")
        self.assertEqual((first["fallbacks"], first["betas"]), ("default", [api_agent.FALLBACK_BETA]))
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual(first["thinking"], {"type": "adaptive"})
        self.assertTrue(first["system"].startswith(agent_reader.UNATTENDED_NOTE))
        self.assertEqual([t["name"] for t in first["tools"]], ["agent_tools", "view_image"])
        second = api.requests[1]["messages"]
        self.assertEqual(second[1]["content"][0].type, "thinking", "the assistant turn goes back whole, thinking included")
        self.assertEqual([r["tool_use_id"] for r in second[2]["content"]], ["t1", "t2"], "one turn's results in one message")
        usage = json.loads((self.work / "usage_t.json").read_text(encoding="utf-8"))
        self.assertEqual((usage["turns"], usage["peak_context_tokens"], usage["error"]), (3, 2005, None))
        for name in ("events_t.txt", "stderr_t.txt", "answer_t.txt"):
            self.assertTrue((self.work / name).is_file(), name)

    def test_a_relay_gets_plain_requests_and_the_model_can_be_chosen(self):
        api = FakeAPI([response([block("text", text=ANSWER)])])
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://relay.example", "CTLE_API_MODEL": "claude-opus-5-5"}):
            got = self.run_api(api)
        self.assertEqual(api.requests[0]["route"], "messages")
        self.assertNotIn("fallbacks", api.requests[0])
        self.assertEqual((got["model"], api.requests[0]["model"]), ("claude-opus-5-5", "claude-opus-5-5"))
        self.assertEqual(api_agent.api_model("opus"), "claude-opus-5-5")
        self.assertEqual((api_agent.api_model("sonnet"), api_agent.api_model(None), api_agent.api_model("x-model")),
                         ("claude-sonnet-5-5", "claude-sonnet-5-5", "x-model"))
        self.assertIsNone(api_agent.cost_usd("x-model", {"input_tokens": 1}), "no price claimed for an unknown model")

    def test_failures_are_failed_calls_not_crashes(self):
        cases = [
            ([RuntimeError("APIConnectionError: network down")], {}, "network down"),
            ([response([block("text", text="")], "refusal")], {}, "refusal"),
            ([response([block("tool_use", id="t", name="agent_tools", input={"args": ["overview"]})], "max_tokens")], {}, "max_tokens"),
            ([], {"started": 0.0}, "[timeout after 60 s]"),
            ([response([block("tool_use", id="t", name="agent_tools", input={"args": ["overview"]})], "tool_use", (900_000, 1, 0, 0))], {}, "context"),
        ]
        for i, (replies, kwargs, expected) in enumerate(cases):
            with mock.patch.object(api_agent, "run_tool", lambda *a: ("{}", False)):
                got = self.run_api(FakeAPI(replies), tag=f"_{i}", **kwargs)
            self.assertEqual((got["returncode"], got["answer"]), (-1, None), expected)
            self.assertIn(expected, (self.work / f"stderr_{i}.txt").read_text(encoding="utf-8"))
            self.assertIn(expected, json.loads((self.work / f"usage_{i}.json").read_text(encoding="utf-8"))["error"])

    def test_the_answer_given_before_a_closing_remark_is_kept(self):
        api = FakeAPI([response([block("text", text=ANSWER), block("tool_use", id="t", name="agent_tools", input={"args": ["overview"]})], "tool_use"),
                       response([block("text", text="完成。")])])
        with mock.patch.object(api_agent, "run_tool", lambda *a: ("{}", False)):
            got = self.run_api(api)
        self.assertTrue(got["answer_from_earlier_message"])
        self.assertEqual(got["answer"]["buildings"][0]["name"], "A")

    def test_the_model_can_only_use_the_drawing_tools_inside_the_work_directory(self):
        def refused(name, data):
            content, is_error = api_agent.run_tool(self.work, name, data)
            self.assertTrue(is_error, (name, data))
            return content

        self.assertIn("not available", refused("agent_tools", {"args": ["index", "C:/secret.dxf"]}))
        self.assertIn("list of strings", refused("agent_tools", {"args": "overview; del *"}))
        self.assertIn("inside the work directory", refused("agent_tools", {"args": ["render", "0", "0", "1", "1", "../../x.png"]}))
        self.assertIn("inside the work directory", refused("agent_tools", {"args": ["render", "0", "0", "1", "1", "C:/x.png"]}))
        refused("view_image", {"path": "../outside.png"})
        refused("view_image", {"path": "missing.png"})
        refused("bash", {"command": "dir"})

        seen = []
        def fake_cli(command, **kwargs):
            seen.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "x" * (api_agent.TOOL_OUTPUT_MAX_CHARS + 10), "")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), mock.patch.object(agent_reader, "run_cli", fake_cli):
            content, is_error = api_agent.run_tool(self.work, "agent_tools", {"args": ["overview"]})
        command, kwargs = seen[0]
        self.assertEqual(command[-2:], ["cad_to_layout.zero_manual.agent_tools", "overview"])
        self.assertEqual(Path(kwargs["cwd"]), self.work)
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"], "the tools never see the key")
        self.assertFalse(is_error)
        self.assertIn("output cut", content)

        from PIL import Image

        Image.new("RGB", (4000, 1000), "white").save(self.work / "big.png")
        content, is_error = api_agent.run_tool(self.work, "view_image", {"path": "big.png"})
        self.assertFalse(is_error)
        self.assertEqual(content[0]["type"], "image")
        import base64, io

        shown = Image.open(io.BytesIO(base64.b64decode(content[0]["source"]["data"])))
        self.assertEqual(max(shown.size), api_agent.IMAGE_MAX_EDGE_PX)

    def test_the_equipment_judge_on_the_api_records_usage_and_failures(self):
        from PIL import Image

        image = self.work / "tile.png"
        Image.new("RGB", (64, 64), "white").save(image)
        api = FakeAPI([response([block("text", text='{"drawing_count": 1, "cut_ids": []}')], usage=(1500, 60, 0, 0)),
                       RuntimeError("RateLimitError: 429")])
        transport = api_agent.api_transport(api=api)
        raw = transport("PROMPT", image, self.work / "call-1")
        self.assertEqual((raw["provider"], raw["model"], raw["error"], raw["tokens_used"]), ("anthropic-api", "claude-sonnet-5-5", None, 1560))
        sent = api.requests[0]["messages"][0]["content"]
        self.assertEqual([c["type"] for c in sent], ["image", "text"])
        self.assertNotIn("tools", api.requests[0])
        self.assertEqual(json.loads((self.work / "call-1" / "usage.json").read_text(encoding="utf-8"))["usage"]["input_tokens"], 1500)
        failed = transport("PROMPT", image, self.work / "call-2")
        self.assertEqual((failed["returncode"], failed["text"]), (-1, ""))
        self.assertIn("429", failed["error"])
        result = llm_judge.judge_split(image=image, work_dir=self.work / "judge", transport=lambda *a: failed, allow_external=True)
        self.assertEqual(result.status, "invalid")

    def test_one_switch_routes_the_reader_the_adjudicator_and_the_judge(self):
        with mock.patch.object(agent_reader, "AGENT_BACKEND", "api"):
            self.assertIn("api_transport", llm_judge.external_transport().__qualname__)
            with mock.patch.object(api_agent, "run_api", return_value={"routed": True}) as run:
                self.assertEqual(agent_reader.run_prompt(self.work, "PROMPT", tag="_a", answer_key="verdicts", model="sonnet"), {"routed": True})
            self.assertEqual(run.call_args.args[4:], ("_a", "verdicts", "sonnet"))

    def test_the_subscription_runs_never_see_an_api_key(self):
        seen = []
        def capture(command, **kwargs):
            seen.append(kwargs.get("env"))
            return subprocess.CompletedProcess(command, 0, json.dumps({"type": "result", "result": "{}", "usage": {}}), "")
        image = self.work / "tile.png"
        image.write_bytes(b"png")
        (self.work / "call").mkdir()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "tok"}), \
                mock.patch.object(agent_reader, "AGENT_BACKEND", "claude"), mock.patch.object(agent_reader.subprocess, "run", capture):
            agent_reader.run_prompt(self.work, "PROMPT")
            llm_judge.claude_exec_transport()("PROMPT", image, self.work / "call")
        self.assertEqual(len(seen), 2)
        for env in seen:
            self.assertIsNotNone(env)
            self.assertFalse({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} & set(env))


if __name__ == "__main__":
    unittest.main()
