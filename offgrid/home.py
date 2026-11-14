"""Guided Home Assistant setup for OffGrid Studio.

The device layer is deliberately explicit, but asking a non-programmer to
write JSON and export environment variables is not a setup flow.  This module
does the small, bounded amount of work behind the Studio form:

* test a local Home Assistant URL and long-lived token;
* show the entities OffGrid can safely understand;
* write the device registry and a private token file itself.

It does not discover or control locks, covers, valves or climate devices from
the guided flow.  Those need the explicit, audited device configuration in
``docs/DEVICES.md``.  Reads are the default; control is a separate opt-in.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .devices import MAX_RESPONSE, DeviceError, DeviceManager, _HttpClient, _slug

DEFAULT_URL = "http://homeassistant.local:8123"
DEFAULT_NAME = "home"
TOKEN_DIR = ".offgrid"
MAX_ENTITIES = 500
SAFE_DOMAINS = (
    "light",
    "switch",
    "fan",
    "input_boolean",
    "binary_sensor",
    "sensor",
    "number",
    "input_number",
)
GROUP_ORDER = ("Lights & switches", "Sensors", "Numbers")
STATES_WITHOUT_VALUE = ("unknown", "unavailable", "none", "")


class HomeSetupError(Exception):
    """A guided setup step failed in a way the browser can explain."""


def _normalise_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = DEFAULT_URL
    if "://" not in raw:
        raw = "http://" + raw
    raw = raw.rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HomeSetupError(
            "Enter the Home Assistant address, for example http://homeassistant.local:8123"
        )
    if len(raw) > 2048:
        raise HomeSetupError("that Home Assistant address is too long")
    return raw


def _clean_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        raise HomeSetupError(
            "Create a long-lived access token in Home Assistant: Profile → Security → "
            "Long-lived access tokens → Create token."
        )
    if len(token) > 8192:
        raise HomeSetupError("that access token is too long")
    return token


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _friendly_name(entity_id: str, attributes: Dict[str, Any]) -> str:
    name = str(attributes.get("friendly_name") or "").strip()
    if not name:
        name = entity_id.split(".", 1)[-1].replace("_", " ").strip().title()
    return name[:100] or entity_id


def _point_spec(entity_id: str, state: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one Home Assistant state into a safe OffGrid point."""
    domain = entity_id.split(".", 1)[0].lower()
    if domain not in SAFE_DOMAINS:
        return {}
    available = str(state or "").strip().lower() not in STATES_WITHOUT_VALUE
    unit = str(attributes.get("unit_of_measurement") or "").strip()[:24]
    kind = domain
    writable = domain in ("light", "switch", "fan", "input_boolean")
    minimum = maximum = None

    if domain == "input_boolean":
        kind = "switch"
    elif domain == "binary_sensor":
        kind = "binary_sensor"
        writable = False
    elif domain == "sensor":
        state_number = _number(state)
        kind = "gauge" if unit or state_number is not None else "sensor"
        writable = False
    elif domain in ("number", "input_number"):
        minimum = _number(attributes.get("min"))
        maximum = _number(attributes.get("max"))
        if minimum is None or maximum is None or minimum > maximum:
            # Without a declared safe range OffGrid cannot write this value.
            kind = "gauge" if unit or _number(state) is not None else "sensor"
            writable = False
            minimum = maximum = None
        else:
            kind = "number"
            writable = True
    elif domain in ("light", "switch", "fan"):
        kind = domain

    if not available:
        writable = False

    point: Dict[str, Any] = {
        "kind": kind,
        "entity": entity_id,
        "writable": writable,
    }
    if unit:
        point["unit"] = unit
    if minimum is not None and maximum is not None:
        point["min"] = minimum
        point["max"] = maximum
    if kind == "number":
        point["value_service"] = "set_value"
        point["value_field"] = "value"
    return point


def _group_for(kind: str) -> str:
    if kind in ("light", "switch", "fan"):
        return "Lights & switches"
    if kind == "number":
        return "Numbers"
    return "Sensors"


def _entity_options(states: List[Any]) -> Tuple[List[Dict[str, Any]], bool]:
    options: List[Dict[str, Any]] = []
    seen = set()
    truncated = False
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        if "." not in entity_id or entity_id in seen:
            continue
        domain = entity_id.split(".", 1)[0].lower()
        if domain not in SAFE_DOMAINS:
            continue
        seen.add(entity_id)
        if len(options) >= MAX_ENTITIES:
            truncated = True
            break
        attributes = item.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        state = str(item.get("state") or "")[:80]
        spec = _point_spec(entity_id, state, attributes)
        if not spec:
            continue
        available = state.strip().lower() not in STATES_WITHOUT_VALUE
        kind = str(spec["kind"])
        options.append(
            {
                "entity_id": entity_id,
                "name": _friendly_name(entity_id, attributes),
                "domain": domain,
                "kind": kind,
                "group": _group_for(kind),
                "unit": str(spec.get("unit") or ""),
                "state": state,
                "available": available,
                "writable": bool(spec.get("writable")),
                "min": spec.get("min"),
                "max": spec.get("max"),
                # Safe entities are selected by default.  Unknown/unavailable
                # entities stay unchecked until Home Assistant reports a value.
                "selected": available,
            }
        )
    options.sort(key=lambda item: (GROUP_ORDER.index(item["group"]), item["name"].lower(), item["entity_id"]))
    return options, truncated


def _error_message(exc: DeviceError, url: str) -> str:
    text = str(exc)
    lowered = text.lower()
    if "http 401" in lowered or "http 403" in lowered:
        return (
            "Home Assistant rejected that access token. Create a new long-lived token "
            "in Profile → Security, then try again."
        )
    if "http 404" in lowered:
        return (
            "That address answered, but it does not look like Home Assistant. "
            "Check the address and port (usually 8123)."
        )
    if "public" in lowered or "private" in lowered:
        return (
            "OffGrid only connects to Home Assistant on your local network. "
            "Use its local address, not a cloud address."
        )
    if "failed" in lowered or "resolve" in lowered:
        return (
            f"Could not reach Home Assistant at {url}. Check that it is on, on the "
            "same network, and that the address is correct."
        )
    return text


class HomeSetup:
    """The small server-side engine behind the Studio home panel."""

    def __init__(self, config: Any, workspace: Path) -> None:
        self.config = config
        self.workspace = Path(workspace).expanduser().resolve()
        configured = str(
            getattr(config, "devices_file", ".offgrid/devices.json")
            or ".offgrid/devices.json"
        )
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        try:
            path.resolve().relative_to(self.workspace)
        except ValueError as exc:
            raise HomeSetupError(
                "the device registry must stay inside the OffGrid workspace"
            ) from exc
        self.registry_path = path

    def _token_path(self, relative: str) -> Path:
        path = Path(str(relative or ""))
        if not path.is_absolute():
            path = self.workspace / path
        return path

    # -- status ----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        manager = DeviceManager(self.workspace, self.config)
        devices = []
        tokens_present = []
        for device in manager.devices.values():
            if device.adapter != "homeassistant":
                continue
            token_file = str(device.config.get("token_file") or "")
            token_present = bool(token_file) and self._token_path(token_file).is_file()
            tokens_present.append(token_present)
            devices.append(
                {
                    "name": device.name,
                    "adapter": device.adapter,
                    "points": len(device.points),
                    "url": str(device.config.get("url") or ""),
                    "has_private_token": token_present,
                }
            )
        return {
            "available": True,
            "connected": bool(devices),
            "policy": manager.policy,
            "devices": devices,
            "warnings": list(manager.warnings),
            "token_present": any(tokens_present),
            "defaults": {
                "url": DEFAULT_URL,
                "name": DEFAULT_NAME,
                "read_only": True,
            },
        }

    # -- connection test and discovery -----------------------------------
    def scan(self, url: Any, token: Any) -> Dict[str, Any]:
        target = _normalise_url(url)
        secret = _clean_token(token)
        states = self._states(target, secret)
        options, truncated = _entity_options(states)
        if not options:
            raise HomeSetupError(
                "Home Assistant answered, but no supported lights, switches, fans, "
                "sensors or numbers were found. Add a device in Home Assistant first."
            )
        return {
            "ok": True,
            "url": target,
            "entities": options,
            "count": len(options),
            "truncated": truncated,
            "note": (
                "Read-only is the default. Control of lights and switches is a separate "
                "opt-in on the next step."
            ),
        }

    # -- save ------------------------------------------------------------
    def connect(
        self,
        url: Any,
        token: Any,
        name: Any,
        entities: Any,
        allow_control: bool = False,
    ) -> Dict[str, Any]:
        target = _normalise_url(url)
        secret = _clean_token(token)
        try:
            device_name = _slug(name or DEFAULT_NAME, "home name")
        except DeviceError as exc:
            raise HomeSetupError(str(exc)) from exc
        if not isinstance(entities, list) or not entities:
            raise HomeSetupError("choose at least one device to connect")
        selected = []
        for item in entities[: MAX_ENTITIES + 1]:
            if not isinstance(item, str):
                raise HomeSetupError("the selected device list is invalid")
            if item not in selected:
                selected.append(item)
        if len(selected) > MAX_ENTITIES:
            raise HomeSetupError(f"choose at most {MAX_ENTITIES} devices")

        states = self._states(target, secret)
        options, _ = _entity_options(states)
        by_entity = {item["entity_id"]: item for item in options}
        unknown = [item for item in selected if item not in by_entity]
        if unknown:
            raise HomeSetupError(
                "some selected devices are no longer available in Home Assistant. "
                "Test the connection again and reselect them."
            )
        chosen = [by_entity[item] for item in selected]
        writable = any(item["writable"] for item in chosen)
        requested_policy = "auto" if allow_control and writable else "read-only"

        registry = self._read_registry()
        entries = registry.get("devices", [])
        if isinstance(entries, dict):
            entries = [dict(value, name=key) for key, value in entries.items()]
            registry["devices"] = entries
        if not isinstance(entries, list):
            raise HomeSetupError("the existing device registry is not valid; it was not changed")
        if any(isinstance(item, dict) and item.get("name") == device_name for item in entries):
            raise HomeSetupError(
                f'a home named "{device_name}" is already connected. Choose another name.'
            )

        points: Dict[str, Any] = {}
        for item in chosen:
            point_name = self._point_name(item, points)
            # Rebuild from the selected option so values that changed between
            # scan and save cannot change the meaning of the point.
            point = {
                "kind": item["kind"],
                "entity": item["entity_id"],
                # Even when an existing registry allows writes, a guided
                # connection that did not explicitly opt in stays read-only.
                "writable": bool(item["writable"]) and allow_control,
                "description": item["name"],
            }
            if item["unit"]:
                point["unit"] = item["unit"]
            if item["min"] is not None and item["max"] is not None:
                point["min"] = item["min"]
                point["max"] = item["max"]
            if item["kind"] == "number":
                point["value_service"] = "set_value"
                point["value_field"] = "value"
            points[point_name] = point

        # One token file per home: adding a second Home Assistant must not
        # overwrite the credential used by the first.
        token_relative = f"{TOKEN_DIR}/home-assistant-{device_name}.token"
        token_path = self._token_path(token_relative)
        device = {
            "name": device_name,
            "adapter": "homeassistant",
            "url": target,
            "token_file": token_relative,
            "wait": 0.2,
            "points": points,
        }
        entries.append(device)
        registry["devices"] = entries
        if "policy" not in registry or registry.get("policy") not in ("read-only", "ask", "auto"):
            registry["policy"] = requested_policy
        actual_policy = str(registry.get("policy") or "read-only").lower()
        if actual_policy not in ("read-only", "ask", "auto"):
            actual_policy = "read-only"
            registry["policy"] = actual_policy

        self._write_private(token_path, secret)
        self._write_json(self.registry_path, registry)
        return {
            "ok": True,
            "device": device_name,
            "points": len(points),
            "policy": actual_policy,
            "policy_preserved": actual_policy != requested_policy,
            "message": (
                f"Connected {len(points)} device(s) from Home Assistant."
                + (
                    " Existing device policy was kept."
                    if actual_policy != requested_policy
                    else ""
                )
            ),
            "status": self.status(),
        }

    # -- Home Assistant HTTP --------------------------------------------
    def _states(self, url: str, token: str) -> List[Any]:
        try:
            # A large Home Assistant install can have more entities than a
            # single device response; keep the cap bounded but generous.
            client = _HttpClient(
                url, token=token, timeout=8.0, max_response=8 * MAX_RESPONSE
            )
            states = client.json("GET", "/api/states")
        except DeviceError as exc:
            raise HomeSetupError(_error_message(exc, url)) from exc
        if not isinstance(states, list):
            raise HomeSetupError("Home Assistant returned an unexpected device list")
        return states

    # -- files -----------------------------------------------------------
    def _read_registry(self) -> Dict[str, Any]:
        if not self.registry_path.is_file():
            # No policy yet: connect() can choose the requested default.  An
            # existing policy is always preserved, never silently widened.
            return {"devices": []}
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HomeSetupError(
                "the existing device registry could not be read; it was not changed"
            ) from exc
        if not isinstance(raw, dict):
            raise HomeSetupError("the existing device registry is not valid; it was not changed")
        return raw

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        self._write_private(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def _write_private(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            descriptor, temporary = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, path)
            finally:
                try:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                except OSError:
                    pass
        except OSError as exc:
            raise HomeSetupError(f"OffGrid could not save the home setup: {exc}") from exc

    @staticmethod
    def _point_name(option: Dict[str, Any], used: Dict[str, Any]) -> str:
        entity_id = str(option["entity_id"])
        domain, object_id = entity_id.split(".", 1)
        base = object_id.replace("-", "_")
        try:
            candidate = _slug(base, "point name")
        except DeviceError:
            candidate = _slug(domain, "point name")
        if candidate not in used:
            return candidate
        candidate = _slug(f"{domain}_{base}", "point name")
        if candidate not in used:
            return candidate
        suffix = 2
        while f"{candidate}_{suffix}" in used:
            suffix += 1
        return f"{candidate}_{suffix}"
