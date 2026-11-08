"""Tool behaviour, including the safety rails."""

import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.tools import ToolBox
from offgrid.workspace import Workspace


def make(tmp: Path, **overrides) -> ToolBox:
    config = Config(**{"command_policy": "auto", "color": False, **overrides})
    return ToolBox(Workspace(str(tmp)), config)


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tools = make(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_read(self):
        result = self.tools.write_file("src/util.py", "def add(a, b):\n    return a + b\n")
        self.assertTrue(result.ok)
        read = self.tools.read_file("src/util.py")
        self.assertTrue(read.ok)
        self.assertIn("1| def add", read.content)
        self.assertIn("lines 1-2 of 2", read.content)

    def test_read_missing_file_is_an_error_not_a_crash(self):
        result = self.tools.call("read_file", {"path": "nope.py"})
        self.assertFalse(result.ok)
        self.assertIn("no such file", result.content)

    def test_edit_replaces_unique_snippet(self):
        self.tools.write_file("a.py", "x = 1\ny = 2\n")
        result = self.tools.edit_file("a.py", "y = 2", "y = 3")
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "a.py").read_text(), "x = 1\ny = 3\n")

    def test_edit_missing_snippet_gives_guidance(self):
        self.tools.write_file("a.py", "x = 1\n")
        result = self.tools.edit_file("a.py", "z = 9", "z = 10")
        self.assertFalse(result.ok)
        self.assertIn("could not find", result.content)

    def test_edit_ambiguous_snippet_refused(self):
        self.tools.write_file("a.py", "same\nsame\n")
        result = self.tools.edit_file("a.py", "same", "other")
        self.assertFalse(result.ok)
        self.assertIn("appears 2 times", result.content)

    def test_edit_replace_all(self):
        self.tools.write_file("a.py", "same\nsame\n")
        result = self.tools.edit_file("a.py", "same", "other", replace_all=True)
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "a.py").read_text(), "other\nother\n")

    def test_search_finds_matches(self):
        self.tools.write_file("src/app.py", "import os\n\ndef run():\n    pass\n")
        self.tools.write_file("src/other.js", "function run() {}\n")
        result = self.tools.search_files(r"def run", glob="*.py")
        self.assertTrue(result.ok)
        self.assertIn("src/app.py:3", result.content)
        self.assertNotIn("other.js", result.content)

    def test_search_no_matches_is_ok(self):
        self.tools.write_file("a.py", "x = 1\n")
        result = self.tools.search_files("zzz")
        self.assertTrue(result.ok)
        self.assertIn("no matches", result.content)

    def test_list_files_shows_tree(self):
        self.tools.write_file("pkg/deep/file.py", "x\n")
        result = self.tools.list_files(".")
        self.assertTrue(result.ok)
        self.assertIn("pkg/", result.content)

    def test_escape_attempt_is_blocked(self):
        result = self.tools.call("write_file", {"path": "../outside.txt", "content": "nope"})
        self.assertFalse(result.ok)
        self.assertIn("outside the workspace", result.content)

    def test_finish_sets_control_state(self):
        self.assertFalse(self.tools.finished)
        result = self.tools.finish(summary="all done")
        self.assertTrue(result.ok)
        self.assertTrue(self.tools.finished)
        self.assertEqual(self.tools.summary, "all done")

    def test_unknown_tool(self):
        result = self.tools.call("teleport", {})
        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.content)

    def test_bad_arguments_do_not_raise(self):
        result = self.tools.call("read_file", {"wrong": 1})
        self.assertFalse(result.ok)
        self.assertIn("bad arguments", result.content)


class CommandPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_command_runs(self):
        tools = make(self.root)
        result = tools.run_command("echo hello")
        self.assertTrue(result.ok)
        self.assertIn("hello", result.content)
        self.assertIn("exit 0", result.content)

    def test_nonzero_exit_is_reported(self):
        tools = make(self.root)
        result = tools.run_command("exit 7")
        self.assertFalse(result.ok)
        self.assertIn("exit 7", result.content)

    def test_forbidden_command_always_refused(self):
        tools = make(self.root)
        result = tools.run_command("rm -rf /")
        self.assertFalse(result.ok)
        self.assertIn("refused", result.content)

    def test_mkfs_refused(self):
        tools = make(self.root)
        result = tools.run_command("mkfs.ext4 /dev/sda1")
        self.assertFalse(result.ok)

    def test_destructive_needs_approval_even_in_auto_mode(self):
        approvals = []
        tools = make(self.root, command_policy="auto")
        tools.approve = lambda command: approvals.append(command) or False
        result = tools.run_command("rm -rf build")
        self.assertFalse(result.ok)
        self.assertIn("declined", result.content)
        self.assertEqual(approvals, ["rm -rf build"])

    def test_destructive_allowed_when_human_agrees(self):
        (self.root / "build").mkdir()
        (self.root / "build" / "junk.txt").write_text("x")
        tools = make(self.root)
        tools.approve = lambda command: True
        result = tools.run_command("rm -rf build")
        self.assertTrue(result.ok)
        self.assertFalse((self.root / "build").exists())

    def test_deny_policy_blocks_shell_entirely(self):
        tools = make(self.root, command_policy="deny")
        result = tools.run_command("echo hi")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.content)

    def test_offline_mode_blocks_package_installs(self):
        tools = make(self.root, network="deny")
        for command in ("pip install requests", "npm install left-pad", "curl https://example.com"):
            result = tools.run_command(command)
            self.assertFalse(result.ok, command)
            self.assertIn("offline", result.content.lower(), command)

    def test_offline_mode_allows_local_installs(self):
        tools = make(self.root, network="deny")
        result = tools.run_command("python3 -c 'print(1)'")
        self.assertTrue(result.ok)

    def test_cwd_cannot_escape_workspace(self):
        tools = make(self.root)
        result = tools.run_command("pwd", cwd="..")
        self.assertFalse(result.ok)
        self.assertIn("outside the workspace", result.content)

    def test_cwd_subdirectory_is_used(self):
        (self.root / "sub").mkdir()
        tools = make(self.root)
        result = tools.run_command("pwd", cwd="sub")
        self.assertTrue(result.ok)
        self.assertIn("sub", result.content)


if __name__ == "__main__":
    unittest.main()
