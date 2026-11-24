import itertools
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from jarvis import builder
from jarvis.builder import AGENT_SYSTEM, Builder, Builds, Limits, Sandbox
from jarvis.tools.registry import Registry

PYTHON = f'"{Path(sys.executable).as_posix()}"'
_ids = itertools.count(1)


def call(name, **args):
    return NS(type="tool_use", id=f"t{next(_ids)}", name=name, input=args)


class FakeClient:
    """Stands in for the Anthropic client: scripted agent steps and reviewer verdicts."""

    def __init__(self, agent_steps, verdicts, tokens=10):
        self.agent_steps, self.verdicts, self.tokens = iter(agent_steps), iter(verdicts), tokens
        self.messages = self
        self.reviews = 0

    def create(self, **kw):
        usage = NS(input_tokens=self.tokens, output_tokens=self.tokens)
        if kw["system"] == AGENT_SYSTEM:
            return NS(content=next(self.agent_steps), usage=usage, stop_reason="tool_use")
        self.reviews += 1
        return NS(content=[NS(type="text", text=json.dumps(next(self.verdicts)))], usage=usage, stop_reason="end_turn")


def passing(met=True, score=9, issues=()):
    return {"met": met, "score": score, "issues": list(issues), "improvements": []}


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.box = Sandbox(Path(self.dir.name) / "proj", use_docker=False)

    def test_files_stay_inside_the_project(self):
        self.box.write("src/app.py", "print(1)")
        self.assertEqual(self.box.read("src/app.py"), "print(1)")
        for bad in ("../escape.txt", "..\\escape.txt", "C:/Windows/win.ini"):
            with self.assertRaises(PermissionError):
                self.box.write(bad, "x")

    def test_secret_files_are_refused_even_inside(self):
        with self.assertRaises(PermissionError):
            self.box.write(".env", "KEY=1")

    def test_dangerous_commands_are_refused_in_local_mode(self):
        for command in ("rm -rf .", "powershell -c dir", "python a.py && del x", "python a.py | more",
                        "git push origin main", "git remote add x y", "python ../other.py", "npm publish",
                        "python C:/Windows/system32/x.py", "curl http://x"):
            with self.assertRaises(PermissionError, msg=command):
                self.box.check_command(command)

    def test_plain_commands_are_allowed(self):
        self.assertTrue(self.box.check_command(f"{PYTHON} -c \"print(1)\""))
        self.assertTrue(self.box.check_command("git status"))

    def test_the_environment_has_no_keys(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_x", "MY_TOKEN": "t", "PATH": os.environ.get("PATH", "")}):
            env = self.box._clean_env()
        self.assertNotIn("GROQ_API_KEY", env)
        self.assertNotIn("MY_TOKEN", env)
        self.assertIn("PATH", env)

    def test_run_returns_exit_code_and_output(self):
        code, out = self.box.run(f"{PYTHON} -c \"print('hello')\"")
        self.assertEqual(code, 0)
        self.assertIn("hello", out)
        code, _ = self.box.run(f"{PYTHON} -c \"raise SystemExit(3)\"")
        self.assertEqual(code, 3)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.box = Sandbox(Path(self.dir.name) / "proj", use_docker=False)
        self.test_cmd = f"{PYTHON} test_app.py"

    def test_it_keeps_going_until_tests_pass_and_the_review_is_satisfied(self):
        round1 = [[call("write_file", path="test_app.py", content="assert 1 == 2")],
                  [call("finish", summary="first attempt", test_command=self.test_cmd)]]
        round2 = [[call("write_file", path="test_app.py", content="assert 1 == 1")],
                  [call("finish", summary="fixed it")]]
        client = FakeClient(round1 + round2, [passing(False, 4, ["the logic is wrong"]), passing()])
        result = Builder(self.box, client=client, model="m", limits=Limits(rounds=5, tokens=10**6, minutes=5)).build(
            "make it work", "1 == 1 holds")
        self.assertTrue(result.met)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(client.reviews, 2)
        self.assertIn("exit code 0", result.tests)
        self.assertTrue((self.box.root / ".jay" / "build_log.md").exists())

    def test_it_never_trusts_a_happy_reviewer_when_tests_fail(self):
        steps = [[call("write_file", path="test_app.py", content="assert False")],
                 [call("finish", summary="done", test_command=self.test_cmd)]]
        client = FakeClient(steps * 2, [passing(), passing()])
        result = Builder(self.box, client=client, model="m", limits=Limits(rounds=2, tokens=10**6, minutes=5)).build("x")
        self.assertFalse(result.met)
        self.assertEqual(result.reason, "round limit reached")
        self.assertEqual(result.rounds, 2)

    def test_it_stops_at_the_token_budget(self):
        steps = [[call("write_file", path="a.txt", content="x")], [call("finish", summary="s")]] * 5
        client = FakeClient(steps, [passing(False, 3, ["more"])] * 5, tokens=600)
        result = Builder(self.box, client=client, model="m", limits=Limits(rounds=5, tokens=1000, minutes=5)).build("x")
        self.assertFalse(result.met)
        self.assertIn("token budget", result.reason)

    def test_agent_actions_outside_the_project_are_refused_not_run(self):
        steps = [[call("write_file", path="../evil.txt", content="x"), call("run", command="rm -rf /")],
                 [call("finish", summary="s")]]
        client = FakeClient(steps, [passing(False, 1, ["x"])])
        Builder(self.box, client=client, model="m", limits=Limits(rounds=1, tokens=10**6, minutes=5)).build("x")
        self.assertFalse((self.box.root.parent / "evil.txt").exists())


class BuildsTests(unittest.TestCase):
    def test_finished_build_is_reported_to_the_phone_and_desktop(self):
        with tempfile.TemporaryDirectory() as d:
            phone = mock.Mock()
            phone.available.return_value = True
            told = []
            builds = Builds(Path(d), phone=phone, notify=lambda t, x: told.append(x),
                            client=FakeClient([[call("write_file", path="test_app.py", content="assert True")],
                                               [call("finish", summary="done", test_command=f"{PYTHON} test_app.py")]],
                                              [passing()]))
            with mock.patch("jarvis.builder.docker_ready", return_value=False):
                message = builds.start("My App!", "build it", "it works")
                self.assertIn("my-app", message)
                for _ in range(100):
                    if builds.jobs["my-app"]["status"] != "running":
                        break
                    threading.Event().wait(0.1)
            self.assertEqual(builds.jobs["my-app"]["status"], "met")
            self.assertTrue(any("passed its tests" in t for t in told))
            phone.send.assert_called()
            phone.send_voice.assert_called()

    def test_tools_are_registered_and_building_needs_a_yes(self):
        reg = Registry()
        builder.register(reg, mock.Mock())
        self.assertTrue(reg.needs_approval("build_app", {"name": "x", "goal": "y"}))
        self.assertFalse(reg.needs_approval("build_status", {}))


if __name__ == "__main__":
    unittest.main()
