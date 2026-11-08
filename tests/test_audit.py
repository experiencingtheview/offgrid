import os
import stat
import tempfile
import unittest
from pathlib import Path

from offgrid.audit import (
    counts,
    history,
    mask,
    record,
    review_workspace,
    scan_text,
    verdict,
)


class SecretScanTests(unittest.TestCase):
    def test_aws_key_is_critical_and_masked(self):
        findings = scan_text("app.py", 'key = "AKIAIOSFODNN7EXAMPLE"\n')  # audit:ignore
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "critical")
        self.assertEqual(findings[0].line, 1)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", findings[0].evidence)  # audit:ignore
        self.assertIn("AKIA", findings[0].evidence)

    def test_generic_credential_is_high(self):
        findings = scan_text("settings.py", 'API_KEY = "correct-horse-battery-staple"\n')  # audit:ignore
        self.assertTrue(findings)
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(findings[0].check, "secret")

    def test_placeholder_and_short_values_are_not_flagged(self):
        self.assertEqual(scan_text("a.py", 'api_key = ""\n'), [])
        self.assertEqual(scan_text("a.py", 'password = "short"\n'), [])

    def test_ignore_marker_is_respected(self):
        line = 'api_key = "correct-horse-battery-staple"  # audit:ignore\n'
        self.assertEqual(scan_text("fixture.py", line), [])

    def test_url_password(self):
        findings = scan_text("conf.py", 'url = "https://bob:hunter2hunter2@example.test/x"\n')  # audit:ignore
        self.assertTrue(any(item.check == "secret" for item in findings))

    def test_mask_never_reveals_a_usable_secret(self):
        self.assertEqual(mask("abcdefghijklmnop"), "abcd...op")
        self.assertEqual(mask("abc"), "***")


class WorkspaceReviewTests(unittest.TestCase):
    def test_review_finds_secret_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
            findings = review_workspace(root)
            self.assertTrue(any(item.check == "secret" and item.path == "app.py" for item in findings))
            self.assertEqual(verdict(findings), "high")
            self.assertEqual(counts(findings)["total"], len(findings))

    def test_changed_files_limit_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
            (root / "good.py").write_text("print('hello')\n", encoding="utf-8")
            findings = review_workspace(root, changed=["good.py"])
            self.assertFalse(any(item.check == "secret" for item in findings))

    def test_gitignore_must_cover_state_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            findings = review_workspace(root)
            checks = [item for item in findings if item.check == "gitignore"]
            self.assertEqual(len(checks), 2)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_world_writable_memory_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".offgrid"
            state.mkdir()
            database = state / "memory.db"
            database.write_text("x", encoding="utf-8")
            os.chmod(database, 0o666)
            findings = review_workspace(root)
            self.assertTrue(any(item.check == "permissions" and item.severity == "medium" for item in findings))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_fleet_token_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".offgrid"
            state.mkdir()
            token = state / "fleet-token"
            token.write_text("secret", encoding="utf-8")
            os.chmod(token, 0o644)
            findings = review_workspace(root)
            self.assertTrue(any(item.check == "permissions" and item.severity == "high" for item in findings))
            os.chmod(token, 0o600)
            findings = review_workspace(root)
            self.assertFalse(any(item.check == "permissions" for item in findings))

    def test_posture_notes_public_council(self):
        class FakeConfig:
            endpoint = "http://127.0.0.1:8080/v1"
            allow_remote = False
            allow_public_council = True
            fleet = False

        with tempfile.TemporaryDirectory() as tmp:
            findings = review_workspace(Path(tmp), config=FakeConfig())
            self.assertTrue(any(item.check == "privacy" for item in findings))


class HistoryTests(unittest.TestCase):
    def test_record_and_read_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "security.jsonl"
            findings = scan_text("a.py", 'key = "AKIAIOSFODNN7EXAMPLE"\n')  # audit:ignore
            record(path, findings, source="test")
            record(path, [], source="test")
            entries = history(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["counts"]["critical"], 1)
            self.assertEqual(entries[1]["verdict"], "clean")


if __name__ == "__main__":
    unittest.main()
