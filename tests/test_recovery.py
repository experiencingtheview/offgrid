import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from offgrid import install_manifest, install_transaction, recovery


def make_install(root: Path, name: str = "offgrid-install", config=None) -> Path:
    install = root / name
    (install / "offgrid").mkdir(parents=True)
    (install / "offgrid" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (install / "offgrid" / "cli.py").write_text(
        "print('app')\n", encoding="utf-8"
    )
    (install / "offgrid" / "__main__.py").write_text(
        "print('main')\n", encoding="utf-8"
    )
    (install / "offgrid" / "helper.py").write_text(
        "HELPER = 1\n", encoding="utf-8"
    )
    (install / "launch").mkdir()
    (install / "launch" / "offgrid.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (install / "scripts").mkdir()
    (install / "scripts" / "check.py").write_text("print('check')\n", encoding="utf-8")
    for name in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
        (install / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (install / "README-FIRST.txt").write_text("start here\n", encoding="utf-8")
    (install / "README.md").write_text("# OffGrid\n", encoding="utf-8")
    (install / "LICENSE").write_text("license\n", encoding="utf-8")
    (install / "pyproject.toml").write_text("[project]\nname='offgrid'\n", encoding="utf-8")
    (install / "config.json").write_text(
        json.dumps(config if config is not None else {"offline": True}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    # These must never be copied into a recovery archive.
    (install / ".offgrid").mkdir()
    (install / ".offgrid" / "session-token").write_text("private", encoding="utf-8")
    (install / "models").mkdir()
    (install / "models" / "model.gguf").write_bytes(b"model")
    (install / "runtime" / "llama").mkdir(parents=True)
    (install / "runtime" / "llama" / "llama-server").write_bytes(b"runtime")

    manifest = install_manifest.build_manifest(
        install, artifact_kind="full", platform="any", created="2026-01-02T03:04:05Z"
    )
    install_manifest.write_manifest(install, manifest)
    install_transaction.record_install(
        install, manifest, transaction_id="tx-previous"
    )
    return install


def tree_snapshot(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes(),
            )
    return result


def rewrite_sums(root: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != recovery.SUMS_NAME:
            lines.append(
                f"{install_manifest.sha256_file(path)}  "
                f"{path.relative_to(root).as_posix()}\n"
            )
    (root / recovery.SUMS_NAME).write_text("".join(lines), encoding="ascii")


class RecoveryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = make_install(
            self.root,
            config={
                "offline": True,
                "endpoint": "http://user:password@127.0.0.1:8080/v1",
                "api_key": "super-secret-api-key",
                "fleet_token": "super-secret-fleet-token",
                "nested": {
                    "credential": "unknown-nested-secret",
                    "apiKey": "unknown-camel-secret",
                    "safe": "keep",
                },
                "sync_token_file": ".offgrid/sync-token",
                "household": ["Ada"],
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_writer_captures_app_state_and_sanitizes_config(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="replacing the runtime",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )

        self.assertTrue((destination / recovery.RECOVERY_NAME).is_file())
        self.assertTrue((destination / recovery.SUMS_NAME).is_file())
        self.assertTrue((destination / recovery.RECOVER_NAME).is_file())
        self.assertTrue((destination / install_manifest.MANIFEST_NAME).is_file())
        self.assertTrue((destination / install_transaction.STATE_NAME).is_file())
        self.assertTrue((destination / "offgrid" / "cli.py").is_file())
        self.assertTrue((destination / "launch" / "offgrid.sh").is_file())
        self.assertFalse((destination / "models").exists())
        self.assertFalse((destination / "runtime").exists())
        self.assertFalse((destination / ".offgrid").exists())

        config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["offline"], True)
        self.assertEqual(config["endpoint"], "http://127.0.0.1:8080/v1")
        self.assertNotIn("api_key", config)
        self.assertNotIn("fleet_token", config)
        self.assertNotIn("nested", config)
        self.assertEqual(config["sync_token_file"], ".offgrid/sync-token")
        self.assertEqual(config["household"], ["Ada"])
        all_text = (destination / "config.json").read_text(encoding="utf-8")
        for secret in (
            "super-secret-api-key",
            "super-secret-fleet-token",
            "unknown-nested-secret",
            "unknown-camel-secret",
            "password",
        ):
            self.assertNotIn(secret, all_text)

        payload = recovery.load_recovery_manifest(destination / recovery.RECOVERY_NAME)
        self.assertEqual(payload["schema"], recovery.SCHEMA)
        self.assertEqual(payload["transaction_id"], "tx-new")
        self.assertEqual(payload["reason"], "replacing the runtime")
        self.assertEqual(payload["restore_command"], recovery.RESTORE_COMMAND)
        self.assertEqual(payload["install"]["version"], "0.1.0")
        self.assertEqual(
            payload["assets"]["runtimes"][0]["path"], "runtime/llama"
        )
        self.assertEqual(
            payload["assets"]["models"][0]["path"], "models/model.gguf"
        )
        self.assertEqual(
            payload["assets"]["models"][0]["sha256"],
            install_manifest.sha256_file(self.source / "models" / "model.gguf"),
        )

        report = recovery.verify_recovery_archive(destination)
        self.assertEqual(report.version, "0.1.0")
        self.assertEqual(report.install_id, payload["install"]["install_id"])
        self.assertEqual(report.manifest_digest, payload["manifest_digest"])
        self.assertTrue(report.config_sanitized)
        self.assertGreater(report.files, 0)
        self.assertGreater(report.bytes, 0)

    def test_writer_is_deterministic_across_two_destinations(self):
        first = self.root / "first"
        second = self.root / "second"
        for destination in (first, second):
            recovery.write_recovery_archive(
                self.source,
                destination,
                reason="same input",
                transaction_id="tx-fixed",
                created="2026-02-03T04:05:06Z",
            )
        self.assertEqual(tree_snapshot(first), tree_snapshot(second))
        self.assertEqual(
            (first / recovery.SUMS_NAME).read_bytes(),
            (second / recovery.SUMS_NAME).read_bytes(),
        )

    def test_source_date_epoch_makes_created_reproducible(self):
        first = self.root / "epoch-one"
        second = self.root / "epoch-two"
        old = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
        try:
            for destination in (first, second):
                recovery.write_recovery_archive(
                    self.source,
                    destination,
                    reason="epoch test",
                    transaction_id="tx-fixed",
                )
        finally:
            if old is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old
        payload = recovery.load_recovery_manifest(first / recovery.RECOVERY_NAME)
        self.assertEqual(payload["created"], "2023-11-14T22:13:20Z")
        self.assertEqual(tree_snapshot(first), tree_snapshot(second))

    def _write_custom_entrypoint_archive(self) -> Path:
        entrypoint = self.source / "custom-app.pyz"
        entrypoint.write_bytes(b"custom entrypoint bytes\n")
        manifest = install_manifest.build_manifest(
            self.source,
            artifact_kind="full",
            platform="any",
            created="2026-02-03T04:05:06Z",
            entrypoints={"app": "custom-app.pyz"},
        )
        install_manifest.write_manifest(self.source, manifest)
        install_transaction.record_install(
            self.source, manifest, transaction_id="tx-custom"
        )
        destination = self.root / "recovery-custom"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="custom entrypoint",
            transaction_id="tx-custom",
            created="2026-02-03T04:05:06Z",
        )
        return destination

    def test_custom_manifest_app_entrypoint_is_verified(self):
        destination = self._write_custom_entrypoint_archive()
        self.assertTrue((destination / "custom-app.pyz").is_file())
        self.assertTrue(recovery.verify_recovery_archive(destination).ok)

    def test_missing_custom_manifest_app_entrypoint_is_rejected(self):
        destination = self._write_custom_entrypoint_archive()
        (destination / "custom-app.pyz").unlink()
        rewrite_sums(destination)
        with self.assertRaisesRegex(recovery.RecoveryError, "app entrypoint"):
            recovery.verify_recovery_archive(destination)

    def test_unsupported_recovery_archive_schema_is_rejected(self):
        destination = self.root / "recovery-schema"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="schema compatibility",
            transaction_id="tx-schema",
            created="2026-02-03T04:05:06Z",
        )
        manifest_path = destination / recovery.RECOVERY_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["schema"] = "offgrid.recovery/2"
        payload["future_field"] = "from a newer writer"
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rewrite_sums(destination)
        with self.assertRaisesRegex(
            recovery.RecoveryError, "unsupported recovery archive schema"
        ):
            recovery.verify_recovery_archive(destination)

    def test_tampered_app_file_is_rejected(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify tamper",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        (destination / "offgrid" / "cli.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryError, "SHA-256"):
            recovery.verify_recovery_archive(destination)

    def test_extra_file_is_rejected_even_if_hashes_are_recomputed(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify extra",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        (destination / "unexpected.txt").write_text("extra\n", encoding="utf-8")
        rewrite_sums(destination)
        with self.assertRaisesRegex(
            recovery.RecoveryError, "does not match|not in install-manifest"
        ):
            recovery.verify_recovery_archive(destination)

    def test_unsafe_sums_path_is_rejected(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify path",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        sums = destination / recovery.SUMS_NAME
        lines = sums.read_text(encoding="ascii").splitlines()
        lines[0] = lines[0].split("  ", 1)[0] + "  ../escape"
        sums.write_text("\n".join(lines) + "\n", encoding="ascii")
        with self.assertRaisesRegex(recovery.RecoveryError, "unsafe|relative"):
            recovery.verify_recovery_archive(destination)

    def test_unsanitized_config_is_rejected_even_with_valid_sums(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify config",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        config_path = destination / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["api_key"] = "reintroduced-secret"
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        rewrite_sums(destination)
        with self.assertRaisesRegex(recovery.RecoveryError, "unsanitized"):
            recovery.verify_recovery_archive(destination)

    def test_recovery_manifest_digest_mismatch_is_rejected(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify digest",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        manifest_path = destination / recovery.RECOVERY_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["manifest_digest"] = "0" * 64
        payload["install"]["manifest_digest"] = "0" * 64
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rewrite_sums(destination)
        with self.assertRaisesRegex(recovery.RecoveryError, "digest"):
            recovery.verify_recovery_archive(destination)

    def test_state_install_id_mismatch_is_rejected(self):
        destination = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="verify state",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
        )
        state_path = destination / install_transaction.STATE_NAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["install_id"] = "different-install"
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rewrite_sums(destination)
        with self.assertRaisesRegex(recovery.RecoveryError, "install ID"):
            recovery.verify_recovery_archive(destination)

    def test_missing_state_or_manifest_is_refused(self):
        no_state = make_install(self.root / "no-state")
        (no_state / install_transaction.STATE_NAME).unlink()
        with self.assertRaisesRegex(recovery.RecoveryError, "install-state"):
            recovery.write_recovery_archive(
                no_state,
                self.root / "out-state",
                reason="missing state",
            )

        no_manifest = make_install(self.root / "no-manifest")
        (no_manifest / install_manifest.MANIFEST_NAME).unlink()
        with self.assertRaisesRegex(recovery.RecoveryError, "manifest"):
            recovery.write_recovery_archive(
                no_manifest,
                self.root / "out-manifest",
                reason="missing manifest",
            )

    def test_unlisted_app_file_is_refused(self):
        (self.source / "offgrid" / "unlisted.py").write_text(
            "UNLISTED = 1\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(recovery.RecoveryError, "not listed"):
            recovery.write_recovery_archive(
                self.source,
                self.root / "out-unlisted",
                reason="unlisted",
            )

    def test_symlinked_app_file_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        target = self.root / "outside.py"
        target.write_text("outside\n", encoding="utf-8")
        link = self.source / "offgrid" / "linked.py"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(recovery.RecoveryError, "symlink"):
            recovery.write_recovery_archive(
                self.source,
                self.root / "out-symlink",
                reason="symlink",
            )

    def test_destination_inside_source_is_refused(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "outside"):
            recovery.write_recovery_archive(
                self.source,
                self.source / "recovery",
                reason="inside",
            )

    def test_force_replaces_an_existing_archive(self):
        destination = self.root / "recovery"
        destination.mkdir()
        (destination / "stale.txt").write_text("stale\n", encoding="utf-8")
        recovery.write_recovery_archive(
            self.source,
            destination,
            reason="force",
            transaction_id="tx-new",
            created="2026-02-03T04:05:06Z",
            force=True,
        )
        self.assertFalse((destination / "stale.txt").exists())
        recovery.verify_recovery_archive(destination)

    def test_readiness_matches_restore_preflight_for_an_unusable_parent(self):
        archive = self.root / "recovery-unusable-parent"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="readiness must match restore preflight",
            transaction_id="tx-readiness",
            created="2026-02-03T04:05:06Z",
        )
        blocker = self.root / "not-a-directory"
        blocker.write_text("a file cannot contain a destination\n", encoding="utf-8")
        target = blocker / "restored"

        with self.assertRaisesRegex(
            recovery.RecoveryError, "parent is not a directory"
        ):
            recovery.restore_recovery_archive(archive, target)

        readiness = recovery.recovery_readiness(archive, target)
        self.assertFalse(readiness.ready)
        self.assertFalse(readiness.ok)
        self.assertIn("parent is not a directory", readiness.reason)
        self.assertIsNotNone(readiness.report)
        self.assertEqual(readiness.destination, target)

    def test_readiness_and_restore_reject_the_same_destinations(self):
        archive = self.root / "recovery-readiness-matrix"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="readiness parity",
            transaction_id="tx-readiness-matrix",
            created="2026-02-03T04:05:06Z",
        )
        blocker = self.root / "not-a-directory"
        blocker.write_text("not a directory\n", encoding="utf-8")
        cases = (
            ("parent is not a directory", blocker / "restored"),
            ("overlaps the archive", archive / "restored"),
            ("inside a transaction directory", self.root / ".offgrid-install" / "restored"),
            ("missing required assets", self.root / "missing-assets"),
        )

        for expected, target in cases:
            with self.subTest(target=target):
                with self.assertRaisesRegex(recovery.RecoveryError, expected):
                    recovery.restore_recovery_archive(archive, target)

                readiness = recovery.recovery_readiness(archive, target)
                self.assertFalse(readiness.ready)
                self.assertFalse(readiness.ok)
                self.assertIn(expected, readiness.reason)
                self.assertIsNotNone(readiness.report)

    def test_restore_refuses_a_same_size_corrupt_model(self):
        archive = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="replace the app",
            transaction_id="tx-new",
        )
        target = self.root / "restored"
        target.mkdir()
        shutil.copytree(self.source / "models", target / "models")
        shutil.copytree(self.source / "runtime", target / "runtime")
        (target / "models" / "model.gguf").write_bytes(b"evilm")
        before = tree_snapshot(target)

        with self.assertRaisesRegex(
            recovery.RecoveryError, "model hash mismatch"
        ):
            recovery.restore_recovery_archive(archive, target)

        self.assertEqual(tree_snapshot(target), before)

    def test_restore_refuses_a_same_size_corrupt_runtime(self):
        archive = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="replace the app",
            transaction_id="tx-new",
        )
        target = self.root / "restored"
        target.mkdir()
        shutil.copytree(self.source / "models", target / "models")
        shutil.copytree(self.source / "runtime", target / "runtime")
        (target / "runtime" / "llama" / "llama-server").write_bytes(
            b"corrupt"
        )
        before = tree_snapshot(target)

        with self.assertRaisesRegex(
            recovery.RecoveryError, "runtime hash mismatch"
        ):
            recovery.restore_recovery_archive(archive, target)

        self.assertEqual(tree_snapshot(target), before)

    def _make_older_archive(self) -> Path:
        older = make_install(self.root, "older-install")
        (older / "offgrid" / "cli.py").write_text(
            "# older app\n", encoding="utf-8"
        )
        manifest = install_manifest.build_manifest(
            older, artifact_kind="full", platform="any"
        )
        install_manifest.write_manifest(older, manifest)
        install_transaction.record_install(
            older, manifest, transaction_id="tx-older"
        )
        archive = self.root / "older-archive"
        recovery.write_recovery_archive(
            older,
            archive,
            reason="older app",
            transaction_id="tx-older-archive",
        )
        return archive

    def test_restore_saves_an_app_only_snapshot_that_can_be_restored(self):
        archive = self._make_older_archive()
        current_marker = (self.source / "offgrid" / "cli.py").read_text(
            encoding="utf-8"
        )
        current_state = json.loads(
            (self.source / install_transaction.STATE_NAME).read_text(
                encoding="utf-8"
            )
        )
        current_install_id = current_state["install_id"]
        model_bytes = (self.source / "models" / "model.gguf").read_bytes()
        runtime_bytes = (
            self.source / "runtime" / "llama" / "llama-server"
        ).read_bytes()

        restored = recovery.restore_recovery_archive(
            archive, self.source, confirm=True
        )

        self.assertTrue(restored.safety_snapshot)
        self.assertGreater(restored.safety_generation, 0)
        snapshot = Path(restored.safety_snapshot)
        self.assertTrue(snapshot.is_dir())
        self.assertEqual(recovery.verify_recovery_archive(snapshot).ok, True)
        self.assertEqual(
            (snapshot / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            current_marker,
        )
        self.assertEqual(
            (snapshot / install_transaction.STATE_NAME).is_file(), True
        )
        self.assertEqual(
            (snapshot / install_manifest.MANIFEST_NAME).is_file(), True
        )
        self.assertFalse((snapshot / "models").exists())
        self.assertFalse((snapshot / "runtime").exists())
        self.assertFalse((snapshot / ".offgrid").exists())
        self.assertEqual(
            (self.source / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            "# older app\n",
        )
        self.assertEqual(restored.as_dict()["safety_snapshot"], str(snapshot))

        undone = recovery.restore_recovery_archive(
            snapshot, self.source, force=True, confirm=True
        )

        self.assertEqual(undone.install_id, current_install_id)
        self.assertEqual(
            (self.source / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            current_marker,
        )
        self.assertEqual(
            (self.source / "models" / "model.gguf").read_bytes(), model_bytes
        )
        self.assertEqual(
            (self.source / "runtime" / "llama" / "llama-server").read_bytes(),
            runtime_bytes,
        )
        self.assertEqual(
            (self.source / ".offgrid" / "session-token").read_text(
                encoding="utf-8"
            ),
            "private",
        )

    def test_restore_snapshot_failure_leaves_the_current_app_untouched(self):
        archive = self._make_older_archive()
        before = tree_snapshot(self.source)
        current_marker = (self.source / "offgrid" / "cli.py").read_text(
            encoding="utf-8"
        )

        with mock.patch(
            "offgrid.install_transaction._create_recovery_bundle",
            side_effect=install_transaction.InstallError("snapshot failed"),
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryError, "snapshot failed"
            ):
                recovery.restore_recovery_archive(
                archive, self.source, confirm=True
            )

        self.assertEqual(tree_snapshot(self.source), before)
        self.assertEqual(
            (self.source / "offgrid" / "cli.py").read_text(encoding="utf-8"),
            current_marker,
        )

    def test_restore_without_an_existing_app_has_no_safety_snapshot(self):
        archive = self.root / "recovery"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="restore without an app",
            transaction_id="tx-no-app",
        )
        target = self.root / "assets-only"
        target.mkdir()
        shutil.copytree(self.source / "models", target / "models")
        shutil.copytree(self.source / "runtime", target / "runtime")

        readiness = recovery.recovery_readiness(archive, target)
        plan = recovery.recovery_restore_plan(archive, target)
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.lineage, "unmanaged")
        self.assertTrue(readiness.confirmation_required)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.lineage, "unmanaged")
        self.assertTrue(plan.blockers)
        self.assertTrue(
            any("no managed install metadata" in warning for warning in plan.warnings)
        )

        restored = recovery.restore_recovery_archive(
            archive, target, confirm=True
        )

        self.assertEqual(restored.safety_snapshot, "")
        self.assertEqual(restored.safety_generation, 0)
        self.assertFalse(
            install_transaction.recovery_archive_path(target, 1).exists()
        )

    def test_restore_plan_describes_a_ready_destination_without_writing(self):
        archive = self.root / "recovery-plan"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan a restore",
            transaction_id="tx-plan",
        )
        target = self.root / "planned-target"
        shutil.copytree(self.source, target)
        (target / "offgrid" / "cli.py").write_text(
            "# drifted app\n", encoding="utf-8"
        )
        before_target = tree_snapshot(target)
        before_archive = tree_snapshot(archive)

        plan = recovery.recovery_restore_plan(archive, target)

        self.assertTrue(plan.ready)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.archive, archive.resolve())
        self.assertEqual(plan.destination, target.resolve())
        self.assertEqual(plan.reason, "archive verifies and the destination has its required assets")
        self.assertFalse(plan.file_restore)
        self.assertTrue(plan.overwrite)
        self.assertFalse(plan.force)
        self.assertFalse(plan.force_required)
        self.assertIn("not required", plan.force_status)
        self.assertTrue(plan.drift)
        self.assertIn("offgrid/cli.py", " ".join(plan.drift))
        self.assertIn("models/", plan.preserved_state)
        self.assertIn("runtime/", plan.preserved_state)
        self.assertIn(".offgrid/", plan.preserved_state)
        actions = {item.path: item.action for item in plan.affected_files}
        self.assertEqual(actions["offgrid/cli.py"], "replace")
        self.assertTrue(plan.affected_files)
        assets = {item.path: item for item in plan.required_assets}
        self.assertTrue(assets["models/model.gguf"].present)
        self.assertEqual(assets["models/model.gguf"].status, "ready")
        self.assertTrue(assets["runtime/llama"].present)
        self.assertEqual(assets["runtime/llama"].status, "ready")

        payload = plan.as_dict()
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["destination"], str(target.resolve()))
        self.assertEqual(payload["drift"], list(plan.drift))
        self.assertEqual(payload["affected_files"][0]["path"], "LICENSE")
        self.assertTrue(payload["required_assets"])
        self.assertEqual(tree_snapshot(target), before_target)
        self.assertEqual(tree_snapshot(archive), before_archive)

    def test_restore_plan_carries_binding_safety_and_asset_provenance(self):
        archive = self.root / "recovery-plan-metadata"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan metadata",
            transaction_id="tx-plan-metadata",
        )

        plan = recovery.recovery_restore_plan(archive, self.source)

        self.assertTrue(plan.ready)
        self.assertEqual(plan.plan_schema, recovery.PLAN_SCHEMA)
        self.assertRegex(plan.plan_id, r"^[0-9a-f]{32}$")
        self.assertEqual(plan.blockers, ())
        self.assertTrue(plan.safety_snapshot)
        self.assertGreater(plan.safety_generation, 0)
        self.assertTrue(plan.safety_snapshot_protected)
        self.assertFalse(Path(plan.safety_snapshot).exists())
        self.assertTrue(plan.destination_fingerprint.startswith("managed:"))
        self.assertGreater(plan.required_bytes, 0)
        self.assertGreater(plan.available_bytes, 0)
        self.assertEqual(plan.artifact_platform, "any")

        assets = {asset.path: asset for asset in plan.required_assets}
        self.assertEqual(assets["models/model.gguf"].source, "destination")
        self.assertEqual(
            assets["models/model.gguf"].sha256,
            install_manifest.sha256_file(self.source / "models" / "model.gguf"),
        )
        self.assertEqual(assets["runtime/llama"].source, "destination")
        self.assertEqual(
            assets["runtime/llama"].sha256,
            install_manifest.sha256_file(
                self.source / "runtime" / "llama" / "llama-server"
            ),
        )

        payload = plan.as_dict()
        self.assertEqual(payload["plan_id"], plan.plan_id)
        self.assertEqual(payload["plan_schema"], recovery.PLAN_SCHEMA)
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(
            recovery.recovery_restore_plan(archive, self.source).plan_id,
            plan.plan_id,
        )

    def test_restore_plan_id_changes_when_destination_changes(self):
        archive = self.root / "recovery-plan-binding"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan binding",
            transaction_id="tx-plan-binding",
        )
        target = self.root / "bound-target"
        shutil.copytree(self.source, target)
        plan = recovery.recovery_restore_plan(archive, target)
        (target / "offgrid" / "cli.py").write_text(
            "# changed after review\n", encoding="utf-8"
        )

        changed = recovery.recovery_restore_plan(archive, target)

        self.assertNotEqual(changed.plan_id, plan.plan_id)
        before = tree_snapshot(target)
        with self.assertRaisesRegex(recovery.RecoveryError, "plan changed"):
            recovery.restore_recovery_archive(
                archive, target, expect_plan=plan.plan_id
            )
        self.assertEqual(tree_snapshot(target), before)

    def test_restore_plan_id_changes_when_unmanaged_files_change(self):
        archive = self.root / "recovery-plan-unmanaged"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan unmanaged binding",
            transaction_id="tx-plan-unmanaged",
        )
        target = self.root / "unmanaged-target"
        target.mkdir()
        (target / "app.py").write_text("# original\n", encoding="utf-8")

        plan = recovery.recovery_restore_plan(
            archive,
            target,
            asset_source=self.source,
            confirm=True,
        )
        (target / "app.py").write_text("# changed after review\n", encoding="utf-8")
        changed = recovery.recovery_restore_plan(
            archive,
            target,
            asset_source=self.source,
            confirm=True,
        )

        self.assertNotEqual(changed.plan_id, plan.plan_id)

    def test_restore_accepts_a_matching_expected_plan(self):
        archive = self.root / "recovery-plan-match"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan match",
            transaction_id="tx-plan-match",
        )
        target = self.root / "matching-target"
        shutil.copytree(self.source, target)
        plan = recovery.recovery_restore_plan(archive, target)

        restored = recovery.restore_recovery_archive(
            archive, target, expect_plan=plan.plan_id
        )

        self.assertTrue(restored.ok)
        self.assertEqual(restored.install_id, plan.report.install_id)
        self.assertEqual(restored.safety_snapshot, plan.safety_snapshot)
        self.assertEqual(restored.safety_generation, plan.safety_generation)

    def test_restore_plan_warns_about_low_free_space_without_changing_id(self):
        archive = self.root / "recovery-plan-space"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="plan space",
            transaction_id="tx-plan-space",
        )
        target = self.root / "space-target"
        shutil.copytree(self.source, target)

        with mock.patch(
            "offgrid.recovery.shutil.disk_usage",
            return_value=mock.Mock(free=1),
        ):
            plan = recovery.recovery_restore_plan(archive, target)
        with mock.patch(
            "offgrid.recovery.shutil.disk_usage",
            return_value=mock.Mock(free=10_000_000),
        ):
            roomy = recovery.recovery_restore_plan(archive, target)

        self.assertIn("low free space", " ".join(plan.warnings))
        self.assertEqual(plan.plan_id, roomy.plan_id)
        self.assertTrue(plan.ready)

    def test_restore_plan_rejects_a_destination_missing_required_assets(self):
        archive = self.root / "recovery-rejected-plan"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="reject a restore plan",
            transaction_id="tx-rejected-plan",
        )
        target = self.root / "missing-assets"
        target.mkdir()
        before = tree_snapshot(target)

        plan = recovery.recovery_restore_plan(archive, target)

        self.assertFalse(plan.ready)
        self.assertFalse(plan.ok)
        self.assertIn("missing required assets", plan.reason)
        self.assertTrue(plan.affected_files)
        self.assertTrue(all(item.action == "create" for item in plan.affected_files))
        assets = {item.path: item for item in plan.required_assets}
        self.assertFalse(assets["models/model.gguf"].present)
        self.assertEqual(assets["models/model.gguf"].status, "missing")
        self.assertFalse(assets["runtime/llama"].present)
        self.assertEqual(assets["runtime/llama"].status, "missing")
        self.assertTrue(plan.overwrite)
        self.assertIsNotNone(plan.report)
        self.assertEqual(tree_snapshot(target), before)

    def test_restore_reuses_verified_assets_from_an_explicit_source(self):
        archive = self.root / "recovery-asset-source"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="restore with surviving assets",
            transaction_id="tx-assets",
        )
        assets = self.root / "surviving-assets"
        assets.mkdir()
        shutil.copytree(self.source / "models", assets / "models")
        shutil.copytree(self.source / "runtime", assets / "runtime")
        (self.root / "new-device").mkdir()
        target = self.root / "new-device" / "offgrid"

        plan = recovery.recovery_restore_plan(
            archive, target, asset_source=assets
        )

        self.assertTrue(plan.ready)
        self.assertEqual(plan.asset_source, str(assets.resolve()))
        self.assertIn("reused from", plan.reason)
        self.assertTrue(
            all(asset.status == "ready" for asset in plan.required_assets)
        )

        restored = recovery.restore_recovery_archive(
            archive, target, asset_source=assets
        )

        self.assertTrue(restored.ok)
        self.assertEqual(restored.asset_source, str(assets.resolve()))
        self.assertIn(
            "print('app')",
            (target / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (target / "models" / "model.gguf").read_bytes(), b"model"
        )
        self.assertEqual(
            (target / "runtime" / "llama" / "llama-server").read_bytes(),
            b"runtime",
        )
        self.assertTrue(recovery.verify_recovery_archive(archive).ok)

    def test_asset_source_fills_missing_assets_for_an_existing_app(self):
        archive = self.root / "recovery-partial-app"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="fill missing assets",
            transaction_id="tx-partial-app",
        )
        assets = self.root / "partial-surviving-assets"
        assets.mkdir()
        shutil.copytree(self.source / "models", assets / "models")
        shutil.copytree(self.source / "runtime", assets / "runtime")
        target = self.root / "partial-app"
        shutil.copytree(self.source, target)
        shutil.rmtree(target / "models")
        shutil.rmtree(target / "runtime")

        restored = recovery.restore_recovery_archive(
            archive, target, asset_source=assets
        )

        self.assertTrue(restored.ok)
        self.assertEqual(
            (target / "models" / "model.gguf").read_bytes(), b"model"
        )
        self.assertEqual(
            (target / "runtime" / "llama" / "llama-server").read_bytes(),
            b"runtime",
        )
        self.assertEqual(
            (target / ".offgrid" / "session-token").read_text(
                encoding="utf-8"
            ),
            "private",
        )

    def test_asset_source_hashes_are_checked_before_destination_changes(self):
        archive = self.root / "recovery-bad-assets"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="reject bad asset source",
            transaction_id="tx-bad-assets",
        )
        assets = self.root / "bad-surviving-assets"
        assets.mkdir()
        shutil.copytree(self.source / "models", assets / "models")
        shutil.copytree(self.source / "runtime", assets / "runtime")
        (assets / "models" / "model.gguf").write_bytes(b"evil!")
        (self.root / "lost-device").mkdir()
        target = self.root / "lost-device" / "offgrid"
        before = tree_snapshot(self.root / "lost-device")

        plan = recovery.recovery_restore_plan(
            archive, target, asset_source=assets
        )

        self.assertFalse(plan.ready)
        self.assertEqual(plan.asset_source, str(assets))
        self.assertIn("asset source is missing required assets", plan.reason)
        with self.assertRaisesRegex(
            recovery.RecoveryError, "asset source is missing required assets"
        ):
            recovery.restore_recovery_archive(
                archive, target, asset_source=assets
            )
        self.assertFalse(target.exists())
        self.assertEqual(tree_snapshot(self.root / "lost-device"), before)

    def test_same_install_restore_needs_no_confirmation(self):
        archive = self.root / "same-lineage-archive"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="same lineage",
            transaction_id="tx-same-lineage",
        )

        readiness = recovery.recovery_readiness(archive, self.source)
        plan = recovery.recovery_restore_plan(archive, self.source)
        restored = recovery.restore_recovery_archive(archive, self.source)

        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.lineage, "same")
        self.assertFalse(readiness.confirmation_required)
        self.assertTrue(plan.ready)
        self.assertEqual(plan.lineage, "same")
        self.assertFalse(plan.confirmation_required)
        self.assertTrue(restored.ok)

    def test_lineage_preflight_requires_confirmation_for_a_newer_destination(self):
        first = make_install(self.root, "lineage-first")
        second = make_install(self.root, "lineage-second")
        destination = self.root / "device" / "offgrid"
        install_transaction.install_directory(first, destination)
        install_transaction.install_directory(second, destination, force=True)
        archive = install_transaction.recovery_archive_path(destination, 1)
        first_state = json.loads(
            (archive / install_transaction.STATE_NAME).read_text(
                encoding="utf-8"
            )
        )
        before = tree_snapshot(destination)

        readiness = recovery.recovery_readiness(archive, destination)
        plan = recovery.recovery_restore_plan(archive, destination)
        confirmed_plan = recovery.recovery_restore_plan(
            archive, destination, confirm=True
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.lineage, "newer")
        self.assertTrue(readiness.confirmation_required)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.lineage, "newer")
        self.assertTrue(plan.destination_newer)
        self.assertTrue(confirmed_plan.ready)
        with self.assertRaisesRegex(
            recovery.RecoveryError, "explicit confirmation"
        ):
            recovery.restore_recovery_archive(archive, destination)
        self.assertEqual(tree_snapshot(destination), before)

        restored = recovery.restore_recovery_archive(
            archive, destination, confirm=True
        )

        self.assertTrue(restored.ok)
        self.assertEqual(restored.lineage, "newer")
        self.assertEqual(restored.install_id, first_state["install_id"])

    def test_lineage_preflight_requires_confirmation_for_an_older_destination(self):
        first = make_install(self.root, "lineage-older")
        destination = self.root / "device" / "offgrid"
        install_transaction.install_directory(first, destination)
        older_target = self.root / "older-target"
        shutil.copytree(destination, older_target)
        second = make_install(self.root, "lineage-newer")
        install_transaction.install_directory(second, destination, force=True)
        archive = self.root / "newer-archive"
        recovery.write_recovery_archive(
            destination,
            archive,
            reason="newer install",
            transaction_id="tx-newer-archive",
        )
        second_state = json.loads(
            (destination / install_transaction.STATE_NAME).read_text(
                encoding="utf-8"
            )
        )

        readiness = recovery.recovery_readiness(archive, older_target)
        plan = recovery.recovery_restore_plan(archive, older_target)

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.lineage, "older")
        self.assertTrue(readiness.confirmation_required)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.lineage, "older")
        self.assertTrue(plan.destination_older)
        with self.assertRaisesRegex(
            recovery.RecoveryError, "explicit confirmation"
        ):
            recovery.restore_recovery_archive(archive, older_target)

        restored = recovery.restore_recovery_archive(
            archive, older_target, confirm=True
        )

        self.assertTrue(restored.ok)
        self.assertEqual(restored.install_id, second_state["install_id"])

    def test_lineage_preflight_requires_confirmation_for_a_different_lineage(self):
        archive = self.root / "lineage-archive"
        recovery.write_recovery_archive(
            self.source,
            archive,
            reason="lineage archive",
            transaction_id="tx-lineage-archive",
        )
        target = make_install(self.root, "unrelated-target")

        readiness = recovery.recovery_readiness(archive, target)
        plan = recovery.recovery_restore_plan(archive, target)

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.lineage, "different-lineage")
        self.assertTrue(readiness.confirmation_required)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.lineage, "different-lineage")
        with self.assertRaisesRegex(
            recovery.RecoveryError, "explicit confirmation"
        ):
            recovery.restore_recovery_archive(archive, target)

        restored = recovery.restore_recovery_archive(
            archive, target, confirm=True
        )
        self.assertTrue(restored.ok)

    def test_sanitizer_allowlist_drops_unknown_nested_credentials(self):
        raw = {
            "offline": True,
            "max_turns": 4,
            "household": ["Ada"],
            "endpoint": "http://user:password@127.0.0.1:8080/v1",
            "api_key": "known-secret",
            "fleet_token": "fleet-secret",
            "unknown_provider": {
                "credential": "unknown-secret",
                "apiKey": "camel-secret",
                "safe": "keep",
            },
            "endpoint_options": {"credential": "endpoint-secret"},
            "endpoints": [
                "http://user:password@127.0.0.1:8080/v1",
                {"credential": "list-secret"},
            ],
        }

        clean = recovery.sanitize_config(raw)

        self.assertEqual(
            clean,
            {
                "offline": True,
                "max_turns": 4,
                "household": ["Ada"],
                "endpoint": "http://127.0.0.1:8080/v1",
            },
        )
        serialized = json.dumps(clean)
        for secret in (
            "known-secret",
            "fleet-secret",
            "unknown-secret",
            "camel-secret",
            "endpoint-secret",
            "list-secret",
            "password",
        ):
            self.assertNotIn(secret, serialized)

    def test_sanitizer_rejects_nested_objects_under_safe_fields(self):
        clean = recovery.sanitize_config(
            {
                "workspace": {"credential": "workspace-secret"},
                "native_tools": None,
                "command_policy": "ask",
            }
        )
        self.assertEqual(
            clean, {"native_tools": None, "command_policy": "ask"}
        )
        self.assertNotIn("workspace-secret", json.dumps(clean))

    def test_sanitizer_keeps_known_safe_path_fields(self):
        clean = recovery.sanitize_config(
            {
                "sync_token_file": ".offgrid/sync-token",
                "fleet_file": ".offgrid/fleet.json",
                "security_history": ".offgrid/security.jsonl",
            }
        )
        self.assertEqual(
            clean,
            {
                "sync_token_file": ".offgrid/sync-token",
                "fleet_file": ".offgrid/fleet.json",
                "security_history": ".offgrid/security.jsonl",
            },
        )

    def test_invalid_created_and_reason_are_refused(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "RFC 3339"):
            recovery.write_recovery_archive(
                self.source,
                self.root / "bad-created",
                reason="bad time",
                created="yesterday",
            )
        with self.assertRaisesRegex(recovery.RecoveryError, "reason"):
            recovery.write_recovery_archive(
                self.source,
                self.root / "bad-reason",
                reason="   ",
                created="2026-02-03T04:05:06Z",
            )


if __name__ == "__main__":
    unittest.main()
