"""OffGrid Studio: the team in one local browser app.

The app is a customized copy of llama.cpp's MIT server web UI.  Everything it
shows is served by this module: the static chat interface, a small team panel,
and an OpenAI-compatible endpoint that turns one chat message into one full
OffGrid run (planner, coder, reviewer, tester, lead).

It is deliberately plain ``http.server``.  OffGrid has no runtime dependencies,
and the model server is still whatever llama.cpp, Ollama or LM Studio the user
already runs on localhost.  The browser app is just a new front door to that
same team.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import queue
import threading
import zlib
import time
import urllib.parse
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agents import ROSTER, load_custom_agents
from .config import Config
from .home import HomeSetup, HomeSetupError
from .orchestrator import RunResult, Team
from .providers import ProviderError, open_provider
from .session import SessionLog
from .ui import UI
from .workspace import Workspace

WEB_DIR = Path(__file__).resolve().parent / "web"
MODEL_ID = "offgrid"
MAX_BODY = 1_000_000
MIN_GZIP_BYTES = 256
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


class StudioError(Exception):
    """The Studio could not start a run."""


class _StaticAssetCache:
    """Keep static Studio files in memory and revalidate them by file metadata.

    The browser bundle is several megabytes and is requested repeatedly while
    developing.  Reading it from disk for every request is unnecessary; the
    cache is per Studio process, never written to disk, and a changed mtime or
    size reloads the entry.  The ETag is derived from the bytes themselves so a
    revalidation cannot claim an old body is current.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[int, int, bytes, str]] = {}

    def get(self, path: Path) -> Tuple[bytes, str]:
        stat = path.stat()
        key = str(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] == stat.st_mtime_ns and entry[1] == stat.st_size:
                return entry[2], entry[3]
        body = path.read_bytes()
        # Re-stat after the read: if the file changed in the meantime, a later
        # request will see different metadata and refresh the cache again.
        stat = path.stat()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        with self._lock:
            self._entries[key] = (stat.st_mtime_ns, stat.st_size, body, etag)
        return body, etag


# --------------------------------------------------------------------------
# running the team
# --------------------------------------------------------------------------
class StudioRunner:
    """A serialized Team behind the browser app.

    One Team keeps the running conversation history and memory.  The lock is
    important: two browser requests must never edit the same workspace at once.
    """

    def __init__(self, config: Config, workspace: Workspace) -> None:
        self.config = config
        self.workspace = workspace
        self._lock = threading.Lock()
        self._team: Optional[Team] = None
        self._home_setup: Optional[HomeSetup] = None
        self._session = SessionLog(workspace.root, config.session_dir)
        self.custom_agents, self.warnings = load_custom_agents(
            Path(workspace.root)
            / getattr(config, "agents_file", ".offgrid/agents.json")
        )
        self.ui = UI(config)

    def _build_team(self) -> Team:
        provider = open_provider(self.config)
        ok, message = provider.health()
        if not ok:
            raise StudioError(message)
        if self.config.fleet:
            from .fleet import Fleet, FleetProvider

            provider = FleetProvider(provider, Fleet(self.config).load(), self.config)
        team = Team(
            provider,
            self.workspace,
            self.config,
            self.ui,
            memory=self._memory(),
            library=self._library(),
            council=self._council(),
        )
        team.session_source = self._session.session_id
        return team

    def _memory(self) -> Any:
        if not self.config.memory:
            return None
        from .memory import MemoryStore
        from .semantics import load_lexicon

        lexicon = load_lexicon(Path(self.workspace.root) / self.config.lexicon_file)
        return MemoryStore(
            Path(self.workspace.root) / self.config.memory_dir / "memory.db",
            lexicon=lexicon,
        )

    def _library(self) -> Any:
        from .library import Library
        from .semantics import load_lexicon

        lexicon = load_lexicon(Path(self.workspace.root) / self.config.lexicon_file)
        return Library(Path(self.workspace.root) / self.config.library_dir, lexicon=lexicon)

    def _council(self) -> Any:
        from .council import Council

        return Council(self.config)

    def run(self, task: str) -> Dict[str, Any]:
        task = (task or "").strip()
        if not task:
            raise StudioError("empty message")
        with self._lock:
            if self._team is None:
                self._team = self._build_team()
            result = self._team.run(task, verify_override=self.config.verify_command)
            self._session.record_run(result)
            return _reply(result)

    def _home(self) -> HomeSetup:
        if self._home_setup is None:
            self._home_setup = HomeSetup(self.config, self.workspace.root)
        return self._home_setup

    def home_status(self) -> Dict[str, Any]:
        return self._home().status()

    def home_scan(self, url: Any, token: Any) -> Dict[str, Any]:
        return self._home().scan(url, token)

    def home_connect(
        self,
        url: Any,
        token: Any,
        name: Any,
        entities: Any,
        allow_control: bool = False,
    ) -> Dict[str, Any]:
        return self._home().connect(
            url, token, name, entities, allow_control=allow_control
        )

    def info(self) -> Dict[str, Any]:
        agents: List[Dict[str, Any]] = []
        for spec in ROSTER.values():
            agents.append(
                {
                    "key": spec.key,
                    "name": spec.name,
                    "title": spec.title,
                    "tools": list(spec.tools),
                    "custom": False,
                }
            )
        for spec in self.custom_agents.values():
            agents.append(
                {
                    "key": spec.key,
                    "name": spec.name,
                    "title": spec.title,
                    "tools": list(spec.tools),
                    "custom": True,
                }
            )
        return {
            "app": "OffGrid Studio",
            "workspace": str(self.workspace.root),
            "endpoint": self.config.resolved_endpoint(),
            "model": self.config.model or "auto",
            "agents": agents,
            "custom_agents": len(self.custom_agents),
            "warnings": list(self.warnings),
        }

    def busy(self) -> bool:
        return self._lock.locked()


def _reply(result: RunResult) -> Dict[str, Any]:
    """Turn a full run into the one message the chat surface should show."""
    summary = (result.summary or "").strip() or "The team finished without a summary."
    lines = [summary]
    if result.changed:
        lines.extend(["", "**Changed:** " + ", ".join(result.changed[:12])])
    if result.verify_command:
        if result.verify_ok is True:
            lines.extend(["", "**Verified:** `" + result.verify_command + "`"])
        elif result.verify_ok is False:
            lines.extend(
                ["", "**Verification failed:** `" + result.verify_command + "`"]
            )
        else:
            lines.extend(
                ["", "**Not verified:** `" + result.verify_command + "`"]
            )
    if result.runs:
        detail = ", ".join(
            f"{run.agent} ({run.turns} turn{'s' if run.turns != 1 else ''})"
            for run in result.runs[:8]
        )
        lines.extend(["", "**Agents:** " + detail])
    stats = result.stats
    prompt_tokens = int(getattr(stats, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(stats, "completion_tokens", 0) or 0)
    return {
        "content": "\n".join(lines),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "meta": {
            "changed": list(result.changed),
            "verify": {
                "command": result.verify_command,
                "ok": result.verify_ok,
            },
            "agents": [
                {
                    "agent": run.agent,
                    "turns": run.turns,
                    "tool_calls": run.tool_calls,
                }
                for run in result.runs
            ],
        },
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _accepts_gzip(value: str) -> bool:
    """Return true only for an explicitly acceptable gzip coding."""
    wildcard = False
    for part in (value or "").split(","):
        fields = part.strip().split(";")
        token = fields[0].strip().lower()
        if token not in ("gzip", "*"):
            continue
        quality = 1.0
        for field in fields[1:]:
            key, _, raw = field.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        if token == "gzip":
            return quality > 0
        wildcard = quality > 0
    return wildcard


def _last_user_message(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "".join(parts).strip()
    return ""


class _StudioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    def do_GET(self) -> None:
        self.server.studio.handle_get(self)  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        self.server.studio.handle_post(self)  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


class StudioServer:
    """Threaded local server; bind to loopback unless an access key is set."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8099,
        runner: Optional[StudioRunner] = None,
        api_key: str = "",
    ) -> None:
        self.host = host
        self.runner = runner
        self.api_key = (api_key or "").strip()
        self.verbose = False
        self._assets = _StaticAssetCache()
        self.httpd = ThreadingHTTPServer((host, port), _StudioHandler)
        self.httpd.daemon_threads = True
        self.httpd.studio = self  # type: ignore[attr-defined]

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.5)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        code: int,
        content_type: str,
        body: bytes,
        cache: str = "no-store",
        extra: Optional[Dict[str, str]] = None,
        compress: bool = False,
    ) -> None:
        encoded = False
        if (
            compress
            and len(body) >= MIN_GZIP_BYTES
            and _accepts_gzip(handler.headers.get("Accept-Encoding", ""))
        ):
            candidate = gzip.compress(body, compresslevel=6)
            if len(candidate) < len(body):
                body = candidate
                encoded = True
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        if compress:
            handler.send_header("Vary", "Accept-Encoding")
            if encoded:
                handler.send_header("Content-Encoding", "gzip")
        for key, value in (extra or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    @staticmethod
    def _send_not_modified(
        handler: BaseHTTPRequestHandler,
        cache: str,
        etag: str,
    ) -> None:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()

    def _send_json(
        self,
        handler: BaseHTTPRequestHandler,
        code: int,
        payload: Dict[str, Any],
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self._send(
            handler,
            code,
            "application/json; charset=utf-8",
            _json_bytes(payload),
            extra=extra,
            compress=True,
        )

    def _send_error(
        self, handler: BaseHTTPRequestHandler, code: int, message: str
    ) -> None:
        self._send_json(
            handler,
            code,
            {"error": {"message": message, "type": "offgrid_error", "code": code}},
        )

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.api_key:
            return True
        auth = handler.headers.get("Authorization", "")
        if auth == f"Bearer {self.api_key}":
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = cookie.get("offgrid_studio")
        return bool(morsel and morsel.value == self.api_key)

    def _require_auth(self, handler: BaseHTTPRequestHandler) -> bool:
        if self._authorized(handler):
            return True
        self._send_error(handler, 401, "missing or invalid OffGrid Studio access key")
        return False

    def _login(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: Dict[str, List[str]],
    ) -> bool:
        """Accept ``?key=...`` once, set an HttpOnly cookie, then redirect."""
        if not self.api_key:
            return False
        supplied = (query.get("key") or [""])[0]
        if supplied != self.api_key:
            return False
        cookie = SimpleCookie()
        cookie["offgrid_studio"] = self.api_key
        cookie["offgrid_studio"]["path"] = "/"
        cookie["offgrid_studio"]["httponly"] = True
        cookie["offgrid_studio"]["samesite"] = "Strict"
        target = path or "/"
        handler.send_response(302)
        handler.send_header("Location", target)
        handler.send_header("Set-Cookie", cookie.output(header="").strip())
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    def _serve_static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR)
        except ValueError:
            self._send_error(handler, 404, "not found")
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            # SvelteKit routes such as /c/<id> are client-side.  Anything with
            # a file extension is a missing asset, not a route.
            if "." not in Path(relative).name:
                candidate = WEB_DIR / "index.html"
            else:
                self._send_error(handler, 404, "not found")
                return
        try:
            body, etag = self._assets.get(candidate)
        except OSError:
            self._send_error(handler, 404, "not found")
            return
        content_type = CONTENT_TYPES.get(
            candidate.suffix.lower(), "application/octet-stream"
        )
        cache = (
            "no-cache"
            if candidate.suffix.lower() in (".html", ".js", ".mjs", ".css")
            else "public, max-age=3600"
        )
        if handler.headers.get("If-None-Match") == etag:
            self._send_not_modified(handler, cache, etag)
            return
        self._send(
            handler,
            200,
            content_type,
            body,
            cache=cache,
            extra={"ETag": etag},
        )

    # -- GET -------------------------------------------------------------
    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        path = urllib.parse.unquote(parsed.path or "/")
        query = urllib.parse.parse_qs(parsed.query)
        if self._login(handler, path, query):
            return
        if path == "/health":
            self._send_json(handler, 200, {"status": "ok", "app": "OffGrid Studio"})
            return
        if path == "/v1/offgrid/home":
            if not self._require_auth(handler):
                return
            if self.runner is None or not hasattr(self.runner, "home_status"):
                self._send_error(handler, 503, "home setup is not available in this Studio")
                return
            try:
                self._send_json(handler, 200, self.runner.home_status())
            except HomeSetupError as exc:
                self._send_error(handler, 400, str(exc))
            return
        if path in ("/props", "/slots", "/tools", "/v1/models", "/v1/offgrid/info"):
            if not self._require_auth(handler):
                return
            if path == "/props":
                self._send_props(handler)
            elif path == "/slots":
                self._send_json(
                    handler,
                    200,
                    [
                        {
                            "id": 0,
                            "is_processing": bool(self.runner and self.runner.busy()),
                            "n_ctx": 8192,
                        }
                    ],
                )
            elif path == "/tools":
                self._send_json(handler, 200, {"tools": []})
            elif path == "/v1/models":
                self._send_json(
                    handler,
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": MODEL_ID,
                                "object": "model",
                                "created": 0,
                                "owned_by": "offgrid",
                                "name": "OffGrid",
                                "status": {"value": "loaded"},
                            }
                        ],
                    },
                )
            else:
                self._send_json(handler, 200, self.runner.info() if self.runner else {})
            return
        if path.startswith("/v1/") or path.startswith("/models/"):
            self._send_error(handler, 404, "not found")
            return
        self._serve_static(handler, path)

    def _send_props(self, handler: BaseHTTPRequestHandler) -> None:
        config = getattr(self.runner, "config", None)
        max_tokens = int(getattr(config, "max_tokens", 2048) or 2048)
        temperature = float(getattr(config, "temperature", 0.2) or 0.2)
        payload = {
            "model_path": "offgrid",
            "model_alias": "offgrid",
            "model_name": "offgrid",
            "default_generation_settings": {
                "n_ctx": 8192,
                "params": {
                    "n_predict": max_tokens,
                    "temperature": temperature,
                    "top_k": 40,
                    "top_p": 0.95,
                },
            },
            "total_slots": 1,
            "chat_template": "",
            "build_info": "OffGrid Studio",
            "is_sleeping": False,
            "modalities": {"vision": False, "audio": False},
        }
        self._send_json(handler, 200, payload)

    # -- POST ------------------------------------------------------------
    def _same_origin(self, handler: BaseHTTPRequestHandler) -> bool:
        """A browser POST must come from the page Studio itself served.

        Without this, any web page could POST a simple request to loopback and
        start a team run.  The JSON content-type check below is the second lock;
        a cross-origin simple request cannot set that header without a preflight.
        """
        origin = handler.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = urllib.parse.urlparse(origin).netloc
        except ValueError:
            origin_host = ""
        if origin_host and origin_host == handler.headers.get("Host", ""):
            return True
        self._send_error(handler, 403, "cross-origin requests are not allowed")
        return False

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._same_origin(handler):
            return
        parsed = urllib.parse.urlparse(handler.path)
        path = urllib.parse.unquote(parsed.path or "/")
        if path in ("/models/load", "/models/unload"):
            if not self._require_auth(handler):
                return
            self._send_error(
                handler, 400, "OffGrid chooses its model at startup; restart Studio to change it"
            )
            return
        if path in ("/v1/offgrid/home/scan", "/v1/offgrid/home/connect"):
            if not self._require_auth(handler):
                return
            body = self._read_json(handler)
            if body is None:
                return
            if self.runner is None or not hasattr(self.runner, "home_scan"):
                self._send_error(handler, 503, "home setup is not available in this Studio")
                return
            try:
                if path.endswith("/scan"):
                    result = self.runner.home_scan(body.get("url"), body.get("token"))
                else:
                    result = self.runner.home_connect(
                        body.get("url"),
                        body.get("token"),
                        body.get("name"),
                        body.get("entities"),
                        allow_control=bool(body.get("allow_control")),
                    )
            except HomeSetupError as exc:
                self._send_error(handler, 400, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - browser needs a readable error
                self._send_error(handler, 500, f"home setup stopped: {exc}")
                return
            self._send_json(handler, 200, result)
            return
        if path != "/v1/chat/completions":
            self._send_error(handler, 404, "not found")
            return
        if not self._require_auth(handler):
            return
        body = self._read_json(handler)
        if body is None:
            return
        task = _last_user_message(body.get("messages"))
        if not task:
            self._send_error(handler, 400, "messages must contain a non-empty user message")
            return
        request_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        if bool(body.get("stream")):
            self._stream(handler, task, request_id, created)
            return
        try:
            reply = self.runner.run(task) if self.runner else None
        except (StudioError, ProviderError) as exc:
            self._send_error(handler, 502, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - the browser needs a readable error
            self._send_error(handler, 500, f"the team stopped: {exc}")
            return
        if not isinstance(reply, dict):
            self._send_error(handler, 502, "the team returned no result")
            return
        self._send_json(
            handler,
            200,
            {
                "id": request_id,
                "object": "chat.completion",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": str(reply.get("content") or ""),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": reply.get("usage") or {},
            },
        )

    def _read_json(self, handler: BaseHTTPRequestHandler) -> Optional[Dict[str, Any]]:
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_error(handler, 415, "Content-Type must be application/json")
            return None
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error(handler, 400, "invalid Content-Length")
            return None
        if length <= 0:
            self._send_error(handler, 400, "empty request body")
            return None
        if length > MAX_BODY:
            self._send_error(handler, 413, "request body is too large")
            return None
        encoding = handler.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity", "gzip"):
            self._send_error(handler, 415, "Content-Encoding must be gzip")
            return None
        raw = handler.rfile.read(length)
        if encoding == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    raw = stream.read(MAX_BODY + 1)
            except (OSError, EOFError, zlib.error):
                self._send_error(handler, 400, "request body is not valid gzip")
                return None
            if len(raw) > MAX_BODY:
                self._send_error(handler, 413, "request body is too large")
                return None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_error(handler, 400, "request body must be JSON")
            return None
        if not isinstance(body, dict):
            self._send_error(handler, 400, "request body must be a JSON object")
            return None
        return body

    def _stream(
        self,
        handler: BaseHTTPRequestHandler,
        task: str,
        request_id: str,
        created: int,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.close_connection = True
        self._sse(
            handler,
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            },
        )
        results: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

        def work() -> None:
            try:
                results.put(("ok", self.runner.run(task) if self.runner else None))
            except BaseException as exc:  # noqa: BLE001 - reported to the browser
                results.put(("error", exc))

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        outcome = ""
        value: Any = None
        while True:
            try:
                outcome, value = results.get(timeout=10.0)
                break
            except queue.Empty:
                if not self._write(handler, b": keepalive\n\n"):
                    return
        if outcome == "error":
            content = f"\n\n**OffGrid could not run this task:** {value}"
            usage: Dict[str, Any] = {}
        else:
            reply = value if isinstance(value, dict) else {}
            content = str(reply.get("content") or "")
            usage = reply.get("usage") or {}
        for start in range(0, len(content), 800):
            piece = content[start : start + 800]
            if not self._sse(
                handler,
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": piece},
                            "finish_reason": None,
                        }
                    ],
                },
            ):
                return
        if not self._sse(
            handler,
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            },
        ):
            return
        self._write(handler, b"data: [DONE]\n\n")

    @staticmethod
    def _write(handler: BaseHTTPRequestHandler, data: bytes) -> bool:
        try:
            handler.wfile.write(data)
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _sse(self, handler: BaseHTTPRequestHandler, payload: Dict[str, Any]) -> bool:
        return self._write(
            handler,
            b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n",
        )
