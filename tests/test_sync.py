"""Learning sync: explicit, token-protected, and quarantined before use."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from offgrid.cli import main
from offgrid.config import Config
from offgrid.memory import MemoryStore
from offgrid.sync import (
    INFO_PATH,
    LEARNING_PATH,
    SyncClient,
    SyncError,
    SyncServer,
    configured_peers,
    import_bundle,
    read_export,
    recent_events,
    scheduled_once,
    validate_peer_url,
    write_export,
)


def get_json(url, token=""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_json(url, payload, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class PeerUrlTests(unittest.TestCase):
    def test_loopback_http_is_allowed_for_local_tests(self):
        self.assertEqual(
            validate_peer_url("http://127.0.0.1:8778/"), "http://127.0.0.1:8778"
        )

    def test_public_http_is_not_allowed_by_default(self):
        with self.assertRaises(SyncError):
            validate_peer_url("http://example.com")

    def test_public_https_is_allowed(self):
        self.assertEqual(
            validate_peer_url("https://offgrid.example"), "https://offgrid.example"
        )

    def test_explicit_http_override_is_honest_about_the_risk(self):
        self.assertEqual(
            validate_peer_url("http://example.com", allow_http=True),
            "http://example.com",
        )

    def test_credentials_in_a_url_are_refused(self):
        with self.assertRaises(SyncError):
            validate_peer_url("https://user:secret@example.com")

    def test_peer_lists_are_deduplicated(self):
        config = Config(workspace="/tmp", sync_peers=("https://b.example",))
        self.assertEqual(
            configured_peers(config, ["https://a.example", "https://b.example/"]),
            ["https://a.example", "https://b.example"],
        )


class CliTests(unittest.TestCase):
    def test_status_export_and_import_work_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            store = MemoryStore(root / ".offgrid" / "memory.db")
            store.remember("The CLI exports accepted learning notes.", kind="fact")
            store.close()

            output = StringIO()
            with redirect_stdout(output):
                code = main(["sync", "status", "--workspace", tmp, "--json", "--no-color"])
            self.assertEqual(code, 0, output.getvalue())
            status = json.loads(output.getvalue())
            self.assertEqual(status["accepted"], 1)
            self.assertEqual(status["pending"], 0)

            target = root / "learning.json"
            with redirect_stdout(StringIO()):
                code = main(
                    ["sync", "export", str(target), "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())

            with redirect_stdout(StringIO()):
                code = main(
                    ["sync", "import", str(target), "--workspace", other, "--no-color"]
                )
            self.assertEqual(code, 0)
            imported = MemoryStore(Path(other) / ".offgrid" / "memory.db")
            self.addCleanup(imported.close)
            self.assertEqual(imported.all(), [])
            self.assertEqual(len(imported.pending()), 1)

    def test_status_reports_managed_peer_file_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".offgrid.json"
            path.write_text(
                json.dumps(
                    {
                        "sync_peers": [
                            "https://desk.example",
                            "http://insecure.example",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def status(*extra):
                output = StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    code = main(
                        [
                            "sync",
                            "status",
                            "--workspace",
                            tmp,
                            *extra,
                            "--json",
                            "--no-color",
                        ]
                    )
                self.assertEqual(code, 0, output.getvalue())
                return json.loads(output.getvalue())

            payload = status()
            self.assertEqual(payload["peer_config"], str(path))
            self.assertEqual(
                payload["managed_peers"],
                ["https://desk.example", "http://insecure.example"],
            )
            checks = {item["peer"]: item for item in payload["peer_validation"]}
            self.assertTrue(checks["https://desk.example"]["valid"])
            self.assertFalse(checks["http://insecure.example"]["valid"])
            self.assertIn(
                "refusing plain HTTP", checks["http://insecure.example"]["error"]
            )

            allowed = status("--allow-http")
            allowed_checks = {
                item["peer"]: item for item in allowed["peer_validation"]
            }
            self.assertTrue(allowed_checks["http://insecure.example"]["valid"])

            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(["sync", "status", "--workspace", tmp, "--no-color"])
            self.assertEqual(code, 0, output.getvalue())
            text = output.getvalue()
            self.assertIn(f"peer file {path}", text)
            self.assertIn("peer https://desk.example  valid", text)
            self.assertIn("peer http://insecure.example  INVALID", text)
            self.assertIn("refusing plain HTTP", text)

    def test_status_reports_an_unreadable_peer_config_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".offgrid.json"
            path.write_text("{not json\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    ["sync", "status", "--workspace", tmp, "--json", "--no-color"]
                )
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["peer_config"], str(path))
            self.assertEqual(payload["managed_peers"], [])
            self.assertIn("not valid JSON", payload["peer_config_error"])

    def test_status_strict_fails_when_a_configured_peer_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".offgrid.json").write_text(
                json.dumps(
                    {
                        "sync_peers": [
                            "https://desk.example",
                            "http://insecure.example",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def status(*extra):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "sync",
                            "status",
                            "--workspace",
                            tmp,
                            *extra,
                            "--json",
                            "--no-color",
                        ]
                    )
                return code, json.loads(stdout.getvalue()), stderr.getvalue()

            code, payload, stderr = status()
            self.assertEqual(code, 0, stderr)
            self.assertFalse(payload["strict"])
            self.assertFalse(payload["strict_failed"])

            code, payload, stderr = status("--strict")
            self.assertEqual(code, 1, stderr)
            self.assertTrue(payload["strict"])
            self.assertTrue(payload["strict_failed"])
            checks = {item["peer"]: item for item in payload["peer_validation"]}
            self.assertFalse(checks["http://insecure.example"]["valid"])

            code, payload, stderr = status("--strict", "--allow-http")
            self.assertEqual(code, 0, stderr)
            self.assertFalse(payload["strict_failed"])

            # A --peer override must not hide an invalid managed peer.
            code, payload, stderr = status(
                "--strict", "--peer", "https://safe.example"
            )
            self.assertEqual(code, 1, stderr)
            checks = {item["peer"]: item for item in payload["peer_validation"]}
            self.assertIn("http://insecure.example", checks)
            self.assertFalse(checks["http://insecure.example"]["valid"])

            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    ["sync", "status", "--strict", "--workspace", tmp, "--no-color"]
                )
            self.assertEqual(code, 1)
            self.assertIn("strict mode: invalid peer(s)", output.getvalue())

    def test_status_strict_fails_closed_on_an_unreadable_peer_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".offgrid.json"
            path.write_text("{not json\n", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "sync",
                        "status",
                        "--strict",
                        "--workspace",
                        tmp,
                        "--json",
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["strict_failed"])
            self.assertIn("not valid JSON", payload["peer_config_error"])

    def test_schedule_once_requires_network_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = main(
                    [
                        "sync",
                        "schedule",
                        "--once",
                        "--offline",
                        "--workspace",
                        tmp,
                        "--no-color",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("networking", output.getvalue())


class PeerManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / ".offgrid.json"
        self.path.write_text(
            json.dumps(
                {
                    "keep": {"enabled": True},
                    "sync_peers": ["https://old.example"],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(
                [
                    "sync",
                    "peers",
                    *args,
                    "--workspace",
                    str(self.root),
                    "--json",
                    "--no-color",
                ]
            )
        text = output.getvalue()
        payload = json.loads(text) if text.strip().startswith("{") else None
        return code, text, payload

    def test_add_list_and_remove_preserve_unrelated_config(self):
        code, text, payload = self.cli("add", "https://desk.example/")
        self.assertEqual(code, 0, text)
        self.assertTrue(payload["added"])
        self.assertEqual(payload["peer"], "https://desk.example")

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["keep"], {"enabled": True})
        self.assertEqual(
            data["sync_peers"], ["https://old.example", "https://desk.example"]
        )

        code, text, payload = self.cli("list")
        self.assertEqual(code, 0, text)
        self.assertEqual(
            payload["peers"], ["https://old.example", "https://desk.example"]
        )
        self.assertEqual(payload["config"], str(self.path))

        code, text, payload = self.cli("add", "https://desk.example")
        self.assertEqual(code, 0, text)
        self.assertFalse(payload["added"])

        code, text, payload = self.cli("remove", "https://desk.example/")
        self.assertEqual(code, 0, text)
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["peers"], ["https://old.example"])
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["sync_peers"],
            ["https://old.example"],
        )

    def test_add_several_peers_in_one_command_preserves_config(self):
        code, text, payload = self.cli(
            "add",
            "https://desk.example/",
            "https://laptop.example",
            "https://desk.example",
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(
            payload["requested"], ["https://desk.example", "https://laptop.example"]
        )
        self.assertTrue(payload["added"])
        self.assertEqual(
            payload["added_peers"],
            ["https://desk.example", "https://laptop.example"],
        )
        self.assertEqual(payload["already_configured"], [])

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["keep"], {"enabled": True})
        self.assertEqual(
            data["sync_peers"],
            ["https://old.example", "https://desk.example", "https://laptop.example"],
        )

        code, text, payload = self.cli(
            "add",
            "https://old.example",
            "https://desk.example",
            "https://new.example",
        )
        self.assertEqual(code, 0, text)
        self.assertTrue(payload["added"])
        self.assertEqual(payload["added_peers"], ["https://new.example"])
        self.assertEqual(
            payload["already_configured"],
            ["https://old.example", "https://desk.example"],
        )
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["sync_peers"],
            [
                "https://old.example",
                "https://desk.example",
                "https://laptop.example",
                "https://new.example",
            ],
        )

    def test_add_several_peers_rejects_every_url_before_writing(self):
        before = self.path.read_text(encoding="utf-8")
        code, text, payload = self.cli(
            "add", "https://good.example", "http://example.com"
        )
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("refusing plain HTTP", text)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_add_rejects_public_http_without_rewriting_config(self):
        before = self.path.read_text(encoding="utf-8")
        code, text, payload = self.cli("add", "http://example.com")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("refusing plain HTTP", text)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_remove_several_peers_in_one_command_preserves_config(self):
        self.path.write_text(
            json.dumps(
                {
                    "keep": {"enabled": True},
                    "sync_peers": [
                        "https://old.example",
                        "https://desk.example",
                        "https://laptop.example",
                    ],
                }
            ),
            encoding="utf-8",
        )

        code, text, payload = self.cli(
            "remove",
            "https://desk.example/",
            "https://old.example",
            "https://desk.example",
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(
            payload["requested"],
            ["https://desk.example", "https://old.example"],
        )
        self.assertTrue(payload["removed"])
        self.assertEqual(
            payload["removed_peers"],
            ["https://desk.example", "https://old.example"],
        )
        self.assertEqual(payload["missing"], [])
        self.assertEqual(payload["peers"], ["https://laptop.example"])

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["keep"], {"enabled": True})
        self.assertEqual(data["sync_peers"], ["https://laptop.example"])

        code, text, payload = self.cli(
            "remove", "https://laptop.example", "https://missing.example"
        )
        self.assertEqual(code, 1, text)
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["removed_peers"], ["https://laptop.example"])
        self.assertEqual(payload["missing"], ["https://missing.example"])
        self.assertEqual(payload["peers"], [])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["keep"], {"enabled": True})
        self.assertEqual(data["sync_peers"], [])

    def test_remove_several_peers_validates_every_url_before_writing(self):
        before = self.path.read_text(encoding="utf-8")
        code, text, payload = self.cli("remove", "https://old.example", "   ")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("peer URL is required", text)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_remove_missing_peer_returns_failure(self):
        code, text, payload = self.cli("remove", "https://missing.example")
        self.assertEqual(code, 1, text)
        self.assertFalse(payload["removed"])
        self.assertEqual(payload["peers"], ["https://old.example"])

    def test_malformed_config_is_not_overwritten(self):
        self.path.write_text("{not json\n", encoding="utf-8")
        code, text, payload = self.cli("add", "https://desk.example")
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("not valid JSON", text)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json\n")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / ".offgrid" / "memory.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_remote_lessons_are_pending_not_used(self):
        counts = self.store.import_sync(
            [
                {
                    "kind": "lesson",
                    "text": "Use the staging verification command before release.",
                    "confidence": 0.8,
                }
            ],
            source="sync:desk",
        )
        self.assertEqual(counts["new"], 1)
        self.assertEqual(self.store.all(), [])
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source, "sync:desk")
        self.assertIn("staging verification", pending[0].text)

    def test_duplicate_remote_lessons_do_not_pile_up(self):
        entry = {"kind": "fact", "text": "The release script lives in scripts/release.sh."}
        first = import_bundle(
            self.store, {"offgrid": 1, "version": 1, "lessons": [entry]}
        )
        second = import_bundle(
            self.store, {"offgrid": 1, "version": 1, "lessons": [entry]}
        )
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.store.pending()), 1)

    def test_a_rejected_lesson_does_not_return_from_sync(self):
        text = "Always run the old release branch."
        ident = self.store.remember(text, kind="lesson", status="pending")
        self.store.reject([ident])
        counts = import_bundle(
            self.store, {"offgrid": 1, "version": 1, "lessons": [{"text": text}]}
        )
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.all(), [])

    def test_export_only_contains_accepted_notes(self):
        self.store.remember("Accepted note about the build command", kind="repo")
        self.store.remember(
            "Pending note that must never leave unaccepted", kind="lesson", status="pending"
        )
        exported = self.store.export_sync()
        self.assertEqual([item["text"] for item in exported], ["Accepted note about the build command"])
        self.assertIn("key", exported[0])

    def test_export_import_round_trip_is_pending(self):
        self.store.remember("The local build uses make test.", kind="repo")
        target = self.root / "learning.json"
        write_export(self.store, target, source="desk")
        bundle = read_export(target)
        self.assertEqual(len(bundle["lessons"]), 1)

        other = MemoryStore(self.root / "other" / "memory.db")
        self.addCleanup(other.close)
        counts = import_bundle(other, bundle, source="file:learning.json")
        self.assertEqual(counts["new"], 1)
        self.assertEqual(other.all(), [])
        self.assertEqual(len(other.pending()), 1)

    def test_bad_bundles_are_refused(self):
        with self.assertRaises(SyncError):
            import_bundle(self.store, {"offgrid": 2, "lessons": []})
        with self.assertRaises(SyncError):
            import_bundle(self.store, {"offgrid": 1, "lessons": "not a list"})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_store = MemoryStore(self.root / "server" / "memory.db")
        self.server_store.remember(
            "The verified build command is make test.", kind="repo", confidence=0.9
        )
        self.config = Config(
            workspace=str(self.root / "server"),
            sync_port=0,
            sync_timeout=5,
            color=False,
            verbose=0,
        )
        self.server = SyncServer(
            self.config, self.server_store, token="shared-secret", port=0, name="desk"
        )
        self.port = self.server.start(host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        self.server_store.close()
        self.tmp.cleanup()

    def test_info_is_public_but_does_not_leak_the_token(self):
        status, info = get_json(self.base + INFO_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(info["name"], "desk")
        self.assertEqual(info["accepted"], 1)
        self.assertNotIn("shared-secret", json.dumps(info))

    def test_learning_requires_the_shared_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            get_json(self.base + LEARNING_PATH)
        self.assertEqual(caught.exception.code, 401)

    def test_authorised_get_returns_accepted_notes(self):
        status, bundle = get_json(self.base + LEARNING_PATH, token="shared-secret")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["offgrid"], 1)
        self.assertEqual(len(bundle["lessons"]), 1)
        self.assertIn("make test", bundle["lessons"][0]["text"])

    def test_push_quarantines_a_peer_lesson(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other_store = MemoryStore(other_root / "memory.db")
        self.addCleanup(other_store.close)
        other_store.remember("A peer lesson about deployment checks.", kind="lesson")
        other_config = Config(workspace=str(other_root), sync_timeout=5)
        client = SyncClient(other_config, "shared-secret")
        result = client.push(self.base, other_store)
        self.assertEqual(result["counts"]["new"], 1)
        self.assertEqual(self.server_store.stats()["pending"], 1)
        pending = self.server_store.pending()
        self.assertTrue(any("deployment checks" in item.text for item in pending))
        self.assertTrue(all(item.status == "pending" for item in pending))

    def test_wrong_token_is_rejected_for_writes(self):
        status, body = post_json(
            self.base + LEARNING_PATH,
            {"offgrid": 1, "lessons": [{"text": "A peer lesson that should not land."}]},
            token="wrong",
        )
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_pull_imports_peer_notes_as_pending(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other_store = MemoryStore(other_root / "memory.db")
        self.addCleanup(other_store.close)
        other_config = Config(workspace=str(other_root), sync_timeout=5)
        client = SyncClient(other_config, "shared-secret")
        result = client.pull(self.base, other_store)
        self.assertEqual(result["counts"]["new"], 1)
        self.assertEqual(other_store.all(), [])
        self.assertEqual(len(other_store.pending()), 1)

    def test_scheduled_sync_refuses_without_network_permission(self):
        root = self.root / "blocked"
        root.mkdir()
        store = MemoryStore(root / ".offgrid" / "memory.db")
        self.addCleanup(store.close)
        config = Config(
            workspace=str(root),
            network="deny",
            sync_peers=("https://peer.example",),
        )
        with self.assertRaises(SyncError) as caught:
            scheduled_once(config, store, configured_peers(config))
        self.assertIn("networking", str(caught.exception))
        events = recent_events(config)
        self.assertEqual([event["action"] for event in events], ["schedule"])
        self.assertIn("networking", events[0]["error"])
        self.assertFalse((root / ".offgrid" / "sync-token").exists())

    def test_scheduled_sync_logs_each_exchange(self):
        root = self.root / "scheduled"
        root.mkdir()
        store = MemoryStore(root / ".offgrid" / "memory.db")
        self.addCleanup(store.close)
        store.remember("A scheduled client lesson.", kind="lesson")
        config = Config(
            workspace=str(root),
            network="allow",
            sync_timeout=5,
            sync_peers=(self.base,),
        )
        results = scheduled_once(
            config, store, configured_peers(config), token="shared-secret"
        )
        self.assertEqual(results[0]["push"]["new"], 1)
        self.assertEqual(results[0]["pull"]["new"], 1)
        events = recent_events(config)
        self.assertEqual([event["action"] for event in events], ["push", "pull", "schedule"])
        self.assertEqual(events[0]["counts"]["new"], 1)
        self.assertEqual(events[1]["counts"]["new"], 1)
        self.assertEqual(events[2]["counts"], {"peers": 1, "failed": 0})


if __name__ == "__main__":
    unittest.main()
