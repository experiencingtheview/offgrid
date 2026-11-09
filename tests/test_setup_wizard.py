"""The setup wizard: what it reports, and that it never blocks on a prompt."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid import desktop, setup_wizard
from offgrid.cli import main
from offgrid.config import Config

SERVERS = [("http://127.0.0.1:8080/v1", ["qwen2.5-coder-7b"])]
NOTHING = []


def run(**kwargs):
    lines = []
    kwargs.setdefault("workspace", "/tmp/project")
    kwargs.setdefault("out", lines.append)
    kwargs.setdefault("interactive", False)
    code = setup_wizard.run_wizard(Config(), **kwargs)
    return code, "\n".join(lines)


class SetupWizardTests(unittest.TestCase):
    def test_a_found_server_is_ready(self):
        code, text = run(probe=lambda config: SERVERS)
        self.assertEqual(code, 0)
        self.assertIn("http://127.0.0.1:8080/v1", text)
        self.assertIn("qwen2.5-coder-7b", text)
        self.assertIn("offgrid chat", text)

    def test_no_server_exits_nonzero_and_names_both_free_options(self):
        code, text = run(probe=lambda config: NOTHING)
        self.assertEqual(code, 1)
        self.assertIn("ollama pull qwen2.5-coder:7b", text)
        self.assertIn("llama-server -m models/model.gguf --port 8080 --jinja", text)
        self.assertIn("not ready yet", text)

    def test_a_pipe_never_blocks_on_the_shortcut_question(self):
        asked = []
        code, text = run(
            probe=lambda config: SERVERS,
            ask=lambda prompt: asked.append(prompt) or "y",
        )
        self.assertEqual(code, 0)
        self.assertEqual(asked, [])
        self.assertIn("not offered without a terminal", text)

    def test_accepting_the_shortcut_installs_it(self):
        asked = []
        with tempfile.TemporaryDirectory() as tmp:
            shortcut = desktop.Shortcut(
                Path(tmp) / "OffGrid.desktop", "linux", Path("/x/OffGrid.sh")
            )
            with patch.object(desktop, "install", return_value=shortcut) as install:
                code, text = run(
                    probe=lambda config: SERVERS,
                    interactive=True,
                    ask=lambda prompt: asked.append(prompt) or "y",
                    desktop_dir=Path(tmp),
                )
        self.assertEqual(code, 0)
        self.assertEqual(asked, ["  Create a desktop shortcut? [y/N] "])
        self.assertEqual(install.call_args.kwargs["desktop_dir"], Path(tmp))
        self.assertIn(str(shortcut.path), text)

    def test_declining_the_shortcut_installs_nothing(self):
        with patch.object(desktop, "install") as install:
            code, text = run(
                probe=lambda config: SERVERS, interactive=True, ask=lambda prompt: ""
            )
        self.assertEqual(code, 0)
        install.assert_not_called()
        self.assertIn("shortcut    skipped", text)

    def test_a_shortcut_failure_is_reported_without_crashing(self):
        with patch.object(
            desktop, "install", side_effect=desktop.DesktopError("no desktop folder")
        ):
            code, text = run(
                probe=lambda config: SERVERS,
                interactive=True,
                ask=lambda prompt: "yes",
            )
        self.assertEqual(code, 0)
        self.assertIn("no desktop folder", text)
        self.assertIn("offgrid desktop install", text)

    def test_an_old_interpreter_is_refused_before_scanning(self):
        scanned = []
        code, text = run(
            probe=lambda config: scanned.append(True) or SERVERS,
            version_info=(3, 8),
        )
        self.assertEqual(code, 1)
        self.assertIn("Python 3.8 is too old", text)
        self.assertIn("needs 3.9 or newer", text)
        self.assertEqual(scanned, [])

    def test_json_report_is_machine_readable(self):
        code, text = run(probe=lambda config: SERVERS, as_json=True)
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(
            payload["servers"],
            [{"endpoint": "http://127.0.0.1:8080/v1", "model": "qwen2.5-coder-7b"}],
        )
        self.assertEqual(payload["shortcut"], "")
        self.assertEqual(payload["problems"], [])

    def test_cli_setup_json_never_prompts(self):
        output = StringIO()
        with patch.object(
            setup_wizard, "discover", return_value=SERVERS
        ), redirect_stdout(output):
            code = main(["setup", "--json", "--no-input"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["workspace"], str(Path.cwd()))


if __name__ == "__main__":
    unittest.main()
