"""Mechanical gates: catch truncated, stubbed and broken output without a model."""

import tempfile
import unittest
from pathlib import Path

from offgrid.quality import check_text, check_workspace
from offgrid.workspace import Workspace


class CheckTextTests(unittest.TestCase):
    def test_clean_file_has_no_issues(self):
        self.assertEqual(check_text("a.py", "def add(a, b):\n    return a + b\n"), [])

    def test_placeholder_comment_is_flagged(self):
        issues = check_text("a.py", "def add(a, b):\n    # rest of the code here\n")
        self.assertTrue(any("placeholder comment" in issue for issue in issues))

    def test_bare_ellipsis_is_flagged(self):
        issues = check_text("a.py", "def add(a, b):\n    ...\n")
        self.assertTrue(any("ellipsis" in issue for issue in issues))

    def test_todo_is_flagged(self):
        issues = check_text("a.py", "x = 1  # TODO finish this\n")
        self.assertTrue(any("TODO" in issue for issue in issues))

    def test_unimplemented_error_is_flagged(self):
        issues = check_text("a.py", "def f():\n    raise NotImplementedError\n")
        self.assertTrue(any("unimplemented" in issue for issue in issues))

    def test_truncated_python_is_a_syntax_error(self):
        issues = check_text("a.py", "def broken(:\n    pass\n")
        self.assertTrue(any("syntax error" in issue for issue in issues))

    def test_invalid_json_is_flagged(self):
        issues = check_text("data.json", '{"a": 1,}')
        self.assertTrue(any("invalid JSON" in issue for issue in issues))

    def test_valid_json_passes(self):
        self.assertEqual(check_text("data.json", '{"a": 1}'), [])

    def test_non_python_is_not_syntax_checked_but_still_scanned(self):
        self.assertEqual(check_text("app.js", "function f() { return 1 }\n"), [])
        issues = check_text("app.js", "// rest of the file\n")
        self.assertTrue(issues)

    def test_legit_ellipsis_in_a_slice_is_not_flagged(self):
        self.assertEqual(check_text("a.py", "x = data[start:...]\n"), [])


class CheckWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_changed_files_are_checked(self):
        (self.root / "already.py").write_text("def f(:\n")  # pre-existing breakage
        self.ws.write_text("new.py", "value = 1\n")
        self.assertEqual(check_workspace(self.ws), [])

    def test_changed_file_with_stub_is_reported(self):
        self.ws.write_text("new.py", "def f():\n    # TODO implement\n")
        issues = check_workspace(self.ws)
        self.assertTrue(any("new.py" in issue for issue in issues))

    def test_deleted_file_is_skipped(self):
        self.ws.write_text("gone.py", "x = 1\n")
        self.ws.delete("gone.py")
        self.assertEqual(check_workspace(self.ws), [])


if __name__ == "__main__":
    unittest.main()
