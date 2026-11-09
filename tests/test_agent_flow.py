"""End-to-end team runs against the scripted offline model server.

These prove the whole thing works with no cloud, no downloads and no keys:
planner -> coder -> reviewer -> lead, with real files written to a temp dir.
"""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.orchestrator import Team, extract_json
from offgrid.providers import LocalProvider
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

PLAN = {
    "summary": "create a greeting module",
    "steps": [
        {
            "id": 1,
            "title": "create hello.py",
            "agent": "coder",
            "task": "Create hello.py with greet() returning 'hi'",
            "files": ["hello.py"],
            "depends_on": [],
        }
    ],
    "verify": "",
}
APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
CHANGES = '{"verdict": "changes", "issues": ["greet() is missing"], "instructions": "add greet()"}'
HELLO = "def greet():\n    return 'hi'\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = None

    def tearDown(self):
        if self.model:
            self.model.stop()
        self.tmp.cleanup()

    def team(self, rules, **config_overrides) -> Team:
        self.model = serve(rules)
        config = Config(
            **{
                "endpoint": self.model.url,
                "model": "fake",
                "command_policy": "auto",
                "color": False,
                "verbose": 0,
                "workspace": str(self.root),
                # These tests pin orchestration mechanics, not deliberation.
                "effort": "fast",
                "scale": False,
                **config_overrides,
            }
        )
        provider = LocalProvider(config)
        ui = UI(config)
        return Team(provider, Workspace(str(self.root)), config, ui)


class HappyPathTests(Harness):
    def test_plan_code_review_report(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content=HELLO)),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("Created hello.py with greet().")),
            ]
        )
        result = team.run("create a greeting module")

        self.assertEqual((self.root / "hello.py").read_text(), HELLO)
        self.assertEqual(result.changed, ["hello.py"])
        self.assertEqual(result.review_verdict, "approve")
        self.assertIsNone(result.verify_ok)  # no test command in a bare temp dir
        self.assertIn("hello.py", result.summary)
        self.assertEqual([run.agent for run in result.runs], ["coder"])
        self.assertEqual(result.stats.model_calls, 5)
        self.assertTrue(result.notes)
        self.assertIn("+def greet", result.diff)

    def test_team_prompts_ignore_a_forced_principal_override(self):
        self.model = serve(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content=HELLO)),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("Created hello.py with greet().")),
            ]
        )
        config = Config(
            endpoint=self.model.url,
            model="fake",
            command_policy="auto",
            color=False,
            verbose=0,
            workspace=str(self.root),
            effort="fast",
            scale=False,
        )
        # Simulate a stale or hostile config path that bypasses Config.__setattr__.
        object.__setattr__(config, "principal", "Mallory")
        team = Team(LocalProvider(config), Workspace(str(self.root)), config, UI(config))
        result = team.run("create a greeting module")

        self.assertEqual(result.review_verdict, "approve")
        self.assertTrue((self.root / "hello.py").is_file())
        system_prompts = [
            message["content"]
            for request in self.model.requests
            for message in request.get("messages") or []
            if message.get("role") == "system"
        ]
        self.assertEqual(len(system_prompts), len(self.model.requests))
        self.assertTrue(system_prompts)
        for prompt in system_prompts:
            self.assertTrue(
                prompt.lstrip().startswith("WHOSE SIDE THIS SOFTWARE IS ON")
            )
            self.assertIn("The principal is EarthAngel", prompt)
            self.assertNotIn("Mallory", prompt)

    def test_individual_agents_had_their_own_turns(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="x.py", content="x = 1\n")),
                when("coder", tool("finish", summary="wrote x.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ]
        )
        result = team.run("write x.py")
        coder = result.runs[0]
        self.assertEqual(coder.turns, 2)
        self.assertEqual(coder.tool_calls, 2)
        self.assertTrue(coder.finished)

    def test_planner_failure_falls_back_to_one_step(self):
        team = self.team(
            [
                when("planner", text("I cannot make a plan, sorry.")),
                when("coder", tool("write_file", path="solo.py", content="y = 2\n")),
                when("coder", tool("finish", summary="wrote solo.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ]
        )
        result = team.run("just write solo.py")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].agent, "coder")
        self.assertTrue((self.root / "solo.py").exists())

    def test_planner_can_assign_unknown_roles(self):
        plan = json.loads(json.dumps(PLAN))
        plan["steps"][0]["agent"] = "implementer"
        team = self.team(
            [
                when("planner", text(json.dumps(plan))),
                when("coder", tool("write_file", path="z.py", content="z = 3\n")),
                when("coder", tool("finish", summary="wrote z.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ]
        )
        result = team.run("write z.py")
        self.assertEqual(result.runs[0].agent, "coder")
        self.assertTrue((self.root / "z.py").exists())


class ReviewLoopTests(Harness):
    def test_reviewer_requests_changes_then_approves(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="def hello():\n    pass\n")),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(CHANGES)),
                when("coder", tool("edit_file", path="hello.py", old="pass", new="return 'hi'")),
                when("coder", tool("finish", summary="added greet body")),
                when("reviewer", text(APPROVE)),
                when("lead", text("Fixed greet() and it passes review.")),
            ]
        )
        result = team.run("create a greeting module")

        self.assertEqual(result.review_verdict, "approve")
        fixed = (self.root / "hello.py").read_text()
        self.assertIn("return 'hi'", fixed)
        self.assertNotIn("pass", fixed)
        self.assertEqual(len(result.runs), 2)  # original step + review fix

    def test_review_rounds_are_bounded(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created")),
                when("reviewer", text(CHANGES)),
                when("coder", tool("finish", summary="tried again")),
                when("reviewer", text(CHANGES)),
            ],
            review_rounds=1,
        )
        result = team.run("create a greeting module")
        self.assertEqual(result.review_verdict, "changes")
        self.assertEqual(len(result.runs), 2)


class VerifyTests(Harness):
    def test_detected_verify_command_runs(self):
        (self.root / "tests").mkdir()
        (self.root / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n")
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content=HELLO)),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ],
            verify_command="python3 -m unittest discover -q",
        )
        result = team.run("create a greeting module")
        self.assertTrue(result.verify_ok)
        self.assertIn("unittest", result.verify_command)

    def test_failing_verification_triggers_fix(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created")),
                when("reviewer", text(APPROVE)),
                when("coder", tool("write_file", path="hello.py", content="x = 2\n")),
                when("coder", tool("finish", summary="changed to 2")),
                when("lead", text("done but tests still fail")),
            ],
            verify_command="python3 -c \"import sys; sys.exit(1)\"",
            test_fix_rounds=1,
        )
        result = team.run("create a greeting module")
        self.assertFalse(result.verify_ok)
        self.assertEqual(len(result.runs), 2)  # step + fix attempt

    def test_passing_verification_no_fix(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content=HELLO)),
                when("coder", tool("finish", summary="created")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ],
            verify_command="python3 -c \"print('ok')\"",
        )
        result = team.run("create a greeting module")
        self.assertTrue(result.verify_ok)
        self.assertEqual(len(result.runs), 1)


WRITE_BLOCK = '```tool\n{"tool": "write_file", "args": {"path": "t.py", "content": "x = 1\\n"}}\n```'
FINISH_BLOCK = '```tool\n{"tool": "finish", "args": {"summary": "wrote t.py"}}\n```'


class TextProtocolTests(Harness):
    """A server that rejects native tool calling entirely."""

    def test_whole_team_runs_without_native_tool_support(self):
        self.model = serve(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(WRITE_BLOCK)),
                when("coder", text(FINISH_BLOCK)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Wrote t.py.")),
            ],
            reject_tools=True,
        )
        config = Config(
            endpoint=self.model.url,
            model="fake",
            command_policy="auto",
            color=False,
            verbose=0,
            workspace=str(self.root),
        )
        provider = LocalProvider(config)
        team = Team(provider, Workspace(str(self.root)), config, UI(config))
        result = team.run("write t.py")
        self.assertTrue((self.root / "t.py").exists())
        self.assertEqual((self.root / "t.py").read_text(), "x = 1\n")
        self.assertFalse(provider.native_tools)
        self.assertEqual(result.review_verdict, "approve")


class ParallelTests(Harness):
    def test_independent_steps_run_in_parallel(self):
        plan = {
            "summary": "two modules",
            "steps": [
                {"id": 1, "title": "a", "agent": "coder", "task": "write a.py", "files": ["a.py"], "depends_on": []},
                {"id": 2, "title": "b", "agent": "coder", "task": "write b.py", "files": ["b.py"], "depends_on": []},
            ],
            "verify": "",
        }
        team = self.team(
            [
                when("planner", text(json.dumps(plan))),
                when("coder", tool("write_file", path="a.py", content="a = 1\n")),
                when("coder", tool("finish", summary="wrote a.py")),
                when("coder", tool("write_file", path="b.py", content="b = 2\n")),
                when("coder", tool("finish", summary="wrote b.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ],
            parallel=2,
        )
        result = team.run("write two modules")
        self.assertTrue((self.root / "a.py").exists())
        self.assertTrue((self.root / "b.py").exists())
        self.assertEqual(len(result.runs), 2)
        self.assertEqual(len(Team._waves([s for s in result.steps])), 1)


class JsonTests(unittest.TestCase):
    def test_extract_json_handles_fences(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')["a"], 1)

    def test_extract_json_handles_prose(self):
        data = extract_json('Sure! {"verdict": "approve", "issues": []} hope that helps')
        self.assertEqual(data["verdict"], "approve")

    def test_extract_json_handles_nested(self):
        data = extract_json('{"steps": [{"id": 1, "files": ["a"]}], "verify": "pytest"}')
        self.assertEqual(data["steps"][0]["files"], ["a"])

    def test_extract_json_returns_none_for_garbage(self):
        self.assertIsNone(extract_json("no json here at all"))


if __name__ == "__main__":
    unittest.main()
