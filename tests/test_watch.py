import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.cli import main
from offgrid.netmap import Connection
from offgrid.watch import NOTE, Snapshot, compare, exit_code, load, render, save


def snap(connections, taken=1000, host="test-box"):
    return Snapshot(taken=taken, host=host, source="ss", connections=list(connections))


def listener(local, port, state="LISTEN", process="", pid=0):
    return Connection("tcp", f"{local}:{port}", "0.0.0.0:*", state, process, pid)


class CompareTests(unittest.TestCase):
    def test_new_wildcard_listener_is_high(self):
        changes = compare(snap([]), snap([listener("0.0.0.0", 9000)]))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "new-listener")
        self.assertEqual(changes[0].severity, "high")
        self.assertEqual(exit_code(changes), 2)

    def test_new_loopback_listener_is_informational(self):
        changes = compare(snap([]), snap([listener("127.0.0.1", 8080)]))
        self.assertEqual(changes[0].severity, "info")
        self.assertEqual(exit_code(changes), 0)

    def test_removed_listener_is_reported(self):
        changes = compare(snap([listener("127.0.0.1", 8080)]), snap([]))
        self.assertEqual(changes[0].kind, "removed-listener")

    def test_listener_process_change_is_medium(self):
        old = snap([listener("127.0.0.1", 8080, process="llama-server", pid=1)])
        new = snap([listener("127.0.0.1", 8080, process="unknown-service", pid=2)])
        changes = compare(old, new)
        self.assertEqual(changes[0].kind, "process-changed")
        self.assertEqual(changes[0].severity, "medium")
        self.assertIn("llama-server", changes[0].note)

    def test_new_public_connection_is_reported(self):
        old = snap([])
        new = snap([Connection("tcp", "192.168.1.5:5000", "93.184.216.34:443", "ESTAB")])
        changes = compare(old, new)
        self.assertEqual(changes[0].kind, "new-public-connection")
        self.assertIn("public", changes[0].note)

    def test_no_changes(self):
        same = [listener("127.0.0.1", 8080, process="llama-server", pid=1)]
        self.assertEqual(compare(snap(same), snap(same)), [])


class SnapshotTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_roundtrip_and_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".offgrid" / "watch.json"
            original = snap([listener("127.0.0.1", 8080)])
            save(original, path)
            loaded = load(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.host, "test-box")
            self.assertEqual(loaded.connections[0].local_port, 8080)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bad_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(load(path))

    def test_render_is_honest_about_the_limit(self):
        text = "\n".join(render(snap([]), snap([]), []))
        self.assertIn("no changes", text)
        self.assertIn("cannot detect a warrant", NOTE)
        self.assertIn("cannot detect a warrant", text)


class CliTests(unittest.TestCase):
    def test_first_watch_saves_a_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["security", "watch", "--workspace", tmp, "--json", "--no-color"])
            self.assertEqual(code, 0)
            data = json.loads(output.getvalue())
            self.assertIsNone(data["baseline"])
            self.assertTrue((Path(tmp) / ".offgrid" / "watch.json").is_file())

    def test_second_watch_compares_against_the_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(StringIO()):
                main(["security", "watch", "--workspace", tmp, "--json", "--no-color"])
            output = StringIO()
            with redirect_stdout(output):
                code = main(["security", "watch", "--workspace", tmp, "--json", "--no-color"])
            self.assertIn(code, (0, 2))
            data = json.loads(output.getvalue())
            self.assertIsNotNone(data["baseline"])
            self.assertIn("changes", data)


if __name__ == "__main__":
    unittest.main()
