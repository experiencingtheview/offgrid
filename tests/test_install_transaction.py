import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from offgrid import (
    install_manifest,
    install_transaction,
    portable,
    recovery,
    runtime_identity,
)


def make_bundle(
    root,
    *,
    name="offgrid-usb",
    marker="v1",
    model=False,
    runtime=False,
):
    bundle = root / name
    (bundle / "offgrid").mkdir(parents=True)
    (bundle / "offgrid" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (bundle / "offgrid" / "cli.py").write_text(
        f"# source {marker}\n", encoding="utf-8"
    )
    (bundle / "offgrid" / "__main__.py").write_text(
        "print('offgrid 0.1.0')\n", encoding="utf-8"
    )
    (bundle / "launch").mkdir()
    (bundle / "launch" / "offgrid.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    for launcher in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
        (bundle / launcher).write_text("#!/bin/sh\n", encoding="utf-8")
    (bundle / "config.json").write_text('{"offline": true}\n', encoding="utf-8")
    if model:
        models = bundle / "models"
        models.mkdir()
        (models / "model.gguf").write_bytes(b"same model bytes")
    if runtime:
        binary = bundle / "runtime" / "llama" / "llama-server"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"same runtime bytes")
        binary.chmod(0o755)
    manifest = install_manifest.build_manifest(
        bundle, artifact_kind="full", platform="any"
    )
    install_manifest.write_manifest(bundle, manifest)
    return bundle


def add_runtime_fingerprint(bundle):
    runtime = bundle / "runtime" / "llama"
    (runtime / "extra.bin").write_bytes(b"runtime extra bytes")
    fingerprint = {
        "schema": runtime_identity.SCHEMA,
        "kind": "llama",
        "platform": "any",
        "version": "test-runtime",
        "source_release": "",
        "source_asset": "",
        "archive_sha256": "",
        "binary_path": "llama-server",
        "binary_size": (runtime / "llama-server").stat().st_size,
        "binary_sha256": runtime_identity.sha256_file(runtime / "llama-server"),
        "tree_schema": runtime_identity.TREE_SCHEMA,
        "tree_digest": runtime_identity.tree_digest(runtime).digest,
        "tree_files": 2,
        "capabilities": {},
        "installer_version": "test",
        "created": "2026-02-03T04:05:06Z",
    }
    (runtime / ".offgrid-runtime.json").write_text(
        json.dumps(fingerprint, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = install_manifest.build_manifest(
        bundle, artifact_kind="full", platform="any"
    )
    install_manifest.write_manifest(bundle, manifest)
    return runtime


def load_state(destination):
    return json.loads(
        (Path(destination) / install_transaction.STATE_NAME).read_text(
            encoding="utf-8"
        )
    )


class TransactionalInstallTests(unittest.TestCase):
    def test_first_install_commits_manifest_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()

            report = install_transaction.install_directory(
                source, device / source.name
            )

            self.assertTrue(report.committed)
            destination = device / source.name
            self.assertTrue((destination / "offgrid" / "cli.py").is_file())
            state = load_state(destination)
            self.assertEqual(state["schema"], install_transaction.STATE_SCHEMA)
            self.assertEqual(state["status"], "committed")
            self.assertEqual(state["install_id"], report.install_id)
            self.assertEqual(state["transaction_id"], report.transaction_id)
            self.assertEqual(
                state["manifest_digest"],
                install_manifest.manifest_digest(
                    install_manifest.load_manifest(
                        destination / install_manifest.MANIFEST_NAME
                    )
                ),
            )
            self.assertEqual(state["previous_install"], {})
            self.assertTrue(state["health"]["ok"])
            self.assertEqual(
                install_manifest.verify_manifest(destination).errors, ()
            )
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_prepared_transaction_stage_commits_without_copying_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "device"
            device.mkdir()
            destination = device / "offgrid-usb"
            source = make_bundle(root / "source", model=True)
            staged = install_transaction.prepare_directory_stage(destination)
            shutil.copytree(source, staged.stage, dirs_exist_ok=True)
            staged_model = staged.stage / "models" / "model.gguf"
            inode = staged_model.stat().st_ino

            with patch.object(
                install_transaction,
                "_copy_source_tree",
                side_effect=AssertionError("a prepared stage must not be copied"),
            ):
                report = install_transaction.install_directory(
                    staged.stage,
                    destination,
                    staged=staged,
                    health_checker=lambda root, manifest: True,
                )

            self.assertTrue(report.committed)
            self.assertEqual(
                (destination / "models" / "model.gguf").stat().st_ino,
                inode,
            )
            self.assertFalse(staged.stage.exists())

    def test_existing_install_requires_force_and_records_previous_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            first_state = load_state(destination)
            (destination / ".offgrid").mkdir()
            (destination / ".offgrid" / "keep.txt").write_text(
                "user state\n", encoding="utf-8"
            )

            second = make_bundle(root / "second", marker="second")
            with self.assertRaisesRegex(install_transaction.InstallError, "already exists"):
                install_transaction.install_directory(second, destination)
            self.assertIn(
                "first", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )

            install_transaction.install_directory(second, destination, force=True)
            self.assertIn(
                "second", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )
            state = load_state(destination)
            self.assertEqual(
                state["previous_install"]["install_id"], first_state["install_id"]
            )
            self.assertEqual(
                (destination / ".offgrid" / "keep.txt").read_text(encoding="utf-8"),
                "user state\n",
            )

    def test_first_install_health_failure_leaves_no_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()

            def fail(_root, _manifest):
                raise install_transaction.HealthCheckError("injected first-install failure")

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_directory(
                    source,
                    device / source.name,
                    health_checker=fail,
                )

            self.assertFalse((device / source.name).exists())
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_first_install_failure_after_activation_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()
            calls = []

            def fail_after_activation(stage, _manifest):
                calls.append(stage)
                if len(calls) == 2:
                    raise install_transaction.HealthCheckError(
                        "injected activated-tree failure"
                    )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_directory(
                    source,
                    device / source.name,
                    health_checker=fail_after_activation,
                )

            self.assertEqual(len(calls), 2)
            self.assertFalse((device / source.name).exists())
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_replacement_writes_a_verified_recovery_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)

            report = install_transaction.install_directory(
                second, destination, force=True
            )

            archive = Path(report.recovery_bundle)
            self.assertTrue(archive.is_dir())
            self.assertEqual(
                load_state(destination)["recovery_bundle"], str(archive)
            )
            payload = recovery.load_recovery_manifest(
                archive / recovery.RECOVERY_NAME
            )
            self.assertEqual(payload["transaction_id"], report.transaction_id)
            self.assertEqual(
                payload["install"]["install_id"], old_state["install_id"]
            )
            self.assertIn(
                "first",
                (archive / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "second",
                (archive / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            report_archive = recovery.verify_recovery_archive(archive)
            self.assertEqual(report_archive.install_id, old_state["install_id"])

    def test_recovery_bundle_failure_aborts_before_destination_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)
            old_app = (destination / "offgrid" / "cli.py").read_text(
                encoding="utf-8"
            )

            with patch.object(
                install_transaction.recovery,
                "write_recovery_archive",
                side_effect=recovery.RecoveryError("injected recovery failure"),
            ):
                with self.assertRaisesRegex(
                    install_transaction.InstallError, "recovery bundle"
                ):
                    install_transaction.install_directory(
                        second, destination, force=True
                    )

            self.assertEqual(
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
                old_app,
            )
            self.assertEqual(load_state(destination)["install_id"], old_state["install_id"])
            paths = install_transaction.transaction_paths(destination)
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_recovery_archive_fsync_failure_aborts_before_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)
            old_app = (destination / "offgrid" / "cli.py").read_text(
                encoding="utf-8"
            )

            with patch.object(
                install_transaction.recovery,
                "_fsync_directory",
                side_effect=OSError("injected recovery fsync failure"),
            ):
                with self.assertRaisesRegex(
                    install_transaction.InstallError, "recovery bundle"
                ):
                    install_transaction.install_directory(
                        second, destination, force=True
                    )

            self.assertEqual(
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
                old_app,
            )
            self.assertEqual(load_state(destination)["install_id"], old_state["install_id"])
            paths = install_transaction.transaction_paths(destination)
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_committed_state_fsync_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)
            old_app = (destination / "offgrid" / "cli.py").read_text(
                encoding="utf-8"
            )
            original = install_transaction._fsync_directory

            def fail_destination(path):
                if Path(path) == destination:
                    raise OSError("injected state fsync failure")
                return original(Path(path))

            with patch.object(
                install_transaction,
                "_fsync_directory",
                side_effect=fail_destination,
            ):
                with self.assertRaisesRegex(
                    install_transaction.InstallError, "state fsync failure"
                ):
                    install_transaction.install_directory(
                        second, destination, force=True
                    )

            self.assertEqual(
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
                old_app,
            )
            self.assertEqual(load_state(destination)["install_id"], old_state["install_id"])
            paths = install_transaction.transaction_paths(destination)
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(paths.stage.exists())
            self.assertFalse(paths.journal.exists())

    def test_replacement_stage_health_failure_preserves_previous_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)

            def fail(_stage, _manifest):
                raise install_transaction.HealthCheckError(
                    "injected staged-tree failure"
                )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_directory(
                    second,
                    destination,
                    force=True,
                    health_checker=fail,
                )

            self.assertIn(
                "first", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )
            self.assertEqual(load_state(destination)["install_id"], old_state["install_id"])
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_activated_install_health_failure_restores_previous_tree_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            old_state = load_state(destination)
            calls = []

            def fail_after_activation(stage, _manifest):
                calls.append(stage)
                if len(calls) == 2:
                    raise install_transaction.HealthCheckError(
                        "injected activated-tree failure"
                    )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_directory(
                    second,
                    destination,
                    force=True,
                    health_checker=fail_after_activation,
                )

            self.assertEqual(len(calls), 2)
            self.assertIn(
                "first", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )
            restored_state = load_state(destination)
            self.assertEqual(restored_state["install_id"], old_state["install_id"])
            self.assertEqual(restored_state["status"], "committed")
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    @unittest.skipUnless(
        os.name == "posix", "hard-link inode checks require POSIX filesystems"
    )
    def test_unchanged_model_and_runtime_are_reused_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first", model=True, runtime=True)
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            model = destination / "models" / "model.gguf"
            runtime = destination / "runtime" / "llama" / "llama-server"
            model_inode = model.stat().st_ino
            runtime_inode = runtime.stat().st_ino

            second = make_bundle(
                root / "second", marker="second", model=True, runtime=True
            )
            install_transaction.install_directory(second, destination, force=True)

            self.assertIn(
                "second", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )
            self.assertEqual(
                (destination / "models" / "model.gguf").stat().st_ino,
                model_inode,
            )
            self.assertEqual(
                (destination / "runtime" / "llama" / "llama-server").stat().st_ino,
                runtime_inode,
            )

    def test_corrupted_model_is_copied_from_the_bundle_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first", model=True)
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            model = destination / "models" / "model.gguf"
            model.write_bytes(b"bad model bytes!")  # same size, wrong content

            second = make_bundle(root / "second", marker="second", model=True)
            install_transaction.install_directory(second, destination, force=True)

            self.assertEqual(
                (destination / "models" / "model.gguf").read_bytes(),
                b"same model bytes",
            )

    def test_same_size_model_corruption_is_never_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src", model=True)
            (source / "models" / "model.gguf").write_bytes(b"evil model bytes")
            device = root / "device"
            device.mkdir()
            destination = device / source.name

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "SHA-256 mismatch: models/model.gguf",
            ):
                install_transaction.install_directory(source, destination)

            self.assertFalse(destination.exists())

    def test_same_size_runtime_corruption_is_never_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src", runtime=True)
            (source / "runtime" / "llama" / "llama-server").write_bytes(
                b"evil runtime bytes"
            )
            device = root / "device"
            device.mkdir()
            destination = device / source.name

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "SHA-256 mismatch: runtime/llama/llama-server",
            ):
                install_transaction.install_directory(source, destination)

            self.assertFalse(destination.exists())

    def test_same_size_runtime_fingerprint_corruption_is_never_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src", runtime=True)
            fingerprint = add_runtime_fingerprint(source) / ".offgrid-runtime.json"
            fingerprint.write_text(
                fingerprint.read_text(encoding="utf-8").replace(
                    "test-runtime", "test-runtimf"
                ),
                encoding="utf-8",
            )
            device = root / "device"
            device.mkdir()
            destination = device / source.name

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "SHA-256 mismatch: runtime/llama/.offgrid-runtime.json",
            ):
                install_transaction.install_directory(source, destination)

            self.assertFalse(destination.exists())

    def test_snapshot_is_created_before_the_destination_is_swapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            real_activate = install_transaction._activate_directory
            observed = []

            def activate(paths, *, quarantine=None):
                archives = [
                    path
                    for path in paths.root.iterdir()
                    if path.is_dir() and ".recovery-" in path.name
                ]
                self.assertFalse(quarantine)
                self.assertTrue(paths.destination.is_dir())
                self.assertIn(
                    "first",
                    (paths.destination / "offgrid" / "cli.py").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertEqual(len(archives), 1)
                payload = recovery.load_recovery_manifest(
                    archives[0] / recovery.RECOVERY_NAME
                )
                self.assertIn(
                    "first",
                    (archives[0] / "offgrid" / "cli.py").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertEqual(payload["transaction_id"], paths.transaction_id)
                self.assertEqual(
                    json.loads(paths.journal.read_text(encoding="utf-8"))[
                        "status"
                    ],
                    "swapping",
                )
                observed.append(archives[0])
                return real_activate(paths, quarantine=quarantine)

            with patch.object(
                install_transaction, "_activate_directory", side_effect=activate
            ):
                install_transaction.install_directory(second, destination, force=True)

            self.assertEqual(len(observed), 1)

    def test_journal_and_state_include_the_complete_transaction_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            seen = []
            real_journal = install_transaction._write_journal

            def capture(paths, payload):
                seen.append(json.loads(json.dumps(dict(payload))))
                return real_journal(paths, payload)

            with patch.object(
                install_transaction, "_write_journal", side_effect=capture
            ):
                install_transaction.install_directory(second, destination, force=True)

            required = {
                "schema",
                "transaction_id",
                "status",
                "kind",
                "destination",
                "staging",
                "previous",
                "quarantine",
                "quarantine_state_dir",
                "quarantine_reason",
                "state_dir",
                "previous_state_dir",
                "manifest_path",
                "manifest_digest",
                "install_id",
                "artifact",
                "previous_install",
                "recovery_bundle",
                "recovery_generation",
                "retention",
                "created",
                "updated",
                "error",
            }
            self.assertTrue(seen)
            for payload in seen:
                self.assertLessEqual(required, set(payload))
            self.assertEqual(len({item["created"] for item in seen}), 1)
            self.assertTrue(
                any(item["status"] == "committed" for item in seen)
            )

            state = load_state(destination)
            required_state = {
                "schema",
                "install_id",
                "transaction_id",
                "status",
                "destination",
                "manifest_digest",
                "manifest_path",
                "artifact",
                "previous_install",
                "recovery_bundle",
                "recovery_generation",
                "quarantine",
                "quarantine_state_dir",
                "quarantine_reason",
                "created",
                "updated",
                "last_error",
                "health",
            }
            self.assertLessEqual(required_state, set(state))
            self.assertEqual(state["status"], "committed")
            self.assertEqual(state["recovery_generation"], 1)
            self.assertEqual(state["quarantine"], "")
            self.assertTrue(state["created"])
            self.assertTrue(state["health"]["checked_at"])

    def test_committed_journal_failure_does_not_roll_back_the_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            real_journal = install_transaction._write_journal

            def fail_committed(paths, payload):
                if payload.get("status") == "committed":
                    raise OSError("injected journal failure")
                return real_journal(paths, payload)

            with patch.object(
                install_transaction, "_write_journal", side_effect=fail_committed
            ):
                report = install_transaction.install_directory(
                    second, destination, force=True
                )

            self.assertTrue(report.committed)
            self.assertIn(
                "second",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            paths = install_transaction.transaction_paths(destination)
            self.assertTrue(paths.journal.exists())
            journal = json.loads(paths.journal.read_text(encoding="utf-8"))
            self.assertEqual(journal["status"], "swapping")
            self.assertTrue(Path(journal["previous"]).exists())

            install_transaction._recover_interrupted(destination)

            self.assertIn(
                "second",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            self.assertFalse(paths.journal.exists())
            self.assertFalse(Path(journal["previous"]).exists())

    def test_recovery_archives_are_pruned_to_three_per_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "device"
            device.mkdir()
            first = make_bundle(root / "first", marker="first")
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            reports = []
            for index in range(1, 6):
                bundle = make_bundle(root / f"bundle-{index}", marker=str(index))
                reports.append(
                    install_transaction.install_directory(
                        bundle, destination, force=True
                    )
                )

            paths = install_transaction.transaction_paths(destination)
            archives = sorted(
                path
                for path in paths.root.iterdir()
                if path.is_dir() and ".recovery-" in path.name
            )
            self.assertEqual(len(archives), 3)
            self.assertEqual(len(reports[-1].retention.kept), 3)
            self.assertEqual(
                [path.name[-8:] for path in archives],
                ["00000003", "00000004", "00000005"],
            )

    def test_pruning_failure_does_not_roll_back_a_committed_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)

            with patch.object(
                install_transaction,
                "prune_recovery_archives",
                side_effect=RuntimeError("injected retention failure"),
            ):
                report = install_transaction.install_directory(
                    second, destination, force=True
                )

            self.assertTrue(report.committed)
            self.assertIsNotNone(report.retention)
            self.assertFalse(report.retention.ok)
            self.assertIn(
                "second",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(load_state(destination)["status"], "committed")
            self.assertFalse(
                install_transaction.transaction_paths(destination).journal.exists()
            )

    def test_corrupt_current_install_is_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            (destination / install_transaction.STATE_NAME).write_text(
                "{not json", encoding="utf-8"
            )

            report = install_transaction.install_directory(second, destination, force=True)

            self.assertTrue(report.committed)
            self.assertEqual(report.recovery_bundle, "")
            self.assertTrue(report.quarantine)
            quarantine = Path(report.quarantine)
            self.assertTrue(quarantine.is_dir())
            self.assertIn(
                "first",
                (quarantine / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            state = load_state(destination)
            self.assertEqual(state["quarantine"], str(quarantine))
            self.assertIn("install state", state["quarantine_reason"])
            self.assertIn(
                "second",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )

    def test_corrupt_current_missing_app_file_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            (destination / "offgrid" / "cli.py").unlink()

            report = install_transaction.install_directory(second, destination, force=True)

            self.assertTrue(report.committed)
            self.assertEqual(report.recovery_bundle, "")
            quarantine = Path(report.quarantine)
            self.assertTrue(quarantine.is_dir())
            self.assertFalse((quarantine / "offgrid" / "cli.py").exists())
            self.assertIn(
                "missing", report.quarantine_reason
            )
            self.assertIn(
                "second",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )

    def test_failed_replacement_restores_a_quarantined_current_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            second = make_bundle(root / "second", marker="second")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            (destination / install_transaction.STATE_NAME).write_text(
                "{not json", encoding="utf-8"
            )

            def fail_activated(stage, _manifest):
                if Path(stage) == destination:
                    raise install_transaction.HealthCheckError(
                        "injected activated failure"
                    )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_directory(
                    second,
                    destination,
                    force=True,
                    health_checker=fail_activated,
                )

            self.assertIn(
                "first",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            paths = install_transaction.transaction_paths(destination)
            self.assertFalse(paths.quarantine.exists())
            self.assertFalse(paths.journal.exists())
            self.assertFalse(paths.previous.exists())

    def test_corrupt_runtime_tree_falls_back_to_the_bundle_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first", runtime=True)
            add_runtime_fingerprint(first)
            second = make_bundle(root / "second", marker="second", runtime=True)
            add_runtime_fingerprint(second)
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            (destination / "runtime" / "llama" / "extra.bin").write_bytes(
                b"corrupt"
            )

            install_transaction.install_directory(second, destination, force=True)

            self.assertEqual(
                (destination / "runtime" / "llama" / "extra.bin").read_bytes(),
                b"runtime extra bytes",
            )

    def test_runtime_reuse_link_failure_falls_back_to_the_bundle_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first", runtime=True)
            add_runtime_fingerprint(first)
            second = make_bundle(root / "second", marker="second", runtime=True)
            add_runtime_fingerprint(second)
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)

            with patch.object(
                install_transaction,
                "_reuse_tree",
                side_effect=OSError("injected link failure"),
            ):
                report = install_transaction.install_directory(
                    second, destination, force=True
                )

            self.assertTrue(report.committed)
            self.assertEqual(
                (destination / "runtime" / "llama" / "extra.bin").read_bytes(),
                b"runtime extra bytes",
            )

    def test_install_lock_refuses_a_second_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()
            destination = device / source.name
            with install_transaction.install_lock(destination):
                with self.assertRaises(install_transaction.InstallBusy):
                    install_transaction.install_directory(source, destination)

    def test_install_lock_refuses_a_foreign_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "device" / "offgrid-usb"
            paths = install_transaction.transaction_paths(destination)
            paths.root.mkdir(parents=True)
            payload = {
                "pid": os.getpid(),
                "host": "other-machine.invalid",
                "created": "2026-01-02T03:04:05Z",
                "destination": str(destination),
                "token": "foreign-token",
            }
            paths.lock.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(
                install_transaction.platform, "node", return_value="this-machine"
            ):
                self.assertFalse(install_transaction._stale_lock(paths.lock))
                with self.assertRaisesRegex(
                    install_transaction.InstallBusy, "another install"
                ):
                    with install_transaction.install_lock(destination):
                        pass

            self.assertTrue(paths.lock.is_file())
            self.assertEqual(
                json.loads(paths.lock.read_text(encoding="utf-8"))["token"],
                "foreign-token",
            )

    def test_windows_process_start_marker_uses_get_process_times(self):
        class FakeKernel32:
            def __init__(self):
                self.opened = None
                self.closed = None
                self.times_called = False

            def OpenProcess(self, access, inherit, pid):
                self.opened = (access, inherit, pid)
                return 123

            def GetProcessTimes(self, handle, created, exited, kernel, user):
                self.times_called = True
                created._obj.dwLowDateTime = 0x12345678
                created._obj.dwHighDateTime = 0x00000001
                return 1

            def CloseHandle(self, handle):
                self.closed = handle
                return 1

        kernel32 = FakeKernel32()
        marker = install_transaction._windows_process_start(
            4242, kernel32=kernel32
        )

        self.assertEqual(marker, str((1 << 32) | 0x12345678))
        self.assertEqual(kernel32.opened, (0x1000, False, 4242))
        self.assertTrue(kernel32.times_called)
        self.assertEqual(kernel32.closed, 123)

        with patch.object(install_transaction.os, "name", "nt"), patch.object(
            install_transaction,
            "_windows_process_start",
            return_value=marker,
        ) as windows_start:
            self.assertEqual(install_transaction._process_start(4242), marker)
            windows_start.assert_called_once_with(4242)

    @unittest.skipUnless(os.name == "nt", "GetProcessTimes is Windows-only")
    def test_windows_process_start_marker_is_real_and_stable(self):
        first = install_transaction._windows_process_start(os.getpid())
        second = install_transaction._windows_process_start(os.getpid())

        self.assertTrue(first)
        self.assertEqual(first, second)

    @unittest.skipUnless(os.name == "nt", "GetProcessTimes is Windows-only")
    def test_windows_recycled_pid_lock_is_reclaimed_with_real_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "device" / "offgrid"
            paths = install_transaction.transaction_paths(destination)
            paths.root.mkdir(parents=True)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            try:
                live_marker = install_transaction._windows_process_start(child.pid)
                self.assertTrue(live_marker)
                lock_payload = {
                    "pid": child.pid,
                    "host": platform.node(),
                    "pid_start": live_marker,
                    "destination": str(destination),
                    "token": "live-child",
                }
                paths.lock.write_text(json.dumps(lock_payload), encoding="utf-8")
                self.assertFalse(install_transaction._stale_lock(paths.lock))

                with self.assertRaises(install_transaction.InstallBusy):
                    with install_transaction.install_lock(destination):
                        pass

                # The child is still alive, but its start marker no longer
                # matches. This is the branch Windows uses when a PID has been
                # recycled for another process.
                self.assertIsNone(child.poll())
                lock_payload["pid_start"] = "different-process-start"
                paths.lock.write_text(json.dumps(lock_payload), encoding="utf-8")
                self.assertTrue(install_transaction._stale_lock(paths.lock))
                with install_transaction.install_lock(destination) as token:
                    self.assertTrue(token)
                    self.assertTrue(paths.lock.is_file())

                self.assertFalse(paths.lock.exists())
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

    def test_recycled_local_pid_does_not_keep_a_dead_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()
            destination = device / source.name
            paths = install_transaction.transaction_paths(destination)
            paths.root.mkdir(parents=True)
            payload = {
                "pid": os.getpid(),
                "host": platform.node(),
                "pid_start": "start-of-the-dead-process",
                "created": "2026-01-02T03:04:05Z",
                "destination": str(destination),
                "token": "dead-token",
            }
            paths.lock.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(
                install_transaction,
                "_process_start",
                return_value="start-of-a-reused-pid",
            ):
                self.assertTrue(install_transaction._stale_lock(paths.lock))
                report = install_transaction.install_directory(source, destination)

            self.assertTrue(report.committed)
            self.assertTrue((destination / "offgrid" / "cli.py").is_file())
            self.assertFalse(paths.lock.exists())

    def test_recovery_rejects_each_journal_path_outside_the_transaction_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "device" / "offgrid"
            destination.parent.mkdir(parents=True)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True)
            fields = (
                "staging",
                "previous",
                "state_dir",
                "previous_state_dir",
                "quarantine",
                "quarantine_state_dir",
                "recovery_bundle",
                "manifest_path",
            )
            for field in fields:
                with self.subTest(field=field):
                    victim = root / "outside" / field / "keep"
                    victim.mkdir(parents=True)
                    (victim / "keep.txt").write_text(
                        "do not delete\n", encoding="utf-8"
                    )
                    payload = {
                        "schema": install_transaction.JOURNAL_SCHEMA,
                        "transaction_id": "crashed",
                        "status": "prepared",
                        "kind": "directory",
                        "destination": str(destination),
                        field: str(victim),
                    }
                    paths.journal.write_text(
                        json.dumps(payload), encoding="utf-8"
                    )

                    with self.assertRaisesRegex(
                        install_transaction.InstallError,
                        "escapes the destination or transaction root",
                    ):
                        install_transaction._recover_interrupted(destination)

                    self.assertTrue(
                        (victim / "keep.txt").is_file(), field
                    )
                    self.assertTrue(paths.journal.is_file(), field)

    def test_recovery_rejects_a_symlink_journal_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "device" / "offgrid"
            destination.parent.mkdir(parents=True)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True)
            outside = root / "outside"
            victim = outside / "keep"
            victim.mkdir(parents=True)
            (victim / "keep.txt").write_text("do not delete\n", encoding="utf-8")
            escape = paths.root / "escape"
            try:
                escape.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")
            payload = {
                "schema": install_transaction.JOURNAL_SCHEMA,
                "transaction_id": "crashed",
                "status": "prepared",
                "kind": "directory",
                "destination": str(destination),
                "staging": str(escape / "keep"),
            }
            paths.journal.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "escapes the destination or transaction root",
            ):
                install_transaction._recover_interrupted(destination)

            self.assertTrue((victim / "keep.txt").is_file())
            self.assertTrue(paths.journal.is_file())

    def test_recovery_rejects_a_non_string_journal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "device" / "offgrid"
            destination.parent.mkdir(parents=True)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True)
            payload = {
                "schema": install_transaction.JOURNAL_SCHEMA,
                "transaction_id": "crashed",
                "status": "prepared",
                "kind": "directory",
                "destination": str(destination),
                "previous": 42,
            }
            paths.journal.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "previous is not a path",
            ):
                install_transaction._recover_interrupted(destination)

            self.assertTrue(paths.journal.is_file())

    def test_recovery_rejects_a_mismatched_journal_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "device" / "offgrid"
            destination.parent.mkdir(parents=True)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True)
            payload = {
                "schema": install_transaction.JOURNAL_SCHEMA,
                "transaction_id": "crashed",
                "status": "prepared",
                "kind": "directory",
                "destination": str(root / "somewhere-else"),
            }
            paths.journal.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                install_transaction.InstallError,
                "does not match the requested destination",
            ):
                install_transaction._recover_interrupted(destination)

            self.assertTrue(paths.journal.is_file())

    def test_interrupted_swap_is_recovered_before_the_next_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_bundle(root / "first", marker="first")
            device = root / "device"
            device.mkdir()
            destination = device / first.name
            install_transaction.install_directory(first, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            os.replace(destination, paths.previous)
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "status": "swapping",
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "manifest_digest": "",
                },
            )

            install_transaction._recover_interrupted(destination)

            self.assertTrue((destination / "offgrid" / "cli.py").is_file())
            self.assertIn(
                "first", (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
            )
            self.assertFalse(paths.journal.exists())
            self.assertFalse(paths.previous.exists())

    def test_interrupted_quarantine_is_restored_before_the_next_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "source", marker="current")
            device = root / "device"
            device.mkdir()
            destination = device / source.name
            install_transaction.install_directory(source, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            os.replace(destination, paths.quarantine)
            install_transaction._write_journal(
                paths,
                install_transaction._journal_payload(
                    paths,
                    status="quarantining",
                    kind="directory",
                    manifest_digest="",
                    install_id="crashed",
                    artifact={},
                    previous={},
                    quarantine=str(paths.quarantine),
                    quarantine_reason="simulated corrupt current",
                    created="2026-02-03T04:05:06Z",
                ),
            )

            install_transaction._recover_interrupted(destination)

            self.assertIn(
                "current",
                (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            )
            self.assertFalse(paths.quarantine.exists())
            self.assertFalse(paths.journal.exists())

    def test_interrupted_first_install_rollback_removes_staged_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "source")
            device = root / "device"
            device.mkdir()
            destination = device / source.name
            install_transaction.install_directory(source, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            state = load_state(destination)
            state["status"] = "staged"
            state["health"] = {"ok": False, "checked_at": ""}
            (destination / install_transaction.STATE_NAME).write_text(
                json.dumps(state), encoding="utf-8"
            )
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "status": "rolling_back",
                    "activated": True,
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "previous_install": {},
                    "manifest_digest": state["manifest_digest"],
                },
            )

            install_transaction._recover_interrupted(destination)

            self.assertFalse(destination.exists())
            self.assertFalse(paths.stage.exists())
            self.assertFalse(paths.journal.exists())

    def test_interrupted_pyz_first_install_rollback_removes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pyz = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / pyz.name
            install_transaction.install_file(pyz, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "kind": "file",
                    "status": "rolling_back",
                    "activated": True,
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "previous_state_dir": str(paths.previous_state_dir),
                    "state_dir": str(paths.state_dir),
                    "previous_install": {},
                    "manifest_digest": state["manifest_digest"],
                },
            )

            install_transaction._recover_interrupted(destination)

            self.assertFalse(destination.exists())
            self.assertFalse(paths.state_dir.exists())
            self.assertFalse(paths.stage.exists())
            self.assertFalse(paths.journal.exists())

    def test_interrupted_unmanaged_replacement_rollback_keeps_restored_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "device"
            device.mkdir()
            destination = device / "unmanaged"
            destination.mkdir()
            (destination / "keep.txt").write_text("keep\n", encoding="utf-8")
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "status": "rolling_back",
                    "activated": True,
                    "had_current": True,
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "previous_install": {},
                    "manifest_digest": "",
                },
            )

            install_transaction._recover_interrupted(destination)

            self.assertEqual(
                (destination / "keep.txt").read_text(encoding="utf-8"), "keep\n"
            )
            self.assertFalse(paths.journal.exists())

    def test_interrupted_pyz_first_install_rollback_cleans_orphaned_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pyz = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / pyz.name
            install_transaction.install_file(pyz, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "kind": "file",
                    "status": "rolling_back",
                    "activated": True,
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "previous_state_dir": str(paths.previous_state_dir),
                    "state_dir": str(paths.state_dir),
                    "previous_install": {},
                    "manifest_digest": state["manifest_digest"],
                },
            )
            destination.unlink()

            install_transaction._recover_interrupted(destination)

            self.assertFalse(destination.exists())
            self.assertFalse(paths.state_dir.exists())
            self.assertFalse(paths.journal.exists())

    def test_pyz_swap_crash_after_committed_state_is_rolled_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / "offgrid.pyz"
            install_transaction.install_file(first_pyz, destination)
            paths = install_transaction.transaction_paths(destination, "crashed")
            old_state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )

            paths.stage.mkdir(parents=True)
            candidate_root = paths.stage / "root"
            candidate_root.mkdir()
            candidate = candidate_root / destination.name
            shutil.copy2(second_pyz, candidate)
            manifest = install_transaction._pyz_manifest(
                candidate_root, destination.name
            )
            candidate_digest = install_manifest.manifest_digest(manifest)

            os.replace(destination, paths.previous)
            os.replace(paths.state_dir, paths.previous_state_dir)
            os.replace(candidate, destination)
            paths.state_dir.mkdir(parents=True)
            shutil.copy2(
                candidate_root / install_manifest.MANIFEST_NAME,
                paths.state_dir / install_manifest.MANIFEST_NAME,
            )
            install_transaction._write_state(
                paths.state_dir,
                install_transaction._state_payload(
                    destination,
                    manifest,
                    transaction_id="crashed",
                    install_id="candidate",
                    previous={"install_id": old_state["install_id"]},
                    status="committed",
                    manifest_path=str(
                        paths.state_dir / install_manifest.MANIFEST_NAME
                    ),
                ),
            )
            shutil.rmtree(paths.stage)
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "kind": "file",
                    "status": "swapping",
                    "activated": True,
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "previous_state_dir": str(paths.previous_state_dir),
                    "state_dir": str(paths.state_dir),
                    "previous_install": {"install_id": old_state["install_id"]},
                    "manifest_digest": candidate_digest,
                },
            )

            install_transaction._recover_interrupted(destination)

            self.assertEqual(destination.read_bytes(), second_pyz.read_bytes())
            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "committed")
            self.assertFalse(paths.previous.exists())
            self.assertFalse(paths.previous_state_dir.exists())
            self.assertFalse(paths.journal.exists())

    def test_public_retry_recovers_abandoned_first_install_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_bundle(root / "src")
            device = root / "device"
            device.mkdir()
            destination = device / source.name
            paths = install_transaction.transaction_paths(destination, "crashed")
            paths.root.mkdir(parents=True, exist_ok=True)
            (device / source.name).mkdir()
            (destination / "partial.txt").write_text("partial\n", encoding="utf-8")
            install_transaction._write_journal(
                paths,
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "transaction_id": "crashed",
                    "status": "swapping",
                    "destination": str(destination),
                    "staging": str(paths.stage),
                    "previous": str(paths.previous),
                    "manifest_digest": "",
                },
            )

            report = install_transaction.install_directory(source, destination)

            self.assertTrue(report.committed)
            self.assertTrue((destination / "offgrid" / "cli.py").is_file())
            self.assertFalse((destination / "partial.txt").exists())
            self.assertFalse(paths.journal.exists())

    def test_pyz_first_install_failure_after_activation_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pyz = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / pyz.name
            calls = []

            def fail_after_activation(stage, _manifest):
                calls.append(stage)
                if len(calls) == 2:
                    raise install_transaction.HealthCheckError(
                        "injected activated-pyz failure"
                    )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_file(
                    pyz, destination, health_checker=fail_after_activation
                )

            self.assertEqual(len(calls), 2)
            self.assertFalse(destination.exists())
            paths = install_transaction.transaction_paths(destination)
            self.assertFalse(paths.state_dir.exists())
            self.assertFalse(
                any((device / install_transaction.TRANSACTION_DIR).glob("*"))
            )

    def test_pyz_first_install_replaces_orphaned_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / "offgrid.pyz"
            install_transaction.install_file(first_pyz, destination)
            paths = install_transaction.transaction_paths(destination)
            destination.unlink()

            report = install_transaction.install_file(second_pyz, destination)

            self.assertTrue(report.committed)
            self.assertEqual(destination.read_bytes(), second_pyz.read_bytes())
            self.assertEqual(
                json.loads(
                    (paths.state_dir / install_transaction.STATE_NAME).read_text(
                        encoding="utf-8"
                    )
                )["install_id"],
                report.install_id,
            )

    def test_pyz_replacement_writes_a_recovery_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            paths = install_transaction.transaction_paths(destination)
            old_state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )

            report = install_transaction.install_file(
                second_pyz, destination, force=True
            )

            archive = Path(report.recovery_bundle)
            self.assertTrue(archive.is_dir())
            payload = recovery.load_recovery_manifest(
                archive / recovery.RECOVERY_NAME
            )
            self.assertEqual(
                payload["install"]["install_id"], old_state["install_id"]
            )
            self.assertTrue((archive / "offgrid.pyz").is_file())
            self.assertFalse((archive / "offgrid").exists())
            self.assertTrue(recovery.verify_recovery_archive(archive).ok)

    def test_pyz_replacement_with_custom_filename_snapshots_that_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / "custom-app.pyz"
            install_transaction.install_file(first_pyz, destination)
            paths = install_transaction.transaction_paths(destination)
            old_state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )

            report = install_transaction.install_file(
                second_pyz, destination, force=True
            )

            archive = Path(report.recovery_bundle)
            self.assertTrue(archive.is_dir())
            payload = recovery.load_recovery_manifest(
                archive / recovery.RECOVERY_NAME
            )
            self.assertEqual(
                payload["install"]["install_id"], old_state["install_id"]
            )
            self.assertTrue((archive / "custom-app.pyz").is_file())
            self.assertFalse((archive / "offgrid.pyz").exists())
            manifest = install_manifest.load_manifest(
                archive / install_manifest.MANIFEST_NAME
            )
            self.assertEqual(manifest["entrypoints"]["app"], "custom-app.pyz")
            self.assertTrue(recovery.verify_recovery_archive(archive).ok)

            restored = recovery.restore_recovery_archive(
                archive, device / "restored-app.pyz"
            )
            self.assertEqual(restored.destination, device / "restored-app.pyz")
            self.assertEqual(restored.install_id, old_state["install_id"])
            self.assertEqual(
                restored.destination.read_bytes(), first_pyz.read_bytes()
            )

    def test_pyz_restore_saves_the_current_app_for_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(
                root / "first" / "offgrid.zip"
            )
            _, second_pyz = portable.build_distribution(
                root / "second" / "offgrid.zip"
            )
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            install_transaction.install_file(second_pyz, destination, force=True)
            archive = install_transaction.recovery_archive_path(destination, 1)

            restored = recovery.restore_recovery_archive(
                archive, destination, confirm=True
            )

            self.assertTrue(restored.safety_snapshot)
            snapshot = Path(restored.safety_snapshot)
            self.assertTrue(snapshot.is_dir())
            self.assertTrue(recovery.verify_recovery_archive(snapshot).ok)
            self.assertEqual(
                (snapshot / destination.name).read_bytes(),
                second_pyz.read_bytes(),
            )
            self.assertEqual(destination.read_bytes(), first_pyz.read_bytes())

            recovery.restore_recovery_archive(
                snapshot, destination, force=True, confirm=True
            )

            self.assertEqual(destination.read_bytes(), second_pyz.read_bytes())

    def test_pyz_recovery_failure_aborts_before_destination_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            old_bytes = destination.read_bytes()
            paths = install_transaction.transaction_paths(destination)
            old_state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )

            with patch.object(
                install_transaction.recovery,
                "write_recovery_archive",
                side_effect=recovery.RecoveryError("injected pyz recovery failure"),
            ):
                with self.assertRaisesRegex(
                    install_transaction.InstallError, "recovery bundle"
                ):
                    install_transaction.install_file(
                        second_pyz, destination, force=True
                    )

            self.assertEqual(destination.read_bytes(), old_bytes)
            restored_state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(restored_state["install_id"], old_state["install_id"])
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(paths.stage.exists())
            self.assertFalse(paths.journal.exists())

    def test_pyz_corrupt_current_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            paths = install_transaction.transaction_paths(destination)
            (paths.state_dir / install_transaction.STATE_NAME).write_text(
                "{not json", encoding="utf-8"
            )
            old_bytes = destination.read_bytes()

            report = install_transaction.install_file(
                second_pyz, destination, force=True
            )

            paths = install_transaction.transaction_paths(
                destination, report.transaction_id
            )
            self.assertTrue(report.committed)
            self.assertEqual(report.recovery_bundle, "")
            quarantine = Path(report.quarantine)
            self.assertTrue(quarantine.is_file())
            self.assertEqual(quarantine.read_bytes(), old_bytes)
            self.assertTrue(paths.quarantine_state_dir.is_dir())
            self.assertIn(
                "{not json",
                (paths.quarantine_state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                ),
            )
            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["quarantine"], str(quarantine))
            self.assertEqual(state["destination"], str(destination))

    def test_pyz_failed_replacement_restores_a_quarantined_current_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            old_bytes = destination.read_bytes()
            paths = install_transaction.transaction_paths(destination)
            (paths.state_dir / install_transaction.STATE_NAME).write_text(
                "{not json", encoding="utf-8"
            )
            calls = []

            def fail_activated(stage, _manifest):
                calls.append(stage)
                if len(calls) == 2:
                    raise install_transaction.HealthCheckError(
                        "injected activated pyz failure"
                    )

            with self.assertRaises(install_transaction.HealthCheckError):
                install_transaction.install_file(
                    second_pyz,
                    destination,
                    force=True,
                    health_checker=fail_activated,
                )

            self.assertEqual(destination.read_bytes(), old_bytes)
            self.assertIn(
                "{not json",
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                ),
            )
            self.assertFalse(
                any(
                    ".quarantine" in path.name
                    for path in paths.root.iterdir()
                )
            )
            self.assertFalse(paths.journal.exists())

    def test_pyz_recovery_archives_are_pruned_to_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "device"
            device.mkdir()
            first_dir = root / "first"
            first_dir.mkdir()
            _, first_pyz = portable.build_distribution(first_dir / "offgrid.zip")
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            reports = []
            for index in range(1, 6):
                build = root / f"bundle-{index}"
                build.mkdir()
                _, candidate = portable.build_distribution(build / "offgrid.zip")
                reports.append(
                    install_transaction.install_file(
                        candidate, destination, force=True
                    )
                )

            paths = install_transaction.transaction_paths(destination)
            archives = sorted(
                path
                for path in paths.root.iterdir()
                if path.is_dir() and ".recovery-" in path.name
            )
            self.assertEqual(len(archives), 3)
            self.assertEqual(len(reports[-1].retention.kept), 3)
            self.assertEqual(
                [path.name[-8:] for path in archives],
                ["00000003", "00000004", "00000005"],
            )

    def test_pyz_committed_journal_failure_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            _, first_pyz = portable.build_distribution(root / "first" / "offgrid.zip")
            _, second_pyz = portable.build_distribution(root / "second" / "offgrid.zip")
            device = root / "device"
            device.mkdir()
            destination = device / first_pyz.name
            install_transaction.install_file(first_pyz, destination)
            real_journal = install_transaction._write_journal

            def fail_committed(paths, payload):
                if payload.get("status") == "committed":
                    raise OSError("injected pyz journal failure")
                return real_journal(paths, payload)

            with patch.object(
                install_transaction, "_write_journal", side_effect=fail_committed
            ):
                report = install_transaction.install_file(
                    second_pyz, destination, force=True
                )

            self.assertTrue(report.committed)
            paths = install_transaction.transaction_paths(destination)
            journal = json.loads(paths.journal.read_text(encoding="utf-8"))
            self.assertEqual(journal["status"], "committing")
            self.assertTrue(Path(journal["previous"]).exists())

            install_transaction._recover_interrupted(destination)

            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "committed")
            self.assertFalse(paths.journal.exists())
            self.assertFalse(Path(journal["previous"]).exists())

    def test_pyz_install_uses_atomic_file_replacement_and_sidecar_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, pyz = portable.build_distribution(root / "offgrid.zip")
            device = root / "device"
            device.mkdir()

            report = install_transaction.install_file(pyz, device / pyz.name)

            self.assertTrue(report.committed)
            destination = device / pyz.name
            self.assertTrue(destination.is_file())
            paths = install_transaction.transaction_paths(destination)
            self.assertTrue((paths.state_dir / "install-manifest.json").is_file())
            state = json.loads(
                (paths.state_dir / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "committed")
            self.assertEqual(state["manifest_digest"], report.manifest_digest)
            self.assertEqual(state["destination"], str(destination))
            self.assertTrue(
                os.access(destination, os.X_OK),
                "installed pyz should remain executable",
            )


if __name__ == "__main__":
    unittest.main()
