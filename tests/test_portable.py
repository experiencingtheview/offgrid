import hashlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid import portable
from offgrid.cli import main


def make_full_bundle(root, name="offgrid-usb"):
    bundle = root / name
    (bundle / "offgrid").mkdir(parents=True)
    (bundle / "offgrid" / "cli.py").write_text("# source\n", encoding="utf-8")
    (bundle / "launch").mkdir()
    (bundle / "launch" / "offgrid.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (bundle / "config.json").write_text("{}\n", encoding="utf-8")
    return bundle


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
                f"{prefix}/OffGrid.sh",
                f"{prefix}/OffGrid.command",
                f"{prefix}/OffGrid.bat",
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
            self.assertTrue((destination / "OffGrid.sh").is_file())
            if os.name == "posix":
                self.assertTrue(os.access(destination / "OffGrid.sh", os.X_OK))
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

    def test_install_copies_a_full_bundle_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = make_full_bundle(root)
            device = root / "device"
            device.mkdir()

            destination = portable.install_bundle(bundle, device)

            self.assertEqual(destination, device / bundle.name)
            self.assertTrue((destination / "offgrid" / "cli.py").is_file())
            self.assertTrue((destination / "launch" / "offgrid.sh").is_file())
            self.assertTrue((destination / "config.json").is_file())
            if os.name == "posix":
                self.assertTrue(os.access(destination / "launch" / "offgrid.sh", os.X_OK))
            with self.assertRaisesRegex(portable.PortableError, "already exists"):
                portable.install_bundle(bundle, device)
            (bundle / "config.json").write_text('{"offline": true}\n', encoding="utf-8")
            portable.install_bundle(bundle, device, force=True)
            self.assertIn("offline", (destination / "config.json").read_text(encoding="utf-8"))

    def test_install_can_dry_run_and_rejects_an_invalid_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = make_full_bundle(root)
            device = root / "device"
            device.mkdir()

            destination = portable.install_bundle(bundle, device, dry_run=True)
            self.assertFalse(destination.exists())

            not_a_bundle = root / "not-a-bundle"
            not_a_bundle.mkdir()
            with self.assertRaisesRegex(portable.PortableError, "launch"):
                portable.install_bundle(not_a_bundle, device)

    def test_install_refuses_to_copy_a_bundle_into_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = make_full_bundle(root)
            nested_device = bundle / "device"
            nested_device.mkdir()

            with self.assertRaisesRegex(portable.PortableError, "inside the source"):
                portable.install_bundle(bundle, nested_device)

    def test_full_bundle_directory_dereferences_internal_links_and_rejects_escapes(self):
        if os.name != "posix":
            self.skipTest("symlink creation is not reliable on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = make_full_bundle(root)
            (bundle / "alias.py").symlink_to(bundle / "offgrid" / "cli.py")
            device = root / "device"
            device.mkdir()

            destination = portable.install_bundle(bundle, device)
            self.assertEqual(
                (destination / "alias.py").read_text(encoding="utf-8"), "# source\n"
            )
            self.assertFalse((destination / "alias.py").is_symlink())

            outside = root / "outside.txt"
            outside.write_text("do not copy\n", encoding="utf-8")
            (bundle / "escape.txt").symlink_to(outside)
            with self.assertRaisesRegex(portable.PortableError, "outside the bundle"):
                portable.install_bundle(bundle, device)

    def test_connected_devices_lists_mounted_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "USB").mkdir()
            (root / ".hidden").mkdir()
            (root / "not-a-device.txt").write_text("x", encoding="utf-8")
            devices = portable.connected_devices([root])
            self.assertEqual([device.path for device in devices], [(root / "USB").resolve()])


class DriveReadinessTests(unittest.TestCase):
    def test_drive_check_requires_exfat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="ext4"):
                with self.assertRaisesRegex(portable.PortableError, "not exFAT"):
                    portable.check_usb_drive(device, bundle)

    def test_drive_check_reports_space_and_no_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ):
                report = portable.check_usb_drive(device, bundle)
            self.assertEqual(report.filesystem, "exfat")
            self.assertEqual(report.free_gb, 10.0)
            self.assertGreaterEqual(report.required_gb, 1.0)
            self.assertEqual(report.model_files, ())
            self.assertFalse(report.model_checked)
            self.assertEqual(report.as_dict()["target"], str(device.resolve()))

    def test_drive_check_rejects_insufficient_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=128 * 1024 ** 2
                )
            ):
                with self.assertRaisesRegex(portable.PortableError, "not enough free space"):
                    portable.check_usb_drive(device, bundle)

    def test_drive_check_verifies_a_model_inside_a_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "full-bundle.zip"
            model_name = "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(f"offgrid-test/models/{model_name}", b"weights")
            expected = hashlib.sha256(b"weights").hexdigest()
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ), patch.object(portable, "_known_model_hashes", return_value={model_name: expected}):
                report = portable.check_usb_drive(device, bundle)
            self.assertEqual(report.model_files, (model_name,))
            self.assertTrue(report.model_checked)

    def test_drive_check_verifies_known_model_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "full-bundle"
            models = bundle / "models"
            models.mkdir(parents=True)
            model = models / "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
            model.write_bytes(b"weights")
            expected = hashlib.sha256(b"weights").hexdigest()
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ), patch.object(portable, "_known_model_hashes", return_value={model.name: expected}):
                report = portable.check_usb_drive(device, bundle)
            self.assertEqual(report.model_files, (model.name,))
            self.assertTrue(report.model_checked)

    def test_drive_check_rejects_a_model_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "full-bundle"
            models = bundle / "models"
            models.mkdir(parents=True)
            model = models / "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
            model.write_bytes(b"weights")
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ), patch.object(portable, "_known_model_hashes", return_value={model.name: "0" * 64}):
                with self.assertRaisesRegex(portable.PortableError, "SHA-256 mismatch"):
                    portable.check_usb_drive(device, bundle)

    def test_drive_check_rejects_unknown_models_and_fat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "full-bundle"
            models = bundle / "models"
            models.mkdir(parents=True)
            (models / "custom.gguf").write_bytes(b"weights")
            device = root / "device"
            device.mkdir()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ), patch.object(portable, "_known_model_hashes", return_value={}):
                with self.assertRaisesRegex(portable.PortableError, "no pinned SHA-256"):
                    portable.check_usb_drive(device, bundle)
            with patch.object(portable, "filesystem_type", return_value="vfat"):
                with self.assertRaisesRegex(portable.PortableError, "4 GB"):
                    portable.check_usb_drive(device, bundle, require_exfat=False)


class DistributeCliTests(unittest.TestCase):
    def test_check_drive_cli_reports_a_ready_exfat_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            with patch.object(portable, "filesystem_type", return_value="exfat"), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ):
                with redirect_stdout(output):
                    code = main(
                        [
                            "distribute",
                            "--check-drive",
                            str(device),
                            "--bundle",
                            str(bundle),
                            "--workspace",
                            tmp,
                            "--no-color",
                        ]
                    )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("drive ready", output.getvalue())
            self.assertIn("exfat", output.getvalue())
            self.assertIn("checksum not applicable", output.getvalue())

    def test_to_refuses_a_non_exfat_mounted_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            errors = StringIO()
            with patch.object(portable, "is_mount_point", return_value=True), patch.object(
                portable, "filesystem_type", return_value="ext4"
            ):
                with redirect_stdout(output), redirect_stderr(errors):
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
            self.assertEqual(code, 1)
            self.assertIn("not exFAT", errors.getvalue())
            self.assertFalse((device / portable.BUNDLE_PREFIX).exists())

    def test_to_installs_a_full_bundle_directory_after_checking_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = make_full_bundle(root)
            device = root / "device"
            device.mkdir()
            output = StringIO()
            with patch.object(portable, "is_mount_point", return_value=True), patch.object(
                portable, "filesystem_type", return_value="exfat"
            ), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ):
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
            self.assertIn("drive check: exfat", output.getvalue())
            self.assertTrue(
                (device / bundle.name / "offgrid" / "cli.py").is_file()
            )

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

    def test_wizard_installs_only_after_the_drive_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            with patch.object(portable, "is_mount_point", return_value=True), patch.object(
                portable, "filesystem_type", return_value="exfat"
            ), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ), patch(
                "builtins.input",
                side_effect=["2", str(device), str(bundle), "install"],
            ):
                with redirect_stdout(output):
                    code = main(["distribute", "--wizard", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("checking drive", output.getvalue())
            self.assertIn("drive check: exfat", output.getvalue())
            self.assertIn("model checksum: not applicable", output.getvalue())
            self.assertTrue((device / portable.BUNDLE_PREFIX / "offgrid.pyz").is_file())

    def test_wizard_refuses_to_install_on_a_non_exfat_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            errors = StringIO()
            with patch.object(portable, "is_mount_point", return_value=True), patch.object(
                portable, "filesystem_type", return_value="ext4"
            ), patch(
                "builtins.input",
                side_effect=["2", str(device), str(bundle), "install"],
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    code = main(["distribute", "--wizard", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 1)
            self.assertIn("not exFAT", errors.getvalue())
            self.assertFalse((device / portable.BUNDLE_PREFIX).exists())

    def test_yes_installs_bundle_without_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, _ = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            output = StringIO()
            with patch.object(portable, "is_mount_point", return_value=False), patch.object(
                portable, "filesystem_type", return_value="ext4"
            ), patch.object(
                portable.shutil, "disk_usage", return_value=types.SimpleNamespace(
                    free=10 * 1024 ** 3
                )
            ):
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
