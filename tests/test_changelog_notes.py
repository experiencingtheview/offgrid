"""The release notes are the CHANGELOG section for the version being tagged."""

import importlib.util
import unittest
from pathlib import Path

from offgrid import __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "changelog_notes.py"
CHANGELOG = ROOT / "CHANGELOG.md"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SAMPLE = (
    "# Changelog\n"
    "\n"
    "Intro text that belongs to no section.\n"
    "\n"
    "## [1.2.0] - 2026-01-02\n"
    "\n"
    "### Added\n"
    "\n"
    "- the newer thing\n"
    "\n"
    "## [1.1.0] - 2026-01-01\n"
    "\n"
    "- the older thing\n"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("changelog_notes", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChangelogNotesTests(unittest.TestCase):
    def setUp(self):
        self.notes = _load_script()
        self.text = CHANGELOG.read_text(encoding="utf-8")

    def test_the_notes_are_that_version_section(self):
        body = self.notes.notes_for(self.text, f"v{__version__}")
        self.assertIn("### Added", body)
        self.assertIn("offgrid-portable.zip", body)
        self.assertNotIn("## [", body)
        self.assertNotIn("# Changelog", body)

    def test_the_tag_may_be_given_without_the_v(self):
        self.assertEqual(
            self.notes.notes_for(SAMPLE, "v1.2.0"),
            self.notes.notes_for(SAMPLE, "1.2.0"),
        )

    def test_the_body_stops_at_the_next_heading(self):
        self.assertEqual(
            self.notes.notes_for(SAMPLE, "v1.2.0"),
            "### Added\n\n- the newer thing",
        )
        self.assertEqual(self.notes.notes_for(SAMPLE, "v1.1.0"), "- the older thing")

    def test_a_missing_section_is_refused(self):
        with self.assertRaises(LookupError) as caught:
            self.notes.notes_for(SAMPLE, "v9.9.9")
        self.assertIn("no section for 9.9.9", str(caught.exception))
        self.assertEqual(self.notes.main(["v9.9.9"]), 1)

    def test_the_workflow_publishes_that_text(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('changelog_notes.py "$TAG" > dist/notes.md', workflow)
        self.assertIn('gh release create', workflow)
        self.assertIn("--notes-file dist/notes.md", workflow)
        self.assertIn('gh release edit "$TAG" --notes-file dist/notes.md', workflow)
        # The old wording lived in the workflow; notes now come from CHANGELOG.md.
        self.assertNotIn('--notes "Portable download.', workflow)


if __name__ == "__main__":
    unittest.main()
