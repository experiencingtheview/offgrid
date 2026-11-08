import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.cli import main
from offgrid.config import Config
from offgrid.orchestrator import RunResult, Team
from offgrid.ui import UI
from offgrid.workspace import Workspace


class FakeProvider:
    model = "fake"


class SecurityFlowTests(unittest.TestCase):
    def test_a_secret_in_a_changed_file_is_recorded_and_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
            config = Config(
                workspace=str(root), effort="fast", scale=False, security_history=".offgrid/security.jsonl"
            )
            team = Team(FakeProvider(), Workspace(root), config, UI(config))
            result = RunResult(task="add a token")
            result.changed = ["app.py"]
            team.security_review(result)
            self.assertTrue(result.security)
            self.assertIn("secret", {item["check"] for item in result.security})
            log = root / ".offgrid" / "security.jsonl"
            self.assertTrue(log.is_file())
            self.assertIn("secret", log.read_text(encoding="utf-8"))

    def test_the_review_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
            config = Config(workspace=str(root), effort="fast", scale=False, security_review=False)
            team = Team(FakeProvider(), Workspace(root), config, UI(config))
            result = RunResult(task="x")
            result.changed = ["app.py"]
            team.security_review(result)
            self.assertEqual(result.security, [])
            self.assertFalse((root / ".offgrid" / "security.jsonl").exists())

    def test_cli_review_reports_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
            output = StringIO()
            with redirect_stdout(output):
                code = main(["security", "review", "--workspace", str(root), "--json"])
            self.assertEqual(code, 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["verdict"], "high")
            self.assertEqual(data["counts"]["high"], 1)

    def test_cli_trust_states_the_contract_and_the_posture(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["trust", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("not a person", text)
            self.assertIn("full-disk encryption", text)
            self.assertIn("council", text)

    def test_security_plan_refuses_full_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["security", "plan", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0)
            self.assertIn("will not encrypt the running system", output.getvalue())


if __name__ == "__main__":
    unittest.main()
