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
    def test_no_args_starts_chat(self):
        with patch("offgrid.launch.cli_main", return_value=0) as cli:
            self.assertEqual(launch.main([]), 0)
        cli.assert_called_once_with(["chat"])

    def test_explicit_command_passes_through(self):
        with patch("offgrid.launch.cli_main", return_value=7) as cli:
            self.assertEqual(launch.main(["doctor", "--json"]), 7)
        cli.assert_called_once_with(["doctor", "--json"])


class ShellLauncherTests(unittest.TestCase):
    def test_root_launchers_exist(self):
        for name in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
            self.assertTrue((REPO_ROOT / name).is_file(), name)

    @unittest.skipUnless(os.name == "posix", "shell launchers are POSIX-only")
    def test_launcher_defaults_to_chat_and_passes_arguments(self):
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
                ["-m", "offgrid", "chat"],
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
