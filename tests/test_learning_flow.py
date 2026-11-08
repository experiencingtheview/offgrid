"""Memory and self-learning wired into a real team run."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.memory import MemoryStore
from offgrid.orchestrator import Team
from offgrid.providers import LocalProvider
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

PLAN = {
    "summary": "create hello.py",
    "steps": [
        {
            "id": 1,
            "title": "create hello.py",
            "agent": "coder",
            "task": "Create hello.py",
            "files": ["hello.py"],
            "depends_on": [],
        }
    ],
    "verify": "",
}
APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
LEARNING = '{"lessons": [{"text": "This repository verifies with make test", "kind": "lesson", "confidence": 0.7}]}'


class LearningFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.model = None

    def tearDown(self):
        if self.model:
            self.model.stop()
        self.memory.close()
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
                "effort": "fast",
                "scale": False,
                **overrides,
            }
        )
        provider = LocalProvider(config)
        return Team(provider, Workspace(str(self.root)), config, UI(config), memory=self.memory)

    def test_past_notes_reach_the_planner(self):
        self.memory.remember("This project verifies with make test", kind="repo")
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
                when("archivist", text(LEARNING)),
            ],
            verify_command="python3 -c pass",
        )
        team.run("create hello.py")
        planner_prompt = json.dumps(self.model.requests[0]["messages"])
        self.assertIn("This project verifies with make test", planner_prompt)
        self.assertIn("WHAT YOU ALREADY KNOW", planner_prompt)

    def test_new_lessons_wait_for_review_before_they_count(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
                when("archivist", text(LEARNING)),
            ],
            verify_command="python3 -c pass",
        )
        result = team.run("create hello.py")

        self.assertTrue(result.lessons)
        self.assertGreaterEqual(len(result.pending_lessons), 2)
        self.assertEqual(
            [item["id"] for item in result.pending_lessons],
            [item.id for item in self.memory.pending()],
        )

        # Nothing the run just learned may reach a prompt before a human says so.
        self.assertEqual(self.memory.all(), [])
        self.assertEqual(self.memory.prompt_block("verify this project"), "")
        self.assertTrue(all(item.status == "pending" for item in self.memory.pending()))

        # Accepting is what makes it knowledge.
        self.memory.accept([item["id"] for item in result.pending_lessons])
        stored = [item.text for item in self.memory.all()]
        self.assertTrue(any("make test" in text_ for text_ in stored))
        self.assertTrue(any("python3 -c pass" in text_ for text_ in stored))
        self.assertIn("make test", self.memory.prompt_block("repository verifies make test"))

    def test_a_rejected_lesson_never_survives(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
                when("archivist", text(LEARNING)),
            ],
            verify_command="python3 -c pass",
        )
        result = team.run("create hello.py")
        ids = [item["id"] for item in result.pending_lessons]
        self.assertTrue(ids)
        self.assertEqual(self.memory.reject(ids), len(ids))
        self.assertEqual(self.memory.pending(), [])
        self.assertEqual(self.memory.all(), [])

    def test_remembered_verify_command_is_reused_without_flag(self):
        self.memory.remember(
            "Verification command for this project: `make check` (it passed).",
            kind="repo",
            key="verify-command",
            confidence=0.9,
        )
        team = self.team([], verify_command="")
        self.assertEqual(team.detect_verify_command(), "make check")

    def test_memory_disabled_means_no_learning(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", tool("write_file", path="hello.py", content="x = 1\n")),
                when("coder", tool("finish", summary="created hello.py")),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ],
            verify_command="python3 -c pass",
            memory=False,
            reflect=False,
        )
        result = team.run("create hello.py")
        self.assertEqual(result.lessons, [])
        self.assertEqual(self.memory.all(), [])


if __name__ == "__main__":
    unittest.main()
