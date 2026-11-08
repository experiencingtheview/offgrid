import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid import desktop
from offgrid.cli import main


class DesktopShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_root(self, launcher):
        root = self.root / "Off Grid"
        root.mkdir()
        (root / launcher).write_text("#!/bin/sh\n", encoding="utf-8")
        return root

    def test_linux_writes_an_executable_desktop_entry(self):
        root = self.make_root("OffGrid.sh")
        desktop_dir = self.root / "Desktop"
        shortcut = desktop.install(
            desktop_dir=desktop_dir, root=root, platform="linux"
        )

        self.assertEqual(shortcut.path, desktop_dir / "OffGrid.desktop")
        self.assertEqual(shortcut.target, root / "OffGrid.sh")
        text = shortcut.path.read_text(encoding="utf-8")
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("Type=Application", text)
        self.assertIn("Name=OffGrid", text)
        self.assertIn("Terminal=true", text)
        self.assertIn('Exec="' + str(root / "OffGrid.sh") + '"', text)
        self.assertIn(f"Path={root}", text)
        self.assertIn("X-OffGrid-Shortcut=true", text)
        self.assertTrue(os.access(shortcut.path, os.X_OK))

    def test_linux_install_is_idempotent_and_force_is_available(self):
        root = self.make_root("OffGrid.sh")
        desktop_dir = self.root / "Desktop"
        desktop.install(desktop_dir=desktop_dir, root=root, platform="linux")
        desktop.install(desktop_dir=desktop_dir, root=root, platform="linux")

        shortcut = desktop_dir / "OffGrid.desktop"
        shortcut.write_text("something else\n", encoding="utf-8")
        with self.assertRaises(desktop.DesktopError):
            desktop.install(desktop_dir=desktop_dir, root=root, platform="linux")
        desktop.install(
            desktop_dir=desktop_dir, root=root, platform="linux", force=True
        )
        self.assertIn("X-OffGrid-Shortcut=true", shortcut.read_text(encoding="utf-8"))

    def test_macos_writes_a_double_clickable_command(self):
        root = self.make_root("OffGrid.command")
        desktop_dir = self.root / "Desktop"
        shortcut = desktop.install(
            desktop_dir=desktop_dir, root=root, platform="macos"
        )

        self.assertEqual(shortcut.path, desktop_dir / "OffGrid.command")
        text = shortcut.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn(f"cd '{root}'", text)
        self.assertIn(f"exec '{root / 'OffGrid.command'}'", text)
        self.assertTrue(os.access(shortcut.path, os.X_OK))

    def test_windows_creates_a_lnk_through_powershell(self):
        root = self.make_root("OffGrid.bat")
        desktop_dir = self.root / "Desktop"
        completed = desktop.subprocess.CompletedProcess([], 0, "", "")

        with patch.object(
            desktop.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        ), patch.object(desktop.subprocess, "run", return_value=completed) as run:
            shortcut = desktop.install(
                desktop_dir=desktop_dir, root=root, platform="windows"
            )

        self.assertEqual(shortcut.path, desktop_dir / "OffGrid.lnk")
        call = run.call_args
        self.assertIn("-NoProfile", call.args[0])
        self.assertIn("WScript.Shell", call.args[0][-1])
        environment = call.kwargs["env"]
        self.assertEqual(environment["OFFGRID_SHORTCUT_PATH"], str(shortcut.path))
        self.assertEqual(environment["OFFGRID_SHORTCUT_TARGET"], str(root / "OffGrid.bat"))
        self.assertEqual(environment["OFFGRID_SHORTCUT_WORKDIR"], str(root))

    def test_windows_reports_a_powershell_failure(self):
        root = self.make_root("OffGrid.bat")
        desktop_dir = self.root / "Desktop"
        completed = desktop.subprocess.CompletedProcess([], 1, "", "access denied")

        with patch.object(
            desktop.shutil, "which", return_value="powershell"
        ), patch.object(desktop.subprocess, "run", return_value=completed):
            with self.assertRaises(desktop.DesktopError) as caught:
                desktop.install(
                    desktop_dir=desktop_dir, root=root, platform="windows"
                )
        self.assertIn("access denied", str(caught.exception))

    def test_remove_only_removes_an_offgrid_shortcut(self):
        root = self.make_root("OffGrid.sh")
        desktop_dir = self.root / "Desktop"
        shortcut = desktop.install(
            desktop_dir=desktop_dir, root=root, platform="linux"
        )
        self.assertEqual(
            desktop.remove(desktop_dir=desktop_dir, platform="linux"), shortcut.path
        )
        self.assertFalse(shortcut.path.exists())

        shortcut.path.write_text("not ours\n", encoding="utf-8")
        with self.assertRaises(desktop.DesktopError):
            desktop.remove(desktop_dir=desktop_dir, platform="linux")
        self.assertEqual(
            desktop.remove(desktop_dir=desktop_dir, platform="linux", force=True),
            shortcut.path,
        )

    def test_linux_desktop_directory_honours_user_dirs(self):
        home = self.root / "home"
        config = home / ".config"
        config.mkdir(parents=True)
        (config / "user-dirs.dirs").write_text(
            'XDG_DESKTOP_DIR="$HOME/My Desktop"\n', encoding="utf-8"
        )
        self.assertEqual(
            desktop.desktop_directory(
                platform="linux", env={"HOME": str(home)}, home=home
            ),
            home / "My Desktop",
        )

    def test_cli_installs_and_reports_json(self):
        root = self.make_root("OffGrid.sh")
        desktop_dir = self.root / "Desktop"
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "desktop",
                    "install",
                    "--desktop",
                    str(desktop_dir),
                    "--root",
                    str(root),
                    "--json",
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], str(desktop_dir / "OffGrid.desktop"))
        self.assertTrue(Path(payload["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
