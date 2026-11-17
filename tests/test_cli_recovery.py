"""Recovery-archive verification and restore CLI coverage."""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from offgrid import install_manifest, install_transaction, portable, recovery
from offgrid.cli import main


def make_bundle(root: Path, *, marker: str = "v1", with_assets: bool = True) -> Path:
    bundle = root / f"bundle-{marker}"
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
    for name in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
        (bundle / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (bundle / "config.json").write_text('{"offline": true}\n', encoding="utf-8")
    if with_assets:
        (bundle / "models").mkdir()
        (bundle / "models" / "model.gguf").write_bytes(b"model bytes")
        binary = bundle / "runtime" / "llama" / "llama-server"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"runtime bytes")
        binary.chmod(0o755)
    manifest = install_manifest.build_manifest(
        bundle, artifact_kind="full", platform="any"
    )
    install_manifest.write_manifest(bundle, manifest)
    return bundle


def install_and_archive(root: Path, *, with_assets: bool = True):
    source = make_bundle(root, marker="previous", with_assets=with_assets)
    device = root / "device"
    device.mkdir()
    destination = device / source.name
    install_transaction.install_directory(source, destination)
    archive = root / "archive"
    recovery.write_recovery_archive(
        destination,
        archive,
        reason="CLI recovery test",
        transaction_id="tx-previous",
        created="2026-01-02T03:04:05Z",
    )
    return destination, archive


def install_pyz_and_archive(root: Path):
    build_dir = root / "pyz-build"
    build_dir.mkdir()
    _, pyz = portable.build_distribution(build_dir / "offgrid.zip")
    device = root / "pyz-device"
    device.mkdir()
    destination = device / pyz.name
    install_transaction.install_file(pyz, destination)
    install_transaction.install_file(pyz, destination, force=True)
    archive = install_transaction.recovery_archive_path(destination, 1)
    return destination, archive, pyz


def tree_snapshot(root: Path):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.stat()
        if path.is_dir():
            snapshot[relative] = ("dir", info.st_mode, info.st_mtime_ns)
        else:
            snapshot[relative] = (
                "file",
                info.st_mode,
                info.st_mtime_ns,
                path.read_bytes(),
            )
    return snapshot


def run_cli(root: Path, *args):
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        code = main(["protect", *args, "--workspace", str(root), "--no-color"])
    return code, output.getvalue()


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_verify_reports_a_valid_recovery_archive(self):
        _, archive = install_and_archive(self.root)

        code, output = run_cli(
            self.root,
            "--verify",
            str(archive),
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], str(archive.resolve()))
        self.assertEqual(payload["version"], "0.1.0")
        self.assertTrue(payload["config_sanitized"])

    def test_verify_rejects_a_tampered_archive_without_writing(self):
        destination, archive = install_and_archive(self.root)
        (archive / "offgrid" / "cli.py").write_text("tampered\n", encoding="utf-8")
        before_archive = tree_snapshot(archive)
        before_destination = tree_snapshot(destination)

        code, output = run_cli(self.root, "--verify", str(archive))

        self.assertEqual(code, 1, output)
        self.assertIn("SHA-256", output)
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_verify_rejects_a_missing_archive_without_writing(self):
        missing = self.root / "missing-archive"

        code, output = run_cli(
            self.root,
            "--verify",
            str(missing),
        )

        self.assertEqual(code, 1, output)
        self.assertIn("not a directory", output)
        self.assertFalse(missing.exists())

    def test_verify_rejects_an_archive_missing_a_file_without_writing(self):
        destination, archive = install_and_archive(self.root)
        (archive / recovery.SUMS_NAME).unlink()
        before_archive = tree_snapshot(archive)
        before_destination = tree_snapshot(destination)

        code, output = run_cli(self.root, "--verify", str(archive))

        self.assertEqual(code, 1, output)
        self.assertIn(recovery.SUMS_NAME, output)
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_verify_empty_path_does_not_fall_through_to_backup(self):
        backup = self.root / "backup-should-not-write"
        backup.mkdir()

        code, output = run_cli(self.root, "--verify", "", "--backup", str(backup))

        self.assertEqual(code, 2, output)
        self.assertIn("archive path", output)
        self.assertEqual(tree_snapshot(backup), {})

    def test_recover_empty_path_does_not_fall_through_to_backup(self):
        backup = self.root / "backup-should-not-write"
        backup.mkdir()

        code, output = run_cli(
            self.root, "--recover", "", "--backup", str(backup)
        )

        self.assertEqual(code, 2, output)
        self.assertIn("--recover requires", output)
        self.assertIn("archive path", output)
        self.assertEqual(tree_snapshot(backup), {})

    def test_verify_is_read_only_and_needs_no_local_assets(self):
        destination, archive = install_and_archive(self.root)

        before_archive = tree_snapshot(archive)

        shutil.rmtree(destination / "models")
        shutil.rmtree(destination / "runtime")
        before_destination = tree_snapshot(destination)
        code, output = run_cli(
            self.root,
            "--verify",
            str(archive),
            "--json",
        )

        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)["ok"])
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_verify_rejects_backup_without_writing(self):
        destination, archive = install_and_archive(self.root)
        backup = self.root / "backup-should-not-write"
        backup.mkdir()
        before_archive = tree_snapshot(archive)
        before_destination = tree_snapshot(destination)

        code, output = run_cli(
            self.root,
            "--verify",
            str(archive),
            "--backup",
            str(backup),
        )

        self.assertEqual(code, 2, output)
        self.assertIn("--backup", output)
        self.assertIn("verification is read-only", output)
        self.assertEqual(tree_snapshot(backup), {})
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_verify_rejects_out_without_writing(self):
        destination, archive = install_and_archive(self.root)
        output_target = self.root / "output-should-not-write"
        before_archive = tree_snapshot(archive)
        before_destination = tree_snapshot(destination)

        code, output = run_cli(
            self.root,
            "--verify",
            str(archive),
            "--out",
            str(output_target),
        )

        self.assertEqual(code, 2, output)
        self.assertIn("--out", output)
        self.assertIn("verification is read-only", output)
        self.assertFalse(output_target.exists())
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_verify_ignores_sign_key_and_model_out_without_writing(self):
        destination, archive = install_and_archive(self.root)
        before_archive = tree_snapshot(archive)
        before_destination = tree_snapshot(destination)
        model_target = self.root / "model-should-not-write"
        key_target = self.root / "key-should-not-write"

        code, output = run_cli(
            self.root,
            "--verify",
            str(archive),
            "--sign-key",
            str(key_target),
            "--model-out",
            str(model_target),
            "--force",
            "--json",
        )

        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)["ok"])
        self.assertFalse(model_target.exists())
        self.assertFalse(key_target.exists())
        self.assertEqual(tree_snapshot(archive), before_archive)
        self.assertEqual(tree_snapshot(destination), before_destination)

    def test_recover_restores_the_previous_app_and_preserves_assets(self):
        destination, archive = install_and_archive(self.root)
        target = self.root / "restored"
        target.mkdir()
        shutil.copytree(destination / "models", target / "models")
        shutil.copytree(destination / "runtime", target / "runtime")
        previous_state = json.loads(
            (archive / install_transaction.STATE_NAME).read_text(encoding="utf-8")
        )

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--confirm",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["destination"], str(target.resolve()))
        self.assertIn(
            "source previous",
            (target / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (target / "models" / "model.gguf").read_bytes(), b"model bytes"
        )
        self.assertEqual(
            (target / "runtime" / "llama" / "llama-server").read_bytes(),
            b"runtime bytes",
        )
        state = json.loads(
            (target / install_transaction.STATE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(state["install_id"], previous_state["install_id"])
        self.assertEqual(state["status"], "committed")

    def test_recover_refuses_missing_required_assets(self):
        _, archive = install_and_archive(self.root)
        target = self.root / "empty-target"

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
        )

        self.assertEqual(code, 1, output)
        self.assertIn("missing required assets", output)
        self.assertIn("model.gguf", output)
        self.assertFalse(target.exists())

    def test_recover_refuses_an_archive_destination(self):
        _, archive = install_and_archive(self.root)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(archive),
            "--force",
        )

        self.assertEqual(code, 1, output)
        self.assertIn("unsafe recovery destination", output)

    def test_recover_dry_run_plans_without_restoring(self):
        destination, archive = install_and_archive(self.root)
        target = self.root / "dry-run-target"
        target.mkdir()
        shutil.copytree(destination / "models", target / "models")
        shutil.copytree(destination / "runtime", target / "runtime")
        before = tree_snapshot(target)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--confirm",
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["archive"], str(archive.resolve()))
        self.assertEqual(payload["destination"], str(target.resolve()))
        self.assertTrue(payload["overwrite"])
        self.assertFalse(payload["force_required"])
        self.assertEqual(payload["drift"], [])
        self.assertTrue(payload["affected_files"])
        self.assertTrue(
            all(item["action"] == "create" for item in payload["affected_files"])
        )
        self.assertIn("models/", payload["preserved_state"])
        self.assertIn("runtime/", payload["preserved_state"])
        self.assertTrue(
            all(
                asset["status"] == "ready"
                for asset in payload["required_assets"]
            )
        )
        self.assertEqual(tree_snapshot(target), before)

    def test_recover_dry_run_reports_a_rejected_destination(self):
        _destination, archive = install_and_archive(self.root)
        target = self.root / "dry-run-rejected"
        target.mkdir()
        before = tree_snapshot(target)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 1, output)
        payload = json.loads(output)
        self.assertFalse(payload["ready"])
        self.assertIn("missing required assets", payload["reason"])
        self.assertTrue(payload["affected_files"])
        self.assertTrue(
            all(
                asset["status"] == "missing"
                for asset in payload["required_assets"]
            )
        )
        self.assertEqual(tree_snapshot(target), before)

    def test_recover_latest_dry_run_plans_without_restoring(self):
        destination, _archive = install_and_archive(self.root)
        latest = install_transaction.recovery_archive_path(destination, 1)
        recovery.write_recovery_archive(
            destination,
            latest,
            reason="latest dry-run test",
            transaction_id="tx-latest-dry-run",
            created="2026-01-02T03:04:05Z",
        )
        before_destination = tree_snapshot(destination)
        before_archive = tree_snapshot(latest)

        code, output = run_cli(
            self.root,
            "--recover-latest",
            str(destination),
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["file_restore"])
        self.assertEqual(payload["archive"], str(latest))
        self.assertEqual(payload["destination"], str(destination.resolve()))
        self.assertEqual(payload["plan_schema"], recovery.PLAN_SCHEMA)
        self.assertRegex(payload["plan_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["blockers"], [])
        self.assertTrue(payload["safety_snapshot"])
        self.assertEqual(payload["selection"]["archive"], str(latest))
        self.assertEqual(payload["selection"]["generation"], 1)
        self.assertEqual(payload["selection"]["skipped"], [])
        self.assertTrue(
            all(item["action"] == "unchanged" for item in payload["affected_files"])
        )
        self.assertEqual(tree_snapshot(destination), before_destination)
        self.assertEqual(tree_snapshot(latest), before_archive)

    def test_recover_latest_dry_run_reports_skipped_corrupt_archive(self):
        first = make_bundle(self.root, marker="first")
        device = self.root / "device"
        device.mkdir()
        destination = device / "offgrid"
        install_transaction.install_directory(first, destination)
        second = make_bundle(self.root, marker="second")
        install_transaction.install_directory(second, destination, force=True)
        third = make_bundle(self.root, marker="third")
        install_transaction.install_directory(third, destination, force=True)
        newest = install_transaction.recovery_archive_path(destination, 2)
        (newest / "offgrid" / "cli.py").write_text(
            "tampered\n", encoding="utf-8"
        )

        code, output = run_cli(
            self.root,
            "--recover-latest",
            str(destination),
            "--confirm",
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        selected = install_transaction.recovery_archive_path(destination, 1)
        self.assertEqual(payload["archive"], str(selected))
        self.assertEqual(payload["selection"]["generation"], 1)
        self.assertEqual(
            [item["generation"] for item in payload["selection"]["skipped"]],
            [2],
        )
        self.assertIn(
            "offgrid/cli.py", payload["selection"]["skipped"][0]["reason"]
        )

    def test_recover_expect_plan_rejects_a_changed_destination(self):
        destination, archive = install_and_archive(self.root)
        target = self.root / "plan-bound-target"
        shutil.copytree(destination, target)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--dry-run",
            "--json",
        )
        self.assertEqual(code, 0, output)
        plan = json.loads(output)
        (target / "offgrid" / "cli.py").write_text(
            "# changed after review\n", encoding="utf-8"
        )
        before = tree_snapshot(target)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--expect-plan",
            plan["plan_id"],
            "--json",
        )

        self.assertEqual(code, 1, output)
        self.assertIn("plan changed", output)
        self.assertEqual(tree_snapshot(target), before)

    def test_recover_latest_dry_run_reports_when_nothing_is_retained(self):
        destination = self.root / "device" / "offgrid"
        destination.parent.mkdir(parents=True)

        code, output = run_cli(
            self.root,
            "--recover-latest",
            str(destination),
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 1, output)
        self.assertIn("no valid recovery archive", output)
        self.assertFalse(destination.exists())

    def test_recover_dry_run_plans_a_pyz_restore(self):
        destination, archive, pyz = install_pyz_and_archive(self.root)
        target = self.root / "restored.pyz"
        before_archive = tree_snapshot(archive)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["file_restore"])
        self.assertEqual(payload["destination"], str(target.resolve()))
        self.assertEqual(
            [(item["path"], item["action"]) for item in payload["affected_files"]],
            [(target.name, "create")],
        )
        self.assertFalse(target.exists())
        self.assertEqual(destination.read_bytes(), pyz.read_bytes())
        self.assertEqual(tree_snapshot(archive), before_archive)

    def test_recover_dry_run_reports_existing_pyz_details(self):
        destination, archive, pyz = install_pyz_and_archive(self.root)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(destination),
            "--confirm",
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["file_restore"])
        self.assertEqual(
            payload["existing_app_sha256"], install_manifest.sha256_file(pyz)
        )
        self.assertEqual(payload["existing_app_size"], pyz.stat().st_size)
        self.assertEqual(
            [(item["path"], item["action"]) for item in payload["affected_files"]],
            [(destination.name, "unchanged")],
        )

    def test_recover_dry_run_reports_a_rejected_pyz_destination(self):
        _destination, archive, _pyz = install_pyz_and_archive(self.root)
        target = self.root / "missing-parent" / "restored.pyz"
        before_archive = tree_snapshot(archive)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--dry-run",
            "--json",
        )

        self.assertEqual(code, 1, output)
        payload = json.loads(output)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["file_restore"])
        self.assertIn("parent is not a directory", payload["reason"])
        self.assertFalse(target.exists())
        self.assertEqual(tree_snapshot(archive), before_archive)

    def test_recover_reuses_assets_when_the_destination_is_lost(self):
        destination, archive = install_and_archive(self.root)
        assets = self.root / "surviving-assets"
        assets.mkdir()
        shutil.copytree(destination / "models", assets / "models")
        shutil.copytree(destination / "runtime", assets / "runtime")
        (self.root / "new-device").mkdir()
        target = self.root / "new-device" / "offgrid"

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(target),
            "--asset-source",
            str(assets),
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["asset_source"], str(assets.resolve()))
        self.assertIn(
            "source previous",
            (target / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (target / "models" / "model.gguf").read_bytes(), b"model bytes"
        )
        self.assertEqual(
            (target / "runtime" / "llama" / "llama-server").read_bytes(),
            b"runtime bytes",
        )

    def test_pre_restore_safety_snapshot_survives_retention_and_status(self):
        bundles = [
            make_bundle(self.root, marker=f"retention-{index}")
            for index in range(5)
        ]
        device = self.root / "device"
        device.mkdir()
        destination = device / "offgrid"
        install_transaction.install_directory(bundles[0], destination)
        install_transaction.install_directory(
            bundles[1], destination, force=True
        )
        archive = install_transaction.recovery_archive_path(destination, 1)
        restored = recovery.restore_recovery_archive(
            archive, destination, confirm=True
        )
        snapshot = Path(restored.safety_snapshot)
        self.assertTrue(snapshot.is_dir())

        for bundle in bundles[2:]:
            install_transaction.install_directory(
                bundle, destination, force=True
            )

        self.assertTrue(snapshot.is_dir())
        code, output = run_cli(
            self.root,
            "--recovery-status",
            str(destination),
            "--confirm",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        entries = {entry["path"]: entry for entry in payload["archives"]}
        self.assertIn(str(snapshot), entries)
        self.assertTrue(entries[str(snapshot)]["valid"])

    def test_recovery_status_lists_retained_generations(self):
        destination, _archive = install_and_archive(self.root)
        for generation in (1, 2):
            recovery.write_recovery_archive(
                destination,
                install_transaction.recovery_archive_path(destination, generation),
                reason="retained generation",
                transaction_id=f"tx-{generation}",
                created="2026-01-02T03:04:05Z",
            )

        code, output = run_cli(
            self.root,
            "--recovery-status",
            str(destination),
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ready"])
        self.assertEqual(
            [entry["generation"] for entry in payload["archives"]], [2, 1]
        )
        self.assertTrue(all(entry["valid"] for entry in payload["archives"]))
        self.assertTrue(all(entry["ready"] for entry in payload["archives"]))
        self.assertEqual(
            payload["latest_valid"],
            str(install_transaction.recovery_archive_path(destination, 2)),
        )

        code, output = run_cli(
            self.root,
            "--recovery-status",
            str(destination),
        )
        self.assertEqual(code, 0, output)
        self.assertIn("generation 2", output)
        self.assertIn("restore-ready: yes", output)

    def test_recovery_status_skips_a_corrupt_newest_generation(self):
        destination, _archive = install_and_archive(self.root)
        for generation in (1, 2):
            recovery.write_recovery_archive(
                destination,
                install_transaction.recovery_archive_path(destination, generation),
                reason="retained generation",
                transaction_id=f"tx-{generation}",
                created="2026-01-02T03:04:05Z",
            )
        newest = install_transaction.recovery_archive_path(destination, 2)
        (newest / "offgrid" / "cli.py").write_text(
            "tampered\n", encoding="utf-8"
        )

        code, output = run_cli(
            self.root,
            "--recovery-status",
            str(destination),
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ready"])
        self.assertEqual(
            payload["latest_valid"],
            str(install_transaction.recovery_archive_path(destination, 1)),
        )
        self.assertFalse(payload["archives"][0]["valid"])
        self.assertTrue(payload["archives"][1]["valid"])

    def test_undo_output_prints_only_the_snapshot_path(self):
        first = make_bundle(self.root, marker="first")
        second = make_bundle(self.root, marker="second")
        device = self.root / "device with spaces;touch-pwned"
        destination = device / "offgrid"
        install_transaction.install_directory(first, destination)
        install_transaction.install_directory(second, destination, force=True)
        archive = install_transaction.recovery_archive_path(destination, 1)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(destination),
            "--confirm",
        )

        self.assertEqual(code, 0, output)
        self.assertIn("undo snapshot: ", output)
        self.assertNotIn("offgrid protect --recover", output)
        self.assertNotIn("undo: offgrid", output)
        undo_line = next(
            line for line in output.splitlines() if "undo snapshot: " in line
        )
        self.assertIn("device with spaces;touch-pwned", undo_line)

    def test_recover_requires_confirmation_for_a_newer_destination(self):
        first = make_bundle(self.root, marker="first")
        second = make_bundle(self.root, marker="second")
        destination = self.root / "device" / "offgrid"
        install_transaction.install_directory(first, destination)
        install_transaction.install_directory(second, destination, force=True)
        archive = install_transaction.recovery_archive_path(destination, 1)
        before = tree_snapshot(destination)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(destination),
            "--json",
        )

        self.assertEqual(code, 1, output)
        self.assertIn("explicit confirmation", output)
        self.assertEqual(tree_snapshot(destination), before)

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(destination),
            "--confirm",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["lineage"], "newer")
        self.assertTrue(payload["confirmation_required"])
        self.assertIn(
            "source first",
            (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )

    def test_recover_latest_restores_the_newest_valid_generation(self):
        first = make_bundle(self.root, marker="first")
        device = self.root / "device"
        device.mkdir()
        destination = device / "offgrid"
        install_transaction.install_directory(first, destination)
        second = make_bundle(self.root, marker="second")
        install_transaction.install_directory(second, destination, force=True)
        third = make_bundle(self.root, marker="third")
        install_transaction.install_directory(third, destination, force=True)

        code, output = run_cli(
            self.root,
            "--recover-latest",
            str(destination),
            "--confirm",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        latest = install_transaction.recovery_archive_path(destination, 2)
        self.assertEqual(payload["archive"], str(latest))
        self.assertIn(
            "source second",
            (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )
        self.assertTrue((destination / "models" / "model.gguf").is_file())
        safety_snapshot = Path(payload["safety_snapshot"])
        self.assertGreater(payload["safety_generation"], 0)
        self.assertEqual(recovery.verify_recovery_archive(safety_snapshot).ok, True)
        self.assertIn(
            "source third",
            (safety_snapshot / "offgrid" / "cli.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertFalse((safety_snapshot / "models").exists())

    def test_recover_latest_skips_a_corrupt_newest_generation(self):
        first = make_bundle(self.root, marker="first")
        device = self.root / "device"
        device.mkdir()
        destination = device / "offgrid"
        install_transaction.install_directory(first, destination)
        second = make_bundle(self.root, marker="second")
        install_transaction.install_directory(second, destination, force=True)
        third = make_bundle(self.root, marker="third")
        install_transaction.install_directory(third, destination, force=True)
        newest = install_transaction.recovery_archive_path(destination, 2)
        (newest / "offgrid" / "cli.py").write_text(
            "tampered\n", encoding="utf-8"
        )

        code, output = run_cli(
            self.root,
            "--recover-latest",
            str(destination),
            "--confirm",
            "--json",
        )

        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(
            payload["archive"],
            str(install_transaction.recovery_archive_path(destination, 1)),
        )
        self.assertIn(
            "source first",
            (destination / "offgrid" / "cli.py").read_text(encoding="utf-8"),
        )

    def test_recovery_status_reports_when_nothing_is_retained(self):
        destination = self.root / "device" / "offgrid"
        destination.parent.mkdir(parents=True)

        code, output = run_cli(
            self.root,
            "--recovery-status",
            str(destination),
            "--json",
        )

        self.assertEqual(code, 1, output)
        payload = json.loads(output)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["archives"], [])
        self.assertEqual(payload["latest_valid"], "")

    def test_recover_refuses_a_symlinked_destination(self):
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlinks are unavailable")
        _, archive = install_and_archive(self.root)
        link = self.root / "recovery-link"
        try:
            link.symlink_to(archive, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        code, output = run_cli(
            self.root,
            "--recover",
            str(archive),
            "--out",
            str(link),
            "--force",
        )

        self.assertEqual(code, 1, output)
        self.assertIn("symlink", output)


if __name__ == "__main__":
    unittest.main()
