"""Security review watch: interval reviews that report only newly added findings."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.cli import main


class SecurityReviewWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / "app.py"

    def tearDown(self):
        self.tmp.cleanup()

    def watch(self, *extra, review=True):
        args = ["security", "watch"]
        if review:
            args.append("--review")
        args.extend(
            [
                "--once",
                "--workspace",
                str(self.root),
                "--json",
                "--no-color",
                *extra,
            ]
        )
        output = StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_first_run_reviews_the_workspace_and_saves_a_baseline(self):
        self.app.write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore

        code, data = self.watch()

        self.assertEqual(code, 0)
        self.assertIsNone(data["baseline"])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["check"], "secret")
        self.assertEqual(data["reviewed"], 1)
        self.assertTrue((self.root / ".offgrid" / "security.jsonl").is_file())

    def test_later_run_reports_only_findings_new_since_the_last_review(self):
        self.app.write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
        self.watch()
        with self.app.open("a", encoding="utf-8") as handle:
            handle.write('password = "qrstuvwxyz123456"\n')  # audit:ignore

        code, data = self.watch()

        self.assertEqual(code, 0)
        self.assertIsNotNone(data["baseline"])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["line"], 2)
        self.assertEqual(data["reviewed"], 2)

    def test_unchanged_workspace_reports_no_new_findings(self):
        self.app.write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore
        self.watch()

        code, data = self.watch()

        self.assertEqual(code, 0)
        self.assertEqual(data["findings"], [])
        self.assertEqual(data["counts"]["total"], 0)
        self.assertEqual(data["reviewed"], 1)

    def test_interval_flag_selects_the_workspace_review_mode(self):
        self.app.write_text('token = "abcdefghijklmnop"\n', encoding="utf-8")  # audit:ignore

        code, data = self.watch("--interval", "60", review=False)

        self.assertEqual(code, 0)
        self.assertIsNone(data["baseline"])
        self.assertEqual(len(data["findings"]), 1)


if __name__ == "__main__":
    unittest.main()
