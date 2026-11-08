"""Self-learning: deterministic lessons, reflective lessons, honesty filters."""

import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.learn import Retrospective, deterministic_lessons
from offgrid.memory import MemoryStore
from offgrid.models import Completion
from offgrid.orchestrator import RunResult
from offgrid.ui import UI


def make_result(**overrides) -> RunResult:
    base = dict(
        task="add a health endpoint",
        changed=["app.py"],
        verify_command="python3 -m pytest -q",
        verify_ok=True,
        review_verdict="approve",
    )
    base.update(overrides)
    return RunResult(**base)


def council_record(**overrides):
    record = {
        "agent": "coder",
        "question": "Which flag enables JSON mode?",
        "members": ["peer"],
        "member": "peer",
        "answer_text": "Use --jinja when starting llama-server.",
        "answer": "COUNCIL (1 member(s) consulted, question 34 chars)\n--- peer ---\nUse --jinja when starting llama-server.",
        "disagreement": False,
        "skipped": [],
    }
    record.update(overrides)
    return record


class DummyProvider:
    def __init__(self, text):
        self.payload = text
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return Completion(text=self.payload)


class DeterministicTests(unittest.TestCase):
    def test_passing_verify_becomes_a_repo_fact(self):
        lessons = deterministic_lessons(make_result())
        repo = [item for item in lessons if item.kind == "repo"]
        self.assertEqual(len(repo), 1)
        self.assertIn("python3 -m pytest -q", repo[0].text)
        self.assertEqual(repo[0].key, "verify-command")

    def test_failing_verify_becomes_a_warning(self):
        lessons = deterministic_lessons(make_result(verify_ok=False))
        self.assertTrue(any(item.kind == "warning" for item in lessons))

    def test_review_rejection_becomes_a_correction(self):
        lessons = deterministic_lessons(
            make_result(review_verdict="changes", review_issues=["no error handling", "unused import"])
        )
        correction = [item for item in lessons if item.kind == "correction"][0]
        self.assertIn("no error handling", correction.text)

    def test_files_become_a_fact(self):
        lessons = deterministic_lessons(make_result(changed=["a.py", "b.py"]))
        self.assertTrue(any("a.py, b.py" in item.text for item in lessons))

    def test_nothing_changed_means_almost_no_lessons(self):
        lessons = deterministic_lessons(RunResult(task="explain the codebase"))
        self.assertEqual(lessons, [])

    def test_a_verified_council_answer_becomes_a_council_sourced_lesson(self):
        lessons = deterministic_lessons(make_result(council=[council_record()]))
        council = [item for item in lessons if item.source.startswith("council")]
        self.assertEqual(len(council), 1)
        self.assertIn("--jinja", council[0].text)
        self.assertIn("later confirmed by verification", council[0].text)
        self.assertEqual(council[0].source, "council: peer")

    def test_a_verified_council_lesson_carries_the_run_evidence(self):
        result = make_result(
            council=[council_record()], verify_output="1 passed in 0.03s"
        )
        council = [
            item for item in deterministic_lessons(result) if item.source.startswith("council")
        ][0]
        self.assertEqual(council.verify_command, "python3 -m pytest -q")
        self.assertEqual(council.verify_output, "1 passed in 0.03s")

    def test_an_unverified_council_answer_is_not_a_lesson(self):
        for ok in (False, None):
            lessons = deterministic_lessons(
                make_result(verify_ok=ok, council=[council_record()])
            )
            self.assertFalse(any(item.source.startswith("council") for item in lessons))

    def test_a_disagreeing_council_answer_is_not_a_lesson(self):
        lessons = deterministic_lessons(
            make_result(council=[council_record(disagreement=True)])
        )
        self.assertFalse(any(item.source.startswith("council") for item in lessons))


class ReflectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / "memory.db")
        self.config = Config(memory=True, color=False, verbose=0)
        self.ui = UI(self.config)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def record(self, payload, result=None):
        provider = DummyProvider(payload)
        retrospective = Retrospective(provider, self.memory, self.config, self.ui)
        return retrospective.record(result or make_result(), source="s1")

    def test_model_lessons_are_stored_as_pending(self):
        payload = '{"lessons": [{"text": "Run ruff before pytest in this repo", "kind": "lesson", "confidence": 0.7}]}'
        stored = self.record(payload)
        self.assertTrue(stored)
        pending = self.memory.pending()
        self.assertTrue(any("ruff before pytest" in item.text for item in pending))
        self.assertTrue(all(item.status == "pending" for item in pending))
        self.assertEqual(self.memory.all(), [])  # not knowledge until reviewed

    def test_empty_lesson_list_is_respected(self):
        quiet = make_result(changed=[], verify_ok=None, verify_command="")
        stored = self.record('{"lessons": []}', quiet)
        self.assertEqual(stored, [])

    def test_noise_is_filtered(self):
        payload = '{"lessons": [{"text": "I cannot determine anything", "kind": "lesson"}]}'
        stored = self.record(payload, make_result(changed=[], verify_ok=None))
        self.assertEqual(stored, [])

    def test_lessons_are_capped_at_three(self):
        payload = '{"lessons": [' + ",".join(
            '{"text": "durable lesson number %d about this repository", "kind": "lesson"}' % index
            for index in range(6)
        ) + "]}"
        self.record(payload)
        reflective = [item for item in self.memory.pending() if item.text.startswith("durable lesson")]
        self.assertEqual(len(reflective), 3)

    def test_invalid_json_falls_back_to_deterministic(self):
        stored = self.record("I am not going to answer in JSON")
        self.assertTrue(any("pytest" in item.text for item in stored))

    def test_second_identical_run_does_not_duplicate(self):
        payload = '{"lessons": [{"text": "Always run the verify command", "kind": "lesson"}]}'
        self.record(payload)
        before = len(self.memory.pending())
        self.record(payload)
        self.assertEqual(len(self.memory.pending()), before)  # still offered for review once

    def test_a_rejected_lesson_is_not_offered_again(self):
        text = "This repository verifies with make test"
        rejected = self.memory.remember(text, kind="lesson", status="pending")
        self.memory.reject([rejected])
        payload = '{"lessons": [{"text": "This repository verifies with make test", "kind": "lesson", "confidence": 0.7}]}'
        stored = self.record(
            payload, make_result(changed=[], verify_ok=None, verify_command="")
        )
        self.assertEqual(stored, [])
        self.assertEqual(self.memory.pending(), [])
        self.assertTrue(self.memory.was_rejected(text))

    def test_a_semantically_reworded_rejection_is_not_offered_again(self):
        text = "Always run pytest before the tests"
        rejected = self.memory.remember(text, kind="lesson", status="pending")
        self.memory.reject([rejected])
        payload = (
            '{"lessons": [{"text": "Execute the test suite before committing.", '
            '"kind": "lesson", "confidence": 0.7}]}'
        )
        stored = self.record(
            payload, make_result(changed=[], verify_ok=None, verify_command="")
        )
        self.assertEqual(stored, [])
        self.assertEqual(self.memory.pending(), [])

    def test_the_archivist_is_told_about_rejected_lessons(self):
        text = "Never run the migration before a backup"
        rejected = self.memory.remember(text, kind="lesson", status="pending")
        self.memory.reject([rejected])
        provider = DummyProvider('{"lessons": []}')
        retrospective = Retrospective(provider, self.memory, self.config, self.ui)
        retrospective.record(
            make_result(changed=[], verify_ok=None, verify_command=""), source="s1"
        )
        prompt = provider.calls[0][0][1].content
        self.assertIn("REJECTED BY THE HUMAN", prompt)
        self.assertIn(text, prompt)

    def test_a_verified_council_lesson_is_pending_and_names_its_source(self):
        stored = self.record('{"lessons": []}', make_result(council=[council_record()]))
        council = [item for item in stored if item.source.startswith("council")]
        self.assertEqual(len(council), 1)
        item = self.memory.get(council[0].id)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.kind, "lesson")
        self.assertEqual(item.source, "council: peer")
        self.assertIn("--jinja", item.text)
        self.assertNotIn(item.id, [entry.id for entry in self.memory.all()])

    def test_a_pending_council_lesson_keeps_its_verification_evidence(self):
        stored = self.record(
            '{"lessons": []}',
            make_result(council=[council_record()], verify_output="1 passed in 0.03s"),
        )
        council = [item for item in stored if item.source.startswith("council")][0]
        item = self.memory.get(council.id)
        self.assertEqual(item.verify_command, "python3 -m pytest -q")
        self.assertEqual(item.verify_output, "1 passed in 0.03s")
        self.assertTrue(item.evidence_lines())

    def test_provider_failure_does_not_raise(self):
        class Broken:
            def chat(self, messages, **kwargs):
                from offgrid.providers import ProviderError

                raise ProviderError("server died")

        retrospective = Retrospective(Broken(), self.memory, self.config, self.ui)
        stored = retrospective.record(make_result())
        self.assertTrue(any("pytest" in item.text for item in stored))


if __name__ == "__main__":
    unittest.main()
