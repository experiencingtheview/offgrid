"""The setup wizard: what it reports, and that it never blocks on a prompt."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from offgrid import cli as cli_module
from offgrid import desktop, setup_wizard
from offgrid.cli import main
from offgrid.config import Config
from offgrid.providers import ProviderError
from offgrid.ui import UI

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


class FakeProvider:
    """A local provider whose health the test decides."""

    model = "qwen2.5-coder-7b"
    endpoint = "http://127.0.0.1:8080/v1"

    def __init__(self, ok=False):
        self.ok = ok

    def health(self):
        return (True, "") if self.ok else (False, "cannot reach " + self.endpoint)


class ChatSetupOfferTests(unittest.TestCase):
    """A failed start offers the wizard; scripts and pipes are never asked."""

    def _offer(self, *, answer="", tty=True, wizard=None, provider=None):
        provider = provider if provider is not None else FakeProvider()
        out = StringIO()
        with patch.object(
            cli_module, "open_provider", return_value=provider
        ), patch.object(cli_module.sys.stdin, "isatty", return_value=tty), patch.object(
            setup_wizard, "run_wizard", wizard or MagicMock(return_value=1)
        ), patch("builtins.input", return_value=answer), redirect_stdout(out):
            result = cli_module.offer_setup(
                Config(), UI(Config()), SimpleNamespace(root="/tmp/project")
            )
        return result, out.getvalue(), provider

    def test_accepting_runs_the_wizard_in_place(self):
        wizard = MagicMock(return_value=1)
        result, _text, _provider = self._offer(wizard=wizard)
        self.assertIsNone(result)
        wizard.assert_called_once()
        self.assertTrue(wizard.call_args.kwargs["interactive"])

    def test_declining_prints_the_servers_and_gives_up(self):
        wizard = MagicMock()
        result, text, _provider = self._offer(answer="n", wizard=wizard)
        self.assertIsNone(result)
        wizard.assert_not_called()
        self.assertIn("ollama serve", text)

    def test_a_pipe_is_never_asked(self):
        wizard = MagicMock()
        result, text, _provider = self._offer(tty=False, wizard=wizard)
        self.assertIsNone(result)
        wizard.assert_not_called()
        self.assertIn("llama-server -m", text)

    def test_a_server_started_meanwhile_continues_into_chat(self):
        provider = FakeProvider()

        def wizard(config, **kwargs):
            provider.ok = True
            return 0

        result, _text, _provider = self._offer(wizard=wizard, provider=provider)
        self.assertIs(result, provider)

    def test_chat_exits_2_without_a_server_when_piped(self):
        out = StringIO()
        with patch.object(
            cli_module, "open_provider", side_effect=ProviderError("no endpoint configured")
        ), patch.object(cli_module.sys.stdin, "isatty", return_value=False), redirect_stdout(
            out
        ):
            self.assertEqual(main(["chat"]), 2)
        self.assertIn("ollama serve", out.getvalue())

    def test_a_one_shot_run_never_prompts(self):
        with patch.object(
            cli_module, "open_provider", side_effect=ProviderError("no endpoint configured")
        ), patch.object(cli_module.sys.stdin, "isatty", return_value=True), patch(
            "builtins.input", side_effect=AssertionError("a run must not ask a question")
        ), patch.object(
            setup_wizard, "run_wizard"
        ) as wizard, redirect_stdout(
            StringIO()
        ):
            self.assertEqual(main(["run", "add a /health endpoint"]), 2)
        wizard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
