"""Friendly first-run setup for people who have never used a terminal.

The normal Studio command assumes that Python and a local model server already
exist.  That is a reasonable assumption for a developer and a terrible one for
someone who just downloaded OffGrid to try it.  This module is the front door
used by the double-click launchers:

* it runs a tiny local web page, so there is no prompt to answer in a console;
* it finds an existing Ollama / LM Studio / llama.cpp server when there is one;
* otherwise, with one click, it downloads a small llama.cpp engine and a
  starter Qwen model into this OffGrid folder, verifies the model checksum, and
  starts them;
* when the model answers it opens Studio, the actual chat interface.

Nothing is installed system-wide.  Nothing is sent anywhere except the two
downloads the user explicitly starts.  The model and runtime stay in the
OffGrid folder, next to the launchers.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import signal
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .config import Config
from .fullbundle import (
    MODELS,
    platform_key,
    resolve_model,
    extract,
    flatten_binary_dir,
)
from .providers import discover
from .studio import StudioRunner, StudioServer
from .workspace import Workspace

WEB_DIR = Path(__file__).resolve().parent / "web"
STARTER_MODEL = "3b"
DEFAULT_PORT = 8099
MODEL_LABEL = "Qwen2.5-Coder 3B"
UA = f"offgrid-easy/{__version__}"
# Stable llama.cpp release used for the first run. These are the official
# ggml-org CPU builds, pinned so setup does not depend on the GitHub API (which
# rate-limits anonymous users) or on a build that a still-supported older
# Linux cannot load. b9000 needs glibc 2.34, so Ubuntu 22.04 works.
_PINNED_LLAMA_TAG = "b9000"
_PINNED_LLAMA_ASSETS = {
    ("linux", "x64"): "llama-b9000-bin-ubuntu-x64.tar.gz",
    ("linux", "arm64"): "llama-b9000-bin-ubuntu-arm64.tar.gz",
    ("darwin", "arm64"): "llama-b9000-bin-macos-arm64.tar.gz",
    ("darwin", "x64"): "llama-b9000-bin-macos-x64.tar.gz",
    ("windows", "x64"): "llama-b9000-bin-win-cpu-x64.zip",
    ("windows", "arm64"): "llama-b9000-bin-win-cpu-arm64.zip",
}
PINNED_LLAMA = {
    key: (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{_PINNED_LLAMA_TAG}/{name}",
        name,
    )
    for key, name in _PINNED_LLAMA_ASSETS.items()
}


class EasyError(Exception):
    """A setup step failed in a way the browser can explain."""


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _free_port(preferred: int) -> int:
    """Return a localhost port, preferring the friendly default."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return int(sock.getsockname()[1])
            except OSError:
                continue
    raise EasyError("cannot find a free local port")


def _probe(endpoint: str, timeout: float = 2.0) -> Tuple[bool, str]:
    """Check one OpenAI-compatible endpoint without raising at the UI."""
    url = endpoint.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return False, ""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(entries, list):
        names = [str(item.get("id")) for item in entries if isinstance(item, dict) and item.get("id")]
        if names:
            return True, names[0]
    return False, ""


class EasyController:
    """State and one-click installation for the setup page."""

    def __init__(self, config: Config, workspace_root: Path) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.app_root = self._find_app_root()
        self._lock = threading.RLock()
        self._install_lock = threading.Lock()
        self._studio_server: Optional[StudioServer] = None
        self._studio_thread: Optional[threading.Thread] = None
        self._child: Optional[subprocess.Popen[Any]] = None
        self._endpoint = ""
        self._model_name = ""
        self._ready = False
        self._stage = "checking"
        self._message = "Checking this computer..."
        self._done = 0
        self._total = 0
        self._error = ""
        self._source = ""
        self._server_url = ""
        self._studio_url = ""

    # -- paths -----------------------------------------------------------
    def _find_app_root(self) -> Path:
        """Use the unpacked OffGrid folder when it is writable.

        A wheel install has no writable launcher folder, so it stores the
        runtime and model under the user's home instead.  Both choices are
        local and are shown in the setup page.
        """
        candidate = Path(__file__).resolve().parent.parent
        if (candidate / "OffGrid.sh").is_file() and os.access(candidate, os.W_OK):
            return candidate
        return Path.home() / ".offgrid" / "easy"

    @property
    def models_dir(self) -> Path:
        return self.app_root / "models"

    @property
    def runtime_dir(self) -> Path:
        return self.app_root / "runtime" / "llama"

    @property
    def downloads_dir(self) -> Path:
        return self.app_root / ".downloads"

    def _model_path(self) -> Optional[Path]:
        try:
            files = sorted(self.models_dir.glob("*.gguf"))
        except OSError:
            return None
        return files[0] if files else None

    def _llama_binary(self) -> Optional[Path]:
        names = ("llama-server.exe", "llama-server")
        for name in names:
            direct = self.runtime_dir / name
            if direct.is_file():
                return direct
        try:
            for name in names:
                found = next(iter(self.runtime_dir.rglob(name)), None)
                if found and found.is_file():
                    return found
        except OSError:
            pass
        return None

    # -- public state ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            percent = int(self._done * 100 / self._total) if self._total else 0
            return {
                "ready": self._ready,
                "stage": self._stage,
                "message": self._message,
                "error": self._error,
                "source": self._source,
                "server_url": self._server_url,
                "studio_url": self._studio_url,
                "workspace": str(self.workspace_root),
                "app_folder": str(self.app_root),
                "model": MODEL_LABEL,
                "model_key": STARTER_MODEL,
                "model_size": "about 2 GB",
                "progress": {
                    "done": self._done,
                    "total": self._total,
                    "percent": percent,
                    "label": _human_bytes(self._done) if self._done else "",
                },
            }

    def _set(
        self,
        stage: str,
        message: str,
        *,
        done: Optional[int] = None,
        total: Optional[int] = None,
        error: str = "",
    ) -> None:
        with self._lock:
            self._stage = stage
            self._message = message
            self._error = error
            if done is not None:
                self._done = max(0, done)
            if total is not None:
                self._total = max(0, total)

    def _mark_ready(self, endpoint: str, source: str) -> str:
        with self._lock:
            self.config.endpoint = endpoint
            self._server_url = endpoint
            self._endpoint = endpoint
            self._source = source
            # Studio is part of readiness: if its port cannot be opened, do
            # not report a ready setup page that can never continue.
            self._ready = False
            self._error = ""
            self._stage = "loading"
            self._message = "Opening OffGrid..."
            self._done = 0
            self._total = 0
        studio = self._start_studio()
        with self._lock:
            self._ready = True
            self._stage = "ready"
            self._message = "OffGrid is ready."
        return studio

    # -- discovery / startup ---------------------------------------------
    def check(self) -> Dict[str, Any]:
        """Look for a server or a previously downloaded local model."""
        self._set("checking", "Looking for an AI on this computer...")
        if _probe(self.config.resolved_endpoint())[0]:
            return self._mark_ready(self.config.resolved_endpoint(), "existing")
        found = discover(self.config)
        if found:
            endpoint, names = found[0]
            self.config.model = names[0] if names else ""
            return self._mark_ready(endpoint, "existing")

        if _probe("http://127.0.0.1:8089/v1")[0]:
            return self._mark_ready("http://127.0.0.1:8089/v1", "existing")

        model = self._model_path()
        binary = self._llama_binary()
        if model and binary:
            self._set("loading", "Starting the AI that is already on this computer...")
            try:
                endpoint = self._start_model_server(model, binary)
            except EasyError as exc:
                self._set("error", "The local AI could not start.", error=str(exc))
                return self.status()
            return self._mark_ready(endpoint, "download")

        self._set(
            "needs-model",
            "No local AI found yet. Download the free starter model to continue.",
        )
        return self.status()

    def _start_model_server(self, model: Path, binary: Path) -> str:
        preferred = 8089
        endpoint = ""
        for port in (preferred, 8090, 8091, 8092):
            if _probe(f"http://127.0.0.1:{port}/v1")[0]:
                return f"http://127.0.0.1:{port}/v1"
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            endpoint = f"http://127.0.0.1:{port}/v1"
            break
        if not endpoint:
            raise EasyError("no free local port is available for the model server")

        logs = self.app_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_handle = (logs / "llama.log").open("ab")
        command = [
            str(binary),
            "-m",
            str(model),
            "--host",
            "127.0.0.1",
            "--port",
            endpoint.rsplit(":", 1)[1].split("/", 1)[0],
            "-c",
            "8192",
            "--jinja",
        ]
        try:
            self._child = subprocess.Popen(
                command,
                cwd=str(self.app_root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            log_handle.close()
            raise EasyError(f"could not start the local AI: {exc}") from exc

        deadline = time.time() + 180
        while time.time() < deadline:
            if _probe(endpoint)[0]:
                log_handle.close()
                return endpoint
            if self._child.poll() is not None:
                log_handle.close()
                tail = ""
                try:
                    tail = (logs / "llama.log").read_text(encoding="utf-8", errors="replace")[-800:]
                except OSError:
                    pass
                raise EasyError(
                    "the local AI stopped before it was ready"
                    + (f": {tail.strip()}" if tail.strip() else "")
                )
            time.sleep(1.0)
        log_handle.close()
        if self._child is not None and self._child.poll() is None:
            try:
                self._child.terminate()
                self._child.wait(timeout=3)
            except Exception:
                try:
                    self._child.kill()
                    self._child.wait(timeout=3)
                except Exception:
                    pass
        raise EasyError("the local AI took too long to start")

    # -- installation ----------------------------------------------------
    def start_install(self) -> Dict[str, Any]:
        """Kick off the one-button install, or report that it is already busy."""
        with self._lock:
            if self._ready:
                return self.status()
        if not self._install_lock.acquire(blocking=False):
            return self.status()
        self._set("preparing", "Getting the free starter AI ready...")
        thread = threading.Thread(target=self._install_worker, daemon=True)
        thread.start()
        return self.status()

    def _install_worker(self) -> None:
        try:
            self._install_locked()
        except EasyError as exc:
            self._set("error", "Setup could not finish.", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to a non-technical user
            self._set(
                "error",
                "Setup could not finish.",
                error=(
                    f"{exc}. Check your internet connection and try again. "
                    "If it keeps failing, install Ollama from ollama.com/download."
                ),
            )
        finally:
            self._install_lock.release()

    def _install_locked(self) -> None:
        # Re-check in case another server was started while the button was
        # clicked (for example, the user opened Ollama).
        if _probe(self.config.resolved_endpoint())[0]:
            self._mark_ready(self.config.resolved_endpoint(), "existing")
            return
        found = discover(self.config)
        if found:
            endpoint, names = found[0]
            self.config.model = names[0] if names else ""
            self._mark_ready(endpoint, "existing")
            return

        model = self._model_path()
        binary = self._llama_binary()
        if not (model and binary):
            self._set("engine", "Downloading the local AI engine...")
            binary = self._ensure_runtime()

        if model is None:
            self._require_free_space(3 * 1024 ** 3)
            model = self._download_model()

        self._set("loading", "Starting the AI for the first time...")
        endpoint = self._start_model_server(model, binary)
        self._mark_ready(endpoint, "download")

    def _ensure_runtime(self) -> Path:
        os_name, arch = platform_key()
        # The pinned release is tried first: it is deterministic, needs no API
        # call and is known to load on Ubuntu 22.04 as well as current systems.
        resolved = PINNED_LLAMA.get((os_name, arch))
        if not resolved:
            raise EasyError(
                "No ready-made AI engine is available for this computer. "
                "Install Ollama from ollama.com/download, start it, then click Check again."
            )
        url, name = resolved
        archive = self.downloads_dir / name
        self._download(url, archive, "", "Downloading the local AI engine")
        staging = self.runtime_dir.parent / "llama-tmp"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            extract(archive, staging)
        except Exception as exc:
            # A truncated archive must not be reused on the next click.
            try:
                archive.unlink()
            except OSError:
                pass
            shutil.rmtree(staging, ignore_errors=True)
            raise EasyError("the AI engine download was incomplete; please try again") from exc
        try:
            archive.unlink()
        except OSError:
            pass
        binary_name = "llama-server.exe" if os_name == "windows" else "llama-server"
        located = flatten_binary_dir(staging, binary_name)
        if not located:
            raise EasyError("the downloaded AI engine did not contain llama-server")
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
        wanted = self.runtime_dir
        wanted.parent.mkdir(parents=True, exist_ok=True)
        if staging.name == wanted.name or len(list(staging.iterdir())) == 1:
            children = list(staging.iterdir())
            if len(children) == 1 and children[0].is_dir():
                shutil.move(str(children[0]), str(wanted))
                shutil.rmtree(staging, ignore_errors=True)
            else:
                shutil.move(str(staging), str(wanted))
        else:
            shutil.move(str(staging), str(wanted))
        found = self._llama_binary()
        if not found:
            raise EasyError("the AI engine was downloaded but could not be found")
        try:
            found.chmod(found.stat().st_mode | 0o111)
        except OSError:
            pass
        return found

    def _require_free_space(self, required: int) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(self.models_dir).free
        except OSError:
            return
        if free < required:
            raise EasyError(
                "There is not enough free disk space for the starter model. "
                f"OffGrid needs about {_human_bytes(required)} free; "
                f"this folder has {_human_bytes(free)}. Free some space and try again."
            )

    def _download_model(self) -> Path:
        resolved = resolve_model(STARTER_MODEL, False)
        if not resolved:
            raise EasyError("the starter model is not available; try again later")
        url, name, spec = resolved
        self.models_dir.mkdir(parents=True, exist_ok=True)
        destination = self.models_dir / name
        self._download(
            url,
            destination,
            str(spec.get("sha256") or ""),
            f"Downloading {MODEL_LABEL}",
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise EasyError("the model download did not produce a model file")
        return destination

    def _download(
        self,
        url: str,
        destination: Path,
        expected_sha256: str,
        label: str,
    ) -> None:
        """Download with a browser-visible progress bar and checksum check."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and destination.stat().st_size > 0:
            if not expected_sha256 or self._sha256(destination) == expected_sha256.lower():
                self._set("downloading", f"{label} is already downloaded.", done=1, total=1)
                return
            destination.unlink()
        part = destination.with_name(destination.name + ".part")
        part.unlink(missing_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            response = urllib.request.urlopen(request, timeout=120)
        except (urllib.error.URLError, OSError) as exc:
            raise EasyError(
                f"{label} could not be downloaded. Check the internet connection and retry."
            ) from exc
        total = 0
        try:
            header = response.headers.get("Content-Length")
            if header:
                total = int(header)
        except ValueError:
            total = 0
        done = 0
        self._set("downloading", label, done=0, total=total)
        try:
            with part.open("wb") as handle, response:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    self._set("downloading", label, done=done, total=total)
        except (OSError, urllib.error.URLError) as exc:
            part.unlink(missing_ok=True)
            raise EasyError(f"{label} stopped downloading: {exc}") from exc
        if expected_sha256:
            actual = self._sha256(part)
            if actual.lower() != expected_sha256.lower():
                part.unlink(missing_ok=True)
                raise EasyError(
                    "The downloaded model was damaged. Nothing was installed; "
                    "please try the download again."
                )
        part.replace(destination)
        self._set("downloading", f"{label} downloaded.", done=done, total=total or done)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # -- Studio ----------------------------------------------------------
    def _start_studio(self) -> str:
        with self._lock:
            if self._studio_url:
                return self._studio_url
        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            runner = StudioRunner(self.config, Workspace(self.workspace_root))
            server = StudioServer("127.0.0.1", 0, runner)
        except OSError as exc:
            raise EasyError(f"could not open the OffGrid app: {exc}") from exc
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with self._lock:
            self._studio_server = server
            self._studio_thread = thread
            self._studio_url = server.url
            return server.url

    def open_studio(self) -> str:
        if not self._ready:
            raise EasyError("the AI is not ready yet")
        return self._start_studio()

    def close(self) -> None:
        server = self._studio_server
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
        child = self._child
        if child is not None and child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=3)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# tiny setup web server
# ---------------------------------------------------------------------------
class _EasyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.easy.handle_get(self)  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self.server.easy.handle_post(self)  # type: ignore[attr-defined]


class EasyServer:
    """The setup page and its small local JSON API."""

    def __init__(self, controller: EasyController, host: str = "127.0.0.1", port: int = 0) -> None:
        self.controller = controller
        self.httpd = ThreadingHTTPServer((host, port), _EasyHandler)
        self.httpd.daemon_threads = True
        self.httpd.easy = self  # type: ignore[attr-defined]

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.5)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- transport -------------------------------------------------------
    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        code: int,
        content_type: str,
        body: bytes,
        *,
        cache: str = "no-store",
    ) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    def _json(self, handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
        self._send(
            handler,
            code,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _static(self, handler: BaseHTTPRequestHandler, relative: str) -> None:
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR)
        except ValueError:
            self._json(handler, 404, {"error": {"message": "not found"}})
            return
        if not candidate.is_file():
            self._json(handler, 404, {"error": {"message": "not found"}})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        try:
            body = candidate.read_bytes()
        except OSError:
            self._json(handler, 404, {"error": {"message": "not found"}})
            return
        self._send(handler, 200, content_type, body, cache="no-cache")

    @staticmethod
    def _same_origin(handler: BaseHTTPRequestHandler) -> bool:
        origin = handler.headers.get("Origin", "")
        if not origin:
            return True
        try:
            host = urlparse(origin).netloc
        except ValueError:
            return False
        return host == handler.headers.get("Host", "")

    # -- routes ----------------------------------------------------------
    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        path = urlparse(handler.path).path or "/"
        if path in ("/", "/setup"):
            self._static(handler, "setup.html")
            return
        if path == "/api/status":
            self._json(handler, 200, self.controller.status())
            return
        if path == "/health":
            self._json(handler, 200, {"status": "ok", "app": "OffGrid setup"})
            return
        if path in ("/setup.css", "/setup.js", "/offgrid.svg"):
            self._static(handler, path.lstrip("/"))
            return
        self._json(handler, 404, {"error": {"message": "not found"}})

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._same_origin(handler):
            self._json(handler, 403, {"error": {"message": "cross-origin request refused"}})
            return
        path = urlparse(handler.path).path
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length:
            handler.rfile.read(min(length, 65536))
        if path == "/api/install":
            self._json(handler, 202, self.controller.start_install())
            return
        if path == "/api/recheck":
            try:
                self._json(handler, 200, self.controller.check())
            except EasyError as exc:
                self._json(handler, 500, {"error": {"message": str(exc)}})
            return
        if path == "/api/open":
            try:
                url = self.controller.open_studio()
                self._json(handler, 200, {"studio_url": url})
            except EasyError as exc:
                self._json(handler, 409, {"error": {"message": str(exc)}})
            return
        self._json(handler, 404, {"error": {"message": "not found"}})


def run_easy(
    config: Config,
    workspace_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """Run the setup page, or Studio directly when a model is already ready."""
    controller = EasyController(config, workspace_root)

    def _stop_on_signal(_signum: int, _frame: Any) -> None:
        # A closed terminal sends SIGHUP/SIGTERM instead of Ctrl+C. Stop the
        # model process with the window instead of leaving it running.
        controller.close()
        raise SystemExit(0)

    old_handlers = {}
    for signal_name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            try:
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _stop_on_signal)
            except (OSError, ValueError):
                pass
    atexit.register(controller.close)
    try:
        try:
            controller.check()
        except EasyError as exc:
            controller._set("error", "OffGrid could not finish its first check.", error=str(exc))
        if controller.status().get("ready"):
            url = controller.open_studio()
            print()
            print("  OffGrid is ready.")
            print(f"  Open: {url}")
            if open_browser:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            print("  Keep this window open while you use OffGrid. Ctrl+C stops it.")
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print()
                print("  Stopped OffGrid.")
            return 0

        chosen = _free_port(port)
        server = EasyServer(controller, host=host, port=chosen)
        print()
        print("  OffGrid setup is open in your browser.")
        print(f"  If it did not open: {server.url}")
        print("  No commands to type. Keep this window open while setup runs.")
        if open_browser:
            try:
                webbrowser.open(server.url)
            except Exception:
                pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
            print("  Stopped OffGrid setup.")
        finally:
            try:
                server.stop()
            except Exception:
                pass
        return 0
    finally:
        controller.close()
        for signum, handler in old_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    print("Run OffGrid with no arguments, or: offgrid easy", file=sys.stderr)
    raise SystemExit(2)
