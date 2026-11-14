"""Local device control for an off-grid property or rig.

The model never talks to a protocool directly.  It reads a small registry of
typed points (a battery voltage, a relay, a light) and this module translates
those points into Home Assistant, HTTP or Modbus TCP requests.  Three rules are
deliberate:

* reads are safe and always allowed;
* writes are read-only unless the installation explicitly opts in;
* a write must have a declared safe range for numeric points and is audited
  before anything is sent.

This is not a safety-rated controller.  Keep pumps, heaters, locks and any
life-support load on their own interlocks; the AI is an operator convenience,
not a substitute for a fuse, breaker or thermostat.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .private import append_private_text

POLICIES = ("read-only", "ask", "auto")
ADAPTERS = ("homeassistant", "http", "modbus", "simulator")
BOOL_KINDS = ("binary_sensor", "switch", "lock", "cover")
NUMERIC_KINDS = ("sensor", "gauge", "number", "input_number", "climate")
MAX_RESPONSE = 1024 * 1024
_MISSING = object()


class DeviceError(Exception):
    """A user-facing device error.  Never include credentials in the message."""


def _slug(value: Any, what: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-.")
    if not text or ".." in text:
        raise DeviceError(f"{what} must be a short name using letters, digits, '.', '-' or '_'")
    return text


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("1", "on", "true", "yes", "open", "enable", "enabled"):
        return True
    if text in ("0", "off", "false", "no", "close", "closed", "disable", "disabled"):
        return False
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _get_path(data: Any, path: str) -> Any:
    """Read ``a.b.0`` from nested dict/list JSON without evaluating code."""
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _safe_value(value: Any) -> Any:
    """A bounded, JSON-safe representation for an audit record."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = str(value)
    return text[:240]


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


def _check_local_host(host: str, port: int, allow_public: bool) -> None:
    if allow_public:
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DeviceError(f"cannot resolve device host {host!r}: {exc}") from exc
    if not infos:
        raise DeviceError(f"device host {host!r} did not resolve")
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DeviceError(f"device host {host!r} resolved to an invalid address") from exc
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise DeviceError(
                f"device host {host!r} is public ({ip}); set allow_public=true only "
                "if you truly intend to reach it over the internet"
            )


def _validate_url(url: str, allow_public: bool) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DeviceError("device url must be an http:// or https:// URL with a host")
    if parsed.username or parsed.password:
        raise DeviceError("do not put credentials in a device URL; enter the token separately")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _check_local_host(parsed.hostname, port, allow_public)
    return parsed


@dataclass
class DevicePoint:
    name: str
    kind: str = "sensor"
    unit: str = ""
    writable: bool = False
    dangerous: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""
    config: Dict[str, Any] = field(default_factory=dict)

    def range_note(self) -> str:
        if self.minimum is None or self.maximum is None:
            return ""
        return f"{_format_value(self.minimum)}..{_format_value(self.maximum)}"

    def describe(self) -> str:
        access = "read/write" if self.writable else "read-only"
        bits = [self.kind, access]
        if self.unit:
            bits.insert(1, self.unit)
        note = self.range_note()
        if note:
            bits.append(f"range {note}")
        if self.dangerous:
            bits.append("dangerous")
        return f"{self.name} [{' · '.join(bits)}]" + (f" - {self.description}" if self.description else "")


@dataclass
class Device:
    name: str
    adapter: str
    points: Dict[str, DevicePoint]
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Reading:
    device: str
    point: str
    ok: bool
    value: Any = None
    unit: str = ""
    kind: str = ""
    detail: str = ""
    raw: Any = None
    at: float = field(default_factory=time.time)

    def text(self) -> str:
        label = f"{self.device}.{self.point}"
        if not self.ok:
            return f"{label}: ERROR {self.detail}"
        suffix = f" {self.unit}" if self.unit else ""
        detail = f" ({_format_value(self.detail)})" if self.detail else ""
        return f"{label}: {_format_value(self.value)}{suffix}{detail}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "device": self.device,
            "point": self.point,
            "ok": self.ok,
            "value": _safe_value(self.value),
            "unit": self.unit,
            "kind": self.kind,
            "detail": self.detail,
            "at": round(self.at, 3),
        }


@dataclass
class CommandResult:
    device: str
    point: str
    requested: Any
    ok: bool
    confirmed: bool = False
    observed: Any = None
    detail: str = ""

    def text(self) -> str:
        label = f"{self.device}.{self.point}"
        if not self.ok:
            return f"{label}: NOT SET - {self.detail}"
        state = "confirmed" if self.confirmed else "not confirmed"
        observed = f"observed {_format_value(self.observed)}" if self.confirmed else "no matching reading"
        return f"{label} <- {_format_value(self.requested)}: {state} ({observed})" + (
            f" - {self.detail}" if self.detail else ""
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "device": self.device,
            "point": self.point,
            "requested": _safe_value(self.requested),
            "ok": self.ok,
            "confirmed": self.confirmed,
            "observed": _safe_value(self.observed),
            "detail": self.detail,
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeviceError(f"device endpoint attempted an HTTP redirect ({code})")


class _HttpClient:
    """Small JSON HTTP client with a local-host guard and no redirects."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        token_env: str = "",
        token_file: str = "",
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 5.0,
        allow_public: bool = False,
        max_response: int = MAX_RESPONSE,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.base = _validate_url(self.base_url, allow_public)
        self.token = str(token or "")
        self.token_env = token_env
        self.token_file = str(token_file or "")
        self.auth_header = auth_header or "Authorization"
        self.auth_prefix = auth_prefix
        self.headers = dict(headers or {})
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.allow_public = allow_public
        try:
            self.max_response = max(64 * 1024, min(int(max_response), 16 * MAX_RESPONSE))
        except (TypeError, ValueError):
            self.max_response = MAX_RESPONSE
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _target(self, path: str) -> str:
        raw = str(path or "/")
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme or parsed.netloc or raw.startswith("//"):
            raise DeviceError("device request paths must be relative to the configured base URL")
        target = urllib.parse.urljoin(self.base_url + "/", raw.lstrip("/"))
        check = urllib.parse.urlsplit(target)
        if check.netloc != self.base.netloc:
            raise DeviceError("device request escaped the configured base URL")
        return target

    def request(
        self, method: str, path: str, body: Any = None
    ) -> Tuple[int, Any]:
        url = self._target(path)
        headers = {
            "Accept": "application/json",
            "User-Agent": "OffGrid-device-layer/1",
        }
        headers.update(self.headers)
        token = self.token
        if not token and self.token_env:
            token = os.environ.get(self.token_env, "")
            if not token:
                raise DeviceError(f"device token environment variable {self.token_env} is not set")
        if not token and self.token_file:
            try:
                token = Path(self.token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise DeviceError("the private device token file could not be read") from exc
            if not token:
                raise DeviceError("the private device token file is empty")
        if token:
            headers[self.auth_header] = f"{self.auth_prefix}{token}"
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                data = str(body).encode("utf-8")
                headers["Content-Type"] = "text/plain; charset=utf-8"
        request = urllib.request.Request(
            url, data=data, headers=headers, method=str(method or "GET").upper()
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(self.max_response + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raise DeviceError(
                f"{request.method} {path} returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeviceError(f"{request.method} {path} failed: {exc}") from exc
        if len(payload) > self.max_response:
            raise DeviceError(f"{request.method} {path} returned too much data")
        if not payload:
            return status, {}
        try:
            return status, json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DeviceError(f"{request.method} {path} did not return JSON") from exc

    def json(self, method: str, path: str, body: Any = None) -> Any:
        _status, data = self.request(method, path, body)
        return data


class _Adapter:
    def __init__(self, manager: "DeviceManager", device: Device) -> None:
        self.manager = manager
        self.device = device

    def read(self, point: DevicePoint) -> Reading:
        raise NotImplementedError

    def write(self, point: DevicePoint, value: Any) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return


class SimulatorAdapter(_Adapter):
    def __init__(self, manager: "DeviceManager", device: Device) -> None:
        super().__init__(manager, device)
        self.values: Dict[str, Any] = dict(device.config.get("values") or {})
        for name, point in device.points.items():
            if "initial" in point.config and name not in self.values:
                self.values[name] = point.config["initial"]

    def read(self, point: DevicePoint) -> Reading:
        if point.name not in self.values:
            raise DeviceError(f"simulator has no value for {point.name}")
        raw = self.values[point.name]
        return Reading(
            self.device.name,
            point.name,
            True,
            value=raw,
            unit=point.unit,
            kind=point.kind,
            raw=raw,
            detail="simulator",
        )

    def write(self, point: DevicePoint, value: Any) -> None:
        self.values[point.name] = value


class HomeAssistantAdapter(_Adapter):
    ON_SERVICE = {
        "light": "turn_on",
        "switch": "turn_on",
        "fan": "turn_on",
        "input_boolean": "turn_on",
        "climate": "turn_on",
        "cover": "open_cover",
        "lock": "unlock",
        "valve": "open_valve",
    }
    OFF_SERVICE = {
        "light": "turn_off",
        "switch": "turn_off",
        "fan": "turn_off",
        "input_boolean": "turn_off",
        "climate": "turn_off",
        "cover": "close_cover",
        "lock": "lock",
        "valve": "close_valve",
    }

    def __init__(self, manager: "DeviceManager", device: Device) -> None:
        super().__init__(manager, device)
        token_file = str(device.config.get("token_file") or "")
        if token_file and not Path(token_file).is_absolute():
            token_file = str(manager.root / token_file)
        self.client = _HttpClient(
            str(device.config.get("url") or ""),
            token_env=str(device.config.get("token_env") or ""),
            token_file=token_file,
            timeout=manager.timeout,
            allow_public=manager.allow_public,
        )
        self.wait = max(0.0, min(float(device.config.get("wait", 0.2)), 5.0))

    def _entity(self, point: DevicePoint) -> str:
        entity = str(point.config.get("entity") or "").strip()
        if "." not in entity:
            raise DeviceError(f"{self.device.name}.{point.name} needs a Home Assistant entity")
        return entity

    def read(self, point: DevicePoint) -> Reading:
        entity = self._entity(point)
        data = self.client.json("GET", f"/api/states/{urllib.parse.quote(entity, safe='')}")
        if not isinstance(data, dict):
            raise DeviceError(f"Home Assistant returned an invalid state for {entity}")
        state = data.get("state")
        attributes = data.get("attributes") or {}
        attribute = str(point.config.get("attribute") or "")
        raw = _get_path(attributes, attribute) if attribute else state
        if raw is _MISSING or raw is None:
            raise DeviceError(f"{entity} has no value for {attribute or 'state'}")
        value = self._value(raw)
        detail = str(state) if not attribute else f"{state}, {attribute}={_format_value(value)}"
        return Reading(
            self.device.name,
            point.name,
            True,
            value=value,
            unit=point.unit,
            kind=point.kind,
            detail=detail[:160],
            raw=state,
        )

    @staticmethod
    def _value(raw: Any) -> Any:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw
        text = str(raw)
        if text.lower() in ("on", "off"):
            return text.lower() == "on"
        if text.lower() in ("unknown", "unavailable", "none", ""):
            raise DeviceError(f"Home Assistant entity is {text or 'empty'}")
        number = _as_number(text)
        return number if number is not None else text

    def _service(self, point: DevicePoint, value: Any) -> Tuple[str, Dict[str, Any]]:
        entity = self._entity(point)
        domain = entity.split(".", 1)[0]
        data = dict(point.config.get("data") or {})
        if isinstance(value, bool):
            service = point.config.get("on_service" if value else "off_service")
            if not service:
                service = (self.ON_SERVICE if value else self.OFF_SERVICE).get(
                    domain, "turn_on" if value else "turn_off"
                )
            return str(service), data
        service = str(point.config.get("value_service") or "")
        field_name = str(point.config.get("value_field") or "")
        if not service or not field_name:
            raise DeviceError(
                f"{self.device.name}.{point.name} needs value_service and value_field for a numeric write"
            )
        data[field_name] = value
        return service, data

    def write(self, point: DevicePoint, value: Any) -> None:
        entity = self._entity(point)
        domain = entity.split(".", 1)[0]
        service, extra = self._service(point, value)
        body = {"entity_id": entity}
        body.update(extra)
        self.client.json("POST", f"/api/services/{domain}/{service}", body)
        if self.wait:
            time.sleep(self.wait)


class HttpAdapter(_Adapter):
    def __init__(self, manager: "DeviceManager", device: Device) -> None:
        super().__init__(manager, device)
        token_file = str(device.config.get("token_file") or "")
        if token_file and not Path(token_file).is_absolute():
            token_file = str(manager.root / token_file)
        self.client = _HttpClient(
            str(device.config.get("url") or ""),
            token_env=str(device.config.get("token_env") or ""),
            token_file=token_file,
            auth_header=str(device.config.get("auth_header") or "Authorization"),
            auth_prefix=str(device.config.get("auth_prefix") or "Bearer "),
            headers=device.config.get("headers") or {},
            timeout=manager.timeout,
            allow_public=manager.allow_public,
        )

    def read(self, point: DevicePoint) -> Reading:
        spec = point.config.get("read")
        if not isinstance(spec, dict):
            raise DeviceError(f"{self.device.name}.{point.name} has no read request")
        method = str(spec.get("method") or "GET").upper()
        data = self.client.json(method, str(spec.get("path") or "/"), spec.get("body"))
        raw = _get_path(data, str(spec.get("value") or ""))
        if raw is _MISSING or raw is None:
            raise DeviceError(f"{self.device.name}.{point.name} read value was not in the response")
        return Reading(
            self.device.name,
            point.name,
            True,
            value=raw,
            unit=point.unit,
            kind=point.kind,
            detail="http",
            raw=raw,
        )

    @staticmethod
    def _template(value: Any, replacement: Any) -> Any:
        if isinstance(value, dict):
            return {key: HttpAdapter._template(item, replacement) for key, item in value.items()}
        if isinstance(value, list):
            return [HttpAdapter._template(item, replacement) for item in value]
        if isinstance(value, str):
            if value == "{value}":
                return replacement
            return value.replace("{value}", _format_value(replacement))
        return value

    def write(self, point: DevicePoint, value: Any) -> None:
        spec = point.config.get("write")
        if not isinstance(spec, dict):
            raise DeviceError(f"{self.device.name}.{point.name} has no write request")
        path = str(spec.get("path") or "/").replace(
            "{value}", urllib.parse.quote(_format_value(value), safe="")
        )
        body = self._template(spec.get("body"), value)
        self.client.request(str(spec.get("method") or "POST").upper(), path, body)


class ModbusAdapter(_Adapter):
    def __init__(self, manager: "DeviceManager", device: Device) -> None:
        super().__init__(manager, device)
        self.host = str(device.config.get("host") or "")
        self.port = int(device.config.get("port") or 502)
        self.unit = int(device.config.get("unit") or 1)
        if not self.host:
            raise DeviceError("modbus device needs a host")
        _check_local_host(self.host, self.port, manager.allow_public)

    def _exchange(self, pdu: bytes) -> bytes:
        transaction = secrets.randbits(16)
        packet = struct.pack(">HHHB", transaction, 0, len(pdu) + 1, self.unit) + pdu
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.manager.timeout
            ) as sock:
                sock.sendall(packet)
                header = self._recv_exact(sock, 7)
                tid, protocol, length, unit = struct.unpack(">HHHB", header)
                if protocol != 0:
                    raise DeviceError("Modbus response had an invalid protocol id")
                if tid != transaction:
                    raise DeviceError("Modbus response transaction did not match")
                if unit != self.unit:
                    raise DeviceError("Modbus response unit did not match")
                if length < 2 or length > 260:
                    raise DeviceError("Modbus response length was invalid")
                body = self._recv_exact(sock, length - 1)
        except DeviceError:
            raise
        except (OSError, TimeoutError) as exc:
            raise DeviceError(f"Modbus {self.host}:{self.port} failed: {exc}") from exc
        if not body:
            raise DeviceError("Modbus returned an empty response")
        if body[0] & 0x80:
            raise DeviceError(f"Modbus exception code {body[1] if len(body) > 1 else '?'}")
        return body

    @staticmethod
    def _recv_exact(sock: socket.socket, count: int) -> bytes:
        chunks = []
        remaining = count
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise DeviceError("Modbus connection closed before the response was complete")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _area(point: DevicePoint) -> str:
        return str(point.config.get("area") or "holding").lower()

    @staticmethod
    def _data_type(point: DevicePoint) -> str:
        return str(point.config.get("data_type") or "u16").lower()

    def read(self, point: DevicePoint) -> Reading:
        area = self._area(point)
        address = int(point.config.get("register") or point.config.get("address") or 0)
        if area in ("coil", "discrete"):
            if address < 0 or address > 65535:
                raise DeviceError("Modbus bit address is out of range")
            function = 1 if area == "coil" else 2
            body = self._exchange(struct.pack(">BHH", function, address, 1))
            if len(body) < 3:
                raise DeviceError("Modbus bit response was short")
            raw = bool(body[2] & 1)
        elif area in ("holding", "input"):
            data_type = self._data_type(point)
            count = 2 if data_type in ("u32", "s32", "f32") else 1
            if address < 0 or address + count - 1 > 65535:
                raise DeviceError("Modbus register address is out of range")
            function = 3 if area == "holding" else 4
            body = self._exchange(struct.pack(">BHH", function, address, count))
            if len(body) < 2:
                raise DeviceError("Modbus register response was short")
            byte_count = body[1]
            payload = body[2 : 2 + byte_count]
            if len(payload) < count * 2:
                raise DeviceError("Modbus register response was incomplete")
            raw = self._unpack(data_type, payload[: count * 2])
        else:
            raise DeviceError(f"unknown Modbus area {area!r}")
        scale = float(point.config.get("scale", 1) or 1)
        if scale == 0:
            raise DeviceError("Modbus scale cannot be zero")
        offset = float(point.config.get("offset", 0) or 0)
        value = raw * scale + offset if isinstance(raw, (int, float)) and not isinstance(raw, bool) else raw
        return Reading(
            self.device.name,
            point.name,
            True,
            value=value,
            unit=point.unit,
            kind=point.kind,
            detail=f"modbus {area}[{address}] raw={raw}",
            raw=raw,
        )

    @staticmethod
    def _unpack(data_type: str, payload: bytes) -> Any:
        if data_type == "u16":
            return struct.unpack(">H", payload)[0]
        if data_type == "s16":
            return struct.unpack(">h", payload)[0]
        if data_type == "u32":
            return struct.unpack(">I", payload)[0]
        if data_type == "s32":
            return struct.unpack(">i", payload)[0]
        if data_type == "f32":
            return struct.unpack(">f", payload)[0]
        raise DeviceError(f"unsupported Modbus data_type {data_type!r}")

    def write(self, point: DevicePoint, value: Any) -> None:
        area = self._area(point)
        address = int(point.config.get("register") or point.config.get("address") or 0)
        if not (0 <= address <= 65535):
            raise DeviceError("Modbus address is out of range")
        if area == "coil":
            if not isinstance(value, bool):
                raise DeviceError("a Modbus coil write needs on/off")
            body = self._exchange(
                struct.pack(">BHH", 5, address, 0xFF00 if value else 0x0000)
            )
            if len(body) < 5:
                raise DeviceError("Modbus coil write response was short")
            return
        if area != "holding":
            raise DeviceError("only holding registers and coils are writable")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DeviceError("a Modbus register write needs a number")
        scale = float(point.config.get("scale", 1) or 1)
        if scale == 0:
            raise DeviceError("Modbus scale cannot be zero")
        offset = float(point.config.get("offset", 0) or 0)
        raw = int(round((float(value) - offset) / scale))
        data_type = self._data_type(point)
        if data_type in ("u16", "s16"):
            if data_type == "u16" and not (0 <= raw <= 65535):
                raise DeviceError("Modbus u16 value is out of range")
            if data_type == "s16" and not (-32768 <= raw <= 32767):
                raise DeviceError("Modbus s16 value is out of range")
            body = self._exchange(struct.pack(">BHH", 6, address, raw & 0xFFFF))
            if len(body) < 5:
                raise DeviceError("Modbus register write response was short")
            return
        if data_type == "u32":
            if not (0 <= raw <= 0xFFFFFFFF):
                raise DeviceError("Modbus u32 value is out of range")
            packed = struct.pack(">I", raw)
        elif data_type == "s32":
            if not (-2147483648 <= raw <= 2147483647):
                raise DeviceError("Modbus s32 value is out of range")
            packed = struct.pack(">i", raw)
        elif data_type == "f32":
            packed = struct.pack(">f", (float(value) - offset) / scale)
        else:
            raise DeviceError(f"unsupported Modbus data_type {data_type!r}")
        count = 2
        pdu = struct.pack(">BHHB", 16, address, count, count * 2) + packed
        body = self._exchange(pdu)
        if len(body) < 5:
            raise DeviceError("Modbus register write response was short")


def _adapter_class(name: str):
    return {
        "simulator": SimulatorAdapter,
        "homeassistant": HomeAssistantAdapter,
        "http": HttpAdapter,
        "modbus": ModbusAdapter,
    }.get(name)


class DeviceManager:
    """Loads ``.offgrid/devices.json`` and enforces the write policy."""

    def __init__(
        self,
        workspace: Path,
        config: Any = None,
        approve: Optional[Any] = None,
        log: Optional[Any] = None,
    ) -> None:
        self.root = Path(workspace)
        self.cfg = config
        self.approve = approve
        self.log = log or (lambda _message: None)
        configured_path = str(getattr(config, "devices_file", ".offgrid/devices.json") or ".offgrid/devices.json")
        self.path = Path(configured_path).expanduser()
        if not self.path.is_absolute():
            self.path = self.root / self.path
        self.devices: Dict[str, Device] = {}
        self.warnings: List[str] = []
        self.policy = "read-only"
        self.allow_dangerous = False
        self.allow_public = False
        self.timeout = 5.0
        configured_audit = str(getattr(config, "devices_audit", ".offgrid/devices-audit.jsonl") or ".offgrid/devices-audit.jsonl")
        self.audit_path = Path(configured_audit).expanduser()
        if not self.audit_path.is_absolute():
            self.audit_path = self.root / self.audit_path
        self.loaded = False
        self._adapters: Dict[str, _Adapter] = {}
        self.load()

    # -- loading ---------------------------------------------------------
    def load(self) -> None:
        self.loaded = True
        if not self.path.is_file():
            # A missing registry is normal: most coding sessions have no devices.
            # The device tools give the setup hint when they are actually used.
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.warnings.append(f"could not read {self.path}: {exc}")
            return
        if not isinstance(raw, dict):
            self.warnings.append("device registry must be a JSON object")
            return
        configured_policy = str(raw.get("policy") or "read-only").lower()
        if configured_policy not in POLICIES:
            self.warnings.append(
                f"device policy {configured_policy!r} is not one of {', '.join(POLICIES)}; using read-only"
            )
            configured_policy = "read-only"
        cli_policy = str(getattr(self.cfg, "device_policy", "") or "").lower()
        self.policy = cli_policy if cli_policy in POLICIES else configured_policy
        self.allow_dangerous = bool(
            raw.get("allow_dangerous", False)
            or getattr(self.cfg, "allow_dangerous_devices", False)
        )
        self.allow_public = bool(raw.get("allow_public", False))
        try:
            self.timeout = max(1.0, min(float(getattr(self.cfg, "device_timeout", 0) or raw.get("timeout", 5)), 30.0))
        except (TypeError, ValueError):
            self.timeout = 5.0
        audit = str(raw.get("audit") or "").strip()
        if audit:
            self.audit_path = Path(audit).expanduser()
            if not self.audit_path.is_absolute():
                self.audit_path = self.root / self.audit_path

        entries = raw.get("devices", [])
        if isinstance(entries, dict):
            entries = [dict(value, name=key) for key, value in entries.items()]
        if not isinstance(entries, list):
            self.warnings.append("device registry `devices` must be a list or object")
            return
        for entry in entries:
            try:
                device = self._parse_device(entry)
            except DeviceError as exc:
                self.warnings.append(str(exc))
                continue
            if device.name in self.devices:
                self.warnings.append(f"duplicate device {device.name!r} ignored")
                continue
            self.devices[device.name] = device

    def _parse_device(self, raw: Any) -> Device:
        if not isinstance(raw, dict):
            raise DeviceError("each device entry must be an object")
        name = _slug(raw.get("name"), "device name")
        adapter = str(raw.get("adapter") or "").strip().lower()
        if adapter not in ADAPTERS:
            raise DeviceError(
                f"device {name!r} has adapter {adapter!r}; use one of {', '.join(ADAPTERS)}"
            )
        points_raw = raw.get("points")
        if isinstance(points_raw, dict):
            points_raw = [dict(value, name=key) for key, value in points_raw.items()]
        if not isinstance(points_raw, list) or not points_raw:
            raise DeviceError(f"device {name!r} needs at least one point")
        points: Dict[str, DevicePoint] = {}
        for entry in points_raw:
            point = self._parse_point(name, adapter, entry)
            if point.name in points:
                raise DeviceError(f"device {name!r} has duplicate point {point.name!r}")
            points[point.name] = point
        if adapter in ("homeassistant", "http") and not str(raw.get("url") or "").strip():
            raise DeviceError(f"device {name!r} needs a url")
        if adapter == "modbus" and not str(raw.get("host") or "").strip():
            raise DeviceError(f"device {name!r} needs a host")
        return Device(name, adapter, points, dict(raw))

    def _parse_point(self, device: str, adapter: str, raw: Any) -> DevicePoint:
        if not isinstance(raw, dict):
            raise DeviceError(f"device {device!r} has a point that is not an object")
        name = _slug(raw.get("name"), "point name")
        kind = str(raw.get("kind") or "").strip().lower()
        entity = str(raw.get("entity") or "").strip()
        if not kind and adapter == "homeassistant" and "." in entity:
            kind = entity.split(".", 1)[0]
        kind = kind or "sensor"
        if kind not in (
            "sensor", "gauge", "binary_sensor", "switch", "light", "fan",
            "climate", "cover", "lock", "number", "input_number", "valve",
        ):
            raise DeviceError(f"{device}.{name} has unknown kind {kind!r}")
        try:
            minimum = float(raw["min"]) if "min" in raw else None
            maximum = float(raw["max"]) if "max" in raw else None
        except (TypeError, ValueError) as exc:
            raise DeviceError(f"{device}.{name} min/max must be numbers") from exc
        if minimum is not None and maximum is not None and minimum > maximum:
            raise DeviceError(f"{device}.{name} min is greater than max")
        writable = bool(raw.get("writable", False))
        # Door locks, covers, valves and HVAC can hurt someone or move a
        # machine.  They stay blocked until the installation explicitly says
        # that its physical interlocks and procedures are in place.
        dangerous = bool(
            raw.get("dangerous", kind in ("lock", "cover", "valve", "climate"))
        )
        if adapter == "homeassistant":
            if "." not in entity:
                raise DeviceError(f"{device}.{name} needs a Home Assistant entity")
        elif adapter == "http":
            if not isinstance(raw.get("read"), dict):
                raise DeviceError(f"{device}.{name} needs a read request")
            if writable and not isinstance(raw.get("write"), dict):
                raise DeviceError(f"{device}.{name} is writable but has no write request")
        elif adapter == "modbus":
            area = str(raw.get("area") or "holding").lower()
            if area not in ("holding", "input", "coil", "discrete"):
                raise DeviceError(f"{device}.{name} has unknown Modbus area {area!r}")
            data_type = str(raw.get("data_type") or "u16").lower()
            if data_type not in ("u16", "s16", "u32", "s32", "f32"):
                raise DeviceError(f"{device}.{name} has unsupported data_type {data_type!r}")
            if writable and area not in ("holding", "coil"):
                raise DeviceError(f"{device}.{name} is writable but its Modbus area is read-only")
            try:
                register = int(raw.get("register", raw.get("address", 0)))
            except (TypeError, ValueError) as exc:
                raise DeviceError(f"{device}.{name} has an invalid register") from exc
            if register < 0 or register > 65535:
                raise DeviceError(f"{device}.{name} register is out of range")
        elif adapter == "simulator":
            pass
        if writable and kind in NUMERIC_KINDS and (minimum is None or maximum is None):
            raise DeviceError(
                f"{device}.{name} is a writable numeric point and needs both min and max"
            )
        return DevicePoint(
            name=name,
            kind=kind,
            unit=str(raw.get("unit") or ""),
            writable=writable,
            dangerous=dangerous,
            minimum=minimum,
            maximum=maximum,
            description=str(raw.get("description") or "")[:160],
            config=dict(raw),
        )

    # -- inventory -------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.devices)

    def names(self) -> List[str]:
        return sorted(self.devices)

    def list_text(self) -> str:
        if not self.devices:
            return (
                "no local devices are configured. Create "
                f"{self.path} from docs/DEVICES.md, then retry."
            )
        lines = [
            f"{len(self.devices)} device(s); write policy: {self.policy}"
            + ("; dangerous writes allowed" if self.allow_dangerous else ""),
        ]
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        for device in self.devices.values():
            lines.append(f"{device.name} [{device.adapter}]")
            lines.extend(f"  {point.describe()}" for point in device.points.values())
        if self.policy == "read-only":
            lines.append("writes are disabled; set policy to ask or auto only after testing in read-only mode")
        return "\n".join(lines)

    def list_data(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "path": str(self.path),
            "allow_dangerous": self.allow_dangerous,
            "allow_public": self.allow_public,
            "devices": [
                {
                    "name": device.name,
                    "adapter": device.adapter,
                    "points": [
                        {
                            "name": point.name,
                            "kind": point.kind,
                            "unit": point.unit,
                            "writable": point.writable,
                            "dangerous": point.dangerous,
                            "min": point.minimum,
                            "max": point.maximum,
                            "description": point.description,
                        }
                        for point in device.points.values()
                    ],
                }
                for device in self.devices.values()
            ],
        }

    def _find(self, device_name: str, point_name: str = "") -> Tuple[Device, Optional[DevicePoint]]:
        name = _slug(device_name, "device name")
        device = self.devices.get(name)
        if device is None:
            available = ", ".join(self.names()) or "(none)"
            raise DeviceError(f"unknown device {name!r}. Configured: {available}")
        if not point_name:
            return device, None
        point_key = _slug(point_name, "point name")
        point = device.points.get(point_key)
        if point is None:
            available = ", ".join(device.points) or "(none)"
            raise DeviceError(f"unknown point {name}.{point_key}; available: {available}")
        return device, point

    # -- audit -----------------------------------------------------------
    def _audit(self, action: str, device: str, point: str, value: Any, ok: bool, detail: str, source: str) -> bool:
        record = {
            "t": round(time.time(), 3),
            "action": action,
            "source": source,
            "device": device,
            "point": point,
            "value": _safe_value(value),
            "ok": bool(ok),
            "detail": str(detail)[:300],
        }
        try:
            append_private_text(self.audit_path, json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            self.log(f"device audit log could not be written: {exc}")
            return False

    # -- reads and writes ------------------------------------------------
    def read(self, device_name: str = "", point_name: str = "") -> List[Reading]:
        if not self.devices:
            raise DeviceError(
                f"no local devices are configured; create {self.path} from docs/DEVICES.md"
            )
        targets: List[Tuple[Device, Optional[DevicePoint]]] = []
        if device_name:
            targets.append(self._find(device_name, point_name))
        else:
            if point_name:
                raise DeviceError("read needs a device when a point is named")
            targets.extend((device, None) for device in self.devices.values())
        readings: List[Reading] = []
        for device, selected in targets:
            points = [selected] if selected is not None else list(device.points.values())
            try:
                adapter = self._adapter(device)
            except DeviceError as exc:
                readings.extend(
                    Reading(device.name, point.name, False, unit=point.unit, kind=point.kind, detail=str(exc))
                    for point in points
                )
                continue
            for point in points:
                try:
                    readings.append(adapter.read(point))
                except DeviceError as exc:
                    readings.append(
                        Reading(device.name, point.name, False, unit=point.unit, kind=point.kind, detail=str(exc))
                    )
                except Exception as exc:  # a broken adapter must not kill the team
                    readings.append(
                        Reading(
                            device.name,
                            point.name,
                            False,
                            unit=point.unit,
                            kind=point.kind,
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
        return readings

    @staticmethod
    def readings_text(readings: Iterable[Reading]) -> str:
        return "\n".join(item.text() for item in readings) or "(no readings)"

    def _coerce(self, point: DevicePoint, value: Any) -> Any:
        boolean = _as_bool(value)
        if boolean is not None and (point.kind in BOOL_KINDS or point.kind in ("light", "fan", "valve")):
            return boolean
        number = _as_number(value)
        if number is None:
            raise DeviceError(
                f"{point.name} needs a number or on/off; got {value!r}"
            )
        if point.minimum is None or point.maximum is None:
            raise DeviceError(
                f"{point.name} has no declared safe min/max; refusing a numeric write"
            )
        if number < point.minimum or number > point.maximum:
            raise DeviceError(
                f"{point.name} value {_format_value(number)} is outside the safe range "
                f"{point.range_note()}"
            )
        return number

    def _adapter(self, device: Device) -> _Adapter:
        adapter = self._adapters.get(device.name)
        if adapter is None:
            cls = _adapter_class(device.adapter)
            if cls is None:
                raise DeviceError(f"device {device.name!r} has no adapter")
            adapter = cls(self, device)
            self._adapters[device.name] = adapter
        return adapter

    def set(
        self,
        device_name: str,
        point_name: str,
        value: Any,
        *,
        source: str = "agent",
        confirm: Optional[Any] = None,
    ) -> CommandResult:
        device, point = self._find(device_name, point_name)
        assert point is not None
        label = f"{device.name}.{point.name}"
        if not point.writable:
            self._audit("refused", device.name, point.name, value, False, "read-only point", source)
            return CommandResult(device.name, point.name, value, False, detail="that point is read-only")
        if point.dangerous and not self.allow_dangerous:
            self._audit("refused", device.name, point.name, value, False, "dangerous point disabled", source)
            return CommandResult(
                device.name,
                point.name,
                value,
                False,
                detail=(
                    "that point is marked dangerous (lock, cover, heater, etc.); "
                    "set allow_dangerous=true only after fitting physical interlocks"
                ),
            )
        if self.policy == "read-only":
            self._audit("refused", device.name, point.name, value, False, "read-only policy", source)
            return CommandResult(
                device.name,
                point.name,
                value,
                False,
                detail="writes are disabled by the device registry policy (read-only)",
            )
        if self.policy == "ask":
            if confirm is None:
                self._audit("refused", device.name, point.name, value, False, "no approver", source)
                return CommandResult(
                    device.name,
                    point.name,
                    value,
                    False,
                    detail="this write needs human approval and nobody is at the terminal",
                )
            if not confirm(f"Set {label} to {value}?"):
                self._audit("refused", device.name, point.name, value, False, "declined", source)
                return CommandResult(device.name, point.name, value, False, detail="the user declined")
        try:
            coerced = self._coerce(point, value)
        except DeviceError as exc:
            self._audit("refused", device.name, point.name, value, False, str(exc), source)
            return CommandResult(device.name, point.name, value, False, detail=str(exc))

        if not self._audit("attempt", device.name, point.name, coerced, True, "about to write", source):
            return CommandResult(
                device.name,
                point.name,
                value,
                False,
                detail="refusing to write because the audit log could not be opened",
            )
        try:
            self._adapter(device).write(point, coerced)
        except DeviceError as exc:
            self._audit("result", device.name, point.name, coerced, False, str(exc), source)
            return CommandResult(device.name, point.name, value, False, detail=str(exc))
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            self._audit("result", device.name, point.name, coerced, False, text, source)
            return CommandResult(device.name, point.name, value, False, detail=text)

        observed = None
        confirmed = False
        detail = "command accepted"
        try:
            observed = self._adapter(device).read(point).value
            confirmed = self._same(observed, coerced)
            if not confirmed:
                detail = "command accepted, but the device reports a different state"
        except DeviceError as exc:
            detail = f"command accepted; verification failed: {exc}"
        if not self._audit("result", device.name, point.name, coerced, True, detail, source):
            detail += "; the result could not be written to the audit log"
        return CommandResult(
            device.name, point.name, value, True, confirmed=confirmed, observed=observed, detail=detail
        )

    @staticmethod
    def _same(observed: Any, requested: Any) -> bool:
        if isinstance(observed, bool) or isinstance(requested, bool):
            return bool(observed) == bool(requested)
        left = _as_number(observed)
        right = _as_number(requested)
        if left is not None and right is not None:
            return abs(left - right) < 1e-6
        return str(observed).strip().lower() == str(requested).strip().lower()

    # -- diagnostics -----------------------------------------------------
    def diagnose(self, device_name: str = "") -> Dict[str, Any]:
        readings = self.read(device_name)
        checked = len(readings)
        errors = 0
        warnings = 0
        for reading in readings:
            point = self.devices.get(reading.device, Device("", "", {})).points.get(reading.point)
            if not reading.ok:
                errors += 1
                continue
            if point is not None and isinstance(reading.value, (int, float)) and not isinstance(reading.value, bool):
                if point.minimum is not None and reading.value < point.minimum:
                    warnings += 1
                    reading.detail = f"below safe range {point.range_note()}"
                elif point.maximum is not None and reading.value > point.maximum:
                    warnings += 1
                    reading.detail = f"above safe range {point.range_note()}"
        ok = errors == 0
        summary = (
            f"{checked} point(s) checked, {errors} error(s), {warnings} out-of-range warning(s)"
        )
        self._audit("diagnose", device_name or "*", "", "", ok, summary, "diagnose")
        return {
            "ok": ok,
            "summary": summary,
            "errors": errors,
            "warnings": warnings,
            "readings": [reading.as_dict() for reading in readings],
            "text": "\n".join([reading.text() for reading in readings] + [summary]),
        }

    def close(self) -> None:
        for adapter in self._adapters.values():
            try:
                adapter.close()
            except Exception:
                pass
        self._adapters.clear()


def load_device_manager(workspace: Path, config: Any, **kwargs: Any) -> DeviceManager:
    return DeviceManager(workspace, config, **kwargs)
