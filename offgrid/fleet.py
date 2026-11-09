"""Borrow computing power from the other devices you own.

A phone, a tablet, a spare laptop and a good desktop can all be on the same
network. This module lets one of them ask the others to run a model call: the
requesting machine keeps the workspace, the plan and the reviews, and the
strongest available peer does the token generation. Nothing touches the
internet - peers are LAN addresses only, and the whole feature is off unless
you ask for it.

Three parts:

- `Fleet` / `FleetProvider` - the requesting side. Keeps a registry of peers,
  ranks them by measured capability, routes each model call to the best one,
  and falls back to the local model when nobody answers.
- `FleetServer` - the donating side. `offgrid fleet serve` exposes this
  machine's model server over the LAN behind a shared token. With
  `--allow-exec`, it can also run commands for peers that have the token.
- Remote command execution is a separate opt-in on both sides. It keeps the
  private-LAN and token checks; destructive commands are refused.

Honesty rules that are enforced, not just documented:

- a peer that does not answer is marked with its error and skipped; the run
  continues locally instead of pretending the work happened elsewhere;
- the report says how many calls ran on which machine;
- public IP addresses are refused, so "peer" can never quietly mean "cloud".
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .hardware import Capability, probe
from .models import Completion, Message
from .providers import LocalProvider, ProviderError
from .tools import (
    DESTRUCTIVE,
    FORBIDDEN,
    MAX_OUTPUT,
    MAX_TIMEOUT,
    NETWORK,
    OFFLINE_SAFE,
    _matches,
)

DEFAULT_PORT = 8776
BEACON = b"OFFGRID?/1"
MAX_BODY = 8 * 1024 * 1024


class FleetError(Exception):
    """A peer could not be used."""


def is_lan_url(url: str) -> bool:
    """True for loopback, private ranges and `.local` names. Never public IPs."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() in ("localhost", "::1"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A LAN hostname (desk.local, thinkpad). Public DNS names are not peers.
        return host.endswith(".local") or "." not in host
    return address.is_private or address.is_loopback or address.is_link_local


def _post(url: str, payload: Dict[str, Any], token: str = "", timeout: int = 120) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise FleetError(f"HTTP {exc.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise FleetError(f"unreachable ({getattr(exc, 'reason', exc)})") from None
    except ValueError:
        raise FleetError("answer was not JSON") from None


def _get(url: str, token: str = "", timeout: int = 5) -> Dict[str, Any]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise FleetError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise FleetError(f"unreachable ({getattr(exc, 'reason', exc)})") from None
    except ValueError:
        raise FleetError("answer was not JSON") from None


# --------------------------------------------------------------------------
# peers
# --------------------------------------------------------------------------
@dataclass
class Peer:
    name: str
    url: str
    token: str = ""
    tier: str = ""
    ram_gb: float = 0.0
    vram_gb: float = 0.0
    accelerators: Tuple[str, ...] = ()
    models: Tuple[str, ...] = ()
    last_seen: float = 0.0
    latency_ms: float = 0.0
    last_error: str = ""

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        if not is_lan_url(self.url):
            raise FleetError(
                f"{self.url} is not on your local network. Fleet peers must be private "
                "addresses (192.168.x.x, 10.x.x.x, .local) - OffGrid does not farm work out "
                "to the internet."
            )

    @property
    def healthy(self) -> bool:
        return self.last_seen > 0 and not self.last_error

    @property
    def score(self) -> float:
        tier_weight = {"minimal": 1.0, "modest": 2.0, "capable": 3.0, "workstation": 4.0}
        return round(tier_weight.get(self.tier, 1.0) * 100 + self.ram_gb + 2 * self.vram_gb, 2)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["accelerators"] = list(self.accelerators)
        data["models"] = list(self.models)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Peer":
        return cls(
            name=str(data.get("name") or "peer"),
            url=str(data.get("url") or ""),
            token=str(data.get("token") or ""),
            tier=str(data.get("tier") or ""),
            ram_gb=float(data.get("ram_gb") or 0.0),
            vram_gb=float(data.get("vram_gb") or 0.0),
            accelerators=tuple(data.get("accelerators") or ()),
            models=tuple(data.get("models") or ()),
            last_seen=float(data.get("last_seen") or 0.0),
            latency_ms=float(data.get("latency_ms") or 0.0),
            last_error=str(data.get("last_error") or ""),
        )


def peer_from_discovery(
    entry: Dict[str, Any], token: str = "", fallback_port: int = DEFAULT_PORT
) -> Peer:
    """Build a peer from one UDP discovery reply.

    The beacon advertises only what is needed to connect. It never carries the
    shared token, so the caller has to supply that separately.
    """
    host = str(entry.get("host") or "").strip()
    if not host:
        raise FleetError("discovery reply had no address")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = int(entry.get("port") or fallback_port)
    except (TypeError, ValueError):
        raise FleetError("discovery reply had an invalid port") from None
    peer = Peer(
        name=str(entry.get("name") or "peer").strip() or "peer",
        url=f"http://{host}:{port}",
        token=token,
    )
    peer.tier = str(entry.get("tier") or "")
    peer.accelerators = tuple(str(item) for item in (entry.get("accelerators") or ()))
    peer.models = tuple(str(item) for item in (entry.get("models") or ()))
    for field_name in ("ram_gb", "vram_gb"):
        try:
            setattr(peer, field_name, float(entry.get(field_name) or 0.0))
        except (TypeError, ValueError):
            setattr(peer, field_name, 0.0)
    peer.last_seen = time.time()
    return peer


@dataclass
class Fleet:
    """The peers this machine knows about, in a small JSON file."""

    config: Config
    peers: List[Peer] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.config.workspace) / self.config.fleet_file

    # -- registry --------------------------------------------------------
    def load(self) -> "Fleet":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        entries = data.get("peers") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return self
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                self.peers.append(Peer.from_dict(entry))
            except FleetError:
                continue
        return self

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"peers": [peer.as_dict() for peer in self.peers]}, indent=2),
                encoding="utf-8",
            )
            os.chmod(self.path, 0o600)  # peer tokens live in here
        except OSError:
            pass

    def unique_name(self, base: str) -> str:
        """Keep two different machines with the same hostname from colliding."""
        base = (base or "peer").strip() or "peer"
        names = {peer.name for peer in self.peers}
        if base not in names:
            return base
        index = 2
        while f"{base}-{index}" in names:
            index += 1
        return f"{base}-{index}"

    def register(self, peer: Peer) -> Peer:
        """Add or replace a peer by name or URL, then save the registry."""
        self.peers = [
            existing
            for existing in self.peers
            if existing.name != peer.name and existing.url != peer.url
        ]
        self.peers.append(peer)
        self.save()
        return peer

    def add(self, name: str, url: str, token: str = "") -> Peer:
        return self.register(Peer(name=name, url=url, token=token))

    def remove(self, name: str) -> bool:
        before = len(self.peers)
        self.peers = [peer for peer in self.peers if peer.name != name]
        if len(self.peers) != before:
            self.save()
            return True
        return False

    # -- liveness --------------------------------------------------------
    def ping(self, peer: Peer, timeout: int = 5) -> bool:
        started = time.time()
        try:
            info = _get(f"{peer.url}/fleet/info", token=peer.token, timeout=timeout)
        except FleetError as exc:
            peer.last_error = str(exc)
            peer.last_seen = 0.0
            return False
        peer.last_error = ""
        peer.last_seen = time.time()
        peer.latency_ms = round((peer.last_seen - started) * 1000, 1)
        peer.tier = str(info.get("tier") or peer.tier or "")
        peer.ram_gb = float(info.get("ram_gb") or peer.ram_gb or 0.0)
        peer.vram_gb = float(info.get("vram_gb") or peer.vram_gb or 0.0)
        peer.accelerators = tuple(info.get("accelerators") or peer.accelerators)
        peer.models = tuple(info.get("models") or peer.models)
        return True

    def ping_all(self, timeout: int = 3) -> List[Peer]:
        for peer in self.peers:
            self.ping(peer, timeout=timeout)
        self.save()
        return self.peers

    def available(self, limit: int = 3, timeout: int = 3) -> List[Peer]:
        """Peers that answer right now, best first. Pings each at most once."""
        answering = [peer for peer in self.peers if self.ping(peer, timeout=timeout)]
        answering.sort(key=lambda peer: peer.score, reverse=True)
        if answering:
            self.save()
        return answering[:limit]

    def best(self) -> Optional[Peer]:
        candidates = self.available(limit=1)
        return candidates[0] if candidates else None

    # -- remote commands -------------------------------------------------
    def execute(
        self, peer: Peer, command: str, cwd: str = ".", timeout: int = 120
    ) -> Dict[str, Any]:
        """Run one command on a peer that was started with `--allow-exec`."""
        if not (command or "").strip():
            raise FleetError("empty command")
        try:
            seconds = max(1, min(int(timeout or 120), MAX_TIMEOUT))
        except (TypeError, ValueError):
            raise FleetError("timeout must be an integer") from None
        result = _post(
            f"{peer.url}/fleet/v1/exec",
            {"command": command, "cwd": cwd or ".", "timeout": seconds},
            token=peer.token,
            timeout=seconds + 5,
        )
        if not isinstance(result, dict):
            raise FleetError("peer returned an invalid command result")
        return result

    def execute_all(
        self, command: str, cwd: str = ".", timeout: int = 120
    ) -> List[Dict[str, Any]]:
        """Run one command on every peer that answers right now."""
        results: List[Dict[str, Any]] = []
        for peer in self.available(limit=max(1, len(self.peers))):
            try:
                result = self.execute(peer, command, cwd=cwd, timeout=timeout)
            except FleetError as exc:
                results.append({"peer": peer.name, "ok": False, "error": str(exc)})
                continue
            result["peer"] = peer.name
            result["ok"] = True
            results.append(result)
        return results

    # -- discovery -------------------------------------------------------
    def discover(self, hosts: Sequence[str] = ("255.255.255.255",), timeout: float = 1.5) -> List[Dict[str, Any]]:
        """Broadcast for machines running `offgrid fleet serve` on this LAN."""
        replies: List[Dict[str, Any]] = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.2)
        except OSError:
            return replies
        port = self.config.fleet_port + 1
        try:
            for host in hosts:
                try:
                    sock.sendto(BEACON, (host, port))
                except OSError:
                    continue
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    payload, address = sock.recvfrom(4096)
                except (socket.timeout, OSError):
                    continue
                try:
                    data = json.loads(payload.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("offgrid") == 1:
                    data.setdefault("host", address[0])
                    replies.append(data)
        finally:
            sock.close()
        return replies

    # -- stats -----------------------------------------------------------
    def describe(self) -> List[str]:
        if not self.peers:
            return [
                "no peers configured (run `offgrid fleet scan`, or add one with "
                "`offgrid fleet add NAME URL --token ...`)"
            ]
        lines = []
        for peer in sorted(self.peers, key=lambda item: item.score, reverse=True):
            state = "ok" if peer.healthy else (peer.last_error or "not checked")
            extra = f"{peer.tier or '?'} tier"
            if peer.vram_gb:
                extra += f", {peer.vram_gb:g} GB VRAM"
            lines.append(f"{peer.name:<12} {peer.url:<28} {extra:<22} {state}")
        return lines


def lan_address() -> str:
    """Best-effort LAN address, used only to print a copyable URL."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ""


def token_path(config: Config) -> Path:
    return Path(config.workspace) / ".offgrid" / "fleet-token"


def load_or_create_token(config: Config) -> str:
    """The shared LAN token, generated once per workspace and kept private."""
    import secrets

    if config.fleet_token:
        return config.fleet_token
    path = token_path(config)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        from .private import ensure_private_dir, harden_file

        ensure_private_dir(path.parent)
        path.write_text(token + "\n", encoding="utf-8")
        harden_file(path)
    except OSError:
        pass
    return token


# --------------------------------------------------------------------------
# requesting side
# --------------------------------------------------------------------------
class FleetProvider:
    """A LocalProvider that can hand a model call to a peer machine.

    Same interface as `LocalProvider`, so nothing else in the codebase needs to
    know whether a call ran here or three metres away.
    """

    def __init__(self, local: LocalProvider, fleet: Fleet, config: Config) -> None:
        self.local = local
        self.fleet = fleet
        self.config = config
        self.model = local.model
        self.native_tools = local.native_tools
        self.routes: Dict[str, int] = {}
        self.failures = 0

    @property
    def endpoint(self) -> str:
        return self.local.endpoint

    @property
    def remote_calls(self) -> int:
        return sum(count for name, count in self.routes.items() if name != "local")

    def models(self) -> List[str]:
        return self.local.models()

    def health(self) -> Tuple[bool, str]:
        return self.local.health()

    def _count(self, where: str) -> None:
        self.routes[where] = self.routes.get(where, 0) + 1

    def chat(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_names: Sequence[str] = (),
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Completion:
        payload: Dict[str, Any] = {
            "messages": [message.to_api() for message in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        if tools and self.native_tools is not False:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        for peer in self.fleet.available(limit=2):
            started = time.time()
            try:
                envelope = _post(
                    f"{peer.url}/fleet/v1/chat/completions",
                    payload,
                    token=peer.token,
                    timeout=self.config.fleet_timeout,
                )
            except FleetError as exc:
                peer.last_error = str(exc)
                self.failures += 1
                continue
            peer.last_error = ""
            peer.last_seen = time.time()
            if not envelope.get("choices"):
                peer.last_error = "peer returned no choices"
                self.failures += 1
                continue
            self._count(peer.name)
            return self.local._parse(  # reuse the same parsing, so behaviour matches
                envelope,
                text_protocol=self.native_tools is False,
                tool_names=tool_names,
                started=started,
            )

        self._count("local")
        return self.local.chat(
            messages,
            tools=tools,
            tool_names=tool_names,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def summary(self) -> str:
        if not self.routes:
            return ""
        parts = [f"{count} on {name}" for name, count in sorted(self.routes.items())]
        text = "compute: " + ", ".join(parts)
        if self.failures:
            text += f" ({self.failures} peer attempt(s) failed, fell back to this machine)"
        elif not self.remote_calls and self.fleet.peers:
            text += " (no peer answered, everything ran on this machine)"
        return text


# --------------------------------------------------------------------------
# donating side
# --------------------------------------------------------------------------
class FleetServer:
    """Expose this machine's model server to peers on the LAN, behind a token.

    Command execution is a separate opt-in: `allow_exec=True` (CLI:
    `offgrid fleet serve --allow-exec`) adds `/fleet/v1/exec`. It is still
    token-authenticated and LAN-only, and destructive commands are refused.
    """

    def __init__(
        self,
        config: Config,
        provider: LocalProvider,
        token: str,
        port: Optional[int] = None,
        name: Optional[str] = None,
        capability: Optional[Capability] = None,
        allow_exec: bool = False,
    ) -> None:
        self.config = config
        self.provider = provider
        self.token = token
        self.port = int(port if port is not None else config.fleet_port)
        self.name = name or socket.gethostname()
        self.capability = capability or probe(config.workspace or ".")
        self.allow_exec = bool(allow_exec)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._beacon: Optional[threading.Thread] = None
        self._beacon_stop = threading.Event()
        self._lock = threading.Lock()
        self._load = 0
        self.served = 0
        self.executed = 0

    # -- info ------------------------------------------------------------
    def info(self) -> Dict[str, Any]:
        caps = self.capability
        try:
            models = self.provider.models()
        except ProviderError:
            models = []
        return {
            "offgrid": 1,
            "name": self.name,
            "port": self.port,
            "model": self.provider.model,
            "models": models,
            "tier": caps.tier,
            "ram_gb": caps.ram_gb,
            "vram_gb": caps.vram_gb,
            "accelerators": list(caps.accelerators),
            "load": self._load,
            "served": self.served,
            "exec": self.allow_exec,
            "executed": self.executed,
        }

    # -- request handling -------------------------------------------------
    def _authorised(self, header: Optional[str]) -> bool:
        if not self.token:
            return False
        expected = f"Bearer {self.token}"
        return hmac.compare_digest((header or "").strip(), expected)

    def handle_chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Forward one chat completion to the local model server."""
        payload: Dict[str, Any] = {
            key: body[key]
            for key in ("messages", "temperature", "max_tokens", "top_p", "stop")
            if key in body
        }
        payload["stream"] = False
        if self.provider.model:
            payload["model"] = self.provider.model
        if body.get("tools") and self.provider.native_tools is not False:
            payload["tools"] = body["tools"]
            payload["tool_choice"] = body.get("tool_choice", "auto")

        url = f"{self.config.resolved_endpoint()}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._lock:
            self._load += 1
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise FleetError(f"local server said HTTP {exc.code}{': ' + detail if detail else ''}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise FleetError(f"local server unreachable ({getattr(exc, 'reason', exc)})") from None
        finally:
            with self._lock:
                self._load -= 1
        self.served += 1
        return data

    def handle_exec(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Run one command for an authenticated peer, if exec was enabled."""
        if not self.allow_exec:
            raise FleetError("remote command execution is disabled on this peer")
        command = str(body.get("command") or "").strip()
        if not command:
            raise FleetError("command is required")
        if len(command) > 8000:
            raise FleetError("command is too long")
        refusal = _matches(FORBIDDEN, command)
        if refusal:
            raise FleetError(f"refused: {refusal[1]} is never allowed")
        refusal = _matches(DESTRUCTIVE, command)
        if refusal:
            raise FleetError(
                f"refused: {refusal[1]} needs human approval and is not allowed on a fleet peer"
            )
        if self.config.network != "allow" and not any(
            flag in command for flag in OFFLINE_SAFE
        ):
            refusal = _matches(NETWORK, command)
            if refusal:
                raise FleetError(
                    f"refused: {refusal[1]} needs the network, and this peer is offline"
                )

        cwd = str(body.get("cwd") or ".").strip() or "."
        if len(cwd) > 1000:
            raise FleetError("cwd is too long")
        root = Path(self.config.workspace or ".").expanduser().resolve()
        try:
            cwd_path = (root / cwd).resolve()
            cwd_path.relative_to(root)
        except (OSError, ValueError):
            raise FleetError("cwd must stay inside this peer's workspace") from None
        if not cwd_path.is_dir():
            raise FleetError(f"cwd is not a directory: {cwd}")

        try:
            seconds = max(1, min(int(body.get("timeout") or 120), MAX_TIMEOUT))
        except (TypeError, ValueError):
            raise FleetError("timeout must be an integer") from None
        env = dict(os.environ)
        env.update(
            {
                "OFFGRID": "1",
                "CI": "1",
                "PYTHONUNBUFFERED": "1",
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "TERM": "dumb",
            }
        )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=seconds,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise FleetError(f"command timed out after {seconds}s") from None
        except OSError as exc:
            raise FleetError(f"could not run command: {exc}") from None

        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        output = output.strip() or "(no output)"
        truncated = len(output) > MAX_OUTPUT
        if truncated:
            output = (
                output[: MAX_OUTPUT // 2]
                + f"\n... [{len(output) - MAX_OUTPUT} chars trimmed] ...\n"
                + output[-MAX_OUTPUT // 2 :]
            )
        self.executed += 1
        return {
            "peer": self.name,
            "cwd": "." if cwd_path == root else str(cwd_path.relative_to(root)),
            "exit_code": proc.returncode,
            "output": output,
            "truncated": truncated,
        }

    # -- lifecycle --------------------------------------------------------
    def start(self, host: str = "0.0.0.0") -> int:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep the terminal clean
                pass

            def _send(self, code: int, payload: Dict[str, Any]) -> None:
                blob = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/fleet/info":
                    self._send(200, server.info())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                path = self.path.rstrip("/")
                if path not in ("/fleet/v1/chat/completions", "/fleet/v1/exec"):
                    self._send(404, {"error": "not found"})
                    return
                if not server._authorised(self.headers.get("Authorization")):
                    self._send(401, {"error": "a fleet token is required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._send(400, {"error": "bad body length"})
                    return
                if length <= 0 or length > MAX_BODY:
                    self._send(400, {"error": "bad body length"})
                    return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
                except ValueError:
                    self._send(400, {"error": "invalid json"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "invalid json"})
                    return
                if path == "/fleet/v1/exec":
                    if not server.allow_exec:
                        self._send(
                            403,
                            {"error": "remote command execution is disabled on this peer"},
                        )
                        return
                    try:
                        self._send(200, server.handle_exec(body))
                    except FleetError as exc:
                        self._send(502, {"error": str(exc)})
                    return
                if not isinstance(body.get("messages"), list):
                    self._send(400, {"error": "messages is required"})
                    return
                try:
                    self._send(200, server.handle_chat(body))
                except FleetError as exc:
                    self._send(502, {"error": str(exc)})

        self._httpd = ThreadingHTTPServer((host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._start_beacon()
        return self.port

    def _start_beacon(self) -> None:
        """Answer broadcast probes so a new device can find this one."""
        port = self.config.fleet_port + 1
        stop = self._beacon_stop

        def listen() -> None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                sock.settimeout(0.5)
            except OSError:
                return
            try:
                while not stop.is_set():
                    try:
                        payload, address = sock.recvfrom(1024)
                    except (socket.timeout, OSError):
                        continue
                    if payload != BEACON:
                        continue
                    try:
                        sock.sendto(json.dumps(self.info()).encode("utf-8"), address)
                    except OSError:
                        continue
            finally:
                sock.close()

        self._beacon = threading.Thread(target=listen, daemon=True)
        self._beacon.start()

    def stop(self) -> None:
        self._beacon_stop.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        if self._beacon:
            self._beacon.join(timeout=2)
