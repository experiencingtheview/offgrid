"""Guided Home Assistant setup: discovery, private tokens and safe defaults."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from offgrid.config import Config
from offgrid.devices import DeviceManager
from offgrid.home import HomeSetup, HomeSetupError


def _config(root: Path) -> Config:
    return Config(
        workspace=str(root),
        devices_file=".offgrid/devices.json",
        devices_audit=".offgrid/devices-audit.jsonl",
        device_policy="",
        device_timeout=2,
        allow_dangerous_devices=False,
        verbose=0,
        color=False,
    )


def _states() -> list[dict]:
    return [
        {
            "entity_id": "light.porch",
            "state": "off",
            "attributes": {"friendly_name": "Porch light"},
        },
        {
            "entity_id": "switch.pump",
            "state": "on",
            "attributes": {"friendly_name": "Pump"},
        },
        {
            "entity_id": "fan.ceiling",
            "state": "off",
            "attributes": {"friendly_name": "Ceiling fan"},
        },
        {
            "entity_id": "sensor.energy",
            "state": "12.5",
            "attributes": {"friendly_name": "Energy", "unit_of_measurement": "kWh"},
        },
        {
            "entity_id": "binary_sensor.door",
            "state": "off",
            "attributes": {"friendly_name": "Back door"},
        },
        {
            "entity_id": "number.target",
            "state": "20",
            "attributes": {"friendly_name": "Target", "min": 5, "max": 30},
        },
        {
            "entity_id": "sensor.missing",
            "state": "unavailable",
            "attributes": {"friendly_name": "Missing sensor"},
        },
        # These must not be in the guided flow.
        {
            "entity_id": "lock.front",
            "state": "locked",
            "attributes": {"friendly_name": "Front lock"},
        },
        {
            "entity_id": "cover.garage",
            "state": "closed",
            "attributes": {"friendly_name": "Garage"},
        },
        {
            "entity_id": "climate.office",
            "state": "heat",
            "attributes": {"friendly_name": "Office climate"},
        },
    ]


class _FakeHome:
    def __init__(self, token: str, states: list[dict]) -> None:
        self.token = token
        self.states = {item["entity_id"]: item for item in states}
        self.services: list[tuple[str, dict]] = []
        self.requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
                return

            def _authorized(self) -> bool:
                return self.headers.get("Authorization") == "Bearer " + self.server.fake.token

            def _json(self, code: int, payload) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                self.server.fake.requests.append("GET " + self.path)
                if not self._authorized():
                    self._json(401, {"message": "unauthorized"})
                    return
                if self.path == "/api/states":
                    self._json(200, list(self.server.fake.states.values()))
                    return
                if self.path.startswith("/api/states/"):
                    entity = self.path.split("/api/states/", 1)[1]
                    item = self.server.fake.states.get(entity)
                    if item is None:
                        self._json(404, {"message": "not found"})
                    else:
                        self._json(200, item)
                    return
                self._json(404, {"message": "not found"})

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                self.server.fake.requests.append("POST " + self.path)
                if not self._authorized():
                    self._json(401, {"message": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self.server.fake.services.append((self.path, body))
                entity = body.get("entity_id")
                item = self.server.fake.states.get(entity)
                if item is None:
                    self._json(404, {"message": "not found"})
                    return
                service = self.path.rsplit("/", 1)[-1]
                if service in ("turn_on", "open_cover", "unlock", "open_valve"):
                    item["state"] = "on"
                elif service in ("turn_off", "close_cover", "lock", "close_valve"):
                    item["state"] = "off"
                elif service == "set_value":
                    item["state"] = str(body.get("value"))
                self._json(200, {})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HomeSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = _config(self.root)
        self.token = secrets.token_urlsafe(24)
        self.home = _FakeHome(self.token, _states())
        self.setup = HomeSetup(self.config, self.root)

    def tearDown(self) -> None:
        self.home.close()
        self.temp.cleanup()

    def test_scan_finds_safe_entities_and_leaves_dangerous_ones_out(self) -> None:
        result = self.setup.scan(self.home.url, self.token)
        ids = {item["entity_id"] for item in result["entities"]}
        self.assertIn("light.porch", ids)
        self.assertIn("switch.pump", ids)
        self.assertIn("sensor.energy", ids)
        self.assertIn("number.target", ids)
        self.assertNotIn("lock.front", ids)
        self.assertNotIn("cover.garage", ids)
        self.assertNotIn("climate.office", ids)
        self.assertNotIn(self.token, json.dumps(result))

        options = {item["entity_id"]: item for item in result["entities"]}
        self.assertTrue(options["number.target"]["writable"])
        self.assertTrue(options["number.target"]["selected"])
        self.assertFalse(options["sensor.missing"]["selected"])
        self.assertEqual(options["sensor.energy"]["kind"], "gauge")
        self.assertEqual(options["light.porch"]["kind"], "light")

    def test_connection_without_scheme_is_normalised(self) -> None:
        address = self.home.url.split("://", 1)[1]
        result = self.setup.scan(address, self.token)
        self.assertEqual(result["url"], self.home.url)

    def test_read_only_connection_writes_private_token_and_registry(self) -> None:
        result = self.setup.connect(
            self.home.url,
            self.token,
            "cabin",
            ["light.porch", "sensor.energy"],
        )
        self.assertEqual(result["policy"], "read-only")

        registry_path = self.root / ".offgrid" / "devices.json"
        token_path = self.root / ".offgrid" / "home-assistant-cabin.token"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["policy"], "read-only")
        self.assertEqual(
            registry["devices"][0]["token_file"],
            ".offgrid/home-assistant-cabin.token",
        )
        self.assertFalse(registry["devices"][0]["points"]["porch"]["writable"])
        self.assertNotIn(self.token, registry_path.read_text(encoding="utf-8"))
        self.assertEqual(token_path.read_text(encoding="utf-8"), self.token)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(registry_path.stat().st_mode), 0o600)

        manager = DeviceManager(self.root, self.config)
        reading = manager.read("cabin", "porch")[0]
        self.assertFalse(reading.value)
        self.assertIn("cabin", manager.names())
        refused = manager.set("cabin", "porch", "on")
        self.assertFalse(refused.ok)
        self.assertIn("read-only", refused.detail)

    def test_control_is_opt_in_and_uses_the_private_token(self) -> None:
        result = self.setup.connect(
            self.home.url,
            self.token,
            "cabin",
            ["light.porch", "switch.pump"],
            allow_control=True,
        )
        self.assertEqual(result["policy"], "auto")
        manager = DeviceManager(self.root, self.config)
        written = manager.set("cabin", "porch", "on")
        self.assertTrue(written.ok)
        self.assertTrue(manager.read("cabin", "porch")[0].value)
        self.assertTrue(any("light/turn_on" in path for path, _body in self.home.services))
        self.assertTrue(manager.read("cabin", "porch")[0].ok)

    def test_guided_default_stays_read_only_even_when_the_registry_allows_writes(self) -> None:
        directory = self.root / ".offgrid"
        directory.mkdir(parents=True)
        (directory / "devices.json").write_text(
            json.dumps({"policy": "auto", "devices": []}), encoding="utf-8"
        )
        self.setup.connect(self.home.url, self.token, "cabin", ["light.porch"])
        manager = DeviceManager(self.root, self.config)
        self.assertTrue(manager.read("cabin", "porch")[0].ok)
        refused = manager.set("cabin", "porch", "on")
        self.assertFalse(refused.ok)
        self.assertIn("read-only", refused.detail)

    def test_control_stays_read_only_when_only_sensors_are_selected(self) -> None:
        result = self.setup.connect(
            self.home.url,
            self.token,
            "cabin",
            ["sensor.energy"],
            allow_control=True,
        )
        self.assertEqual(result["policy"], "read-only")

    def test_two_homes_receive_separate_token_files(self) -> None:
        self.setup.connect(self.home.url, self.token, "cabin", ["light.porch"])
        self.setup.connect(self.home.url, self.token, "shop", ["switch.pump"])
        registry = json.loads(
            (self.root / ".offgrid" / "devices.json").read_text(encoding="utf-8")
        )
        token_files = [item["token_file"] for item in registry["devices"]]
        self.assertEqual(
            token_files,
            [
                ".offgrid/home-assistant-cabin.token",
                ".offgrid/home-assistant-shop.token",
            ],
        )
        self.assertTrue(all((self.root / item).is_file() for item in token_files))

    def test_existing_devices_are_merged_and_duplicate_names_are_refused(self) -> None:
        directory = self.root / ".offgrid"
        directory.mkdir(parents=True)
        (directory / "devices.json").write_text(
            json.dumps(
                {
                    "policy": "ask",
                    "devices": [
                        {
                            "name": "sim",
                            "adapter": "simulator",
                            "values": {"light": False},
                            "points": {"light": {"kind": "light", "writable": True}},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = self.setup.connect(
            self.home.url, self.token, "cabin", ["light.porch"]
        )
        self.assertTrue(result["policy_preserved"])
        registry = json.loads((directory / "devices.json").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in registry["devices"]], ["sim", "cabin"])
        self.assertEqual(registry["policy"], "ask")

        with self.assertRaises(HomeSetupError) as caught:
            self.setup.connect(self.home.url, self.token, "cabin", ["light.porch"])
        self.assertIn("already connected", str(caught.exception))

    def test_bad_token_is_explained_without_echoing_it(self) -> None:
        secret = self.token + "-not-the-token"
        with self.assertRaises(HomeSetupError) as caught:
            self.setup.scan(self.home.url, secret)
        message = str(caught.exception)
        self.assertIn("rejected", message)
        self.assertNotIn(secret, message)

    def test_invalid_existing_registry_is_not_overwritten(self) -> None:
        path = self.root / ".offgrid" / "devices.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(HomeSetupError) as caught:
            self.setup.connect(self.home.url, self.token, "cabin", ["light.porch"])
        self.assertIn("not changed", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_status_reports_connection_without_the_token(self) -> None:
        self.setup.connect(
            self.home.url, self.token, "cabin", ["light.porch"]
        )
        status = self.setup.status()
        self.assertTrue(status["connected"])
        self.assertTrue(status["token_present"])
        self.assertEqual(status["devices"][0]["name"], "cabin")
        self.assertNotIn(self.token, json.dumps(status))
        self.assertEqual(status["defaults"]["url"], "http://homeassistant.local:8123")


if __name__ == "__main__":
    unittest.main()
