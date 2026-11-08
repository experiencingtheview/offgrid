"""What this machine is talking to, drawn as a map.

Read-only. It asks the operating system for its own connection table (`ss`,
`netstat`, or `/proc/net/tcp`) and renders it as a small tree. It never scans
other hosts, never resolves names, and never opens a socket: this is a view of
connections that already exist, not reconnaissance.

Honest limits, printed by the tool too: a non-root user usually cannot see which
process owns a socket, other devices' connections are invisible from here, and a
missing `ss`/`netstat` on an unusual system means the map may be empty.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

KNOWN_PORTS: Dict[int, str] = {
    22: "ssh",
    53: "dns",
    80: "http",
    443: "https",
    3000: "dev server",
    3306: "mysql",
    5000: "dev server",
    5432: "postgres",
    6379: "redis",
    8000: "dev server / vllm",
    8080: "llama.cpp model server",
    11434: "ollama",
    1234: "lm studio",
    8776: "offgrid fleet",
    8777: "offgrid discovery",
}

WILDCARD_HOSTS = {"0.0.0.0", "::", "*", "[::]", ""}
_SS_LINE = re.compile(r"^\s*(\w+)\s+(\S+)\s+\d+\s+\d+\s+(\S+)\s+(\S+)(?:\s+(.*))?$")
_PROCESS = re.compile(r'\(\("([^"]+)"[^)]*pid=(\d+)')


@dataclass(frozen=True)
class Connection:
    proto: str
    local: str
    remote: str
    state: str
    process: str = ""
    pid: int = 0

    @property
    def local_port(self) -> int:
        return _port(self.local)

    @property
    def remote_port(self) -> int:
        return _port(self.remote)

    @property
    def listening(self) -> bool:
        return self.state.upper() in ("LISTEN", "LISTENING")

    @property
    def wildcard(self) -> bool:
        return _host(self.local) in WILDCARD_HOSTS

    @property
    def remote_public(self) -> bool:
        host = _host(self.remote)
        if not host or host in WILDCARD_HOSTS:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True  # a hostname means something resolved it; treat as remote
        return not (address.is_private or address.is_loopback or address.is_link_local)

    def label(self) -> str:
        port = self.local_port
        if port in KNOWN_PORTS:
            return KNOWN_PORTS[port]
        return f"port {port}" if port else "unknown port"

    def as_dict(self) -> Dict[str, object]:
        return {
            "proto": self.proto,
            "local": self.local,
            "remote": self.remote,
            "state": self.state,
            "process": self.process,
            "pid": self.pid,
            "risk": risk(self),
        }


def _host(address: str) -> str:
    address = (address or "").strip()
    if address.startswith("["):
        return address[1 : address.find("]")] if "]" in address else address
    if ":" in address:
        return address.rsplit(":", 1)[0]
    return address


def _port(address: str) -> int:
    address = (address or "").strip()
    try:
        if address.startswith("["):
            return int(address.rsplit(":", 1)[1].strip("]"))
        if ":" in address:
            return int(address.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return 0
    return 0


def parse_ss(text: str) -> List[Connection]:
    out: List[Connection] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("netid", "state")):
            continue
        match = _SS_LINE.match(line)
        if not match:
            continue
        proto, state, local, remote, rest = match.groups()
        process = ""
        pid = 0
        if rest:
            found = _PROCESS.search(rest)
            if found:
                process, raw_pid = found.groups()
                try:
                    pid = int(raw_pid)
                except ValueError:
                    pid = 0
        out.append(Connection(proto.lower(), local, remote, state.upper(), process, pid))
    return out


def parse_netstat(text: str) -> List[Connection]:
    out: List[Connection] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        proto = parts[0].lower()
        if proto not in ("tcp", "tcp6", "udp", "udp6"):
            continue
        if len(parts) >= 7:
            local, remote, state = parts[3], parts[4], parts[5]
        else:
            local, remote, state = parts[3], parts[4], "UNKNOWN"
        out.append(Connection(proto, local, remote, state.upper()))
    return out


def collect() -> Tuple[List[Connection], str]:
    """Return (connections, source). `source` is empty when nothing worked."""
    if shutil.which("ss"):
        try:
            completed = subprocess.run(
                ["ss", "-tunap"], capture_output=True, text=True, timeout=8, check=False
            )
            if completed.returncode == 0:
                return parse_ss(completed.stdout), "ss"
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("netstat"):
        try:
            completed = subprocess.run(
                ["netstat", "-an"], capture_output=True, text=True, timeout=8, check=False
            )
            if completed.returncode == 0:
                return parse_netstat(completed.stdout), "netstat"
        except (OSError, subprocess.SubprocessError):
            pass
    return [], ""


def risk(connection: Connection) -> str:
    """The sentence a person needs, or an empty string when it is unremarkable."""
    if connection.listening and connection.wildcard:
        return f"listening on all interfaces ({connection.label()}) - reachable from your network"
    if connection.listening and connection.local_port in (8776, 8777):
        return "fleet service is listening"
    if not connection.listening and connection.remote_public:
        return "traffic leaves your local network"
    return ""


def _address_without_port(address: str) -> str:
    host = _host(address)
    if host in ("0.0.0.0", "*", ""):
        return "*"
    if host in ("::", "[::]"):
        return "[::]"
    return host


def render_map(
    connections: Sequence[Connection],
    hostname: str = "this machine",
    include_wait: bool = False,
) -> List[str]:
    """A deterministic ASCII map: listeners first, then live connections."""
    listening = [c for c in connections if c.listening]
    established = [
        c
        for c in connections
        if not c.listening and (include_wait or c.state.upper() in ("ESTAB", "ESTABLISHED"))
    ]
    listening.sort(key=lambda c: (c.local_port, c.proto))
    established.sort(key=lambda c: (c.remote, c.proto))
    lines = [f"  {hostname}"]
    if not connections:
        lines.append("  (no connection table available: install iproute2 or use a system with ss/netstat)")
        return lines

    lines.append(f"  ├── listening ({len(listening)})")
    if not listening:
        lines.append("  │   └── nothing is listening")
    for index, conn in enumerate(listening):
        branch = "└──" if index == len(listening) - 1 else "├──"
        note = risk(conn)
        mark = "  [!]" if note else ""
        lines.append(
            f"  │   {branch} {conn.proto:<4} {_address_without_port(conn.local)}:{conn.local_port:<5} "
            f"{conn.label()}{mark}"
        )
        if note:
            lines.append(f"  │       └── {note}")

    lines.append(f"  └── established ({len(established)})")
    if not established:
        lines.append("      └── none right now")
    for index, conn in enumerate(established):
        branch = "└──" if index == len(established) - 1 else "├──"
        who = f" ({conn.process}, pid {conn.pid})" if conn.process else ""
        note = risk(conn)
        mark = "  [!]" if note else ""
        lines.append(
            f"      {branch} {conn.proto:<4} {conn.local} ⇄ {conn.remote}  {conn.state}{who}{mark}"
        )
        if note:
            lines.append(f"          └── {note}")
    return lines


# Shorter name for callers that already know what it renders.
render = render_map


def describe_sources(connections: Sequence[Connection], source: str) -> List[str]:
    lines = [f"source: {source or 'unavailable'}"]
    if any(not connection.process for connection in connections if connection.listening):
        lines.append("process names may be hidden: the OS usually requires root to reveal them")
    lines.append("this shows this machine only; other devices' connections are not visible from here")
    return lines
