"""Self-improvement is gated: propose in a copy, approve, then restore."""

import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from offgrid.cli import main
from offgrid.config import Config
from offgrid.providers import LocalProvider
from offgrid.self_improve import (
    IdleWatcher,
    ImproveError,
    ImprovementTerminal,
    Request,
    SelfImprover,
)
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when

PLAN = {
    "summary": "improve app.py",
    "steps": [
        {
            "id": 1,
            "title": "update app.py",
            "agent": "coder",
            "task": "Update app.py",
            "files": ["app.py"],
            "depends_on": [],
        }
    ],
    "verify": "",
}
APPROVE = '{"verdict": "approve", "issues": [], "instructions": ""}'
OLD = "def greeting():\n    return 'hello'\n"
NEW = "def greeting():\n    return 'hello, world'\n"
NEWER = "def greeting():\n    return 'hello, world!'\n"


def script() -> list:
    return [
        when("planner", text(json.dumps(PLAN))),
        when("coder", tool("write_file", path="app.py", content=NEW)),
        when("coder", tool("finish", summary="made the greeting clearer")),
        when("reviewer", text(APPROVE)),
        when("lead", text("Updated app.py.")),
    ]


def two_turn_script() -> list:
    return script() + [
        when("planner", text(json.dumps(PLAN))),
        when("coder", tool("write_file", path="app.py", content=NEWER)),
        when("coder", tool("finish", summary="added the missing punctuation")),
        when("reviewer", text(APPROVE)),
        when("lead", text("Updated app.py again.")),
    ]


class SelfImproveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "app.py").write_text(OLD, encoding="utf-8")
        self.model = None

    def tearDown(self):
        if self.model:
            self.model.stop()
        self.tmp.cleanup()

    def improver(self, rules=None, **overrides: Any) -> SelfImprover:
        self.model = serve(rules if rules is not None else script())
        values: Dict[str, Any] = {
            "endpoint": self.model.url,
            "model": "fake",
            "workspace": str(self.root),
            "command_policy": "auto",
            "effort": "fast",
            "scale": False,
            "color": False,
            "verbose": 0,
        }
        values.update(overrides)
        config = Config(**values)
        return SelfImprover(
            Workspace(str(self.root)),
            config,
            LocalProvider(config),
            UI(config),
        )

    def test_proposal_is_pending_and_live_tree_is_unchanged(self):
        improver = self.improver()
        request = improver.propose()

        self.assertEqual(request.state, "pending")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        self.assertEqual([item.id for item in improver.pending()], [request.id])
        self.assertTrue((request.directory / "after" / "app.py").is_file())
        self.assertTrue((request.directory / "request.json").is_file())
        self.assertIn("+    return 'hello, world'", request.data["diff"])

    def test_approve_and_restore_are_exact(self):
        improver = self.improver()
        request = improver.propose()

        applied = improver.approve(request.id)
        self.assertEqual(applied.state, "applied")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), NEW)
        self.assertEqual(improver.pending(), [])

        restored = improver.restore(request.id)
        self.assertEqual(restored.state, "restored")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)

    def test_approval_refuses_a_file_changed_after_the_proposal(self):
        improver = self.improver()
        request = improver.propose()
        (self.root / "app.py").write_text("manual edit\n", encoding="utf-8")

        with self.assertRaises(ImproveError):
            improver.approve(request.id)

        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "manual edit\n")
        self.assertEqual(improver.get(request.id).state, "pending")

    def test_failed_verification_rolls_back_automatically(self):
        improver = self.improver(
            verify_command='python3 -c "raise SystemExit(1)"',
            command_policy="auto",
        )
        request = improver.propose()
        self.assertEqual(request.data["verify_command"], 'python3 -c "raise SystemExit(1)"')

        result = improver.approve(request.id)

        self.assertEqual(result.state, "rolled_back")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        self.assertIn("verification failed", result.data["failure"])

    def test_reject_does_not_touch_the_live_tree(self):
        improver = self.improver()
        request = improver.propose()

        rejected = improver.reject(request.id)

        self.assertEqual(rejected.state, "rejected")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        self.assertEqual(improver.pending(), [])

    def test_cli_propose_approve_restore(self):
        self.model = serve(script())
        common = [
            "--workspace",
            str(self.root),
            "--endpoint",
            self.model.url,
            "--model",
            "fake",
            "--effort",
            "fast",
            "--no-color",
            "--json",
        ]
        output = StringIO()
        with redirect_stdout(output):
            code = main(["improve", "propose", *common])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        request_id = payload["request"]["id"]
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)

        output = StringIO()
        with redirect_stdout(output):
            code = main(["improve", "approve", request_id, "--yes", *common])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), NEW)

        output = StringIO()
        with redirect_stdout(output):
            code = main(["improve", "restore", request_id, *common])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)

    def test_interactive_terminal_proposes_without_touching_live_tree(self):
        improver = self.improver()
        terminal = ImprovementTerminal(improver, improver.ui)
        output = StringIO()

        with patch(
            "builtins.input",
            side_effect=["make the greeting clearer", "/done", "", "/quit"],
        ):
            with redirect_stdout(output):
                code = terminal.run()

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        pending = improver.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].data["source"], "terminal")
        self.assertIn("OffGrid self-improvement terminal", output.getvalue())

    def test_interactive_terminal_keeps_a_multi_turn_staging_conversation(self):
        improver = self.improver(two_turn_script())
        terminal = ImprovementTerminal(improver, improver.ui)
        output = StringIO()

        with patch(
            "builtins.input",
            side_effect=[
                "make the greeting clearer",
                "add punctuation",
                "/done",
                "",
                "/quit",
            ],
        ):
            with redirect_stdout(output):
                code = terminal.run()

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        pending = improver.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0].data["conversation"],
            ["make the greeting clearer", "add punctuation"],
        )
        self.assertEqual(
            (pending[0].directory / "after" / "app.py").read_text(encoding="utf-8"),
            NEWER,
        )

    def test_interactive_terminal_applies_only_after_explicit_approval(self):
        improver = self.improver()
        request = improver.propose()
        terminal = ImprovementTerminal(improver, improver.ui)
        output = StringIO()

        with patch(
            "builtins.input",
            side_effect=["/approve", "approve", "/quit"],
        ):
            with redirect_stdout(output):
                code = terminal.run()

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), NEW)
        self.assertEqual(improver.get(request.id).state, "applied")

    def test_cli_opens_the_interactive_terminal(self):
        self.model = serve(script())
        common = [
            "--workspace",
            str(self.root),
            "--endpoint",
            self.model.url,
            "--model",
            "fake",
            "--effort",
            "fast",
            "--no-color",
        ]
        output = StringIO()

        with patch(
            "builtins.input",
            side_effect=["make the greeting clearer", "/done", "", "/quit"],
        ):
            with redirect_stdout(output):
                code = main(["improve", "terminal", *common])

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), OLD)
        requests = self.root / ".offgrid" / "self-improve" / "requests"
        self.assertEqual(len([item for item in requests.iterdir() if item.is_dir()]), 1)


class FakeImprover:
    def __init__(self, request: Request) -> None:
        self.request = request
        self.calls = 0

    def pending(self):
        return []

    def propose(self, source="manual", quiet=False):
        self.calls += 1
        return self.request


class IdleWatcherTests(unittest.TestCase):
    def test_watcher_notifies_after_a_quiet_period(self):
        request = Request(Path("/tmp/request"), {"id": "request", "state": "pending"})
        improver = FakeImprover(request)
        ready = threading.Event()
        watcher = IdleWatcher(improver, 0.05, notify=lambda _item: ready.set())
        watcher.start()
        try:
            self.assertTrue(ready.wait(3.0))
            self.assertEqual(improver.calls, 1)
            self.assertIs(watcher.last_request, request)
        finally:
            watcher.stop()

    def test_touch_postpones_an_idle_pass(self):
        request = Request(Path("/tmp/request"), {"id": "request", "state": "pending"})
        improver = FakeImprover(request)
        ready = threading.Event()
        watcher = IdleWatcher(improver, 0.12, notify=lambda _item: ready.set())
        watcher.start()
        try:
            time.sleep(0.05)
            watcher.touch()
            self.assertTrue(ready.wait(3.0))
            self.assertEqual(improver.calls, 1)
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
