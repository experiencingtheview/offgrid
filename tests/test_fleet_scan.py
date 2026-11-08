"""Fleet scan: discover peers, ask before registering, never require a manual add."""

import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from offgrid import cli
from offgrid.config import Config
from offgrid.fleet import Fleet, FleetError, Peer, peer_from_discovery


class FakeTTY:
    def __init__(self, interactive=True):
        self.interactive = interactive

    def isatty(self):
        return self.interactive


class FleetScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.entry = {
            "offgrid": 1,
            "name": "desk",
            "host": "192.168.1.20",
            "port": 8776,
            "tier": "capable",
            "ram_gb": 16,
            "vram_gb": 0,
            "accelerators": ["cpu"],
            "models": ["qwen2.5-coder"],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, token="shared", yes=False, json_output=False, action="scan"):
        return SimpleNamespace(
            action=action,
            terms=[],
            workspace=str(self.root),
            token=token,
            yes=yes,
            json=json_output,
            port=0,
            color=False,
            verbose=2,
            no_color=True,
        )

    def peers(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        return Fleet(config).load().peers

    def run_scan(
        self,
        *,
        token="shared",
        yes=False,
        interactive=True,
        answer="",
        token_input="shared",
        json_output=False,
        entries=None,
        ping_ok=True,
    ):
        output = StringIO()
        discovery = [dict(self.entry)] if entries is None else entries
        with (
            patch.object(Fleet, "discover", return_value=discovery),
            patch.object(Fleet, "ping", return_value=ping_ok),
            patch.object(cli.sys, "stdin", FakeTTY(interactive)),
            patch("builtins.input", return_value=answer),
            patch("getpass.getpass", return_value=token_input),
            redirect_stdout(output),
        ):
            code = cli.cmd_fleet(
                self.args(token=token, yes=yes, json_output=json_output)
            )
        return code, output.getvalue()

    def test_scan_confirms_then_registers(self):
        code, output = self.run_scan()
        self.assertEqual(code, 0)
        peers = self.peers()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].name, "desk")
        self.assertEqual(peers[0].url, "http://192.168.1.20:8776")
        self.assertEqual(peers[0].token, "shared")
        self.assertEqual(peers[0].tier, "capable")
        self.assertIn("found: desk", output)

    def test_scan_skips_when_the_user_declines(self):
        code, _ = self.run_scan(answer="n")
        self.assertEqual(code, 1)
        self.assertEqual(self.peers(), [])

    def test_scan_does_not_hang_without_a_terminal(self):
        code, output = self.run_scan(interactive=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.peers(), [])
        self.assertIn("no terminal for confirmation", output)

    def test_yes_registers_without_a_prompt(self):
        code, _ = self.run_scan(interactive=False, yes=True)
        self.assertEqual(code, 0)
        self.assertEqual([peer.name for peer in self.peers()], ["desk"])

    def test_a_known_url_is_not_registered_twice(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        Fleet(config).load().add("desk", "http://192.168.1.20:8776", token="shared")
        code, output = self.run_scan()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.peers()), 1)
        self.assertIn("already registered", output)

    def test_a_name_collision_gets_a_suffix_instead_of_overwriting(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        Fleet(config).load().add("desk", "http://192.168.1.99:8776", token="shared")
        code, _ = self.run_scan(yes=True)
        self.assertEqual(code, 0)
        self.assertEqual({peer.name for peer in self.peers()}, {"desk", "desk-2"})

    def test_scan_prompts_for_the_token_without_echo(self):
        code, _ = self.run_scan(token="", token_input="shared")
        self.assertEqual(code, 0)
        self.assertEqual(self.peers()[0].token, "shared")

    def test_json_scan_reports_discovery_and_registration(self):
        code, output = self.run_scan(yes=True, json_output=True)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "scan")
        self.assertEqual(payload["counts"]["discovered"], 1)
        self.assertEqual(payload["counts"]["registered"], 1)
        self.assertEqual(payload["counts"]["known"], 0)
        self.assertEqual(payload["counts"]["skipped"], 0)
        peer = payload["peers"][0]
        self.assertEqual(peer["name"], "desk")
        self.assertEqual(peer["url"], "http://192.168.1.20:8776")
        self.assertEqual(peer["outcome"], "registered")
        self.assertTrue(peer["reachable"])
        self.assertNotIn("token", peer)
        self.assertNotIn("shared", output)  # the shared token never appears

    def test_json_scan_without_yes_reports_confirmation_required(self):
        code, output = self.run_scan(json_output=True)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["skipped"], 1)
        self.assertEqual(payload["peers"][0]["outcome"], "skipped")
        self.assertIn("--yes", payload["peers"][0]["reason"])
        self.assertEqual(self.peers(), [])

    def test_json_scan_without_a_token_reports_token_required(self):
        code, output = self.run_scan(token="", yes=True, json_output=True)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["skipped"], 1)
        self.assertEqual(payload["peers"][0]["outcome"], "skipped")
        self.assertIn("--token", payload["peers"][0]["reason"])
        self.assertEqual(self.peers(), [])

    def test_json_scan_marks_already_registered_peers(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        Fleet(config).load().add("desk", "http://192.168.1.20:8776", token="shared")
        code, output = self.run_scan(json_output=True)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["known"], 1)
        self.assertEqual(payload["peers"][0]["outcome"], "already_registered")
        self.assertEqual(payload["peers"][0]["name"], "desk")

    def test_json_scan_reports_a_failed_health_check(self):
        code, output = self.run_scan(yes=True, json_output=True, ping_ok=False)
        self.assertEqual(code, 0)
        peer = json.loads(output)["peers"][0]
        self.assertEqual(peer["outcome"], "registered")
        self.assertFalse(peer["reachable"])
        self.assertTrue(peer["reason"])

    def test_json_scan_reports_malformed_replies(self):
        code, output = self.run_scan(entries=[{"name": "ghost"}], json_output=True)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["skipped"], 1)
        self.assertEqual(payload["peers"][0]["name"], "ghost")
        self.assertEqual(payload["peers"][0]["outcome"], "error")
        self.assertIn("address", payload["peers"][0]["reason"])

    def test_json_scan_with_no_peers_is_still_valid_json(self):
        code, output = self.run_scan(entries=[], json_output=True)
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["counts"]["discovered"], 0)
        self.assertEqual(payload["peers"], [])

    def test_json_list_reuses_the_scan_peer_schema(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        fleet = Fleet(config).load()
        peer = fleet.add("desk", "http://192.168.1.20:8776", token="shared")
        peer.tier = "capable"
        peer.ram_gb = 16.0
        peer.vram_gb = 8.0
        peer.accelerators = ("cuda",)
        peer.models = ("qwen2.5-coder",)
        peer.last_seen = time.time()
        peer.latency_ms = 12.5
        fleet.save()

        output = StringIO()
        with redirect_stdout(output):
            code = cli.cmd_fleet(self.args(action="list", json_output=True))

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["action"], "list")
        self.assertEqual(payload["counts"], {"peers": 1})
        item = payload["peers"][0]
        self.assertEqual(
            set(item),
            {
                "name",
                "url",
                "tier",
                "ram_gb",
                "vram_gb",
                "accelerators",
                "models",
                "outcome",
                "reason",
                "reachable",
                "latency_ms",
                "last_error",
            },
        )
        self.assertEqual(item["outcome"], "known")
        self.assertTrue(item["reachable"])
        self.assertEqual(item["latency_ms"], 12.5)
        self.assertNotIn("token", item)
        self.assertNotIn("shared", output.getvalue())

    def test_json_ping_reports_reachable_and_unreachable_peers(self):
        config = Config(workspace=str(self.root), color=False, verbose=0)
        fleet = Fleet(config).load()
        fleet.add("desk", "http://192.168.1.20:8776", token="shared")
        fleet.add("ghost", "http://192.168.1.21:8776", token="shared")
        alive = Peer(
            name="desk",
            url="http://192.168.1.20:8776",
            token="shared",
            tier="capable",
            ram_gb=16.0,
            last_seen=time.time(),
            latency_ms=8.5,
        )
        dead = Peer(
            name="ghost",
            url="http://192.168.1.21:8776",
            token="shared",
            last_error="unreachable (timed out)",
        )

        output = StringIO()
        with patch.object(Fleet, "ping_all", return_value=[alive, dead]), redirect_stdout(output):
            code = cli.cmd_fleet(self.args(action="ping", json_output=True))

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["action"], "ping")
        self.assertEqual(payload["counts"], {"peers": 2, "reachable": 1})
        self.assertEqual(
            [item["outcome"] for item in payload["peers"]],
            ["reachable", "unreachable"],
        )
        self.assertTrue(payload["peers"][0]["reachable"])
        self.assertEqual(payload["peers"][0]["latency_ms"], 8.5)
        self.assertFalse(payload["peers"][1]["reachable"])
        self.assertEqual(payload["peers"][1]["reason"], "unreachable (timed out)")
        self.assertNotIn("token", payload["peers"][0])
        self.assertNotIn("shared", output.getvalue())

    def test_json_ping_with_no_peers_is_valid_json(self):
        output = StringIO()
        with redirect_stdout(output):
            code = cli.cmd_fleet(self.args(action="ping", json_output=True))

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "action": "ping",
                "counts": {"peers": 0, "reachable": 0},
                "peers": [],
            },
        )

    def test_discovery_reply_maps_to_a_peer(self):
        peer = peer_from_discovery(self.entry, token="t")
        self.assertEqual(peer.name, "desk")
        self.assertEqual(peer.url, "http://192.168.1.20:8776")
        self.assertEqual(peer.token, "t")
        self.assertEqual(peer.tier, "capable")
        self.assertEqual(peer.ram_gb, 16.0)
        self.assertEqual(peer.models, ("qwen2.5-coder",))

    def test_discovery_reply_without_an_address_is_refused(self):
        with self.assertRaises(FleetError):
            peer_from_discovery({"name": "desk"}, token="t")


if __name__ == "__main__":
    unittest.main()
