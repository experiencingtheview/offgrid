"""The CLI as a user would run it: `python3 -m offgrid ...` against a fake server."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from offgrid.config import Config
from offgrid.memory import MemoryStore
from offgrid.primary import BACKUP_MANIFEST, continuity_kit
from tests.fake_llm import serve, text, tool, when

REPO_ROOT = Path(__file__).resolve().parent.parent

PLAN = {
    "summary": "create hello.py",
    "steps": [
        {
            "id": 1,
            "title": "create hello.py",
            "agent": "coder",
            "task": "Create hello.py",
            "files": ["hello.py"],
            "depends_on": [],
        }
    ],
    "verify": "",
}


def script() -> list:
    return [
        when("planner", text(json.dumps(PLAN))),
        when("coder", tool("write_file", path="hello.py", content="print('hello')\n")),
        when("coder", tool("finish", summary="created hello.py")),
        when("reviewer", text('{"verdict": "approve", "issues": [], "instructions": ""}')),
        when("lead", text("Created hello.py.")),
    ]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, timeout=90):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["NO_COLOR"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "offgrid", *args],
            cwd=str(self.root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_run_json_mode(self):
        model = serve(script())
        self.addCleanup(model.stop)
        proc = self.run_cli(
            "run",
            "create hello.py",
            "--endpoint",
            model.url,
            "--model",
            "fake",
            "--auto",
            "--no-color",
            "--effort",
            "fast",
            "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["changed_files"], ["hello.py"])
        self.assertEqual(payload["review"]["verdict"], "approve")
        self.assertEqual(payload["task"], "create hello.py")
        self.assertTrue((self.root / "hello.py").exists())
        self.assertIn("session", payload)
        # JSON runs never block on a human: lessons come back pending.
        self.assertTrue(payload["lessons"])
        self.assertTrue(payload["pending_lessons"])
        self.assertEqual(payload["pending_lessons"][0]["kind"], "fact")

    def test_run_without_server_fails_cleanly(self):
        proc = self.run_cli(
            "run",
            "do something",
            "--endpoint",
            "http://127.0.0.1:9/v1",
            "--no-color",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot reach", proc.stderr)

    def test_agents_command(self):
        proc = self.run_cli("agents", "--no-color")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("planner", "coder", "reviewer", "tester", "lead"):
            self.assertIn(name, proc.stdout)

    def test_config_command_is_json(self):
        proc = self.run_cli("config", "--no-color")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["command_policy"], "ask")

    def test_offline_flag_reaches_config(self):
        proc = self.run_cli("config", "--offline", "--no-color")
        data = json.loads(proc.stdout)
        self.assertTrue(data["offline"])
        self.assertEqual(data["network"], "deny")

    def test_workspace_config_file_is_honoured(self):
        (self.root / ".offgrid.json").write_text(json.dumps({"command_policy": "auto", "parallel": 3}))
        proc = self.run_cli("config", "--no-color")
        data = json.loads(proc.stdout)
        self.assertEqual(data["command_policy"], "auto")
        self.assertEqual(data["parallel"], 3)

    def test_doctor_lists_the_running_server_and_continuity_checks(self):
        model = serve([])
        self.addCleanup(model.stop)
        proc = self.run_cli("doctor", "--endpoint", model.url, "--no-color")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OffGrid", proc.stdout)
        self.assertIn("continuity-kit", proc.stdout)
        self.assertIn("FAIL", proc.stdout)

    def test_doctor_reports_ready_continuity(self):
        model = serve([])
        self.addCleanup(model.stop)
        (self.root / ".offgrid").mkdir()
        (self.root / ".offgrid" / "continuity.md").write_text(
            continuity_kit(Config(), self.root), encoding="utf-8"
        )
        (self.root / "backups").mkdir()
        backup = self.root / "backups" / "copy.txt"
        backup.write_text("backup\n", encoding="utf-8")
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        (self.root / "backups" / BACKUP_MANIFEST).write_text(
            f"{digest}  copy.txt\n", encoding="utf-8"
        )
        (self.root / "models").mkdir()
        (self.root / "models" / "model.gguf").write_bytes(b"GGUF")
        proc = self.run_cli("doctor", "--endpoint", model.url, "--no-color")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("continuity", proc.stdout)
        self.assertIn("ready for a continuity handoff", proc.stdout)

    def test_missing_workspace_is_rejected(self):
        proc = self.run_cli("run", "x", "--workspace", "nope", "--no-color")
        self.assertNotEqual(proc.returncode, 0)

    def test_memory_review_shows_council_verification_evidence(self):
        store = MemoryStore(self.root / ".offgrid" / "memory.db")
        store.remember(
            'Council answer to "which flag?", later confirmed by verification: use --jinja',
            kind="lesson",
            source="council: peer",
            status="pending",
            verify_command="python3 -m pytest -q",
            verify_output="1 passed in 0.02s",
        )
        store.close()

        proc = self.run_cli(
            "memory", "review", "--workspace", str(self.root), "--no-color"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("council: peer", proc.stdout)
        self.assertIn("verified with `python3 -m pytest -q`", proc.stdout)
        self.assertIn("1 passed in 0.02s", proc.stdout)

    def test_memory_forgive_removes_ledger_without_reactivating(self):
        store = MemoryStore(self.root / ".offgrid" / "memory.db")
        active = store.remember("Keep this accepted fact", kind="fact")
        wrong = store.remember("A wrong lesson", kind="lesson", status="pending")
        store.reject([wrong])
        ledger_id = store.rejected()[0].id
        store.close()

        proc = self.run_cli(
            "memory",
            "forgive",
            str(ledger_id),
            "--workspace",
            str(self.root),
            "--no-color",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("nothing was restored to active memory", proc.stdout)

        reopened = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.rejected(), [])
        self.assertEqual([item.id for item in reopened.all()], [active])
        self.assertEqual(reopened.pending(), [])

    def test_memory_forgive_accepts_exact_text(self):
        store = MemoryStore(self.root / ".offgrid" / "memory.db")
        active = store.remember("Keep this accepted fact", kind="fact")
        text = "Always run pytest before the tests"
        wrong = store.remember(text, kind="lesson", status="pending")
        store.reject([wrong])
        store.close()

        proc = self.run_cli(
            "memory",
            "forgive",
            text,
            "--workspace",
            str(self.root),
            "--no-color",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("nothing was restored to active memory", proc.stdout)

        reopened = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.rejected(), [])
        self.assertEqual([item.id for item in reopened.all()], [active])
        self.assertEqual(reopened.pending(), [])

    def test_memory_forgive_accepts_a_search_query(self):
        store = MemoryStore(self.root / ".offgrid" / "memory.db")
        first = store.remember(
            "Always run pytest before the tests", kind="lesson", status="pending"
        )
        second = store.remember(
            "Backups live in the backups directory", kind="lesson", status="pending"
        )
        store.reject([first, second])
        store.close()

        proc = self.run_cli(
            "memory",
            "forgive",
            "test suite before commit",
            "--workspace",
            str(self.root),
            "--no-color",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("nothing was restored to active memory", proc.stdout)

        reopened = MemoryStore(self.root / ".offgrid" / "memory.db")
        self.addCleanup(reopened.close)
        self.assertEqual(
            [item.text for item in reopened.rejected()],
            ["Backups live in the backups directory"],
        )
        self.assertEqual(reopened.pending(), [])

    def test_memory_forgive_reports_when_nothing_matches(self):
        store = MemoryStore(self.root / ".offgrid" / "memory.db")
        wrong = store.remember(
            "A wrong lesson about deployments", kind="lesson", status="pending"
        )
        store.reject([wrong])
        store.close()

        proc = self.run_cli(
            "memory",
            "forgive",
            "quantum chromodynamics",
            "--workspace",
            str(self.root),
            "--no-color",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no rejection ledger entry matches", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
