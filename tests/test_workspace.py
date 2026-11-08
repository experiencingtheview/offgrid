"""Workspace confinement and change tracking."""

import tempfile
import unittest
from pathlib import Path

from offgrid.workspace import Workspace, WorkspaceError


class ConfinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path_inside_root(self):
        resolved = self.ws.resolve("src/app.py")
        self.assertTrue(str(resolved).startswith(str(self.root.resolve())))

    def test_parent_escape_is_refused(self):
        with self.assertRaises(WorkspaceError):
            self.ws.resolve("../secrets.txt")

    def test_absolute_escape_is_refused(self):
        with self.assertRaises(WorkspaceError):
            self.ws.resolve("/etc/passwd")

    def test_escape_allowed_when_explicitly_permitted(self):
        ws = Workspace(str(self.root), allow_outside=True)
        self.assertTrue(str(ws.resolve("../outside.txt")))

    def test_empty_path_refused(self):
        with self.assertRaises(WorkspaceError):
            self.ws.resolve("")

    def test_write_and_read_roundtrip(self):
        note = self.ws.write_text("pkg/mod.py", "x = 1\n")
        self.assertIn("created pkg/mod.py", note)
        self.assertEqual(self.ws.read_text("pkg/mod.py"), "x = 1\n")

    def test_overwrite_reports_updated(self):
        self.ws.write_text("a.txt", "one")
        note = self.ws.write_text("a.txt", "two")
        self.assertIn("updated a.txt", note)

    def test_read_lines_numbers_and_total(self):
        self.ws.write_text("big.txt", "\n".join(f"line{i}" for i in range(1, 21)))
        text, total = self.ws.read_lines("big.txt", offset=5, limit=3)
        self.assertEqual(total, 20)
        self.assertIn("5| line5", text)
        self.assertIn("7| line7", text)
        self.assertNotIn("line8", text)

    def test_binary_file_refused(self):
        (self.root / "img.bin").write_bytes(b"\x00\x01\x02")
        with self.assertRaises(WorkspaceError):
            self.ws.read_text("img.bin")

    def test_missing_file_message(self):
        with self.assertRaises(WorkspaceError) as ctx:
            self.ws.read_text("nope.py")
        self.assertIn("no such file", str(ctx.exception))


class ChangeTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_diff_shows_added_lines(self):
        self.ws.write_text("app.py", "a = 1\n")
        self.ws.write_text("app.py", "a = 1\nb = 2\n")
        diff = self.ws.diff()
        self.assertIn("+b = 2", diff)
        self.assertIn("b/app.py", diff)

    def test_new_file_tracked(self):
        self.ws.write_text("new.py", "print('hi')\n")
        self.assertEqual(self.ws.changed_files(), ["new.py"])
        self.assertIn("+print('hi')", self.ws.diff())

    def test_unchanged_content_produces_no_diff(self):
        (self.root / "same.py").write_text("value = 1\n")
        self.ws.write_text("same.py", "value = 1\n")  # records original, rewrites identical bytes
        self.assertEqual(self.ws.diff(), "")

    def test_delete_is_tracked(self):
        self.ws.write_text("gone.py", "x\n")
        self.ws.write_text("gone.py", "x\n")  # ensure recorded
        note = self.ws.delete("gone.py")
        self.assertIn("deleted gone.py", note)
        self.assertIn("gone.py", self.ws.changed_files())

    def test_snapshot_returns_current_content(self):
        self.ws.write_text("s.py", "x = 2\n")
        snapshot = self.ws.snapshot()
        self.assertEqual(snapshot["s.py"], "x = 2\n")


class WalkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_tree_skips_junk_directories(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print(1)\n")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "lib.js").write_text("//x\n")
        (self.root / "keep.txt").write_text("hi\n")
        tree = self.ws.tree()
        self.assertIn("src/", tree)
        self.assertIn("main.py", tree)
        self.assertIn("keep.txt", tree)
        self.assertNotIn("node_modules", tree)

    def test_iter_files_ignores_binaries(self):
        (self.root / "a.py").write_text("x\n")
        (self.root / "b.png").write_bytes(b"\x89PNG")
        names = [path.name for path in self.ws.iter_files()]
        self.assertIn("a.py", names)
        self.assertNotIn("b.png", names)


if __name__ == "__main__":
    unittest.main()
