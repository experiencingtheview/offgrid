"""A local change detector for this machine's network view.

It is deliberately not called surveillance detection. It snapshots the OS
connection table and reports what changed since the last snapshot: a new
listener, a removed one, a listener whose process changed, or a new outbound
connection to a public address.

That is useful for spotting unexpected new services on the machine. It cannot
see a warrant, a phone tap, another device, or collection done outside this
computer, and claiming otherwise would be a lie.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .netmap import Connection, collect
from .private import ensure_private_dir

WATCH_VERSION = 1
REMOTE_ACCESS_PORTS = {22, 23, 3389, 5900, 5901, 5985, 5986}
NOTE = (
    "local change detector, not surveillance detection: this machine's connection "
    "table only; it cannot detect a warrant, a phone tap, or collection outside it"
)


@dataclass
class Snapshot:
    taken: float
    host: str
    source: str
    connections: List[Connection] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": WATCH_VERSION,
            "taken": self.taken,
            "host": self.host,
            "source": self.source,
            "connections": [connection.as_dict() for connection in self.connections],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        connections: List[Connection] = []
        for item in data.get("connections") or []:
            if not isinstance(item, dict):
                continue
            connections.append(
                Connection(
                    proto=str(item.get("proto") or "tcp"),
                    local=str(item.get("local") or ""),
                    remote=str(item.get("remote") or ""),
                    state=str(item.get("state") or ""),
                    process=str(item.get("process") or ""),
                    pid=int(item.get("pid") or 0),
                )
            )
        return cls(
            taken=float(data.get("taken") or 0),
            host=str(data.get("host") or "this machine"),
            source=str(data.get("source") or ""),
            connections=connections,
        )


@dataclass(frozen=True)
class Change:
    kind: str
    connection: Connection
    note: str = ""
    severity: str = "info"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "note": self.note,
            "connection": self.connection.as_dict(),
        }


def capture() -> Snapshot:
    connections, source = collect()
    return Snapshot(time.time(), platform.node() or "this machine", source, connections)


def save(snapshot: Snapshot, path: Path) -> None:
    """Write the baseline atomically and privately: it contains local addresses."""
    path = Path(path)
    ensure_private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(snapshot.as_dict(), indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)


def load(path: Path) -> Optional[Snapshot]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Snapshot.from_dict(data)
    except (TypeError, ValueError):
        return None


def _host(address: str) -> str:
    address = (address or "").strip()
    if address.startswith("["):
        return address[1 : address.find("]")] if "]" in address else address
    return address.rsplit(":", 1)[0] if ":" in address else address


def _listener_key(connection: Connection) -> Tuple[str, str, int]:
    return connection.proto, _host(connection.local), connection.local_port


def _public_key(connection: Connection) -> Tuple[str, str]:
    return connection.proto, connection.remote


def compare(previous: Snapshot, current: Snapshot) -> List[Change]:
    """What is new, gone or changed between two snapshots."""
    changes: List[Change] = []
    old_listeners = {_listener_key(c): c for c in previous.connections if c.listening}
    new_listeners = {_listener_key(c): c for c in current.connections if c.listening}

    for key in sorted(set(new_listeners) - set(old_listeners)):
        connection = new_listeners[key]
        if connection.wildcard:
            severity = "high"
            note = "new listener on all interfaces: reachable from your network"
        elif connection.local_port in REMOTE_ACCESS_PORTS:
            severity = "medium"
            note = "new local remote-access listener"
        else:
            severity = "info"
            note = "new local listener"
        changes.append(Change("new-listener", connection, note, severity))

    for key in sorted(set(old_listeners) - set(new_listeners)):
        changes.append(Change("removed-listener", old_listeners[key], "no longer listening", "info"))

    for key in sorted(set(old_listeners) & set(new_listeners)):
        old, new = old_listeners[key], new_listeners[key]
        if old.process and new.process and old.process != new.process:
            changes.append(
                Change("process-changed", new, f"was {old.process} (pid {old.pid})", "medium")
            )

    old_public = {
        _public_key(c) for c in previous.connections if not c.listening and c.remote_public
    }
    new_public = {
        _public_key(c) for c in current.connections if not c.listening and c.remote_public
    }
    for key in sorted(new_public - old_public):
        connection = next(
            c for c in current.connections if not c.listening and _public_key(c) == key
        )
        changes.append(
            Change("new-public-connection", connection, "outbound traffic to a public address", "info")
        )
    return changes


LABELS = {
    "new-listener": "new listeners",
    "removed-listener": "removed listeners",
    "process-changed": "listener process changes",
    "new-public-connection": "new public endpoints",
}
ORDER = ("new-listener", "removed-listener", "process-changed", "new-public-connection")


def render(previous: Snapshot, current: Snapshot, changes: List[Change]) -> List[str]:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(previous.taken))
    lines = [f"  baseline: {stamp} on {previous.host} ({len(previous.connections)} connection(s))"]
    if not changes:
        lines.append("  no changes since the baseline")
    for kind in ORDER:
        subset = [change for change in changes if change.kind == kind]
        if not subset:
            continue
        lines.append(f"  {LABELS[kind]} ({len(subset)}):")
        for change in subset:
            connection = change.connection
            mark = {"high": " [high]", "medium": " [medium]"}.get(change.severity, "")
            lines.append(
                f"    {connection.proto:<4} {connection.local} -> {connection.remote}  "
                f"{connection.state}{mark}  {change.note}"
            )
    lines.append("  note: " + NOTE)
    return lines


def exit_code(changes: List[Change]) -> int:
    """2 when a new service is reachable from the network; 0 otherwise."""
    return 2 if any(change.severity == "high" for change in changes) else 0
