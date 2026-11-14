"""Device registry, protocol adapters, safety limits and agent tools."""

from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from offgrid.agents import ALL_TOOLS, READ_TOOLS
from offgrid.config import Config
from offgrid.devices import DeviceManager
from offgrid.orchestrator import Team
from offgrid.providers import LocalProvider
from offgrid.tools import ToolBox
from offgrid.ui import UI
from offgrid.workspace import Workspace
from tests.fake_llm import serve, text, tool, when


def _config(root: Path, **overrides) -> Config:
    values = dict(
        workspace=str(root),
        devices_file=".offgrid/devices.json",
        device_policy="",
        device_timeout=2,
        allow_dangerous_devices=False,
        verbose=0,
        color=False,
    )
    values.update(overrides)
    return Config(**values)


def _write(root: Path, payload: dict) -> None:
    directory = root / ".offgrid"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "devices.json").write_text(json.dumps(payload), encoding="utf-8")


class DeviceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write(
            self.root,
            {
                "policy": "auto",
                "devices": [
                    {
                        "name": "cabin",
                        "adapter": "simulator",
                        "values": {"battery": 120, "porch_light": False, "heater": 20, "target": 20},
                        "points": {
                            "battery": {"kind": "gauge", "unit": "%", "min": 0, "max": 100},
                            "porch_light": {"kind": "light", "writable": True},
                            "target": {
                                "kind": "number",
                                "unit": "C",
                                "writable": True,
                                "min": 5,
                                "max": 30,
                            },
                            "heater": {
                                "kind": "climate",
                                "unit": "C",
                                "writable": True,
                                "min": 5,
                                "max": 30,
                            },
                            "front_lock": {
                                "kind": "lock",
                                "writable": True,
                                "dangerous": True,
                            },
                        },
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_and_lists_points(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        self.assertIn("cabin", manager.names())
        self.assertIn("battery", manager.read("cabin", "battery")[0].text())
        self.assertIn("policy: auto", manager.list_text())

    def test_writes_are_range_checked_and_audited(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        result = manager.set("cabin", "porch_light", "on")
        self.assertTrue(result.ok)
        self.assertTrue(result.confirmed)
        self.assertTrue(manager.read("cabin", "porch_light")[0].value)

        refused = manager.set("cabin", "target", "31")
        self.assertFalse(refused.ok)
        self.assertIn("outside the safe range", refused.detail)
        gated = manager.set("cabin", "heater", "20")
        self.assertFalse(gated.ok)
        self.assertIn("dangerous", gated.detail)

        audit = self.root / ".offgrid" / "devices-audit.jsonl"
        self.assertTrue(audit.is_file())
        records = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertTrue(any(item["action"] == "attempt" for item in records))
        self.assertTrue(any(item["action"] == "result" and item["ok"] for item in records))

    def test_read_only_policy_refuses_writes(self) -> None:
        config = _config(self.root, device_policy="read-only")
        manager = DeviceManager(self.root, config)
        result = manager.set("cabin", "porch_light", "on")
        self.assertFalse(result.ok)
        self.assertIn("read-only", result.detail)

    def test_ask_policy_needs_an_explicit_confirmation(self) -> None:
        manager = DeviceManager(self.root, _config(self.root, device_policy="ask"))
        prompts = []
        refused = manager.set(
            "cabin", "porch_light", "on", confirm=lambda message: prompts.append(message) or False
        )
        self.assertFalse(refused.ok)
        self.assertIn("declined", refused.detail)
        self.assertTrue(prompts and "porch_light" in prompts[0])

        accepted = manager.set(
            "cabin", "porch_light", "on", confirm=lambda message: True
        )
        self.assertTrue(accepted.ok, accepted.text())
        self.assertTrue(accepted.confirmed)

    def test_write_is_refused_if_the_audit_log_cannot_be_opened(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        manager.audit_path = self.root / ".offgrid" / "audit-is-a-directory"
        manager.audit_path.mkdir()
        result = manager.set("cabin", "porch_light", "on")
        self.assertFalse(result.ok)
        self.assertIn("audit", result.detail.lower())
        self.assertFalse(manager.read("cabin", "porch_light")[0].value)

    def test_dangerous_points_need_an_explicit_opt_in(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        result = manager.set("cabin", "front_lock", "on")
        self.assertFalse(result.ok)
        self.assertIn("dangerous", result.detail)

        allowed = DeviceManager(
            self.root, _config(self.root, allow_dangerous_devices=True)
        )
        result = allowed.set("cabin", "front_lock", "on")
        self.assertTrue(result.ok, result.text())

    def test_diagnose_flags_out_of_range_and_errors(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        report = manager.diagnose("cabin")
        self.assertFalse(report["ok"])
        self.assertIn("out-of-range", report["summary"])
        self.assertTrue(any("above safe range" in item["detail"] for item in report["readings"]))

    def test_missing_registry_is_a_hint_not_a_crash(self) -> None:
        empty = Path(tempfile.mkdtemp())
        try:
            manager = DeviceManager(empty, _config(empty))
            self.assertEqual(manager.warnings, [])
            with self.assertRaises(Exception) as caught:
                manager.read()
            self.assertIn("no local devices", str(caught.exception))
        finally:
            import shutil

            shutil.rmtree(empty)

    def test_tool_surface_exposes_devices_safely(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        toolbox = ToolBox(
            Workspace(self.root),
            _config(self.root),
            devices=manager,
        )
        self.assertIn("device_set", ALL_TOOLS)
        self.assertIn("device_read", READ_TOOLS)
        listed = toolbox.call("devices_list", {})
        self.assertTrue(listed.ok)
        read = toolbox.call("device_read", {"device": "cabin", "point": "battery"})
        self.assertTrue(read.ok)
        written = toolbox.call(
            "device_set", {"device": "cabin", "point": "porch_light", "value": "on"}
        )
        self.assertTrue(written.ok)


class AgentDeviceFlowTests(unittest.TestCase):
    def test_coder_can_read_a_device_through_the_team_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                {
                    "policy": "read-only",
                    "devices": [
                        {
                            "name": "cabin",
                            "adapter": "simulator",
                            "values": {"battery": 80},
                            "points": {
                                "battery": {
                                    "kind": "gauge",
                                    "unit": "%",
                                    "min": 0,
                                    "max": 100,
                                }
                            },
                        }
                    ],
                },
            )
            plan = {
                "summary": "check the battery",
                "steps": [
                    {
                        "id": 1,
                        "title": "check the battery",
                        "agent": "coder",
                        "task": "Read cabin battery and report it",
                        "files": [],
                        "depends_on": [],
                    }
                ],
                "verify": "",
            }
            rules = [
                when("planner", text(json.dumps(plan))),
                when("coder", tool("device_read", device="cabin", point="battery")),
                when("coder", tool("finish", summary="Cabin battery is 80 percent.")),
                when("reviewer", text('{"verdict": "approve", "issues": [], "instructions": ""}')),
                when("lead", text("Cabin battery is 80 percent.")),
            ]
            model = serve(rules)
            self.addCleanup(model.stop)
            config = Config(
                endpoint=model.url,
                model="fake",
                workspace=str(root),
                effort="fast",
                scale=False,
                memory=False,
                security_review=False,
                reflect=False,
                review_lessons=False,
                quality_gate=False,
                command_policy="deny",
                verbose=0,
                color=False,
            )
            team = Team(LocalProvider(config), Workspace(root), config, UI(config))
            result = team.run("check the battery")
            self.assertIn("80", result.summary)
            self.assertGreaterEqual(result.runs[0].tool_calls, 1)
            self.assertIn("80", result.runs[0].summary)


class _FakeHttpHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep test output clean
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.end_headers()
            return
        if self.path == "/state":
            self._send(200, dict(self.server.state))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/relay":
            self.server.state["relay"] = bool(body.get("state"))
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})


class HttpAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHttpHandler)
        self.server.state = {"power": 12.5, "target": 20, "relay": False}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _manager(self, **overrides) -> DeviceManager:
        _write(
            self.root,
            {
                "policy": "auto",
                "devices": [
                    {
                        "name": "shed",
                        "adapter": "http",
                        "url": f"http://127.0.0.1:{self.server.server_address[1]}",
                        "token_env": "OFFGRID_TEST_DEVICE_TOKEN",
                        "points": {
                            "power": {
                                "kind": "gauge",
                                "unit": "W",
                                "read": {"path": "/state", "value": "power"},
                            },
                            "target": {
                                "kind": "number",
                                "writable": True,
                                "min": 0,
                                "max": 100,
                                "read": {"path": "/state", "value": "target"},
                                "write": {"path": "/relay", "body": {"state": True}},
                            },
                            "relay": {
                                "kind": "switch",
                                "writable": True,
                                "read": {"path": "/state", "value": "relay"},
                                "write": {"path": "/relay", "body": {"state": "{value}"}},
                            },
                            "redirected": {
                                "kind": "sensor",
                                "read": {"path": "/redirect", "value": "x"},
                            },
                        },
                    }
                ],
            },
        )
        return DeviceManager(self.root, _config(self.root, **overrides))

    def test_http_read_and_confirmed_write(self) -> None:
        with mock.patch.dict(os.environ, {"OFFGRID_TEST_DEVICE_TOKEN": "secret"}):
            manager = self._manager()
            self.assertIn("12.5 W", manager.read("shed", "power")[0].text())
            result = manager.set("shed", "relay", "on")
            self.assertTrue(result.ok)
            self.assertTrue(result.confirmed)
            self.assertTrue(self.server.state["relay"])

    def test_missing_token_and_redirect_are_refused(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OFFGRID_TEST_DEVICE_TOKEN", None)
            manager = self._manager()
            reading = manager.read("shed", "power")[0]
            self.assertFalse(reading.ok)
            self.assertIn("token environment", reading.detail)
        with mock.patch.dict(os.environ, {"OFFGRID_TEST_DEVICE_TOKEN": "secret"}):
            manager = self._manager()
            reading = manager.read("shed", "redirected")[0]
            self.assertFalse(reading.ok)
            self.assertIn("redirect", reading.detail)


class _FakeHomeAssistantHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/states/light.porch":
            self._send(
                200,
                {
                    "state": "on" if self.server.state["on"] else "off",
                    "attributes": {"brightness": self.server.state["brightness"]},
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/services/light/turn_on":
            self.server.state["on"] = True
            if "brightness" in body:
                self.server.state["brightness"] = body["brightness"]
            elif "brightness_pct" in body:
                self.server.state["brightness"] = body["brightness_pct"]
            self._send(200, [])
            return
        if self.path == "/api/services/light/turn_off":
            self.server.state["on"] = False
            self._send(200, [])
            return
        self._send(404, {"error": "not found"})


class HomeAssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHomeAssistantHandler)
        self.server.state = {"on": False, "brightness": 0}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write(
            self.root,
            {
                "policy": "auto",
                "devices": [
                    {
                        "name": "house",
                        "adapter": "homeassistant",
                        "url": f"http://127.0.0.1:{self.server.server_address[1]}",
                        "token_env": "OFFGRID_TEST_HA_TOKEN",
                        "wait": 0,
                        "points": {
                            "porch": {
                                "kind": "light",
                                "entity": "light.porch",
                                "writable": True,
                            },
                            "brightness": {
                                "kind": "number",
                                "entity": "light.porch",
                                "attribute": "brightness",
                                "writable": True,
                                "min": 0,
                                "max": 255,
                                "value_service": "turn_on",
                                "value_field": "brightness",
                            },
                            "front_lock": {
                                "kind": "lock",
                                "entity": "lock.front",
                                "writable": True,
                                "dangerous": True,
                            },
                        },
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_home_assistant_read_and_bool_write(self) -> None:
        with mock.patch.dict(os.environ, {"OFFGRID_TEST_HA_TOKEN": "secret"}):
            manager = DeviceManager(self.root, _config(self.root))
            self.assertFalse(manager.read("house", "porch")[0].value)
            result = manager.set("house", "porch", "on")
            self.assertTrue(result.ok)
            self.assertTrue(result.confirmed)
            self.assertTrue(self.server.state["on"])

    def test_home_assistant_numeric_write_and_dangerous_gate(self) -> None:
        with mock.patch.dict(os.environ, {"OFFGRID_TEST_HA_TOKEN": "secret"}):
            manager = DeviceManager(self.root, _config(self.root))
            result = manager.set("house", "brightness", "55")
            self.assertTrue(result.ok)
            self.assertTrue(result.confirmed)
            self.assertEqual(self.server.state["brightness"], 55)
            lock = manager.set("house", "front_lock", "on")
            self.assertFalse(lock.ok)
            self.assertIn("dangerous", lock.detail)


class _ModbusServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(2)
        self.port = self.socket.getsockname()[1]
        self.registers = {0: 520, 10: 0}
        self.coils = {10: False}
        self.stop_event = threading.Event()

    @staticmethod
    def _recv(sock: socket.socket, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            with conn:
                try:
                    header = self._recv(conn, 7)
                    tid, _protocol, length, unit = struct.unpack(">HHHB", header)
                    pdu = self._recv(conn, length - 1)
                    function = pdu[0]
                    if function == 3:
                        address, count = struct.unpack(">HH", pdu[1:5])
                        values = [self.registers.get(address + index, 0) for index in range(count)]
                        response = struct.pack(">BB", 3, count * 2) + b"".join(
                            struct.pack(">H", value) for value in values
                        )
                    elif function == 6:
                        address, value = struct.unpack(">HH", pdu[1:5])
                        self.registers[address] = value
                        response = pdu
                    elif function == 1:
                        address, count = struct.unpack(">HH", pdu[1:5])
                        bits = [self.coils.get(address + index, False) for index in range(count)]
                        byte = sum(1 << index for index, value in enumerate(bits) if value)
                        response = struct.pack(">BBB", 1, 1, byte)
                    elif function == 5:
                        address, value = struct.unpack(">HH", pdu[1:5])
                        self.coils[address] = value == 0xFF00
                        response = pdu
                    else:
                        response = struct.pack(">BB", function | 0x80, 1)
                    packet = struct.pack(">HHHB", tid, 0, len(response) + 1, unit) + response
                    conn.sendall(packet)
                except (OSError, struct.error):
                    continue

    def stop(self) -> None:
        self.stop_event.set()
        self.socket.close()


class ModbusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ModbusServer()
        self.server.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write(
            self.root,
            {
                "policy": "auto",
                "devices": [
                    {
                        "name": "solar",
                        "adapter": "modbus",
                        "host": "127.0.0.1",
                        "port": self.server.port,
                        "unit": 1,
                        "points": {
                            "battery": {
                                "register": 0,
                                "area": "holding",
                                "data_type": "u16",
                                "scale": 0.1,
                                "unit": "%",
                                "min": 0,
                                "max": 100,
                            },
                            "relay": {
                                "register": 10,
                                "area": "coil",
                                "data_type": "u16",
                                "kind": "switch",
                                "writable": True,
                            },
                            "target": {
                                "register": 20,
                                "area": "holding",
                                "data_type": "u16",
                                "kind": "number",
                                "unit": "C",
                                "writable": True,
                                "min": 5,
                                "max": 30,
                            },
                        },
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.server.stop()
        self.server.join(timeout=2)
        self.temp.cleanup()

    def test_modbus_read_scale_and_coil_write(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        reading = manager.read("solar", "battery")[0]
        self.assertTrue(reading.ok, reading.detail)
        self.assertAlmostEqual(reading.value, 52.0)
        result = manager.set("solar", "relay", "on")
        self.assertTrue(result.ok, result.text())
        self.assertTrue(result.confirmed)
        self.assertTrue(self.server.coils[10])

    def test_modbus_range_is_enforced(self) -> None:
        manager = DeviceManager(self.root, _config(self.root))
        refused = manager.set("solar", "target", "50")
        self.assertFalse(refused.ok)
        self.assertIn("outside the safe range", refused.detail)
        report = manager.diagnose("solar")
        self.assertTrue(report["ok"])
        self.assertGreaterEqual(report["warnings"], 0)


if __name__ == "__main__":
    unittest.main()
