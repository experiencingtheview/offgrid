"""Crash-point coverage for install transaction recovery.

Each test aborts an in-flight install at one named filesystem or metadata
operation, prevents the normal rollback from running (as an abrupt process
death would), and then exercises the crash recovery path.  The recovered
destination must be either the previous or the newly staged install and must
still pass the real health check.
"""

import os
import tempfile
import unittest
import zipapp
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from offgrid import install_manifest, install_transaction


def make_bundle(root, *, marker):
    bundle = root / "offgrid"
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
    manifest = install_manifest.build_manifest(
        bundle, artifact_kind="full", platform="any"
    )
    install_manifest.write_manifest(bundle, manifest)
    return bundle


def make_pyz(root, *, marker):
    source = root / "pyz-source"
    package = source / "offgrid"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (package / "cli.py").write_text(f"# source {marker}\n", encoding="utf-8")
    (package / "__main__.py").write_text(
        "print('offgrid 0.1.0')\n", encoding="utf-8"
    )
    (source / "__main__.py").write_text(
        "print('offgrid 0.1.0')\n", encoding="utf-8"
    )
    pyz = root / f"{marker}.pyz"
    zipapp.create_archive(source, target=pyz)
    return pyz


def _same_path(left, right):
    return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


class InstallCrashPointTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _case_root(self, name):
        root = self.root / name
        root.mkdir()
        return root

    def _directory_install(self, name):
        root = self._case_root(name)
        old = make_bundle(root / "old", marker="old")
        new = make_bundle(root / "new", marker="new")
        device = root / "device"
        device.mkdir()
        destination = device / old.name
        install_transaction.install_directory(old, destination)
        return new, destination

    def _pyz_install(self, name):
        root = self._case_root(name)
        old = make_pyz(root / "old", marker="old")
        new = make_pyz(root / "new", marker="new")
        device = root / "device"
        device.mkdir()
        destination = device / "offgrid.pyz"
        install_transaction.install_file(old, destination)
        return new, destination, old, new

    def _crash_install(
        self,
        install,
        destination,
        *,
        replace=None,
        state_call=None,
        journal_status=None,
        retention=False,
    ):
        """Run *install* with one injected abrupt crash point.

        The regular rollback path is deliberately disabled by making its
        first journal transition fail, leaving the same artifacts a killed
        process would leave.  Recovery is then run without any injection.
        """
        real_journal = install_transaction._write_journal
        real_state = install_transaction._write_state
        real_prune = install_transaction._prune_after_commit
        real_paths = install_transaction.transaction_paths
        state_count = 0

        def fixed_paths(target, transaction_id=None):
            return real_paths(target, "crashed")

        def journal_writer(paths, payload):
            if payload.get("status") == "rolling_back":
                raise OSError("simulated abrupt process death")
            result = real_journal(paths, payload)
            if journal_status is not None and payload.get("status") == journal_status:
                raise KeyboardInterrupt("simulated crash after journal transition")
            return result

        def state_writer(path, payload):
            nonlocal state_count
            result = real_state(path, payload)
            state_count += 1
            if state_call is not None and state_count == state_call:
                raise KeyboardInterrupt("simulated crash after state write")
            return result

        def prune_writer(target):
            result = real_prune(target)
            if retention:
                raise KeyboardInterrupt("simulated crash after retention pass")
            return result

        real_replace = os.replace
        replace_triggered = False

        def replace_writer(source, target, *args, **kwargs):
            nonlocal replace_triggered
            result = real_replace(source, target, *args, **kwargs)
            if (
                not replace_triggered
                and replace is not None
                and _same_path(source, replace[0])
                and _same_path(target, replace[1])
            ):
                replace_triggered = True
                raise KeyboardInterrupt("simulated crash after rename")
            return result

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    install_transaction, "transaction_paths", new=fixed_paths
                )
            )
            stack.enter_context(
                patch.object(
                    install_transaction, "_write_journal", new=journal_writer
                )
            )
            if state_call is not None:
                stack.enter_context(
                    patch.object(
                        install_transaction, "_write_state", new=state_writer
                    )
                )
            if retention:
                stack.enter_context(
                    patch.object(
                        install_transaction, "_prune_after_commit", new=prune_writer
                    )
                )
            if replace is not None:
                stack.enter_context(
                    patch.object(
                        install_transaction.os, "replace", new=replace_writer
                    )
                )
            with self.assertRaises(install_transaction.InstallError):
                install()

        install_transaction._recover_interrupted(destination)
        paths = install_transaction.transaction_paths(destination)
        self.assertFalse(paths.journal.exists())

    def _assert_directory_runnable(self, destination, *, allowed):
        self.assertTrue(destination.is_dir())
        marker = (destination / "offgrid" / "cli.py").read_text(encoding="utf-8")
        self.assertIn(marker, allowed)
        manifest = install_manifest.load_manifest(
            destination / install_manifest.MANIFEST_NAME
        )
        install_transaction.health_check(destination, manifest)

    def _assert_pyz_runnable(self, destination, *, allowed):
        self.assertTrue(destination.is_file())
        with zipfile.ZipFile(destination) as archive:
            marker = archive.read("offgrid/cli.py").decode("utf-8")
        self.assertIn(marker, allowed)
        paths = install_transaction.transaction_paths(destination)
        manifest = install_manifest.load_manifest(
            paths.state_dir / install_manifest.MANIFEST_NAME
        )
        install_transaction.health_check(destination.parent, manifest)

    def test_directory_crash_after_each_rename(self):
        for label, index in (("previous", 0), ("activation", 1)):
            with self.subTest(rename=label):
                new, destination = self._directory_install(f"rename-{label}")
                paths = install_transaction.transaction_paths(
                    destination, "crashed"
                )
                renames = [
                    (destination, paths.previous),
                    (paths.stage, destination),
                ]
                self._crash_install(
                    lambda: install_transaction.install_directory(
                        new, destination, force=True
                    ),
                    destination,
                    replace=renames[index],
                )
                self._assert_directory_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_directory_crash_after_each_state_write(self):
        for index in (1, 2):
            with self.subTest(state_write=index):
                new, destination = self._directory_install(f"state-{index}")
                self._crash_install(
                    lambda: install_transaction.install_directory(
                        new, destination, force=True
                    ),
                    destination,
                    state_call=index,
                )
                self._assert_directory_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_directory_crash_after_each_journal_transition(self):
        for status in ("prepared", "staged", "swapping", "committed"):
            with self.subTest(journal=status):
                new, destination = self._directory_install(f"journal-{status}")
                self._crash_install(
                    lambda: install_transaction.install_directory(
                        new, destination, force=True
                    ),
                    destination,
                    journal_status=status,
                )
                self._assert_directory_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_directory_crash_after_retention_pass(self):
        new, destination = self._directory_install("retention")
        self._crash_install(
            lambda: install_transaction.install_directory(
                new, destination, force=True
            ),
            destination,
            retention=True,
        )
        self._assert_directory_runnable(
            destination, allowed={"# source new\n"}
        )

    def test_directory_crash_during_quarantine(self):
        for label, index in (("tree", 0), ("activation", 1)):
            with self.subTest(quarantine_step=label):
                new, destination = self._directory_install(f"quarantine-{label}")
                (destination / install_transaction.STATE_NAME).write_text(
                    "{not valid json", encoding="utf-8"
                )
                paths = install_transaction.transaction_paths(
                    destination, "crashed"
                )
                renames = [
                    (destination, paths.quarantine),
                    (paths.stage, destination),
                ]
                self._crash_install(
                    lambda: install_transaction.install_directory(
                        new, destination, force=True
                    ),
                    destination,
                    replace=renames[index],
                )
                self._assert_directory_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_directory_crash_after_quarantine_journal_transition(self):
        new, destination = self._directory_install("quarantine-journal")
        (destination / install_transaction.STATE_NAME).write_text(
            "{not valid json", encoding="utf-8"
        )
        self._crash_install(
            lambda: install_transaction.install_directory(
                new, destination, force=True
            ),
            destination,
            journal_status="quarantining",
        )
        self._assert_directory_runnable(
            destination,
            allowed={"# source old\n", "# source new\n"},
        )

    def test_pyz_crash_after_each_rename(self):
        for label, index in (
            ("state", 0),
            ("previous", 1),
            ("activation", 2),
        ):
            with self.subTest(rename=label):
                new, destination, old, _ = self._pyz_install(f"pyz-rename-{label}")
                paths = install_transaction.transaction_paths(
                    destination, "crashed"
                )
                staged_pyz = paths.stage / "root" / destination.name
                renames = [
                    (paths.state_dir, paths.previous_state_dir),
                    (destination, paths.previous),
                    (staged_pyz, destination),
                ]
                self._crash_install(
                    lambda: install_transaction.install_file(
                        new, destination, force=True
                    ),
                    destination,
                    replace=renames[index],
                )
                self._assert_pyz_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_pyz_crash_after_each_state_write(self):
        for index in (1, 2):
            with self.subTest(state_write=index):
                new, destination, old, _ = self._pyz_install(f"pyz-state-{index}")
                self._crash_install(
                    lambda: install_transaction.install_file(
                        new, destination, force=True
                    ),
                    destination,
                    state_call=index,
                )
                self._assert_pyz_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_pyz_crash_after_each_journal_transition(self):
        for status in ("prepared", "staged", "swapping", "committing", "committed"):
            with self.subTest(journal=status):
                new, destination, old, _ = self._pyz_install(f"pyz-journal-{status}")
                self._crash_install(
                    lambda: install_transaction.install_file(
                        new, destination, force=True
                    ),
                    destination,
                    journal_status=status,
                )
                self._assert_pyz_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_pyz_crash_after_retention_pass(self):
        new, destination, old, _ = self._pyz_install("pyz-retention")
        self._crash_install(
            lambda: install_transaction.install_file(
                new, destination, force=True
            ),
            destination,
            retention=True,
        )
        self._assert_pyz_runnable(
            destination, allowed={"# source new\n"}
        )

    def test_pyz_crash_during_quarantine(self):
        for label, index in (
            ("state", 0),
            ("tree", 1),
            ("activation", 2),
        ):
            with self.subTest(quarantine_step=label):
                new, destination, old, _ = self._pyz_install(
                    f"pyz-quarantine-{label}"
                )
                paths = install_transaction.transaction_paths(destination)
                (paths.state_dir / install_transaction.STATE_NAME).write_text(
                    "{not valid json", encoding="utf-8"
                )
                paths = install_transaction.transaction_paths(
                    destination, "crashed"
                )
                staged_pyz = paths.stage / "root" / destination.name
                renames = [
                    (paths.state_dir, paths.quarantine_state_dir),
                    (destination, paths.quarantine),
                    (staged_pyz, destination),
                ]
                self._crash_install(
                    lambda: install_transaction.install_file(
                        new, destination, force=True
                    ),
                    destination,
                    replace=renames[index],
                )
                self._assert_pyz_runnable(
                    destination,
                    allowed={"# source old\n", "# source new\n"},
                )

    def test_pyz_crash_after_quarantine_journal_transition(self):
        new, destination, old, _ = self._pyz_install("pyz-quarantine-journal")
        paths = install_transaction.transaction_paths(destination)
        (paths.state_dir / install_transaction.STATE_NAME).write_text(
            "{not valid json", encoding="utf-8"
        )
        self._crash_install(
            lambda: install_transaction.install_file(
                new, destination, force=True
            ),
            destination,
            journal_status="quarantining",
        )
        self._assert_pyz_runnable(
            destination,
            allowed={"# source old\n", "# source new\n"},
        )
