import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.cli import main


class LexiconCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["lexicon", *args, "--workspace", str(self.root), "--no-color"])
        return code, output.getvalue()

    def test_builtin_lexicon_is_shown_when_the_project_file_is_missing(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("merged meaning lexicon", output)
        self.assertIn("verify:", output)
        self.assertIn("not found; built-in groups only", output)
        self.assertIn("warnings: none", output)

    def test_project_file_merges_groups_and_reports_its_source(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"groups": {"billing": ["invoice", "receipt"]}}),
            encoding="utf-8",
        )

        code, output = self.run_cli()

        self.assertEqual(code, 0)
        self.assertIn(f"source file: {path}", output)
        self.assertIn("billing: invoice, receipt", output)
        self.assertIn("verify:", output)

    def test_conflicts_are_reported_as_warnings(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"groups": {"billing": ["ship"]}}), encoding="utf-8")

        code, output = self.run_cli()

        self.assertEqual(code, 0)
        self.assertIn("already belongs to 'deploy'", output)
        self.assertIn("not moved to 'billing'", output)

    def test_strict_passes_a_clean_project_file(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"groups": {"billing": ["invoice", "receipt"]}}),
            encoding="utf-8",
        )

        code, output = self.run_cli("--strict")

        self.assertEqual(code, 0)
        self.assertIn("warnings: none", output)

    def test_strict_passes_when_there_is_no_project_file(self):
        code, output = self.run_cli("--strict")
        self.assertEqual(code, 0)
        self.assertIn("not found; built-in groups only", output)

    def test_strict_fails_on_a_conflict(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"groups": {"billing": ["ship"]}}), encoding="utf-8")

        code, output = self.run_cli("--strict")

        self.assertEqual(code, 1)
        self.assertIn("already belongs to 'deploy'", output)

    def test_strict_fails_on_a_malformed_project_file(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        code, output = self.run_cli("--strict")

        self.assertEqual(code, 1)
        self.assertIn("could not read", output)

    def test_strict_json_still_reports_the_payload_before_failing(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"groups": {"billing": ["ship"]}}), encoding="utf-8")

        code, output = self.run_cli("--json", "--strict")

        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["warnings"])
        self.assertIn("already belongs to 'deploy'", " ".join(payload["warnings"]))

    def test_json_output_contains_the_source_groups_and_warnings(self):
        path = self.root / ".offgrid" / "lexicon.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"groups": {"billing": ["invoice"]}}), encoding="utf-8")

        code, output = self.run_cli("--json")

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["source"], str(path))
        self.assertTrue(payload["found"])
        self.assertEqual(payload["groups"]["billing"], ["invoice"])
        self.assertEqual(payload["warnings"], [])


if __name__ == "__main__":
    unittest.main()
