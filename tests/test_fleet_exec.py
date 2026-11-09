"""Remote fleet commands are opt-in on both sides, authenticated and confined."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from offgrid.agents import ALL_TOOLS
from offgrid.config import Config
from offgrid.fleet import Fleet, FleetServer
from offgrid.providers import LocalProvider
from offgrid.tools import ToolBox
from offgrid.workspace import Workspace
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


class FleetExecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = serve([], default=text("peer model"))
        self.config = Config(
            endpoint=self.model.url,
            model="peer-model",
            workspace=str(self.root),
            color=False,
            verbose=0,
        )
        self.server = FleetServer(
            self.config,
            LocalProvider(self.config),
            token="secret",
            port=0,
            name="desk",
        )
        self.port = self.server.start(host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        self.model.stop()
        self.tmp.cleanup()

    def enabled_server(self):
        server = FleetServer(
            self.config,
            LocalProvider(self.config),
            token="secret",
            port=0,
            name="desk",
            allow_exec=True,
        )
        port = server.start(host="127.0.0.1")
        self.addCleanup(server.stop)
        return server, f"http://127.0.0.1:{port}"

    def test_exec_is_disabled_by_default(self):
        self.assertFalse(get_json(self.base + "/fleet/info")["exec"])
        status, body = post_json(
            self.base + "/fleet/v1/exec",
            {"command": "echo should-not-run"},
            token="secret",
        )
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])

    def test_exec_requires_the_shared_token(self):
        server, base = self.enabled_server()
        self.assertTrue(get_json(base + "/fleet/info")["exec"])
        status, body = post_json(base + "/fleet/v1/exec", {"command": "echo nope"})
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])
        self.assertEqual(server.executed, 0)

    def test_authorised_peer_runs_a_command(self):
        server, base = self.enabled_server()
        status, body = post_json(
            base + "/fleet/v1/exec",
            {"command": "echo fleet-exec", "cwd": "."},
            token="secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["exit_code"], 0)
        self.assertEqual(body["peer"], "desk")
        self.assertIn("fleet-exec", body["output"])
        self.assertEqual(server.executed, 1)

    def test_commands_stay_inside_the_workspace_and_cannot_be_destructive(self):
        _, base = self.enabled_server()
        status, body = post_json(
            base + "/fleet/v1/exec",
            {"command": "echo nope", "cwd": "../outside"},
            token="secret",
        )
        self.assertEqual(status, 502)
        self.assertIn("workspace", body["error"])

        status, body = post_json(
            base + "/fleet/v1/exec",
            {"command": "rm -rf /"},
            token="secret",
        )
        self.assertEqual(status, 502)
        self.assertIn("never allowed", body["error"])

    def test_agent_tool_runs_on_every_responding_peer(self):
        _, base = self.enabled_server()
        second = FleetServer(
            self.config,
            LocalProvider(self.config),
            token="secret",
            port=0,
            name="laptop",
            allow_exec=True,
        )
        second_port = second.start(host="127.0.0.1")
        self.addCleanup(second.stop)
        client_root = self.root / "client"
        client_root.mkdir()
        config = Config(
            workspace=str(client_root),
            fleet=True,
            fleet_exec=True,
            color=False,
            verbose=0,
        )
        fleet = Fleet(config).load()
        fleet.add("desk", base, token="secret")
        fleet.add("laptop", f"http://127.0.0.1:{second_port}", token="secret")
        tools = ToolBox(Workspace(client_root), config, fleet=fleet)

        self.assertIn("run_fleet_command", tools.available(ALL_TOOLS))
        result = tools.run_fleet_command("echo remote")

        self.assertTrue(result.ok, result.content)
        self.assertIn("desk", result.content)
        self.assertIn("laptop", result.content)
        self.assertIn("remote", result.content)

    def test_agent_tool_reports_a_nonzero_remote_exit(self):
        _, base = self.enabled_server()
        client_root = self.root / "client-exit"
        client_root.mkdir()
        config = Config(
            workspace=str(client_root),
            fleet=True,
            fleet_exec=True,
            color=False,
            verbose=0,
        )
        fleet = Fleet(config).load()
        fleet.add("desk", base, token="secret")
        tools = ToolBox(Workspace(client_root), config, fleet=fleet)

        result = tools.run_fleet_command("exit 7")

        self.assertFalse(result.ok)
        self.assertIn("exit 7", result.content)

    def test_tool_is_hidden_unless_both_fleet_and_exec_are_enabled(self):
        _, base = self.enabled_server()
        config = Config(
            workspace=str(self.root),
            fleet=True,
            fleet_exec=False,
            color=False,
            verbose=0,
        )
        fleet = Fleet(config).load()
        fleet.add("desk", base, token="secret")
        tools = ToolBox(Workspace(self.root), config, fleet=fleet)

        self.assertNotIn("run_fleet_command", tools.available(ALL_TOOLS))


if __name__ == "__main__":
    unittest.main()
