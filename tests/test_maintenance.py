"""Self-debug and housekeeping are safe to run continuously on a server."""

import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid.cli import main
from offgrid.config import Config
from offgrid.maintenance import Maintainer, SelfDebugger
from offgrid.self_improve import Request, SelfImprover
from offgrid.ui import UI
from offgrid.workspace import Workspace


class FakeProvider:
    def __init__(self, ok=True, detail="ok, model 'fake'"):
        self.ok = ok
        self.detail = detail

    def health(self):
        return self.ok, self.detail


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(
            workspace=str(self.root),
            verbose=0,
            color=False,
            command_policy="deny",
        )
        self.improver = SelfImprover(
            Workspace(str(self.root)), self.config, None, UI(self.config)
        )

    def tearDown(self):
        self.tmp.cleanup()

    def add_request(self, request_id, state, created):
        directory = (
            self.root
            / ".offgrid"
            / "self-improve"
            / "requests"
            / request_id
        )
        directory.mkdir(parents=True)
        (directory / "request.json").write_text(
            json.dumps(
                {
                    "id": request_id,
                    "state": state,
                    "created": created,
                    "summary": request_id,
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        return directory

    def maintainer(self, **overrides):
        values = {
            "provider": FakeProvider(),
            "provider_error": "",
            "retention_days": 30,
            "keep": 20,
        }
        values.update(overrides)
        return Maintainer(
            Workspace(str(self.root)),
            self.config,
            self.improver,
            ui=UI(self.config),
            **values,
        )

    def test_housekeeping_prunes_only_old_terminal_requests(self):
        now = time.time()
        old_rejected = self.add_request("old-rejected", "rejected", now - 4 * 86400)
        old_applied = self.add_request("old-applied", "applied", now - 4 * 86400)
        old_pending = self.add_request("old-pending", "pending", now - 4 * 86400)
        old_failed = self.add_request("old-failed", "rollback_failed", now - 4 * 86400)
        recent_rejected = self.add_request("recent-rejected", "rejected", now - 3600)

        actions = self.maintainer(retention_days=1, keep=0).housekeeping()

        self.assertFalse(old_rejected.exists())
        self.assertTrue(old_applied.exists())
        self.assertTrue(old_pending.exists())
        self.assertTrue(old_failed.exists())
        self.assertTrue(recent_rejected.exists())
        self.assertTrue(any("pruned rejected request old-rejected" in action for action in actions))

    def test_keep_preserves_the_newest_terminal_requests(self):
        now = time.time()
        old = self.add_request("old", "rejected", now - 4 * 86400)
        newer = self.add_request("newer", "rejected", now - 3 * 86400)
        (self.root / ".offgrid" / "self-improve" / "requests" / "applied").mkdir()
        applied = self.root / ".offgrid" / "self-improve" / "requests" / "applied"
        (applied / "request.json").write_text(
            json.dumps(
                {"id": "applied", "state": "applied", "created": now - 4 * 86400}
            ),
            encoding="utf-8",
        )

        self.maintainer(retention_days=1, keep=1).housekeeping()

        self.assertFalse(old.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(applied.exists())

    def test_debugger_reports_a_malformed_request(self):
        broken = self.root / ".offgrid" / "self-improve" / "requests" / "broken"
        broken.mkdir(parents=True)
        (broken / "request.json").write_text("{not json", encoding="utf-8")

        checks = SelfDebugger(
            Workspace(str(self.root)), self.config, self.improver, FakeProvider()
        ).run()
        request_check = next(check for check in checks if check.name == "requests")
        self.assertFalse(request_check.ok)
        self.assertIn("unreadable", request_check.detail)

    def test_run_once_does_not_improve_unless_asked(self):
        report = self.maintainer().run_once(improve=False)

        self.assertTrue(report.healthy, report.as_dict())
        self.assertIsNone(report.improvement)
        self.assertEqual(report.improvement_skipped, "")

    def test_run_once_creates_a_pending_request_when_asked(self):
        request = Request(
            Path("/tmp/unused"),
            {"id": "new", "state": "pending", "summary": "make it better", "files": []},
        )
        maintainer = self.maintainer()
        with patch.object(self.improver, "propose", return_value=request) as propose:
            report = maintainer.run_once(improve=True)

        self.assertIs(report.improvement, request)
        self.assertTrue(report.healthy, report.as_dict())
        propose.assert_called_once()
        self.assertEqual(propose.call_args.kwargs["source"], "maintenance")
        self.assertEqual(propose.call_args.kwargs["quiet"], True)

    def test_run_once_skips_improvement_when_a_request_is_waiting(self):
        self.add_request("waiting", "pending", time.time())

        report = self.maintainer().run_once(improve=True)

        self.assertIsNone(report.improvement)
        self.assertIn("waiting", report.improvement_skipped)

    def test_run_once_does_not_improve_an_unhealthy_server(self):
        maintainer = self.maintainer(provider=FakeProvider(False, "model is down"))
        with patch.object(self.improver, "propose") as propose:
            report = maintainer.run_once(improve=True)

        self.assertIsNone(report.improvement)
        self.assertIn("self-debug checks failed", report.improvement_skipped)
        propose.assert_not_called()

    def test_once_json_cli_reports_a_healthy_server(self):
        output = StringIO()
        with patch("offgrid.cli.open_provider", return_value=FakeProvider()):
            with redirect_stdout(output):
                code = main(
                    [
                        "maintain",
                        "--once",
                        "--json",
                        "--workspace",
                        str(self.root),
                        "--no-color",
                    ]
                )

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["improvement"], None)
        self.assertTrue(any(check["name"] == "model" for check in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
