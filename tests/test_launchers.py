import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offgrid import launch

REPO_ROOT = Path(__file__).resolve().parent.parent


class LaunchCommandTests(unittest.TestCase):
    def test_no_args_starts_easy_setup(self):
        with patch("offgrid.launch.cli_main", return_value=0) as cli:
            self.assertEqual(launch.main([]), 0)
        cli.assert_called_once_with(["easy"])

    def test_explicit_command_passes_through(self):
        with patch("offgrid.launch.cli_main", return_value=7) as cli:
            self.assertEqual(launch.main(["doctor", "--json"]), 7)
        cli.assert_called_once_with(["doctor", "--json"])


class ShellLauncherTests(unittest.TestCase):
    def test_root_launchers_exist(self):
        for name in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
            self.assertTrue((REPO_ROOT / name).is_file(), name)

    def test_windows_launcher_explains_itself_and_waits_before_closing(self):
        launcher = (REPO_ROOT / "launch" / "offgrid.bat").read_text(encoding="utf-8")
        # A broken or Store-alias python must be diagnosed, not trusted.
        self.assertIn("py -3", launcher)
        self.assertIn("older than 3.9", launcher)
        # A double-click must leave the message on screen, and a missing python
        # must self-install rather than sending the user to a download page.
        self.assertIn("pause", launcher)
        self.assertIn("bootstrap.ps1", launcher)
        self.assertIn("runtime\\python\\python.exe", launcher)
        self.assertIn("Unblock", launcher)
        self.assertIn("easy", launcher)
        # A private path must not inherit the `py -3` argument when repairing
        # an old system interpreter.
        self.assertIn('set "PYARGS="', launcher)
        self.assertIn("goto old_python", launcher)
        top = (REPO_ROOT / "OffGrid.bat").read_text(encoding="utf-8")
        self.assertIn("launch\\offgrid.bat", top)
        self.assertIn("Extract the whole zip", top)

    def test_bootstrap_files_are_present(self):
        self.assertTrue((REPO_ROOT / "launch" / "bootstrap.sh").is_file())
        self.assertTrue((REPO_ROOT / "launch" / "bootstrap.ps1").is_file())
        shell = (REPO_ROOT / "launch" / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("runtime/python", shell)
        self.assertIn("install_only", shell)
        # A half-extracted runtime must be replaced, not trusted.
        self.assertIn('"$RUNTIME/bin/python3" -c', shell)
        powershell = (REPO_ROOT / "launch" / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("$LASTEXITCODE", powershell)
        self.assertIn("install_only", powershell)

    def test_windows_ci_exercises_the_private_runtime_bootstrap(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("windows_easy_bootstrap:", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("tests/windows_easy_smoke.ps1", workflow)
        self.assertIn("windows-easy-smoke-logs", workflow)
        script = (REPO_ROOT / "tests" / "windows_easy_smoke.ps1").read_text(encoding="utf-8")
        for expected in (
            "OffGrid.bat",
            "private Python runtime",
            "setup page",
            "OFFGRID_ENDPOINT",
            "taskkill",
        ):
            self.assertIn(expected, script)

    @unittest.skipUnless(os.name == "posix", "shell launchers are POSIX-only")
    def test_launcher_defaults_to_easy_and_passes_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO_ROOT / "launch", root / "launch")
            shutil.copy2(REPO_ROOT / "OffGrid.sh", root / "OffGrid.sh")
            (root / "OffGrid.sh").chmod(0o755)
            (root / "launch" / "offgrid.sh").chmod(0o755)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            record = root / "args.txt"
            fake = bin_dir / "python3"
            fake.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$OFFGRID_TEST_ARGS\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                "OFFGRID_NO_SERVER": "1",
                "OFFGRID_TEST_ARGS": str(record),
            }

            proc = subprocess.run(
                [str(root / "OffGrid.sh")],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                record.read_text(encoding="utf-8").splitlines(),
                ["-m", "offgrid", "easy"],
            )

            proc = subprocess.run(
                [str(root / "OffGrid.sh"), "doctor"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                record.read_text(encoding="utf-8").splitlines(),
                ["-m", "offgrid", "doctor"],
            )
