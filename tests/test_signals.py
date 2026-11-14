"""Flipper Zero signal bridge, tested against a pseudo-terminal, not hardware."""

from __future__ import annotations

import json
import os
import pty
import select
import tempfile
import threading
import unittest
from pathlib import Path

from offgrid.agents import ALL_TOOLS, READ_TOOLS
from offgrid.config import Config
from offgrid.signals import FlipperSerial, SignalBridge
from offgrid.tools import ToolBox
from offgrid.workspace import Workspace


class FakeFlipper(threading.Thread):
    """A tiny Flipper CLI over a pty: enough to exercise framing and limits."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.commands: list[str] = []
        self.stop_event = threading.Event()
        self.in_nfc = False
        self.started = False

    def _send(self, text: str) -> None:
        try:
            os.write(self.master, text.replace("\n", "\r\n").encode())
        except OSError:
            self.stop_event.set()

    def _prompt(self) -> None:
        self._send("nfc> " if self.in_nfc else ">: ")

    def run(self) -> None:
        buffer = ""
        while not self.stop_event.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(self.master, 4096).decode("utf-8", "replace")
            except OSError:
                return
            for char in data:
                if char == "\x03":
                    self._prompt()
                    continue
                if char not in "\r\n":
                    buffer += char
                    continue
                command = buffer.strip()
                buffer = ""
                if not command:
                    if not self.started:
                        self.started = True
                        self._send(
                            "Welcome to Flipper Zero Command Line Interface!\n"
                            "Firmware version: 1.3.4\n"
                        )
                    self._prompt()
                    continue
                self.commands.append(command)
                if command == "nfc":
                    self.in_nfc = True
                    self._prompt()
                elif command == "exit" and self.in_nfc:
                    self.in_nfc = False
                    self._prompt()
                elif self.in_nfc and command == "scanner":
                    self._send("ISO14443-3A UID: 04AABBCCDD\n")
                elif command == "info device":
                    self._send("Device name: Flipper\nFirmware version: 1.3.4\n")
                elif command == "info power":
                    self._send("Battery: 87%\n")
                elif command == "rfid read":
                    self._send("EM4100 5500824806\n")
                elif command == "ikey read":
                    self._send("Dallas DEADBEEF00000000\n")
                elif command == "ir rx":
                    self._send("NEC 1234 5678\n")
                elif command.startswith("subghz rx "):
                    self._send("RSSI: -70 dBm at 433920000\n")
                elif command.startswith("ir tx "):
                    self._send("Sending IR command\n")
                else:
                    self._send("Unknown command\n")
                if not self.in_nfc:
                    self._prompt()

    def stop(self) -> None:
        self.stop_event.set()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"), "needs POSIX pty")
class SignalBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFlipper()
        self.fake.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.fake.stop()
        self.fake.join(timeout=2)
        self.temp.cleanup()

    def bridge(self, **overrides) -> SignalBridge:
        values = dict(
            workspace=str(self.root),
            signals_port=self.fake.port,
            signals_policy="read-only",
            signals_timeout=2,
            signals_audit=".offgrid/signals-audit.jsonl",
            verbose=0,
            color=False,
        )
        values.update(overrides)
        return SignalBridge(self.root, Config(**values))

    def test_status_and_info(self) -> None:
        bridge = self.bridge()
        data = bridge.status_data()
        self.assertTrue(data["connected"])
        self.assertTrue(data["capabilities"]["rfid_read"])
        self.assertFalse(data["capabilities"]["ir_send"])
        self.assertIn("read-only", bridge.status_text())
        info = bridge.info()
        self.assertTrue(info.ok, info.detail)
        self.assertIn("Flipper", info.text)
        self.assertIn("87%", info.text)
        self.assertIn("info device", self.fake.commands)

    def test_read_only_signals(self) -> None:
        bridge = self.bridge()
        rfid = bridge.read("rfid", 2)
        self.assertTrue(rfid.ok, rfid.detail)
        self.assertIn("EM4100", rfid.text)
        ir = bridge.read("ir", 2)
        self.assertTrue(ir.ok)
        self.assertIn("NEC", ir.text)
        nfc = bridge.read("nfc", 2)
        self.assertTrue(nfc.ok, nfc.detail)
        self.assertIn("ISO14443", nfc.text)
        self.assertIn("nfc", self.fake.commands)

    def test_subghz_receive_is_bounded(self) -> None:
        bridge = self.bridge()
        self.assertFalse(bridge.scan_subghz(1000, 1).ok)
        result = bridge.scan_subghz(433_920_000, 2)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("RSSI", result.text)
        self.assertTrue(
            any(command.startswith("subghz rx 433920000") for command in self.fake.commands)
        )
        self.assertFalse(any("subghz tx" in command for command in self.fake.commands))

    def test_ir_transmit_is_gated_and_audited(self) -> None:
        read_only = self.bridge()
        refused = read_only.send_ir("NEC", "1234", "5678")
        self.assertFalse(refused.ok)
        self.assertIn("read-only", refused.detail)
        self.assertFalse(any(command.startswith("ir tx") for command in self.fake.commands))

        auto = self.bridge(signals_policy="auto")
        sent = auto.send_ir("NEC", "1234", "5678")
        self.assertTrue(sent.ok, sent.detail)
        self.assertTrue(any(command.startswith("ir tx NEC 1234 5678") for command in self.fake.commands))
        audit = self.root / ".offgrid" / "signals-audit.jsonl"
        self.assertTrue(audit.is_file())
        records = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertTrue(any(item["action"] == "attempt" for item in records))
        self.assertTrue(any(item["action"] == "result" and item["ok"] for item in records))

    def test_ir_arguments_are_validated(self) -> None:
        auto = self.bridge(signals_policy="auto")
        before = len(self.fake.commands)
        bad = auto.send_ir("NEC; reboot", "1234", "5678")
        self.assertFalse(bad.ok)
        bad_hex = auto.send_ir("NEC", "not-hex", "1234")
        self.assertFalse(bad_hex.ok)
        self.assertEqual(len(self.fake.commands), before)

    def test_audit_failure_blocks_transmission(self) -> None:
        auto = self.bridge(signals_policy="auto")
        auto.audit_path = self.root / ".offgrid" / "audit-directory"
        auto.audit_path.parent.mkdir(parents=True, exist_ok=True)
        auto.audit_path.mkdir()
        result = auto.send_ir("NEC", "1234", "5678")
        self.assertFalse(result.ok)
        self.assertIn("audit", result.detail.lower())
        self.assertFalse(any(command.startswith("ir tx") for command in self.fake.commands))

    def test_tool_surface_does_not_expose_transmit_other_than_ir(self) -> None:
        bridge = self.bridge(signals_policy="auto")
        toolbox = ToolBox(
            Workspace(self.root),
            Config(workspace=str(self.root), verbose=0, color=False),
            signals=bridge,
        )
        self.assertIn("signals_status", ALL_TOOLS)
        self.assertIn("signal_read", READ_TOOLS)
        status = toolbox.call("signals_status", {})
        self.assertTrue(status.ok)
        read = toolbox.call("signal_read", {"kind": "rfid", "seconds": 2})
        self.assertTrue(read.ok)
        scan = toolbox.call("signal_scan", {"frequency": 433_920_000, "seconds": 2})
        self.assertTrue(scan.ok)
        self.assertNotIn("subghz_tx", ALL_TOOLS)
        self.assertNotIn("nfc_emulate", ALL_TOOLS)
        self.assertNotIn("badusb", ALL_TOOLS)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"), "needs POSIX pty")
class SerialFramingTests(unittest.TestCase):
    def test_command_and_stream_framing(self) -> None:
        fake = FakeFlipper()
        fake.start()
        try:
            with FlipperSerial(fake.port, timeout=2) as client:
                self.assertIn("Device name", client.command("info device"))
                self.assertIn("NEC", client.stream("ir rx", 2))
        finally:
            fake.stop()
            fake.join(timeout=2)
