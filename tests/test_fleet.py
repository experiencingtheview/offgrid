"""Peers on the LAN donate compute; the report says where the calls really ran."""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from offgrid.config import Config
from offgrid.fleet import Fleet, FleetError, FleetProvider, FleetServer, Peer, is_lan_url
from offgrid.models import Message
from offgrid.providers import LocalProvider
from tests.fake_llm import serve, text


def get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url, payload, token=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class LanAddressTests(unittest.TestCase):
    def test_private_and_named_hosts_are_peers(self):
        self.assertTrue(is_lan_url("http://192.168.1.20:8776"))
        self.assertTrue(is_lan_url("http://10.0.0.5:8776"))
        self.assertTrue(is_lan_url("http://127.0.0.1:8776"))
        self.assertTrue(is_lan_url("http://desk.local:8776"))

    def test_public_addresses_are_refused(self):
        self.assertFalse(is_lan_url("http://8.8.8.8:8776"))
        self.assertFalse(is_lan_url("https://example.com"))
        self.assertFalse(is_lan_url("not a url"))


class PeerTests(unittest.TestCase):
    def test_a_public_peer_is_rejected_outright(self):
        with self.assertRaises(FleetError):
            Peer(name="cloud", url="https://example.com")

    def test_stronger_machines_rank_first(self):
        weak = Peer(name="phone", url="http://192.168.1.5:8776", tier="minimal", ram_gb=4)
        strong = Peer(name="desk", url="http://192.168.1.6:8776", tier="workstation", ram_gb=64, vram_gb=24)
        self.assertGreater(strong.score, weak.score)

    def test_a_peer_round_trips_through_json(self):
        peer = Peer(name="desk", url="http://192.168.1.6:8776", token="t", tier="capable")
        again = Peer.from_dict(peer.as_dict())
        self.assertEqual(again.name, "desk")
        self.assertEqual(again.token, "t")
        self.assertEqual(again.tier, "capable")


class FleetRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(workspace=str(self.root), color=False, verbose=0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_peers_persist_between_runs(self):
        fleet = Fleet(self.config).load()
        fleet.add("desk", "http://192.168.1.6:8776", token="shared")
        again = Fleet(self.config).load()
        self.assertEqual([peer.name for peer in again.peers], ["desk"])

    def test_removing_a_peer(self):
        fleet = Fleet(self.config).load()
        fleet.add("desk", "http://192.168.1.6:8776")
        self.assertTrue(fleet.remove("desk"))
        self.assertFalse(fleet.remove("desk"))
        self.assertEqual(Fleet(self.config).load().peers, [])

    def test_the_registry_file_is_private(self):
        if os.name != "posix":
            self.skipTest("posix permissions")
        fleet = Fleet(self.config).load()
        fleet.add("desk", "http://192.168.1.6:8776", token="shared")
        self.assertEqual(os.stat(self.config.workspace + "/" + self.config.fleet_file).st_mode & 0o777, 0o600)

    def test_empty_fleet_says_so_instead_of_pretending(self):
        self.assertIn("no peers configured", Fleet(self.config).describe()[0])


class FleetServerTests(unittest.TestCase):
    """`offgrid fleet serve`: donate this machine's model to the LAN, behind a token."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = serve([], default=text("answered by the peer"))
        self.config = Config(
            endpoint=self.model.url,
            model="peer-model",
            workspace=str(self.root),
            command_policy="auto",
            color=False,
            verbose=0,
        )
        self.provider = LocalProvider(self.config)
        self.server = FleetServer(self.config, self.provider, token="secret", port=0, name="desk")
        self.port = self.server.start(host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        self.model.stop()
        self.tmp.cleanup()

    def test_info_is_visible_without_a_token_and_leaks_nothing(self):
        info = get_json(self.base + "/fleet/info")
        self.assertEqual(info["offgrid"], 1)
        self.assertEqual(info["name"], "desk")
        self.assertEqual(info["model"], "peer-model")
        self.assertNotIn("secret", json.dumps(info))

    def test_inference_requires_the_shared_token(self):
        status, body = post_json(
            self.base + "/fleet/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_an_authorised_peer_gets_the_local_model(self):
        status, body = post_json(
            self.base + "/fleet/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hi"}]},
            token="secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "answered by the peer")
        self.assertEqual(self.server.served, 1)
        self.assertEqual(self.model.requests[-1]["model"], "peer-model")

    def test_unknown_paths_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            get_json(self.base + "/nope")
        self.assertEqual(caught.exception.code, 404)


class FleetProviderTests(unittest.TestCase):
    """Requests route to the strongest peer and fall back home when it vanishes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.peer_model = serve([], default=text("answered by the peer"))
        self.peer_config = Config(
            endpoint=self.peer_model.url,
            model="peer-model",
            workspace=str(self.root / "peer"),
            color=False,
            verbose=0,
        )
        (self.root / "peer").mkdir(parents=True, exist_ok=True)
        self.server = FleetServer(
            self.peer_config, LocalProvider(self.peer_config), token="shared", port=0, name="desk"
        )
        self.peer_port = self.server.start(host="127.0.0.1")

        self.local_model = serve([], default=text("answered here"))
        self.config = Config(
            endpoint=self.local_model.url,
            model="local-model",
            workspace=str(self.root),
            color=False,
            verbose=0,
            fleet=True,
            fleet_timeout=10,
        )
        self.fleet = Fleet(self.config).load()
        self.fleet.add("desk", f"http://127.0.0.1:{self.peer_port}", token="shared")
        self.provider = FleetProvider(LocalProvider(self.config), self.fleet, self.config)

    def tearDown(self):
        self.server.stop()
        self.peer_model.stop()
        self.local_model.stop()
        self.tmp.cleanup()

    def test_a_call_runs_on_the_peer_when_it_answers(self):
        completion = self.provider.chat([Message("user", "hello")])
        self.assertEqual(completion.text, "answered by the peer")
        self.assertEqual(self.provider.remote_calls, 1)
        self.assertEqual(self.local_model.requests, [])
        self.assertIn("desk", self.provider.summary())
        self.assertIn("1 on desk", self.provider.summary())

    def test_a_dead_peer_falls_back_to_this_machine_and_says_so(self):
        self.server.stop()
        completion = self.provider.chat([Message("user", "hello")])
        self.assertEqual(completion.text, "answered here")
        self.assertEqual(self.provider.remote_calls, 0)
        self.assertIn("no peer answered", self.provider.summary())
        self.assertEqual(self.provider.routes, {"local": 1})

    def test_health_still_reports_the_local_model(self):
        ok, message = self.provider.health()
        self.assertTrue(ok)
        self.assertIn("local-model", message)


if __name__ == "__main__":
    unittest.main()
