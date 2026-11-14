"""Flipper Zero and other signal hardware, with hard limits.

This is deliberately not a general Flipper CLI.  A connected Flipper can give
OffGrid read-only eyes and a narrowly-scoped IR remote:

* device and power information;
* read 125 kHz RFID, iButton, NFC and infrared;
* receive (never transmit) Sub-GHz;
* send one infrared protocol/address/command, after an explicit policy gate.

Cloning, replaying Sub-GHz, NFC/RFID emulation, writing tags, GPIO poking,
BadUSB and arbitrary CLI commands are not implemented, even if the attached
firmware supports them.  Those are not diagnostic functions, and a model
should not have a generic radio transmitter.

The serial client uses the Python standard library only (termios on POSIX).
Flipper CLI text is intentionally treated as untrusted data: it is stripped of
ANSI noise, bounded, and never executed.
"""

from __future__ import annotations

import glob
import os
import re
import select
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .private import append_private_text

POLICIES = ("read-only", "ask", "auto")
MAX_TEXT = 6000
PROMPT = ">:"
NFC_PROMPT = "nfc>"
FLIPPER_VID = "0483"
FLIPPER_PID = "5740"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
FAIL_MARKERS = (
    "other application is running",
    "unknown command",
    "not supported",
    "failed to",
    "usage:",
)
HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{1,16}$")
PROTOCOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,24}$")


class SignalError(Exception):
    """A user-facing hardware error.  Never include credentials."""


@dataclass
class SerialPortInfo:
    path: str
    description: str = ""
    is_flipper: bool = False
    vid: str = ""
    pid: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "description": self.description,
            "is_flipper": self.is_flipper,
            "vid": self.vid,
            "pid": self.pid,
        }


@dataclass
class SignalResult:
    ok: bool
    kind: str
    text: str
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "text": self.text,
            "detail": self.detail,
            "data": self.data,
            "at": round(self.at, 3),
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _sysfs_for(port_path: str) -> Tuple[str, str, str]:
    """Return (vendor, product, description) for a Linux tty, when available."""
    name = Path(port_path).resolve().name
    base = Path("/sys/class/tty") / name / "device"
    values = {
        "idVendor": _read_text(base / ".." / "idVendor").lower(),
        "idProduct": _read_text(base / ".." / "idProduct").lower(),
        "manufacturer": _read_text(base / ".." / "manufacturer"),
        "product": _read_text(base / ".." / "product"),
    }
    description = " ".join(
        part for part in (values["manufacturer"], values["product"]) if part
    ).strip()
    return values["idVendor"], values["idProduct"], description


def discover_ports() -> List[SerialPortInfo]:
    """Find USB serial ports without opening them."""
    candidates: List[str] = []
    patterns = (
        "/dev/serial/by-id/*",
        "/dev/cu.usbmodemflip*",
        "/dev/tty.usbmodemflip*",
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    )
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    found: Dict[str, SerialPortInfo] = {}
    for raw in candidates:
        path = os.path.realpath(raw)
        if path in found:
            continue
        label = Path(raw).name
        vid, pid, description = _sysfs_for(path)
        is_flipper = (
            "flipper" in label.lower()
            or "flipper" in description.lower()
            or (vid == FLIPPER_VID and pid == FLIPPER_PID)
        )
        found[path] = SerialPortInfo(
            path,
            description or label,
            is_flipper=is_flipper,
            vid=vid,
            pid=pid,
        )
    return sorted(found.values(), key=lambda item: (not item.is_flipper, item.path))


def _clean_output(raw: str, command: str = "") -> str:
    text = ANSI_RE.sub("", raw or "")
    text = text.replace("\r", "").replace("\x00", "")
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == command.strip():
            continue
        if stripped.startswith("Welcome to Flipper Zero Command Line Interface"):
            continue
        if stripped.startswith("Read the manual:") or stripped.startswith("Run `help`"):
            continue
        if stripped.startswith(">:") or stripped.startswith("nfc>"):
            continue
        lines.append(stripped)
    cleaned = "\n".join(lines)
    return cleaned if len(cleaned) <= MAX_TEXT else cleaned[: MAX_TEXT - 3] + "..."


class FlipperSerial:
    """Minimal POSIX serial client for the Flipper CLI."""

    def __init__(self, path: str, timeout: float = 8.0) -> None:
        self.path = path
        try:
            self.timeout = max(1.0, min(float(timeout), 30.0))
        except (TypeError, ValueError):
            self.timeout = 8.0
        self.fd: Optional[int] = None
        self._open()

    def _open(self) -> None:
        if os.name != "posix":
            raise SignalError(
                "Flipper serial access uses the POSIX termios API; on Windows use a "
                "small Linux/macOS bridge machine for now"
            )
        try:
            import termios
        except ImportError as exc:  # pragma: no cover - non-POSIX
            raise SignalError("this platform has no termios serial support") from exc
        try:
            self.fd = os.open(
                self.path,
                os.O_RDWR | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag
            if hasattr(termios, "B115200"):
                attrs[4] = termios.B115200
                attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except PermissionError as exc:
            raise SignalError(
                f"cannot open {self.path}; add your user to the serial group "
                "(usually dialout) and reconnect the Flipper"
            ) from exc
        except (OSError, termios.error) as exc:
            raise SignalError(f"cannot open Flipper serial port {self.path}: {exc}") from exc
        # Ask for a prompt.  The device may have just enumerated.
        self._write("\r\n")
        self._read_until((PROMPT, NFC_PROMPT), min(self.timeout, 2.0))

    def _write(self, text: str) -> None:
        if self.fd is None:
            raise SignalError("Flipper serial port is closed")
        try:
            os.write(self.fd, text.encode("utf-8", "replace"))
        except OSError as exc:
            raise SignalError(f"could not write to the Flipper: {exc}") from exc

    def _read_once(self, wait: float) -> str:
        if self.fd is None:
            return ""
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, wait))
        except (OSError, ValueError):
            return ""
        if not ready:
            return ""
        try:
            chunk = os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            return ""
        return chunk.decode("utf-8", "replace") if chunk else ""

    def _read_until(self, prompts: Iterable[str], timeout: float) -> str:
        prompt_set = tuple(prompts)
        end = time.monotonic() + max(0.1, timeout)
        buffer = ""
        while time.monotonic() < end:
            chunk = self._read_once(min(0.2, max(0.01, end - time.monotonic())))
            if chunk:
                buffer += chunk
                tail = buffer[-80:]
                if any(marker in tail for marker in prompt_set):
                    break
            else:
                time.sleep(0.02)
        return buffer

    def _collect(self, seconds: float) -> str:
        end = time.monotonic() + max(0.1, seconds)
        chunks: List[str] = []
        while time.monotonic() < end:
            chunk = self._read_once(min(0.25, max(0.01, end - time.monotonic())))
            if chunk:
                chunks.append(chunk)
            else:
                time.sleep(0.03)
        return "".join(chunks)

    def command(self, command: str, timeout: Optional[float] = None) -> str:
        self._write(command.strip() + "\r\n")
        raw = self._read_until((PROMPT,), timeout or self.timeout)
        return _clean_output(raw, command)

    def stream(self, command: str, seconds: float) -> str:
        """Start a CLI command, collect until it prompts or the window ends."""
        self._write(command.strip() + "\r\n")
        raw = ""
        end = time.monotonic() + max(0.1, seconds)
        prompted = False
        while time.monotonic() < end:
            chunk = self._read_once(min(0.25, max(0.01, end - time.monotonic())))
            if chunk:
                raw += chunk
                if PROMPT in raw[-80:]:
                    prompted = True
                    break
            else:
                time.sleep(0.03)
        if not prompted:
            # Receivers such as `ir rx` stream until interrupted.
            self._write("\x03")
            raw += self._read_until((PROMPT,), 2.0)
        return _clean_output(raw, command)

    def subshell(self, shell: str, command: str, seconds: float) -> str:
        """Run one read-only command in the NFC subshell."""
        if shell.strip().lower() != "nfc" or command.strip().lower() not in ("scanner", "scan"):
            raise SignalError("only the read-only NFC scanner subshell is supported")
        self._write(shell.strip() + "\r\n")
        entered = self._read_until((NFC_PROMPT, PROMPT), 2.0)
        cleaned_entered = _clean_output(entered, shell)
        if NFC_PROMPT not in entered and PROMPT in entered:
            # Firmware without an interactive subshell: try the one-shot form.
            return self.command(f"{shell} {command}", timeout=self.timeout)
        self._write(command.strip() + "\r\n")
        raw = self._collect(seconds)
        self._write("\x03")
        raw += self._read_until((NFC_PROMPT, PROMPT), 2.0)
        self._write("exit\r\n")
        raw += self._read_until((PROMPT,), 2.0)
        return (cleaned_entered + "\n" + _clean_output(raw, command)).strip()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self) -> "FlipperSerial":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SignalBridge:
    """Detect Flipper hardware and expose only the safe operations."""

    def __init__(self, workspace: Path, config: Any = None, log: Optional[Any] = None) -> None:
        self.root = Path(workspace)
        self.cfg = config
        self.log = log or (lambda _message: None)
        policy = str(getattr(config, "signals_policy", "read-only") or "read-only").lower()
        self.policy = policy if policy in POLICIES else "read-only"
        self.port_override = str(getattr(config, "signals_port", "") or "").strip()
        try:
            self.timeout = max(1.0, min(float(getattr(config, "signals_timeout", 8) or 8), 30.0))
        except (TypeError, ValueError):
            self.timeout = 8.0
        audit = str(getattr(config, "signals_audit", ".offgrid/signals-audit.jsonl") or ".offgrid/signals-audit.jsonl")
        self.audit_path = Path(audit).expanduser()
        if not self.audit_path.is_absolute():
            self.audit_path = self.root / self.audit_path

    # -- discovery -------------------------------------------------------
    def ports(self) -> List[SerialPortInfo]:
        return discover_ports()

    def flipper(self) -> Optional[SerialPortInfo]:
        if self.port_override:
            if not Path(self.port_override).exists():
                return None
            return SerialPortInfo(self.port_override, "configured serial port", True)
        for port in self.ports():
            if port.is_flipper:
                return port
        return None

    def status_data(self) -> Dict[str, Any]:
        flipper = self.flipper()
        return {
            "connected": flipper is not None,
            "port": flipper.path if flipper else "",
            "description": flipper.description if flipper else "",
            "policy": self.policy,
            "capabilities": {
                "info": flipper is not None,
                "rfid_read": flipper is not None,
                "nfc_read": flipper is not None,
                "ikey_read": flipper is not None,
                "ir_receive": flipper is not None,
                "subghz_receive": flipper is not None,
                "ir_send": flipper is not None and self.policy != "read-only",
            },
            "never": [
                "subghz transmit",
                "NFC/RFID clone, write or emulate",
                "iButton write or emulate",
                "BadUSB",
                "GPIO writes",
                "arbitrary Flipper CLI",
            ],
        }

    def status_text(self) -> str:
        data = self.status_data()
        if not data["connected"]:
            return (
                "No Flipper Zero found over USB serial.\n"
                "Connect it, unlock it, close any running USB app, and ensure your user "
                "can access /dev/ttyACM0 (usually the dialout group). Linux/macOS only; "
                "the standard library has no Windows serial API."
            )
        lines = [
            f"Flipper Zero: {data['port']} ({data['description']})",
            f"signal policy: {self.policy}",
            "read: info, RFID, NFC, iButton, IR receive, Sub-GHz receive",
        ]
        if self.policy == "read-only":
            lines.append("IR transmit is disabled (signal policy is read-only)")
        else:
            lines.append("IR transmit needs the exact protocol/address/command and explicit approval")
        lines.append(
            "never exposed: Sub-GHz transmit, cloning/writing/emulation, BadUSB, GPIO writes, arbitrary CLI"
        )
        return "\n".join(lines)

    # -- audit -----------------------------------------------------------
    def _audit(self, action: str, kind: str, ok: bool, detail: str, data: Any = None) -> bool:
        import json

        record = {
            "t": round(time.time(), 3),
            "action": action,
            "kind": kind,
            "ok": bool(ok),
            "detail": str(detail)[:300],
            "data": data if isinstance(data, (dict, list, str, int, float, bool)) else str(data)[:300],
        }
        try:
            append_private_text(self.audit_path, json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            self.log(f"signal audit log could not be written: {exc}")
            return False

    # -- serial operations ----------------------------------------------
    def _client(self) -> Tuple[SerialPortInfo, FlipperSerial]:
        port = self.flipper()
        if port is None:
            raise SignalError(
                "no Flipper Zero found; connect it over USB and check the serial permissions"
            )
        try:
            return port, FlipperSerial(port.path, timeout=self.timeout)
        except SignalError:
            raise

    @staticmethod
    def _bounded_text(text: str) -> str:
        cleaned = (text or "").strip()
        if len(cleaned) > MAX_TEXT:
            cleaned = cleaned[: MAX_TEXT - 3] + "..."
        return cleaned

    @staticmethod
    def _failed(text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in FAIL_MARKERS)

    def info(self) -> SignalResult:
        try:
            port, client = self._client()
            with client:
                device = client.command("info device")
                power = client.command("info power")
            text = self._bounded_text("\n".join(part for part in (device, power) if part))
            ok = bool(text) and not self._failed(text)
            detail = "" if ok else ("the Flipper reported an error" if text else "empty response")
            self._audit("read", "info", ok, "device and power information", {"port": port.path})
            return SignalResult(
                ok,
                "info",
                text or "the Flipper returned no device information",
                detail,
                {"port": port.path},
            )
        except SignalError as exc:
            self._audit("read", "info", False, str(exc))
            return SignalResult(False, "info", "", str(exc))

    def read(self, kind: str, seconds: Optional[float] = None) -> SignalResult:
        kind = str(kind or "").strip().lower()
        allowed = {"rfid", "nfc", "ikey", "ir"}
        if kind not in allowed:
            return SignalResult(False, kind or "read", "", f"kind must be one of {', '.join(sorted(allowed))}")
        try:
            duration = max(1.0, min(float(seconds or 8), 30.0))
        except (TypeError, ValueError):
            return SignalResult(False, kind, "", "seconds must be a number from 1 to 30")
        try:
            port, client = self._client()
            with client:
                if kind == "rfid":
                    raw = client.stream("rfid read", duration)
                elif kind == "ikey":
                    raw = client.stream("ikey read", duration)
                elif kind == "ir":
                    raw = client.stream("ir rx", duration)
                else:
                    raw = client.subshell("nfc", "scanner", duration)
            text = self._bounded_text(raw)
            ok = bool(text) and not self._failed(text)
            detail = "" if ok else ("the Flipper reported an error" if text else "no data")
            self._audit("read", kind, ok, f"read for {duration:g}s", {"port": port.path})
            return SignalResult(
                ok,
                kind,
                text or f"no {kind} data received in {duration:g}s",
                detail,
                {"port": port.path, "seconds": duration},
            )
        except SignalError as exc:
            self._audit("read", kind, False, str(exc))
            return SignalResult(False, kind, "", str(exc))
        except (ValueError, TypeError):
            return SignalResult(False, kind, "", "seconds must be a number from 1 to 30")

    def scan_subghz(self, frequency: int, seconds: Optional[float] = None) -> SignalResult:
        try:
            freq = int(frequency)
        except (TypeError, ValueError):
            return SignalResult(False, "subghz", "", "frequency must be an integer in Hz")
        if not (300_000_000 <= freq <= 928_000_000):
            return SignalResult(
                False,
                "subghz",
                "",
                "frequency must be between 300000000 and 928000000 Hz (Flipper bands)",
            )
        try:
            duration = max(1.0, min(float(seconds or 8), 30.0))
        except (TypeError, ValueError):
            return SignalResult(False, "subghz", "", "seconds must be a number from 1 to 30")
        try:
            port, client = self._client()
            with client:
                raw = client.stream(f"subghz rx {freq} 0", duration)
            text = self._bounded_text(raw)
            ok = bool(text) and not self._failed(text)
            detail = "" if ok else ("the Flipper reported an error" if text else "no data")
            self._audit(
                "receive",
                "subghz",
                ok,
                f"receive {freq} Hz for {duration:g}s",
                {"port": port.path, "frequency": freq},
            )
            return SignalResult(
                ok,
                "subghz",
                text or f"no Sub-GHz output at {freq} Hz in {duration:g}s",
                detail,
                {"port": port.path, "frequency": freq, "seconds": duration},
            )
        except SignalError as exc:
            self._audit("receive", "subghz", False, str(exc), {"frequency": freq})
            return SignalResult(False, "subghz", "", str(exc))

    @staticmethod
    def _ir_args(protocol: str, address: str, command: str) -> Tuple[str, str, str]:
        protocol = str(protocol or "").strip()
        address = str(address or "").strip()
        command = str(command or "").strip()
        if not PROTOCOL_RE.fullmatch(protocol):
            raise SignalError("protocol must be a short name such as NEC or Samsung32")
        if not HEX_RE.fullmatch(address) or not HEX_RE.fullmatch(command):
            raise SignalError("address and command must be hexadecimal, up to 16 digits")
        return protocol, address, command

    def send_ir(
        self,
        protocol: str,
        address: str,
        command: str,
        confirm: Optional[Any] = None,
    ) -> SignalResult:
        try:
            protocol, address, command = self._ir_args(protocol, address, command)
        except SignalError as exc:
            return SignalResult(False, "ir_tx", "", str(exc))
        if self.policy == "read-only":
            self._audit("refused", "ir_tx", False, "read-only policy", [protocol, address, command])
            return SignalResult(
                False,
                "ir_tx",
                "",
                "IR transmit is disabled by the signal policy (read-only); use ask or auto deliberately",
            )
        if self.policy == "ask":
            if confirm is None:
                return SignalResult(False, "ir_tx", "", "this IR transmission needs human approval")
            try:
                approved = bool(confirm(f"Send IR {protocol} {address} {command}?"))
            except Exception as exc:  # a broken UI must not become an approval
                self._audit("refused", "ir_tx", False, f"confirmation failed: {exc}", [protocol, address, command])
                return SignalResult(False, "ir_tx", "", "the approval prompt failed; nothing was sent")
            if not approved:
                self._audit("refused", "ir_tx", False, "declined", [protocol, address, command])
                return SignalResult(False, "ir_tx", "", "the user declined")
        if not self._audit(
            "attempt", "ir_tx", True, "about to transmit", [protocol, address, command]
        ):
            return SignalResult(False, "ir_tx", "", "refusing to transmit because the audit log could not be opened")
        try:
            port, client = self._client()
            with client:
                output = client.command(f"ir tx {protocol} {address} {command}")
            rejected = any(
                marker in output.lower()
                for marker in ("unknown", "invalid", "error", "usage", "not supported")
            )
            ok = bool(output) and not rejected
            detail = ""
            if not ok:
                detail = (
                    "the Flipper rejected the IR command"
                    if rejected
                    else "the Flipper returned no confirmation"
                )
            self._audit("result", "ir_tx", ok, output or "no confirmation", {"port": port.path})
            return SignalResult(
                ok,
                "ir_tx",
                self._bounded_text(output or "no confirmation from the Flipper"),
                detail,
                {"port": port.path, "protocol": protocol, "address": address, "command": command},
            )
        except SignalError as exc:
            self._audit("result", "ir_tx", False, str(exc), [protocol, address, command])
            return SignalResult(False, "ir_tx", "", str(exc))


def load_signal_bridge(workspace: Path, config: Any, **kwargs: Any) -> SignalBridge:
    return SignalBridge(workspace, config, **kwargs)
