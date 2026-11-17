"""Recovery archive retention tests.

The pruning API is exercised directly here; install transactions apply it
best-effort after commit so cleanup failures cannot roll back a healthy tree.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from offgrid import install_transaction


class RecoveryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def destination(self, name):
        destination = self.root / name
        destination.mkdir()
        return destination

    def archive(self, destination, generation):
        path = install_transaction.recovery_archive_path(destination, generation)
        path.mkdir(parents=True)
        (path / "recovery.json").write_text(
            json.dumps({"generation": generation}), encoding="utf-8"
        )
        return path

    def legacy_archive(self, destination):
        path = install_transaction.transaction_paths(destination).recovery
        path.mkdir(parents=True)
        (path / "recovery.json").write_text("{}", encoding="utf-8")
        return path

    def write_state(self, destination, recovery_bundle):
        state = {
            "schema": install_transaction.STATE_SCHEMA,
            "status": "committed",
            "recovery_bundle": str(recovery_bundle),
        }
        (destination / install_transaction.STATE_NAME).write_text(
            json.dumps(state), encoding="utf-8"
        )

    def archive_paths(self, destination):
        paths = install_transaction.transaction_paths(destination)
        if not paths.root.is_dir():
            return []
        return sorted(
            path
            for path in paths.root.iterdir()
            if path.is_dir()
            and (
                path.name == f"{paths.key}.recovery"
                or path.name.startswith(f"{paths.key}.recovery-")
            )
        )

    def test_prunes_to_the_newest_three_archives(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(
            report.kept,
            (archives[4], archives[3], archives[2]),
        )
        self.assertEqual(set(report.pruned), {archives[0], archives[1]})
        self.assertEqual(
            set(self.archive_paths(destination)),
            {archives[2], archives[3], archives[4]},
        )

    def test_retention_is_per_destination(self):
        device_a = self.destination("device-a")
        device_b = self.destination("device-b")
        archives_a = [self.archive(device_a, generation) for generation in range(1, 5)]
        archives_b = [self.archive(device_b, generation) for generation in range(1, 5)]

        report = install_transaction.prune_recovery_archives(device_a)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(len(self.archive_paths(device_a)), 3)
        self.assertEqual(
            set(self.archive_paths(device_b)),
            set(archives_b),
        )
        self.assertTrue(archives_a[3].is_dir())

    def test_committed_snapshot_is_protected_even_when_it_is_oldest(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]
        self.write_state(destination, archives[0])

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertIn(archives[0], report.kept)
        self.assertIn(archives[0], report.protected)
        self.assertEqual(len(report.kept), 3)
        self.assertTrue(archives[0].is_dir())
        self.assertEqual(
            set(self.archive_paths(destination)),
            {archives[0], archives[3], archives[4]},
        )

    def test_unreadable_install_state_fails_closed(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]
        (destination / install_transaction.STATE_NAME).write_text(
            "{not json", encoding="utf-8"
        )

        report = install_transaction.prune_recovery_archives(destination)

        self.assertFalse(report.ok)
        self.assertEqual(report.pruned, ())
        self.assertEqual(set(report.kept), set(archives))
        self.assertTrue(
            any("install state" in error for error in report.errors)
        )
        self.assertEqual(set(self.archive_paths(destination)), set(archives))

    def test_state_pointer_to_an_unknown_archive_fails_closed(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]
        self.write_state(destination, self.root / "not-an-archive")

        report = install_transaction.prune_recovery_archives(destination)

        self.assertFalse(report.ok)
        self.assertEqual(report.pruned, ())
        self.assertEqual(set(report.kept), set(archives))
        self.assertTrue(
            any("does not name a known archive" in error for error in report.errors)
        )
        self.assertEqual(set(self.archive_paths(destination)), set(archives))

    def test_unreadable_journal_fails_closed(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]
        paths = install_transaction.transaction_paths(destination)
        paths.journal.write_text("{not json", encoding="utf-8")

        report = install_transaction.prune_recovery_archives(destination)

        self.assertFalse(report.ok)
        self.assertEqual(report.pruned, ())
        self.assertEqual(set(report.kept), set(archives))
        self.assertTrue(any("journal" in error for error in report.errors))
        self.assertEqual(set(self.archive_paths(destination)), set(archives))

    def test_live_journal_snapshot_is_protected(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 5)]
        paths = install_transaction.transaction_paths(destination)
        paths.journal.write_text(
            json.dumps(
                {
                    "schema": install_transaction.JOURNAL_SCHEMA,
                    "status": "committed",
                    "recovery_bundle": str(archives[0]),
                }
            ),
            encoding="utf-8",
        )

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertIn(archives[0], report.protected)
        self.assertTrue(archives[0].is_dir())
        self.assertEqual(len(report.kept), 3)

    def test_legacy_fixed_snapshot_is_protected_as_generation_zero(self):
        destination = self.destination("device")
        legacy = self.legacy_archive(destination)
        archives = [self.archive(destination, generation) for generation in range(1, 5)]
        self.write_state(destination, legacy)

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(set(report.pruned), {archives[0], archives[1]})
        self.assertIn(legacy, report.protected)
        self.assertEqual(
            set(self.archive_paths(destination)),
            {legacy, archives[2], archives[3]},
        )

    def test_pyz_sidecar_state_protects_its_snapshot(self):
        destination = self.root / "offgrid.pyz"
        destination.write_bytes(b"pyz bytes")
        paths = install_transaction.transaction_paths(destination)
        paths.state_dir.mkdir(parents=True)
        archives = [self.archive(destination, generation) for generation in range(1, 5)]
        self.write_state(paths.state_dir, archives[0])

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertIn(archives[0], report.protected)
        self.assertTrue(archives[0].is_dir())
        self.assertEqual(len(report.kept), 3)

    def test_crash_leftovers_are_ignored_and_not_counted(self):
        destination = self.destination("device")
        paths = install_transaction.transaction_paths(destination)
        archives = [self.archive(destination, generation) for generation in range(1, 4)]
        candidate = paths.root / f"{paths.key}.recovery-00000009.candidate-crashed"
        candidate.mkdir()
        partial = paths.root / f".{paths.key}.recovery-00000010.tmp"
        partial.write_text("partial", encoding="utf-8")
        file_archive = paths.root / f"{paths.key}.recovery-00000011"
        file_archive.write_text("not a completed archive", encoding="utf-8")

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.pruned, ())
        self.assertEqual(set(report.kept), set(archives))
        self.assertTrue(candidate.is_dir())
        self.assertTrue(partial.is_file())
        self.assertTrue(file_archive.is_file())

    def test_delete_failure_is_reported_instead_of_raised(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]

        def fail(_path):
            raise OSError("archive is busy")

        with mock.patch.object(install_transaction, "_remove_path", side_effect=fail):
            report = install_transaction.prune_recovery_archives(destination)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 2)
        self.assertEqual(set(report.kept), {archives[2], archives[3], archives[4]})
        self.assertEqual(report.pruned, ())
        self.assertTrue(all(archive.is_dir() for archive in archives))

    def test_missing_transaction_root_is_a_noop(self):
        destination = self.root / "never-installed"
        destination.mkdir()

        report = install_transaction.prune_recovery_archives(destination)

        self.assertTrue(report.ok)
        self.assertEqual(report.kept, ())
        self.assertEqual(report.pruned, ())
        self.assertFalse(install_transaction.transaction_paths(destination).root.exists())

    def test_keep_must_be_positive(self):
        destination = self.destination("device")
        self.archive(destination, 1)

        with self.assertRaises(ValueError):
            install_transaction.prune_recovery_archives(destination, keep=0)

    def test_allocate_recovery_archive_skips_gaps_and_selects_numeric_path(self):
        destination = self.destination("device")
        self.legacy_archive(destination)
        self.archive(destination, 2)
        self.archive(destination, 5)

        generation, archive = install_transaction.allocate_recovery_archive(
            destination
        )

        self.assertEqual(generation, 6)
        self.assertEqual(
            archive,
            install_transaction.recovery_archive_path(destination, generation),
        )
        self.assertEqual(
            archive.name,
            f"{install_transaction.transaction_paths(destination).key}.recovery-00000006",
        )
        self.assertFalse(archive.exists())

    def test_allocate_recovery_archive_explicit_generation_selects_that_path(self):
        destination = self.destination("device")

        generation, archive = install_transaction.allocate_recovery_archive(
            destination, generation=4
        )

        self.assertEqual(generation, 4)
        self.assertEqual(
            archive,
            install_transaction.recovery_archive_path(destination, 4),
        )

    def test_allocated_archive_is_discovered_by_retention(self):
        destination = self.destination("device")
        generation, archive = install_transaction.allocate_recovery_archive(
            destination
        )
        archive.mkdir(parents=True)
        (archive / "recovery.json").write_text("{}", encoding="utf-8")

        report = install_transaction.prune_recovery_archives(destination)

        self.assertEqual(generation, 1)
        self.assertEqual(report.kept, (archive,))
        self.assertEqual(report.pruned, ())

    def test_record_install_prunes_after_the_committed_state(self):
        destination = self.destination("device")
        archives = [self.archive(destination, generation) for generation in range(1, 6)]

        state = install_transaction.record_install(destination, {})

        self.assertEqual(state["status"], "committed")
        self.assertEqual(
            set(self.archive_paths(destination)),
            {archives[2], archives[3], archives[4]},
        )

    def test_record_install_retention_failure_does_not_fail_the_commit(self):
        destination = self.destination("device")
        archive = self.archive(destination, 1)

        with mock.patch.object(
            install_transaction,
            "prune_recovery_archives",
            side_effect=RuntimeError("injected retention failure"),
        ):
            state = install_transaction.record_install(destination, {})

        self.assertEqual(state["status"], "committed")
        self.assertEqual(
            json.loads(
                (destination / install_transaction.STATE_NAME).read_text(
                    encoding="utf-8"
                )
            )["status"],
            "committed",
        )
        self.assertTrue(archive.is_dir())


class RecoveryRetentionImportsTests(unittest.TestCase):
    """Keep every install commit path wired to the shared retention helper."""

    def test_install_entrypoints_apply_retention(self):
        import inspect

        for entrypoint in (
            install_transaction.install_directory,
            install_transaction.install_file,
            install_transaction.record_install,
        ):
            self.assertIn("_prune_after_commit", inspect.getsource(entrypoint))


if __name__ == "__main__":
    unittest.main()
