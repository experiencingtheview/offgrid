import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from offgrid import fullbundle, portable
from offgrid.cli import main


class FullBundleBuilderTests(unittest.TestCase):
    def test_dry_run_plans_python_llama_and_model_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb" / "offgrid-usb"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = fullbundle.main(
                    ["--dry-run", "--target", str(target), "--model", "3b"]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("python     ", output.getvalue())
            self.assertIn("llama.cpp", output.getvalue())
            self.assertIn("Qwen2.5-Coder-3B", output.getvalue())
            self.assertFalse(target.exists())

    def test_builder_writes_runtime_llama_model_and_launchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            target = root / "usb" / "offgrid-usb"
            os_name, _ = fullbundle.platform_key()
            llama_binary = "llama-server.exe" if os_name == "windows" else "llama-server"
            python_path = "python/python.exe" if os_name == "windows" else "python/bin/python3"

            llama_archive = fixtures / "llama.zip"
            with zipfile.ZipFile(llama_archive, "w") as archive:
                archive.writestr(llama_binary, "fake llama server")

            python_archive = fixtures / "python.tar.gz"
            payload = b"fake python"
            with tarfile.open(python_archive, "w:gz") as archive:
                info = tarfile.TarInfo(python_path)
                info.size = len(payload)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(payload))

            model_name = "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"
            model_file = fixtures / model_name
            model_file.write_bytes(b"fake gguf")

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_llama",
                return_value=(str(llama_archive), llama_archive.name),
            ), patch.object(
                fullbundle,
                "resolve_python",
                return_value=(str(python_archive), python_archive.name),
            ), patch.object(
                fullbundle,
                "resolve_model",
                return_value=(
                    str(model_file),
                    model_name,
                    fullbundle.MODELS["3b"],
                ),
            ), patch.object(fullbundle, "download", side_effect=fake_download):
                with redirect_stdout(StringIO()):
                    result = fullbundle.build_full_bundle(target, model="3b")

            self.assertTrue(result.complete)
            self.assertEqual(result.target, target.resolve())
            self.assertEqual(result.model_file, model_name)
            self.assertTrue((target / "offgrid" / "cli.py").is_file())
            self.assertTrue((target / "runtime" / python_path).is_file())
            self.assertTrue((target / "runtime" / "llama" / llama_binary).is_file())
            self.assertTrue((target / "models" / model_name).is_file())
            self.assertTrue((target / "config.json").is_file())
            self.assertTrue((target / "README-FIRST.txt").is_file())
            for launcher in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
                self.assertTrue((target / launcher).is_file(), launcher)
            self.assertEqual(fullbundle.missing_components(target), [])

    def test_35b_is_a_real_model_option(self):
        spec = fullbundle.MODELS["35b"]
        self.assertEqual(spec["repo"], "ggml-org/Qwen3.6-35B-A3B-GGUF")
        self.assertEqual(spec["file"], "Qwen3.6-35B-A3B-Q4_K_M.gguf")
        self.assertEqual(len(str(spec["sha256"])), 64)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = fullbundle.main(
                    ["--dry-run", "--target", str(target), "--model", "35b"]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("Qwen3.6-35B-A3B", output.getvalue())
            self.assertIn("about 20.5 GB", output.getvalue())

    def test_80b_is_opt_in_and_carries_a_pinned_hash(self):
        spec = fullbundle.MODELS["80b"]
        self.assertEqual(spec["repo"], "Qwen/Qwen3-Next-80B-A3B-Instruct-GGUF")
        self.assertEqual(spec["file"], "Qwen3-Next-80B-A3B-Instruct-Q4_K_M.gguf")
        self.assertEqual(
            spec["sha256"],
            "d103b2733ec1012a52d01edda66b7e5c24ae50508c9f99f5297ea459ef3c061a",
        )
        self.assertEqual(spec["min_ram_gb"], 64)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = fullbundle.main(
                    ["--dry-run", "--target", str(target), "--model", "80b"]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("Qwen3-Next-80B-A3B", output.getvalue())
            self.assertIn("about 48.4 GB", output.getvalue())

    def test_80b_ram_warning_is_clear_below_64_gb(self):
        warning = fullbundle.ram_warning("80b", 16)
        self.assertIn("64 GB", warning)
        self.assertIn("16.0 GB", warning)
        self.assertIn("disk", warning)
        self.assertEqual(fullbundle.ram_warning("80b", 64), "")
        self.assertIn("could not be detected", fullbundle.ram_warning("80b", 0))
        self.assertEqual(fullbundle.ram_warning("35b", 16), "")

    def test_disk_preflight_rejects_a_short_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fullbundle, "free_space_gb", return_value=10.0):
                with self.assertRaisesRegex(
                    fullbundle.FullBundleError, "not enough free space"
                ):
                    fullbundle.ensure_free_space(Path(tmp), 50.0)

    def test_direct_builder_stops_before_writing_when_disk_is_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb"
            output = StringIO()
            with patch.object(fullbundle, "free_space_gb", return_value=1.0):
                with redirect_stdout(output), redirect_stderr(StringIO()):
                    code = fullbundle.main(
                        [
                            "--target",
                            str(target),
                            "--model",
                            "80b",
                            "--skip-python",
                            "--skip-llama",
                            "--skip-model",
                        ]
                    )
            self.assertEqual(code, 1)
            self.assertIn("not enough free space", output.getvalue())
            self.assertFalse(target.exists())

    def test_bad_download_checksum_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.gguf"
            path.write_bytes(b"not the expected weights")
            with self.assertRaises(OSError):
                fullbundle._verify_download(path, "0" * 64)
            self.assertFalse(path.exists())

    def test_missing_components_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            missing = fullbundle.missing_components(target)
            self.assertIn("the OffGrid source", missing)
            self.assertIn("portable Python", missing)
            self.assertIn("llama.cpp", missing)
            self.assertIn("the GGUF model", missing)


class FullBundleExtractionTests(unittest.TestCase):
    """Tar extraction must never write or link outside the destination."""

    def _assert_traversal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("../escape.txt")
                info.size = 5
                tf.addfile(info, io.BytesIO(b"pwned"))
            with self.assertRaises(fullbundle.FullBundleError):
                fullbundle.extract(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_extracts_a_normal_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "good.tar.gz"
            payload = b"ok"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("pkg/file.txt")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            destination = root / "out"
            fullbundle.extract(archive, destination)
            self.assertEqual((destination / "pkg" / "file.txt").read_bytes(), payload)

    def test_refuses_traversal_entry(self):
        self._assert_traversal_refused()

    def test_refuses_traversal_entry_without_data_filter(self):
        if not hasattr(tarfile, "data_filter"):
            self._assert_traversal_refused()
            return
        saved = tarfile.data_filter
        del tarfile.data_filter
        try:
            self._assert_traversal_refused()
        finally:
            tarfile.data_filter = saved

    def test_refuses_absolute_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "absolute.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo(str(root / "absolute-escape.txt"))
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
            with self.assertRaises(fullbundle.FullBundleError):
                fullbundle.extract(archive, root / "out")
            self.assertFalse((root / "absolute-escape.txt").exists())

    def test_refuses_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "symlink.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside"
                tf.addfile(info)
            with self.assertRaises(fullbundle.FullBundleError):
                fullbundle.extract(archive, root / "out")

    def test_refuses_hardlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hardlink.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("hard")
                info.type = tarfile.LNKTYPE
                info.linkname = "../outside"
                tf.addfile(info)
            with self.assertRaises(fullbundle.FullBundleError):
                fullbundle.extract(archive, root / "out")

    def test_keeps_an_internal_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "internal.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                real = tarfile.TarInfo("lib/real")
                real.size = 2
                tf.addfile(real, io.BytesIO(b"hi"))
                link = tarfile.TarInfo("bin/tool")
                link.type = tarfile.SYMTYPE
                link.linkname = "../lib/real"
                tf.addfile(link)
            destination = root / "out"
            fullbundle.extract(archive, destination)
            self.assertTrue((destination / "bin" / "tool").is_symlink())


class FullBundleScriptTests(unittest.TestCase):
    def test_standalone_setup_script_still_plans_the_full_bundle(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "setup_usb.py"),
                    "--dry-run",
                    "--target",
                    str(target),
                    "--model",
                    "3b",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("total download", proc.stdout)
            self.assertIn("Qwen2.5-Coder-3B", proc.stdout)
            self.assertFalse(target.exists())


class FullBundleWizardTests(unittest.TestCase):
    def test_wizard_refuses_80b_when_free_space_is_short_before_building(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as media:
            device = Path(media) / "USB"
            device.mkdir()
            output = StringIO()
            errors = StringIO()
            inputs = ["4", "1", "offgrid-usb", "80b"]
            with patch.object(
                fullbundle,
                "ensure_free_space",
                side_effect=fullbundle.FullBundleError(
                    "not enough free space for test: need about 50.0 GB"
                ),
            ), patch(
                "offgrid.portable.connected_devices",
                return_value=[portable.Device(device, "USB")],
            ), patch("offgrid.fullbundle.build_full_bundle") as build, patch(
                "builtins.input", side_effect=inputs
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    code = main(
                        [
                            "distribute",
                            "--wizard",
                            "--workspace",
                            workspace,
                            "--no-color",
                        ]
                    )
            self.assertEqual(code, 1)
            self.assertIn("not enough free space", errors.getvalue())
            build.assert_not_called()

    def test_wizard_builds_a_full_bundle_on_the_selected_device(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as media:
            device = Path(media) / "USB"
            device.mkdir()
            target = device / "offgrid-usb"
            calls = []

            def fake_build(destination, model="7b", **kwargs):
                calls.append((Path(destination), model, kwargs))
                Path(destination).mkdir(parents=True)
                return fullbundle.FullBundleResult(
                    Path(destination), model, "Qwen.gguf", True
                )

            output = StringIO()
            with patch("offgrid.fullbundle.build_full_bundle", side_effect=fake_build):
                with patch(
                    "offgrid.portable.connected_devices",
                    return_value=[portable.Device(device, "USB")],
                ):
                    with patch(
                        "builtins.input",
                        side_effect=["4", "1", "offgrid-usb", "3b", "build"],
                    ):
                        with redirect_stdout(output):
                            code = main(
                                [
                                    "distribute",
                                    "--wizard",
                                    "--workspace",
                                    workspace,
                                    "--no-color",
                                ]
                            )

            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(calls, [(target, "3b", {})])
            self.assertIn(str(target), output.getvalue())


if __name__ == "__main__":
    unittest.main()
