"""Share accepted learning notes between OffGrid installations.

OffGrid is offline by default. This module does not change that: syncing only
happens when a person runs an explicit ``offgrid sync`` command or starts the
explicit ``offgrid sync schedule`` loop, and the config allows the network.
Scheduled passes re-check that permission before every exchange. A sync server
exposes only accepted notes, behind a shared token. Incoming notes are always
stored as **pending** and cannot influence a run until the local human accepts
them.

Public peers must use HTTPS. Plain HTTP is accepted only for loopback, or when
the person explicitly sets ``--allow-http`` / ``sync_allow_http``. The server
itself speaks HTTP, so put it behind a TLS reverse proxy before exposing it to
the internet. The token is a bearer secret: never put it in a URL.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import CONFIG_FILENAMES, Config
from .memory import MemoryStore
from .private import append_private_text, harden_file, write_private_text

VERSION = 1
MAX_BODY = 4 * 1024 * 1024
MAX_LESSONS = 1000
INFO_PATH = "/sync/v1/info"
LEARNING_PATH = "/sync/v1/learning"


class SyncError(Exception):
    """A sync operation could not be completed safely."""


def is_loopback_url(url: str) -> bool:
    """True for localhost and loopback IPs, where plain HTTP is acceptable."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_peer_url(url: str, allow_http: bool = False) -> str:
    """Normalise and check a peer URL before a token can be sent to it."""
    url = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SyncError("peer must be an http:// or https:// URL with a host")
    if parsed.username or parsed.password:
        raise SyncError("peer URLs must not contain credentials; use --token")
    if parsed.scheme == "http" and not allow_http and not is_loopback_url(url):
        raise SyncError(
            "refusing plain HTTP to a non-loopback peer; use https:// or pass "
            "--allow-http only on a trusted network"
        )
    return url


def configured_peers(config: Config, extra: Sequence[str] = ()) -> List[str]:
    """The explicit peers for this operation, deduplicated in order."""
    raw: Iterable[Any] = extra or getattr(config, "sync_peers", ())
    if isinstance(raw, str):
        raw = (raw,)
    peers: List[str] = []
    for value in raw:
        for part in str(value).split(","):
            part = part.strip().rstrip("/")
            if part and part not in peers:
                peers.append(part)
    return peers


def peer_config_path(workspace: Path) -> Path:
    """The workspace config file that peer-management commands edit."""
    root = Path(workspace)
    existing = [root / name for name in CONFIG_FILENAMES if (root / name).is_file()]
    return existing[-1] if existing else root / ".offgrid.json"


def _read_peer_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncError(f"could not read {path}: {exc}") from None
    except ValueError:
        raise SyncError(f"{path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise SyncError(f"{path} must contain a JSON object")
    return data


def _write_peer_config(path: Path, data: Dict[str, Any]) -> None:
    try:
        write_private_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise SyncError(f"could not write {path}: {exc}") from None


def _file_peers(data: Dict[str, Any]) -> List[str]:
    raw = data.get("sync_peers")
    if not isinstance(raw, (list, tuple, str)):
        raw = ()
    return configured_peers(Config(sync_peers=raw))


def configured_peer_file(workspace: Path) -> List[str]:
    """Read only the peers managed in this workspace's config file."""
    path = peer_config_path(workspace)
    return _file_peers(_read_peer_config(path))


def _validated_peers(peers: Sequence[str], allow_http: bool = False) -> List[str]:
    """Validate peer URLs and deduplicate them in order."""
    validated: List[str] = []
    for peer in peers:
        url = validate_peer_url(peer, allow_http=allow_http)
        if url not in validated:
            validated.append(url)
    return validated


def add_configured_peers(
    workspace: Path, new_peers: Sequence[str], allow_http: bool = False
) -> Tuple[List[str], List[str], List[str], List[str], Path]:
    """Validate and persist several peers in one config write.

    Every URL is checked before the config file is touched, so one bad URL
    cannot leave a partial update. Returns configured peers, requested peers,
    newly added peers, already-configured peers, and the config path.
    """
    requested = _validated_peers(new_peers, allow_http=allow_http)
    path = peer_config_path(workspace)
    data = _read_peer_config(path)
    peers = _file_peers(data)
    added = [url for url in requested if url not in peers]
    already = [url for url in requested if url in peers]
    if added:
        peers = peers + added
        data["sync_peers"] = peers
        _write_peer_config(path, data)
    return peers, requested, added, already, path


def add_configured_peer(
    workspace: Path, peer: str, allow_http: bool = False
) -> Tuple[List[str], str, bool, Path]:
    """Validate and persist one peer URL; return peers, URL, added, and path."""
    peers, requested, added, _already, path = add_configured_peers(
        workspace, (peer,), allow_http=allow_http
    )
    return peers, requested[0], bool(added), path


def _validated_removals(peers: Sequence[str]) -> List[str]:
    """Normalize peer URLs for removal and require every one to be non-empty."""
    requested: List[str] = []
    for peer in peers:
        url = (peer or "").strip().rstrip("/")
        if not url:
            raise SyncError("peer URL is required")
        if url not in requested:
            requested.append(url)
    return requested


def remove_configured_peers(
    workspace: Path, peers: Sequence[str]
) -> Tuple[List[str], List[str], List[str], List[str], Path]:
    """Remove several peer URLs in one config write.

    Every requested URL is checked before the config file is touched, so an
    empty entry cannot leave a partial update. Peers that are not configured
    are reported instead of silently ignored; the ones that are configured are
    still removed together.
    """
    requested = _validated_removals(peers)
    path = peer_config_path(workspace)
    data = _read_peer_config(path)
    current = _file_peers(data)
    removed = [url for url in requested if url in current]
    missing = [url for url in requested if url not in current]
    remaining = current
    if removed:
        remaining = [url for url in current if url not in requested]
        data["sync_peers"] = remaining
        _write_peer_config(path, data)
    return remaining, requested, removed, missing, path


def remove_configured_peer(
    workspace: Path, peer: str
) -> Tuple[List[str], str, bool, Path]:
    """Remove one peer URL from this workspace's config file."""
    peers, requested, removed, _missing, path = remove_configured_peers(
        workspace, (peer,)
    )
    return peers, requested[0], bool(removed), path


def token_path(config: Config) -> Path:
    return Path(config.workspace) / getattr(config, "sync_token_file", ".offgrid/sync-token")


def load_or_create_token(config: Config) -> str:
    """The shared sync token, generated once per workspace and kept private."""
    env = os.environ.get("OFFGRID_SYNC_TOKEN", "").strip()
    if env:
        return env
    path = token_path(config)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        write_private_text(path, token + "\n")
    except OSError as exc:
        raise SyncError(f"could not write the sync token: {exc}") from None
    return token


def token_ready(config: Config) -> bool:
    if os.environ.get("OFFGRID_SYNC_TOKEN", "").strip():
        return True
    try:
        return bool(token_path(config).read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _log_path(config: Config) -> Path:
    return Path(config.workspace) / getattr(config, "sync_log", ".offgrid/sync.jsonl")


def record_event(
    config: Config,
    action: str,
    peer: str = "",
    counts: Optional[Dict[str, int]] = None,
    error: str = "",
) -> None:
    """Append a small audit record. It never contains the token or lesson text."""
    entry = {
        "t": time.time(),
        "action": action,
        "peer": peer,
        "counts": counts or {},
        "error": error[:300],
    }
    try:
        append_private_text(_log_path(config), json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_events(config: Config, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        lines = _log_path(config).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in lines[-max(1, int(limit)) :]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def export_bundle(store: MemoryStore, source: str = "", limit: int = 5000) -> Dict[str, Any]:
    """A JSON-safe bundle of accepted notes only."""
    return {
        "offgrid": 1,
        "version": VERSION,
        "source": (source or socket.gethostname() or "this machine")[:120],
        "generated": time.time(),
        "lessons": store.export_sync(limit=limit),
    }


def import_bundle(
    store: MemoryStore, bundle: Any, source: str = "sync"
) -> Dict[str, int]:
    """Import a bundle as pending notes; never auto-accept a remote lesson."""
    if not isinstance(bundle, dict):
        raise SyncError("sync bundle must be a JSON object")
    if bundle.get("offgrid") != 1:
        raise SyncError("not an OffGrid sync bundle")
    lessons = bundle.get("lessons")
    if not isinstance(lessons, list):
        raise SyncError("sync bundle has no lessons list")
    if len(lessons) > MAX_LESSONS:
        raise SyncError(f"sync bundle has more than {MAX_LESSONS} lessons")
    return store.import_sync(lessons, source=source)


def write_export(store: MemoryStore, path: Path, source: str = "") -> Dict[str, Any]:
    """Write a private bundle for transfer by USB or another channel.

    The chosen parent directory is not chmodded: exporting into a project or a
    mounted drive should not silently change that directory's permissions.
    """
    bundle = export_bundle(store, source=source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    harden_file(path)
    return bundle


def read_export(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncError(f"could not read {path}: {exc}") from None
    except ValueError:
        raise SyncError(f"{path} is not valid JSON") from None


def _request_json(
    url: str,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise SyncError("peer response was too large")
            decoded = json.loads(raw.decode("utf-8", errors="replace"))
    except SyncError:
        raise
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise SyncError(f"HTTP {exc.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise SyncError(f"unreachable ({getattr(exc, 'reason', exc)})") from None
    except ValueError:
        raise SyncError("peer answer was not JSON") from None
    if not isinstance(decoded, dict):
        raise SyncError("peer answer was not a JSON object")
    return decoded


def _peer_label(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


class SyncClient:
    """Push accepted notes to peers and pull their accepted notes as pending."""

    def __init__(
        self,
        config: Config,
        token: str,
        allow_http: bool = False,
        timeout: Optional[int] = None,
    ) -> None:
        if not token:
            raise SyncError("a sync token is required")
        self.config = config
        self.token = token
        self.allow_http = allow_http
        self.timeout = int(timeout if timeout is not None else config.sync_timeout)

    def push(self, peer: str, store: MemoryStore) -> Dict[str, Any]:
        url = validate_peer_url(peer, allow_http=self.allow_http)
        bundle = export_bundle(store, source=socket.gethostname())
        answer = _request_json(
            f"{url}{LEARNING_PATH}", self.token, payload=bundle, timeout=self.timeout
        )
        counts = answer.get("counts")
        if not isinstance(counts, dict):
            counts = {key: int(answer.get(key) or 0) for key in ("new", "duplicates", "skipped")}
        return {"peer": url, "counts": counts}

    def pull(self, peer: str, store: MemoryStore) -> Dict[str, Any]:
        url = validate_peer_url(peer, allow_http=self.allow_http)
        bundle = _request_json(f"{url}{LEARNING_PATH}", self.token, timeout=self.timeout)
        counts = import_bundle(store, bundle, source=f"sync:{_peer_label(url)}")
        return {"peer": url, "counts": counts}


def network_allowed(config: Config) -> bool:
    """True only when networking is allowed and hard-offline is off."""
    return not bool(getattr(config, "offline", False)) and str(
        getattr(config, "network", "deny")
    ) == "allow"


def exchange(
    config: Config,
    store: MemoryStore,
    peers: Sequence[str],
    action: str = "run",
    token: Optional[str] = None,
    allow_http: bool = False,
) -> List[Dict[str, Any]]:
    """Run one push/pull exchange and record each peer attempt."""
    if not network_allowed(config):
        raise SyncError("sync needs networking; remove --offline or set network to allow")
    if action not in ("push", "pull", "run"):
        raise SyncError(f"unknown sync action: {action}")
    if not peers:
        raise SyncError("no sync peers configured; add one with `offgrid sync peers add URL` or pass --peer URL")

    token = token or load_or_create_token(config)
    client = SyncClient(config, token, allow_http=allow_http)
    results: List[Dict[str, Any]] = []
    for peer in peers:
        item: Dict[str, Any] = {"peer": peer, "push": None, "pull": None, "error": ""}
        if action in ("push", "run"):
            try:
                item["push"] = client.push(peer, store)["counts"]
                record_event(config, "push", peer=peer, counts=item["push"])
            except SyncError as exc:
                item["error"] = str(exc)
                record_event(config, "push", peer=peer, error=str(exc))
        if action in ("pull", "run"):
            try:
                item["pull"] = client.pull(peer, store)["counts"]
                record_event(config, "pull", peer=peer, counts=item["pull"])
            except SyncError as exc:
                if not item["error"]:
                    item["error"] = str(exc)
                record_event(config, "pull", peer=peer, error=str(exc))
        results.append(item)
    return results


def scheduled_once(
    config: Config,
    store: MemoryStore,
    peers: Sequence[str],
    token: Optional[str] = None,
    allow_http: bool = False,
) -> List[Dict[str, Any]]:
    """One scheduled pass, with a fail-closed network check and audit record."""
    try:
        if not network_allowed(config):
            raise SyncError(
                "scheduled sync needs networking; remove --offline or set network to allow"
            )
        results = exchange(
            config, store, peers, action="run", token=token, allow_http=allow_http
        )
    except SyncError as exc:
        record_event(config, "schedule", error=str(exc))
        raise
    failed = sum(1 for item in results if item.get("error"))
    record_event(config, "schedule", counts={"peers": len(results), "failed": failed})
    return results


class SyncServer:
    """Serve accepted notes to token-holding peers; receive notes as pending."""

    def __init__(
        self,
        config: Config,
        store: MemoryStore,
        token: str,
        port: Optional[int] = None,
        name: Optional[str] = None,
    ) -> None:
        if not token:
            raise SyncError("a sync server needs a non-empty token")
        self.config = config
        self.store = store
        self.token = token
        self.port = int(port if port is not None else config.sync_port)
        self.name = name or socket.gethostname() or "OffGrid"
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.served = 0
        self.received = 0

    def info(self) -> Dict[str, Any]:
        stats = self.store.stats()
        return {
            "offgrid": 1,
            "version": VERSION,
            "name": self.name,
            "port": self.port,
            "accepted": stats.get("total", 0),
            "pending": stats.get("pending", 0),
        }

    def _authorised(self, header: Optional[str]) -> bool:
        expected = f"Bearer {self.token}"
        return hmac.compare_digest((header or "").strip(), expected)

    def export(self) -> Dict[str, Any]:
        with self._lock:
            self.served += 1
        return export_bundle(self.store, source=self.name)

    def receive(self, body: Any) -> Dict[str, int]:
        source = "peer"
        if isinstance(body, dict):
            source = str(body.get("source") or "peer").strip()[:80] or "peer"
        counts = import_bundle(self.store, body, source=f"sync:{source}")
        with self._lock:
            self.received += int(counts.get("new", 0))
        return counts

    def start(self, host: str = "0.0.0.0") -> int:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, payload: Dict[str, Any]) -> None:
                blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def _path(self) -> str:
                return urllib.parse.urlparse(self.path).path.rstrip("/")

            def do_GET(self) -> None:
                if self._path() == INFO_PATH:
                    self._send(200, server.info())
                    return
                if self._path() == LEARNING_PATH:
                    if not server._authorised(self.headers.get("Authorization")):
                        self._send(401, {"error": "a sync token is required"})
                        return
                    try:
                        self._send(200, server.export())
                    except SyncError as exc:
                        self._send(500, {"error": str(exc)})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self._path() != LEARNING_PATH:
                    self._send(404, {"error": "not found"})
                    return
                if not server._authorised(self.headers.get("Authorization")):
                    self._send(401, {"error": "a sync token is required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0:
                    self._send(400, {"error": "bad body length"})
                    return
                if length > MAX_BODY:
                    self._send(413, {"error": "body too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
                except ValueError:
                    self._send(400, {"error": "invalid json"})
                    return
                try:
                    self._send(200, {"ok": True, "counts": server.receive(body)})
                except SyncError as exc:
                    self._send(400, {"error": str(exc)})

        self._httpd = ThreadingHTTPServer((host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
