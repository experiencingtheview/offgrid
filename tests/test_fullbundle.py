import hashlib
import io
import json
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

from offgrid import (
    fullbundle,
    install_manifest,
    install_transaction,
    portable,
    runtime_identity,
)
from offgrid.cli import main
from tests.test_install_transaction import make_bundle


class FullBundleBuilderTests(unittest.TestCase):
    @staticmethod
    def _snapshot_tree(path):
        return sorted(
            (
                item.relative_to(path).as_posix(),
                item.read_bytes(),
            )
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )

    def _previous_install(self, root):
        device = root / "usb"
        device.mkdir()
        target = device / "offgrid-usb"
        previous = make_bundle(root / "previous", marker="previous")
        install_transaction.install_directory(previous, target)
        return target, self._snapshot_tree(target)

    def _assert_crash_recovery_clean(self, target, before):
        self.assertEqual(self._snapshot_tree(target), before)
        transaction_root = install_transaction.transaction_paths(target).root
        self.assertFalse(
            any(transaction_root.glob("*.journal.json")),
            "an interrupted pre-commit build must not leave a live journal",
        )
        self.assertFalse(any(transaction_root.glob("*.recovery*")))
        stages = list(transaction_root.glob("*.stage-*"))
        leases = list(transaction_root.glob("*.lease-*"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(leases), 1)
        self.assertTrue(json.loads(leases[0].read_text())["abandoned"])

        # Recovery has no journal to act on and must not touch the previous
        # install. The abandoned stage is reclaimable without a next build.
        install_transaction._recover_interrupted(target)
        self.assertEqual(self._snapshot_tree(target), before)
        self.assertFalse(any(transaction_root.glob("*.journal.json")))
        self.assertFalse(any(transaction_root.glob("*.recovery*")))
        cleanup = install_transaction.cleanup_orphan_stages(target)
        self.assertTrue(cleanup.ok, cleanup.errors)
        self.assertEqual(len(cleanup.removed), 1)
        self.assertFalse(stages[0].exists())
        self.assertFalse(leases[0].exists())
        self.assertEqual(self._snapshot_tree(target), before)

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
            model_spec = dict(fullbundle.MODELS["3b"])
            model_spec["sha256"] = hashlib.sha256(model_file.read_bytes()).hexdigest()

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
                    model_spec,
                ),
            ), patch.object(fullbundle, "download", side_effect=fake_download):
                with redirect_stdout(StringIO()):
                    result = fullbundle.build_full_bundle(target, model="3b")

            self.assertTrue(result.complete)
            self.assertEqual(result.target, target.resolve())
            self.assertEqual(result.model_file, model_name)
            self.assertTrue((target / "offgrid" / "cli.py").is_file())
            self.assertTrue((target / "runtime" / python_path).is_file())
            python_runtime = target / "runtime" / "python"
            python_binary = python_runtime / (
                "python.exe" if os_name == "windows" else "bin/python3"
            )
            python_fingerprint = json.loads(
                (python_runtime / fullbundle.RUNTIME_FINGERPRINT_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                python_fingerprint["schema"],
                fullbundle.RUNTIME_FINGERPRINT_SCHEMA,
            )
            self.assertEqual(
                python_fingerprint["binary_sha256"],
                fullbundle.sha256_file(python_binary),
            )
            runtime_binary = target / "runtime" / "llama" / llama_binary
            self.assertTrue(runtime_binary.is_file())
            fingerprint = json.loads(
                (
                    target / "runtime" / "llama" / fullbundle.RUNTIME_FINGERPRINT_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                fingerprint["binary_sha256"],
                fullbundle.sha256_file(runtime_binary),
            )
            self.assertEqual(fingerprint["schema"], fullbundle.RUNTIME_FINGERPRINT_SCHEMA)
            self.assertTrue((target / "models" / model_name).is_file())
            self.assertTrue((target / "config.json").is_file())
            self.assertTrue((target / "README-FIRST.txt").is_file())
            for launcher in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
                self.assertTrue((target / launcher).is_file(), launcher)
            manifest_path = target / install_manifest.MANIFEST_NAME
            self.assertTrue(manifest_path.is_file())
            manifest = install_manifest.load_manifest(manifest_path)
            self.assertEqual(manifest["artifact"]["kind"], "full")
            self.assertEqual(
                manifest["artifact"]["platform"],
                "-".join(fullbundle.platform_key()),
            )
            self.assertEqual(
                manifest["artifact"]["requested_components"],
                {"python": True, "llama": True, "model": True},
            )
            self.assertIn(
                "offgrid/cli.py",
                {entry["path"] for entry in manifest["files"]},
            )
            self.assertIn(
                "config.json",
                {entry["path"] for entry in manifest["user_state"]},
            )
            self.assertEqual(
                {entry["path"] for entry in manifest["runtimes"]},
                {"runtime/llama", "runtime/python"},
            )
            self.assertEqual(len(manifest["models"]), 1)
            self.assertEqual(
                manifest["models"][0]["sha256"],
                model_spec["sha256"],
            )
            report = install_manifest.verify_manifest(target, manifest)
            self.assertTrue(report.ok, report.errors)
            state = json.loads(
                (target / "install-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "committed")
            self.assertEqual(
                state["manifest_digest"],
                install_manifest.manifest_digest(manifest),
            )
            self.assertEqual(state["destination"], str(target.resolve()))
            self.assertEqual(fullbundle.missing_components(target), [])

    def test_direct_builder_commits_from_the_transaction_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "usb" / "offgrid-usb"
            model_name = "fixture-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            observed = {}
            real_install = install_transaction.install_directory

            def check_stage(source, destination, **kwargs):
                staged = kwargs.get("staged")
                self.assertIsNotNone(staged)
                self.assertEqual(staged.stage, source)
                self.assertIn(
                    install_transaction.TRANSACTION_DIR, source.parts
                )
                staged_model = source / "models" / model_name
                self.assertTrue(staged_model.is_file())
                self.assertNotIn("verify_assets", kwargs)
                observed["inode"] = staged_model.stat().st_ino
                return real_install(source, destination, **kwargs)

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                install_transaction,
                "install_directory",
                side_effect=check_stage,
            ):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    result = fullbundle.build_full_bundle(
                        target,
                        model="3b",
                        skip_python=True,
                        skip_llama=True,
                    )

            self.assertTrue(result.complete)
            self.assertEqual(
                os.stat(target / "models" / model_name).st_ino,
                observed["inode"],
            )
            manifest = install_manifest.load_manifest(
                target / install_manifest.MANIFEST_NAME
            )
            self.assertEqual(
                manifest["artifact"]["requested_components"],
                {"python": False, "llama": False, "model": True},
            )

    def test_direct_builder_rejects_a_model_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "usb" / "offgrid-usb"
            model_name = "mismatch-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(b"different bytes").hexdigest()
            output = StringIO()

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(fullbundle, "download", side_effect=fake_download):
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                        )

            self.assertIn("SHA-256 mismatch", output.getvalue())
            self.assertFalse(target.exists())

    def test_direct_builder_rejects_manifest_model_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            model_name = "manifest-mismatch.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            output = StringIO()
            real_write_manifest = fullbundle.write_install_manifest

            def write_then_tamper(stage, **kwargs):
                path = real_write_manifest(stage, **kwargs)
                manifest_path = Path(stage) / install_manifest.MANIFEST_NAME
                manifest = install_manifest.load_manifest(manifest_path)
                # Leave the model bytes intact; only the manifest hash is wrong.
                manifest["models"][0]["sha256"] = hashlib.sha256(
                    b"different model bytes"
                ).hexdigest()
                manifest_path.write_text(
                    json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
                )
                return path

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                fullbundle,
                "write_install_manifest",
                side_effect=write_then_tamper,
            ), patch.object(
                install_transaction, "install_directory"
            ) as install:
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                        )

            install.assert_not_called()
            self.assertEqual(self._snapshot_tree(target), before)
            self.assertIn("staged full bundle did not verify", output.getvalue())
            self.assertIn("SHA-256 mismatch", output.getvalue())
            self.assertIn("the existing destination was not changed", output.getvalue())

    @unittest.skipUnless(os.name == "posix", "symlink fixtures are POSIX-only")
    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is not installed")
    def test_installed_full_bundle_python_runtime_is_launchable_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python_archive = root / "python.tar.gz"
            payload = b"#!/bin/sh\nexit 0\n"
            with tarfile.open(python_archive, "w:gz") as archive:
                binary = tarfile.TarInfo("python/bin/python3")
                binary.size = len(payload)
                binary.mode = 0o755
                archive.addfile(binary, io.BytesIO(payload))
                link = tarfile.TarInfo("python/bin/python")
                link.type = tarfile.SYMTYPE
                link.linkname = "python3"
                archive.addfile(link)
                module = tarfile.TarInfo("python/lib/os.py")
                module_data = b"value = 1\n"
                module.size = len(module_data)
                module.mode = 0o644
                archive.addfile(module, io.BytesIO(module_data))

            model = root / "fixture.gguf"
            model.write_bytes(b"fixture model")
            model_spec = dict(fullbundle.MODELS["3b"])
            model_spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            target = root / "bundle"

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_python",
                return_value=(str(python_archive), python_archive.name, ""),
            ), patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model.name, model_spec),
            ), patch.object(fullbundle, "download", side_effect=fake_download):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    result = fullbundle.build_full_bundle(
                        target, model="3b", skip_llama=True
                    )

            self.assertTrue(result.complete)
            source_runtime = target / "runtime" / "python"
            source_alias = source_runtime / "bin" / "python"
            self.assertFalse(source_alias.is_symlink())
            self.assertEqual(
                source_alias.read_bytes(),
                (source_runtime / "bin" / "python3").read_bytes(),
            )
            source_fingerprint = json.loads(
                (source_runtime / fullbundle.RUNTIME_FINGERPRINT_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                source_fingerprint["binary_sha256"],
                fullbundle.sha256_file(source_runtime / "bin" / "python3"),
            )

            device = root / "device"
            device.mkdir()
            destination = portable.install_bundle(target, device)
            installed_runtime = destination / "runtime" / "python"
            installed_binary = installed_runtime / "bin" / "python3"
            flattened_alias = installed_runtime / "bin" / "python"
            self.assertFalse(flattened_alias.is_symlink())
            self.assertEqual(
                flattened_alias.read_bytes(),
                installed_binary.read_bytes(),
            )

            installed_fingerprint = json.loads(
                (installed_runtime / fullbundle.RUNTIME_FINGERPRINT_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(installed_fingerprint, source_fingerprint)
            self.assertEqual(
                installed_fingerprint["binary_sha256"],
                fullbundle.sha256_file(installed_binary),
            )
            manifest = install_manifest.load_manifest(
                destination / install_manifest.MANIFEST_NAME
            )
            python_entry = next(
                entry
                for entry in manifest["runtimes"]
                if entry["path"] == "runtime/python"
            )
            self.assertEqual(python_entry["binary"], "bin/python3")
            self.assertEqual(
                python_entry["binary_sha256"],
                fullbundle.sha256_file(installed_binary),
            )
            self.assertEqual(
                python_entry["fingerprint"],
                "runtime/python/.offgrid-runtime.json",
            )
            self.assertEqual(
                python_entry["fingerprint_sha256"],
                fullbundle.sha256_file(
                    installed_runtime / fullbundle.RUNTIME_FINGERPRINT_NAME
                ),
            )
            verification = install_manifest.verify_manifest(
                destination, check_assets=True
            )
            self.assertTrue(verification.ok, verification.errors)

            os_name, arch = fullbundle.platform_key()
            digest = runtime_identity.tree_digest(installed_runtime).digest
            table = destination / "launch" / "python-tree-digests.txt"
            self.assertIn(
                f"{os_name}-{arch} {digest}",
                table.read_text(encoding="utf-8"),
            )
            proc = subprocess.run(
                [
                    "sh",
                    str(destination / "launch" / "bootstrap.sh"),
                    "--verify-only",
                    str(installed_runtime),
                ],
                cwd=str(destination),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_direct_builder_keeps_the_target_untouched_when_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "usb" / "offgrid-usb"
            target.mkdir(parents=True)
            sentinel = target / "previous-install.txt"
            sentinel.write_text("keep me\n", encoding="utf-8")

            with patch.object(
                install_transaction,
                "install_directory",
                side_effect=install_transaction.InstallError(
                    "injected commit failure"
                ),
            ):
                with self.assertRaises(fullbundle.FullBundleError):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse((target / "offgrid").exists())
            self.assertFalse((target / "install-state.json").exists())

    def test_direct_builder_refuses_partial_model_without_replacing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            model_name = "fixture-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            output = StringIO()

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle,
                "download",
                side_effect=OSError("simulated model download failure"),
            ), patch.object(
                install_transaction, "install_directory"
            ) as install:
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                        )

            install.assert_not_called()
            self.assertEqual(self._snapshot_tree(target), before)
            self.assertIn(
                "refusing to commit an incomplete full bundle", output.getvalue()
            )
            self.assertIn("the GGUF model", output.getvalue())

    def test_staged_hash_failure_does_not_touch_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            model_name = "fixture-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            output = StringIO()
            real_write_manifest = fullbundle.write_install_manifest

            def write_then_corrupt(stage, **kwargs):
                path = real_write_manifest(stage, **kwargs)
                (Path(stage) / "models" / model_name).write_bytes(b"bad" * 4)
                return path

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                fullbundle,
                "write_install_manifest",
                side_effect=write_then_corrupt,
            ), patch.object(
                install_transaction, "install_directory"
            ) as install:
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                        )

            install.assert_not_called()
            self.assertEqual(self._snapshot_tree(target), before)
            self.assertIn("staged full bundle did not verify", output.getvalue())
            self.assertIn("SHA-256 mismatch", output.getvalue())

    def test_staged_extra_file_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            model_name = "fixture-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            output = StringIO()
            real_write_manifest = fullbundle.write_install_manifest

            def write_then_add_extra(stage, **kwargs):
                path = real_write_manifest(stage, **kwargs)
                (Path(stage) / "unexpected-extra.txt").write_text(
                    "not in the manifest\n", encoding="utf-8"
                )
                return path

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                fullbundle,
                "write_install_manifest",
                side_effect=write_then_add_extra,
            ), patch.object(
                install_transaction, "install_directory"
            ) as install:
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                            skip_llama=True,
                        )

            install.assert_not_called()
            self.assertEqual(self._snapshot_tree(target), before)
            self.assertIn("staged full bundle did not verify", output.getvalue())
            self.assertIn(
                "unexpected file: unexpected-extra.txt", output.getvalue()
            )

    def test_full_bundle_hashes_staged_assets_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "usb" / "offgrid-usb"
            model_name = "fixture-model.gguf"
            model = root / model_name
            model.write_bytes(b"fixture gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            asset_checks = []
            observed = {}
            real_verify = install_manifest.verify_manifest
            real_verify_staged = install_transaction.verify_staged_directory
            real_install = install_transaction.install_directory

            def counted(root_path, manifest=None, **kwargs):
                if kwargs.get("check_assets"):
                    asset_checks.append(Path(root_path))
                return real_verify(root_path, manifest, **kwargs)

            def capture_verification(*args, **kwargs):
                report = real_verify_staged(*args, **kwargs)
                observed["verification"] = report
                return report

            def capture_install(source, destination, **kwargs):
                observed["verified"] = kwargs.get("verified")
                return real_install(source, destination, **kwargs)

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                install_manifest, "verify_manifest", side_effect=counted
            ), patch.object(
                install_transaction,
                "verify_staged_directory",
                side_effect=capture_verification,
            ), patch.object(
                install_transaction,
                "install_directory",
                side_effect=capture_install,
            ):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    result = fullbundle.build_full_bundle(
                        target,
                        model="3b",
                        skip_python=True,
                        skip_llama=True,
                    )

            self.assertTrue(result.complete)
            self.assertEqual(len(asset_checks), 1, asset_checks)
            self.assertIs(observed["verified"], observed["verification"])
            self.assertTrue(observed["verified"].report.ok)

    def test_direct_builder_refuses_missing_runtime_without_replacing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            output = StringIO()

            with patch.object(
                fullbundle, "resolve_python", return_value=None
            ), patch.object(
                install_transaction, "install_directory"
            ) as install:
                with redirect_stdout(output), redirect_stderr(output):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_llama=True,
                            skip_model=True,
                        )

            install.assert_not_called()
            self.assertEqual(self._snapshot_tree(target), before)
            self.assertIn(
                "refusing to commit an incomplete full bundle", output.getvalue()
            )
            self.assertIn("portable Python", output.getvalue())

    def test_incomplete_refusal_keeps_model_archive_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            model_name = "retained-model.gguf"
            model = root / model_name
            model.write_bytes(b"retained gguf")
            spec = dict(fullbundle.MODELS["3b"])
            spec["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
            model_downloads = []

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination = Path(destination)
                model_downloads.append(destination.is_file())
                if destination.is_file():
                    return
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle, "resolve_llama", return_value=None
            ), patch.object(
                fullbundle,
                "resolve_model",
                return_value=(str(model), model_name, spec),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ):
                first = StringIO()
                with redirect_stdout(first), redirect_stderr(first):
                    with self.assertRaises(fullbundle.FullBundleError):
                        fullbundle.build_full_bundle(
                            target,
                            model="3b",
                            skip_python=True,
                        )

                self.assertEqual(self._snapshot_tree(target), before)
                paths = install_transaction.transaction_paths(target)
                cached = fullbundle._kept_downloads_path(paths)
                self.assertEqual((cached / model_name).read_bytes(), model.read_bytes())
                self.assertIn("downloaded archives kept in", first.getvalue())

                second = StringIO()
                with redirect_stdout(second), redirect_stderr(second):
                    result = fullbundle.build_full_bundle(
                        target,
                        model="3b",
                        skip_python=True,
                        skip_llama=True,
                    )

            self.assertTrue(result.complete)
            self.assertEqual(
                (target / "models" / model_name).read_bytes(), model.read_bytes()
            )
            self.assertEqual(model_downloads, [False, True])
            self.assertFalse(cached.exists())

    def test_direct_builder_interrupt_during_staging_keeps_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "usb" / "offgrid-usb"
            previous = make_bundle(root / "previous", marker="previous")
            install_transaction.install_directory(previous, target)
            before = self._snapshot_tree(target)

            with patch.object(
                fullbundle,
                "write_launchers",
                side_effect=KeyboardInterrupt("simulated interruption during staging"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            transaction_root = install_transaction.transaction_paths(target).root
            stages = list(transaction_root.glob("*.stage-*"))
            self.assertEqual(len(stages), 1)
            self.assertTrue(any(path.is_file() for path in stages[0].rglob("*")))

            # The previous install is untouched before recovery; recovery has
            # no journal to act on and must leave it untouched afterwards too.
            # The partial stage is left for the normal storage-cleanup policy.
            self._assert_crash_recovery_clean(target, before)

    def test_direct_builder_interrupt_during_staging_leaves_target_absent_without_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb" / "offgrid-usb"
            self.assertFalse(
                target.exists(), "this scenario must start without a previous install"
            )

            with patch.object(
                fullbundle,
                "write_launchers",
                side_effect=KeyboardInterrupt(
                    "simulated interruption during staging"
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            # The stage received content, but the interruption happened before
            # any commit. With no previous install, the target must stay absent.
            self.assertFalse(target.exists())
            transaction_root = install_transaction.transaction_paths(target).root
            stages = list(transaction_root.glob("*.stage-*"))
            self.assertEqual(len(stages), 1)
            self.assertTrue(any(path.is_file() for path in stages[0].rglob("*")))
            self.assertFalse(
                any(transaction_root.glob("*.journal.json")),
                "an interrupted pre-commit build must not leave a live journal",
            )

            # No journal exists and there is no previous install to restore.
            # Recovery must still leave the target absent.
            install_transaction._recover_interrupted(target)
            self.assertFalse(target.exists())
            self.assertTrue(stages[0].is_dir())

    def test_interrupt_after_staging_keeps_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, before = self._previous_install(Path(tmp))
            real_write_launchers = fullbundle.write_launchers

            def write_launchers_then_interrupt(*args, **kwargs):
                real_write_launchers(*args, **kwargs)
                raise KeyboardInterrupt("simulated interruption after staging")

            with patch.object(
                fullbundle,
                "write_launchers",
                side_effect=write_launchers_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            self._assert_crash_recovery_clean(target, before)

    def test_interrupt_after_runtime_extraction_keeps_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, before = self._previous_install(root)
            python_archive = root / "python.tar.gz"
            payload = b"#!/bin/sh\nexit 0\n"
            with tarfile.open(python_archive, "w:gz") as archive:
                info = tarfile.TarInfo("python/bin/python3")
                info.size = len(payload)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(payload))

            real_extract = fullbundle.extract

            def extract_then_interrupt(archive, destination):
                real_extract(archive, destination)
                raise KeyboardInterrupt("simulated interruption after extraction")

            def fake_download(url, destination, timeout=120, expected_sha256=""):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(url, destination)

            with patch.object(
                fullbundle,
                "resolve_python",
                return_value=(str(python_archive), python_archive.name),
            ), patch.object(
                fullbundle, "download", side_effect=fake_download
            ), patch.object(
                fullbundle, "extract", side_effect=extract_then_interrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_llama=True,
                            skip_model=True,
                        )

            self._assert_crash_recovery_clean(target, before)

    def test_interrupt_after_manifest_writing_keeps_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, before = self._previous_install(Path(tmp))
            real_write_manifest = fullbundle.write_install_manifest

            def write_then_interrupt(stage, **kwargs):
                real_write_manifest(stage, **kwargs)
                raise KeyboardInterrupt("simulated interruption after manifest")

            with patch.object(
                fullbundle,
                "write_install_manifest",
                side_effect=write_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            self._assert_crash_recovery_clean(target, before)

    def test_interrupt_before_commit_keeps_previous_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, before = self._previous_install(Path(tmp))

            with patch.object(
                install_transaction,
                "install_directory",
                side_effect=KeyboardInterrupt("simulated interruption before commit"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            self._assert_crash_recovery_clean(target, before)

    def test_next_build_reclaims_an_interrupted_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb" / "offgrid-usb"
            real_write_launchers = fullbundle.write_launchers

            def write_launchers_then_interrupt(*args, **kwargs):
                real_write_launchers(*args, **kwargs)
                raise KeyboardInterrupt("simulated interruption after staging")

            with patch.object(
                fullbundle,
                "write_launchers",
                side_effect=write_launchers_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        fullbundle.build_full_bundle(
                            target,
                            skip_python=True,
                            skip_llama=True,
                            skip_model=True,
                        )

            transaction_root = target.parent / install_transaction.TRANSACTION_DIR
            interrupted = sorted(transaction_root.glob("*.stage-*"))
            self.assertEqual(len(interrupted), 1)
            leases = sorted(transaction_root.glob("*.lease-*"))
            self.assertEqual(len(leases), 1)
            self.assertTrue(json.loads(leases[0].read_text())["abandoned"])

            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = fullbundle.main(
                    [
                        "--target",
                        str(target),
                        "--skip-python",
                        "--skip-llama",
                        "--skip-model",
                    ]
                )

            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("reclaimed 1 abandoned staging directory", output.getvalue())
            self.assertFalse(interrupted[0].exists())
            self.assertFalse(leases[0].exists())
            self.assertTrue((target / "offgrid" / "cli.py").is_file())

    def test_build_preserves_another_active_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "usb"
            device.mkdir()
            target = device / "offgrid-usb"
            active = install_transaction.prepare_directory_stage(target)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    code = fullbundle.main(
                        [
                            "--target",
                            str(target),
                            "--skip-python",
                            "--skip-llama",
                            "--skip-model",
                        ]
                    )

                self.assertEqual(code, 0)
                self.assertTrue(active.stage.is_dir())
                self.assertTrue(active.lease.is_file())
            finally:
                install_transaction.discard_directory_stage(active)

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

    def test_12b_is_a_new_agentic_model_option(self):
        spec = fullbundle.MODELS["12b"]
        self.assertEqual(
            spec["repo"], "JetBrains/Mellum2.1-12B-A2.5B-Thinking-GGUF"
        )
        self.assertEqual(
            spec["file"], "Mellum2.1-12B-A2.5B-Thinking-Q4_K_M.gguf"
        )
        self.assertEqual(
            spec["sha256"],
            "ecc4d5b8107fc219e23c494b2d107552137637b6c6002ee713e0ba8af7fa2755",
        )
        self.assertEqual(spec["min_ram_gb"], 15)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "usb"
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = fullbundle.main(
                    ["--dry-run", "--target", str(target), "--model", "12b"]
                )
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("Mellum2.1-12B-A2.5B-Thinking", output.getvalue())
            self.assertIn("about 8.1 GB", output.getvalue())
            self.assertIn("16 GB recommended", output.getvalue())

    def test_12b_warns_below_its_15_gb_floor(self):
        warning = fullbundle.ram_warning("12b", 8)
        self.assertIn("15 GB", warning)
        self.assertIn("16 GB recommended", warning)
        self.assertIn("8.0 GB", warning)
        self.assertIn("smaller model", warning)
        self.assertEqual(fullbundle.ram_warning("12b", 15), "")
        self.assertEqual(fullbundle.ram_warning("12b", 16), "")

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

    def test_release_asset_digest_is_used_as_a_checksum(self):
        digest = "a" * 64
        self.assertEqual(
            fullbundle._asset_sha256({"digest": f"sha256:{digest}"}), digest
        )
        self.assertEqual(fullbundle._asset_sha256({"digest": "not-a-digest"}), "")
        self.assertEqual(
            fullbundle._resolution(("https://example.invalid/a", "a.zip"))[2], ""
        )
        self.assertEqual(
            fullbundle._resolution(("https://example.invalid/a", "a.zip", digest))[2],
            digest,
        )

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
