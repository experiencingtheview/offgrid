"""A `best`-effort run actually deliberates: candidates, critic, gates, escalation."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.orchestrator import Team
from offgrid.providers import LocalProvider
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
CHANGES = '{"verdict": "changes", "issues": ["silent failure path"], "instructions": "handle the error"}'
FAILING = "python3 -c \"import sys; sys.exit(1)\""


def plan(summary="write a.py", agent="coder", task="Write a.py", files=("a.py",)):
    return {
        "summary": summary,
        "steps": [
            {
                "id": 1,
                "title": task,
                "agent": agent,
                "task": task,
                "files": list(files),
                "depends_on": [],
            }
        ],
        "verify": "",
    }


def write_and_finish(path="a.py", content="a = 1\n", summary="done"):
    return [when("coder", tool("write_file", path=path, content=content)), when("coder", tool("finish", summary=summary))]


class BestEffortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = None

    def tearDown(self):
        if self.model:
            self.model.stop()
        self.tmp.cleanup()

    def team(self, rules, **overrides):
        self.model = serve(rules)
        config = Config(
            **{
                "endpoint": self.model.url,
                "model": "fake",
                "command_policy": "auto",
                "color": False,
                "verbose": 0,
                "workspace": str(self.root),
                "scale": False,
                **overrides,
            }
        )
        return Team(LocalProvider(config), Workspace(str(self.root)), config, UI(config))

    def test_two_plans_are_drafted_and_the_critic_chooses(self):
        rules = [
            when("planner", text(json.dumps(plan("first idea")))),
            when("planner", text(json.dumps(plan("second idea")))),
            when("critic", text(json.dumps(plan("merged best idea")))),
            *write_and_finish(),
            when("reviewer", text(APPROVE)),
            when("reviewer", text(APPROVE)),
            when("lead", text("done")),
        ]
        result = self.team(rules).run("write a.py")

        self.assertEqual(result.effort, "best")
        self.assertEqual(result.plan_candidates, ["first idea", "second idea"])
        self.assertTrue(
            any("You are critic" in json.dumps(request["messages"]) for request in self.model.requests)
        )
        self.assertTrue((self.root / "a.py").exists())

    def test_mechanical_gate_sends_truncated_output_back(self):
        rules = [
            when("planner", text(json.dumps(plan()))),
            when("coder", tool("write_file", path="a.py", content="def f():\n    # rest of the code here\n")),
            when("coder", tool("finish", summary="created stub")),
            when("coder", tool("write_file", path="a.py", content="def f():\n    return 1\n")),
            when("coder", tool("finish", summary="finished f")),
            when("reviewer", text(APPROVE)),
            when("lead", text("done")),
        ]
        result = self.team(rules, plan_candidates=1, review_passes=1).run("write f")

        self.assertEqual(result.quality_issues, [])
        self.assertIn("return 1", (self.root / "a.py").read_text())
        self.assertEqual(len(result.runs), 2)  # the step plus the mechanical fix

    def test_second_adversarial_pass_can_block_approval(self):
        rules = [
            when("planner", text(json.dumps(plan()))),
            *write_and_finish(),
            when("reviewer", text(APPROVE)),
            when("reviewer", text(CHANGES)),
            when("lead", text("not approved")),
        ]
        result = self.team(rules, plan_candidates=1, review_rounds=0).run("write a.py")

        self.assertEqual(result.review_verdict, "changes")
        self.assertIn("silent failure path", result.review_issues)
        reviewer_calls = [
            request for request in self.model.requests
            if "You are reviewer" in json.dumps(request["messages"][0].get("content", ""))
        ]
        self.assertEqual(len(reviewer_calls), 2)

    def test_escalation_runs_a_stronger_attempt_after_failure(self):
        rules = [
            when("planner", text(json.dumps(plan()))),
            *write_and_finish(),
            when("reviewer", text(APPROVE)),
            *write_and_finish(content="a = 2\n", summary="tried again"),
            *write_and_finish(content="a = 3\n", summary="reworked the approach"),
            when("lead", text("still failing, here is what is missing")),
        ]
        result = self.team(rules, plan_candidates=1, review_passes=1, test_fix_rounds=1, verify_command=FAILING).run(
            "write a.py"
        )

        self.assertTrue(result.escalated)
        self.assertFalse(result.verify_ok)
        self.assertEqual(len(result.runs), 3)  # step, test fix, escalation
        self.assertIn("a = 3", (self.root / "a.py").read_text())

    def test_fast_profile_skips_all_deliberation(self):
        rules = [
            when("planner", text(json.dumps(plan()))),
            *write_and_finish(),
            when("reviewer", text(APPROVE)),
            when("lead", text("done")),
        ]
        result = self.team(rules, effort="fast").run("write a.py")
        self.assertEqual(result.effort, "fast")
        self.assertEqual(result.stats.model_calls, 5)
        self.assertFalse(result.escalated)


if __name__ == "__main__":
    unittest.main()
