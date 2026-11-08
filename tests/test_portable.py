import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid import portable
from offgrid.cli import main


class PortableBuildTests(unittest.TestCase):
    def test_build_distribution_has_source_launchers_and_single_file_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, pyz = portable.build_distribution(root / "offgrid.zip")

            self.assertTrue(bundle.is_file())
            self.assertTrue(pyz.is_file())
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
            prefix = portable.BUNDLE_PREFIX
            for name in (
                f"{prefix}/README-FIRST.txt",
                f"{prefix}/offgrid.pyz",
                f"{prefix}/offgrid/cli.py",
                f"{prefix}/offgrid/fullbundle.py",
                f"{prefix}/launch/offgrid.sh",
                f"{prefix}/scripts/setup_usb.py",
            ):
                self.assertIn(name, names)

            proc = subprocess.run(
                [sys.executable, str(pyz), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("offgrid", proc.stdout)

    def test_cli_can_build_the_downloadable_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "cli-bundle.zip"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "distribute",
                        "--build",
                        str(target),
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue(target.is_file())
            self.assertTrue(target.with_suffix(".pyz").is_file())


class PortableInstallTests(unittest.TestCase):
    def test_install_unpacks_into_a_device_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = portable.install_bundle(bundle, device)

            self.assertEqual(destination, device / portable.BUNDLE_PREFIX)
            self.assertTrue((destination / "offgrid.pyz").is_file())
            self.assertTrue((destination / "README-FIRST.txt").is_file())
            if os.name == "posix":
                self.assertTrue(os.access(destination / "launch" / "offgrid.sh", os.X_OK))
            with self.assertRaises(portable.PortableError):
                portable.install_bundle(bundle, device)
            portable.install_bundle(bundle, device, force=True)

    def test_install_accepts_a_single_file_pyz(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pyz = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = portable.install_bundle(pyz, device)
            self.assertEqual(destination, device / pyz.name)
            self.assertTrue(destination.is_file())

    def test_install_refuses_the_system_root_and_zip_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            with self.assertRaises(portable.PortableError):
                portable.install_bundle(bundle, Path("/"))

            malicious = root / "malicious.zip"
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr("offgrid-0.1.0/../../escaped.txt", "no")
            device = root / "device"
            device.mkdir()
            with self.assertRaises(portable.PortableError):
                portable.install_bundle(malicious, device)
            self.assertFalse((root / "escaped.txt").exists())

    def test_connected_devices_lists_mounted_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "USB").mkdir()
            (root / ".hidden").mkdir()
            (root / "not-a-device.txt").write_text("x", encoding="utf-8")
            devices = portable.connected_devices([root])
            self.assertEqual([device.path for device in devices], [(root / "USB").resolve()])


class DistributeCliTests(unittest.TestCase):
    def test_wizard_builds_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wizard-bundle.zip"
            output = StringIO()
            with patch("builtins.input", side_effect=["1", str(target), "n"]):
                with redirect_stdout(output):
                    code = main(["distribute", "--wizard", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue(target.is_file())
            self.assertTrue(target.with_suffix(".pyz").is_file())

    def test_yes_installs_bundle_without_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "distribute",
                        "--to",
                        str(device),
                        "--bundle",
                        str(bundle),
                        "--yes",
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue((device / portable.BUNDLE_PREFIX / "offgrid.pyz").is_file())


if __name__ == "__main__":
    unittest.main()
