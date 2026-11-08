"""Systemd user timer installation for the workspace security review."""

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from offgrid.cli import main


@unittest.skipUnless(sys.platform.startswith("linux"), "systemd user timers are Linux-only")
class SecurityTimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.xdg = Path(self.tmp.name) / "xdg"
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "systemctl.log"
        fake = self.bin / "systemctl"
        fake.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$OFFGRID_SYSTEMCTL_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env["XDG_CONFIG_HOME"] = str(self.xdg)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["OFFGRID_SYSTEMCTL_LOG"] = str(self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        output = StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    [
                        "security",
                        "watch",
                        *args,
                        "--workspace",
                        str(self.root),
                        "--json",
                        "--no-color",
                    ]
                )
        return code, output.getvalue()

    def test_install_writes_units_and_enables_the_user_timer(self):
        code, output = self.cli("--review", "--install-timer", "--interval", "900")

        self.assertEqual(code, 0, output)
        data = json.loads(output)
        service = Path(data["service_path"])
        timer = Path(data["timer_path"])
        self.assertTrue(service.is_file())
        self.assertTrue(timer.is_file())
        service_text = service.read_text(encoding="utf-8")
        timer_text = timer.read_text(encoding="utf-8")
        self.assertIn(f'WorkingDirectory="{self.root}"', service_text)
        self.assertIn('"security" "watch" "--review" "--once"', service_text)
        self.assertIn("NoNewPrivileges=yes", service_text)
        self.assertIn(f"Unit={data['service_name']}", timer_text)
        self.assertIn("OnUnitActiveSec=15min", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("--user daemon-reload", calls)
        self.assertIn(f"--user enable --now {data['timer_name']}", calls)

    def test_remove_disables_and_deletes_the_units(self):
        code, output = self.cli("--review", "--install-timer")
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        service = Path(data["service_path"])
        timer = Path(data["timer_path"])

        code, output = self.cli("--remove-timer")

        self.assertEqual(code, 0, output)
        removed = json.loads(output)
        self.assertEqual(len(removed["removed"]), 2)
        self.assertFalse(service.exists())
        self.assertFalse(timer.exists())
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn(f"--user disable --now {data['timer_name']}", calls)

    def test_removing_an_uninstalled_timer_explains_the_problem(self):
        code, output = self.cli("--remove-timer")

        self.assertEqual(code, 1)
        self.assertIn("no OffGrid security timer is installed", output)

    def test_service_file_generation_writes_a_continuous_unit(self):
        service = Path(self.tmp.name) / "offgrid-security-watch.service"

        code, output = self.cli(
            "--review", "--service-file", str(service), "--interval", "900"
        )

        self.assertEqual(code, 0, output)
        data = json.loads(output)
        self.assertEqual(data["mode"], "continuous")
        self.assertEqual(Path(data["service_path"]), service.resolve())
        text = service.read_text(encoding="utf-8")
        self.assertIn("Type=simple", text)
        self.assertIn(
            '"security" "watch" "--review" "--interval" "900"', text
        )
        self.assertNotIn("--once", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertFalse(self.log.exists(), "generation must not call systemctl")

    def test_install_service_enables_the_continuous_service(self):
        code, output = self.cli(
            "--review", "--install-service", "--interval", "900"
        )

        self.assertEqual(code, 0, output)
        data = json.loads(output)
        service = Path(data["service_path"])
        self.assertTrue(service.is_file())
        self.assertNotIn("timer_path", data)
        text = service.read_text(encoding="utf-8")
        self.assertIn("Type=simple", text)
        self.assertNotIn("--once", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn(
            '"security" "watch" "--review" "--interval" "900"', text
        )
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("--user daemon-reload", calls)
        self.assertIn(f"--user enable --now {data['service_name']}", calls)

    def test_remove_service_disables_and_deletes_the_continuous_unit(self):
        code, output = self.cli("--review", "--install-service")
        self.assertEqual(code, 0, output)
        data = json.loads(output)
        service = Path(data["service_path"])

        code, output = self.cli("--remove-service")

        self.assertEqual(code, 0, output)
        removed = json.loads(output)
        self.assertTrue(removed["removed"])
        self.assertFalse(service.exists())
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn(f"--user disable --now {data['service_name']}", calls)


if __name__ == "__main__":
    unittest.main()
