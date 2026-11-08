"""Agents consult the council automatically instead of stopping at "I don't know"."""

import json
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.council import Answer, CouncilAnswer, CouncilUnavailable
from offgrid.memory import MemoryStore
from offgrid.orchestrator import Team
from offgrid.providers import LocalProvider
from offgrid.session import SessionLog
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

PLAN = {
    "summary": "write hello.py",
    "steps": [
        {
            "id": 1,
            "title": "write hello.py",
            "agent": "coder",
            "task": "Write hello.py",
            "files": ["hello.py"],
            "depends_on": [],
        }
    ],
    "verify": "",
}
APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
IGNORANT = "I don't know which flag enables JSON mode in this server."
ANSWERED = "Use `--jinja` when starting llama-server."


class StubCouncil:
    """Same surface as Council, but no network and no waiting."""

    def __init__(self, answer=ANSWERED, available=True, raises=None, answers=None):
        self.answer = answer
        self._available = available
        self.raises = raises
        self.answers = list(answers) if answers is not None else None
        self.questions = []

    def available(self):
        return self._available

    def ask(self, question, context=""):
        self.questions.append(question)
        if self.raises:
            raise self.raises
        if self.answers is not None:
            return CouncilAnswer(question=question, answers=list(self.answers))
        return CouncilAnswer(
            question=question,
            answers=[Answer(member="peer", text=self.answer)],
        )


class CouncilFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = None

    def tearDown(self):
        if self.model:
            self.model.stop()
        self.tmp.cleanup()

    def team(self, rules, council, memory=None, **overrides):
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
                "council": True,
                "council_auto": True,
                "council_auto_limit": 1,
                **overrides,
            }
        )
        return Team(
            LocalProvider(config),
            Workspace(str(self.root)),
            config,
            UI(config),
            memory=memory,
            council=council,
        )

    def test_ignorance_triggers_a_consult_and_the_answer_is_recorded(self):
        council = StubCouncil()
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("coder", text(ANSWERED)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Used the council's suggestion.")),
            ],
            council,
        )
        result = team.run("add JSON mode")

        self.assertEqual(len(result.council), 1)
        record = result.council[0]
        self.assertEqual(record["agent"], "coder")
        self.assertEqual(record["members"], ["peer"])
        self.assertIn("jinja", record["answer"])
        self.assertFalse(record["disagreement"])
        self.assertIn("don't know", council.questions[0])
        self.assertIn("--jinja", result.runs[0].text)
        self.assertEqual(len(result.as_dict()["council"]), 1)

    def test_verified_council_answer_is_offered_as_a_pending_council_lesson(self):
        memory = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(memory.close)
        council = StubCouncil()
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("coder", text(ANSWERED)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Used the council's suggestion.")),
                when("archivist", text('{"lessons": []}')),
            ],
            council,
            memory=memory,
            verify_command="python3 -c pass",
        )
        result = team.run("add JSON mode")

        self.assertTrue(result.verify_ok)
        council_lessons = [
            item for item in result.pending_lessons if item["source"].startswith("council")
        ]
        self.assertEqual(len(council_lessons), 1)
        self.assertIn("--jinja", council_lessons[0]["text"])
        self.assertEqual(council_lessons[0]["verify_command"], "python3 -c pass")
        item = memory.get(council_lessons[0]["id"])
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.source, "council: peer")
        self.assertEqual(item.verify_command, "python3 -c pass")
        self.assertTrue(item.evidence_lines())

    def test_manual_ask_council_is_recorded_and_can_become_a_pending_lesson(self):
        memory = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(memory.close)
        council = StubCouncil()
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when(
                    "coder",
                    tool("ask_council", question="Which flag enables JSON mode?"),
                ),
                when(
                    "coder",
                    tool("write_file", path="json_mode.py", content="FLAG = '--jinja'\n"),
                ),
                when("coder", tool("finish", summary="used the council's answer")),
                when("reviewer", text(APPROVE)),
                when("lead", text("Added JSON mode.")),
                when("archivist", text('{"lessons": []}')),
            ],
            council,
            memory=memory,
            verify_command="python3 -c pass",
        )
        result = team.run("add JSON mode")

        self.assertTrue(result.verify_ok)
        self.assertEqual(len(council.questions), 1)
        self.assertEqual(len(result.council), 1)
        record = result.council[0]
        self.assertEqual(record["agent"], "coder")
        self.assertEqual(record["trigger"], "manual")
        self.assertEqual(record["question"], "Which flag enables JSON mode?")
        self.assertIn("--jinja", record["answer"])
        self.assertEqual(len(result.runs[0].council), 1)

        council_lessons = [
            item for item in result.pending_lessons if item["source"].startswith("council")
        ]
        self.assertEqual(len(council_lessons), 1)
        self.assertIn("--jinja", council_lessons[0]["text"])
        self.assertEqual(council_lessons[0]["verify_command"], "python3 -c pass")
        item = memory.get(council_lessons[0]["id"])
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.source, "council: peer")
        self.assertIn("verified with", "\n".join(item.evidence_lines()))

    def test_a_disputed_manual_council_answer_never_becomes_a_pending_lesson(self):
        memory = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(memory.close)
        council = StubCouncil(
            answers=[
                Answer(member="peer", text=ANSWERED),
                Answer(
                    member="skeptic",
                    text="I don't know; check the server version first.",
                ),
            ]
        )
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when(
                    "coder",
                    tool("ask_council", question="Which flag enables JSON mode?"),
                ),
                when(
                    "coder",
                    tool("write_file", path="json_mode.py", content="FLAG = '--jinja'\n"),
                ),
                when("coder", tool("finish", summary="noted the disputed answer")),
                when("reviewer", text(APPROVE)),
                when("lead", text("Added JSON mode, flag choice disputed.")),
                when("archivist", text('{"lessons": []}')),
            ],
            council,
            memory=memory,
            verify_command="python3 -c pass",
        )
        result = team.run("add JSON mode")

        self.assertTrue(result.verify_ok)
        self.assertEqual(len(council.questions), 1)
        self.assertEqual(len(result.council), 1)
        record = result.council[0]
        self.assertEqual(record["trigger"], "manual")
        self.assertTrue(record["disagreement"])
        self.assertIn("do not agree", record["answer"])

        council_lessons = [
            item for item in result.pending_lessons if item["source"].startswith("council")
        ]
        self.assertEqual(council_lessons, [])
        self.assertEqual(
            [item for item in memory.pending() if item.source.startswith("council")],
            [],
        )
        self.assertFalse(any("--jinja" in item.text for item in memory.pending()))

    def test_a_disputed_automatic_council_answer_never_becomes_a_pending_lesson(self):
        memory = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(memory.close)
        council = StubCouncil(
            answers=[
                Answer(member="peer", text=ANSWERED),
                Answer(
                    member="skeptic",
                    text="I don't know; check the server version first.",
                ),
            ]
        )
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("coder", text(ANSWERED)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Added JSON mode, flag choice disputed.")),
                when("archivist", text('{"lessons": []}')),
            ],
            council,
            memory=memory,
            verify_command="python3 -c pass",
        )
        result = team.run("add JSON mode")

        self.assertTrue(result.verify_ok)
        self.assertEqual(len(council.questions), 1)
        self.assertEqual(len(result.council), 1)
        record = result.council[0]
        self.assertEqual(record["trigger"], "auto")
        self.assertTrue(record["disagreement"])
        self.assertIn("do not agree", record["answer"])
        self.assertIn("don't know", council.questions[0])

        council_lessons = [
            item for item in result.pending_lessons if item["source"].startswith("council")
        ]
        self.assertEqual(council_lessons, [])
        self.assertEqual(
            [item for item in memory.pending() if item.source.startswith("council")],
            [],
        )
        self.assertFalse(any("--jinja" in item.text for item in memory.pending()))

    def test_the_consult_is_written_to_the_session_log(self):
        council = StubCouncil()
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("coder", text(ANSWERED)),
                when("reviewer", text(APPROVE)),
                when("lead", text("done")),
            ],
            council,
        )
        result = team.run("add JSON mode")
        session = SessionLog(self.root, ".offgrid")
        session.record_run(result)
        runs = SessionLog.load_runs(session.path)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["council"][0]["members"], ["peer"])

    def test_auto_consult_can_be_switched_off(self):
        council = StubCouncil()
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Could not find the flag.")),
            ],
            council,
            council_auto=False,
        )
        result = team.run("add JSON mode")
        self.assertEqual(result.council, [])
        self.assertEqual(council.questions, [])
        self.assertEqual(result.runs[0].text, IGNORANT)

    def test_no_council_means_the_gap_is_admitted_honestly(self):
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Could not find the flag.")),
            ],
            None,
        )
        result = team.run("add JSON mode")
        self.assertEqual(result.council, [])
        self.assertEqual(result.runs[0].text, IGNORANT)

    def test_a_dead_council_does_not_break_the_run(self):
        council = StubCouncil(raises=CouncilUnavailable("no members"))
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("reviewer", text(APPROVE)),
                when("lead", text("Could not find the flag.")),
            ],
            council,
        )
        result = team.run("add JSON mode")
        self.assertEqual(result.council, [])
        self.assertEqual(result.runs[0].text, IGNORANT)

    def test_an_unhelpful_council_is_not_asked_forever(self):
        council = StubCouncil(answer="I don't know either.")
        team = self.team(
            [
                when("planner", text(json.dumps(PLAN))),
                when("coder", text(IGNORANT)),
                when("coder", text(IGNORANT)),
                when("reviewer", text(APPROVE)),
                when("lead", text("The flag is unknown.")),
            ],
            council,
        )
        result = team.run("add JSON mode")
        self.assertEqual(len(council.questions), 1)  # limit is 1 per agent run
        self.assertEqual(len(result.council), 1)
        self.assertEqual(result.runs[0].text, IGNORANT)


if __name__ == "__main__":
    unittest.main()
