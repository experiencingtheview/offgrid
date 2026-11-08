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

            def fake_download(url, destination, timeout=120):
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
            self.assertEqual(fullbundle.missing_components(target), [])

    def test_missing_components_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            missing = fullbundle.missing_components(target)
            self.assertIn("the OffGrid source", missing)
            self.assertIn("portable Python", missing)
            self.assertIn("llama.cpp", missing)
            self.assertIn("the GGUF model", missing)


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
