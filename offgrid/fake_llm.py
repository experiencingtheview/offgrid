"""A scripted, OpenAI-compatible model server for demos and tests.

It lets the whole agent stack run with zero downloads and zero network beyond
localhost: useful for CI, for testing the orchestrator, and for showing the
tool loop working before anyone has a GPU.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# response builders
# --------------------------------------------------------------------------
def text(content: str) -> Dict[str, Any]:
    return {"content": content}


def tool(name: str, **arguments: Any) -> Dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "call_" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def when(agent: str, response: Dict[str, Any]) -> Tuple[Callable, Dict[str, Any]]:
    """Match a request whose system prompt belongs to ``agent``."""
    needle = f"You are {agent}"

    def matcher(body: Dict[str, Any]) -> bool:
        for message in body.get("messages") or []:
            if message.get("role") == "system" and needle in (message.get("content") or ""):
                return True
        return False

    return matcher, response


def when_contains(fragment: str, response: Dict[str, Any]) -> Tuple[Callable, Dict[str, Any]]:
    def matcher(body: Dict[str, Any]) -> bool:
        blob = json.dumps(body.get("messages") or [])
        return fragment in blob

    return matcher, response


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class FakeModel:
    def __init__(
        self,
        rules: List[Tuple[Callable, Dict[str, Any]]],
        reject_tools: bool = False,
        default: Optional[Dict[str, Any]] = None,
        model_name: str = "fake-coder-7b",
    ) -> None:
        self.rules = list(rules)
        self.used = [False] * len(self.rules)
        self.reject_tools = reject_tools
        self.default = default or text("(no scripted response left)")
        self.model_name = model_name
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- logic -----------------------------------------------------------
    def answer(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            for index, (matcher, response) in enumerate(self.rules):
                if self.used[index]:
                    continue
                try:
                    matched = bool(matcher(body))
                except Exception:
                    matched = False
                if matched:
                    self.used[index] = True
                    return response
            return self.default

    def envelope(self, response: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        message: Dict[str, Any] = {"role": "assistant", "content": response.get("content", "")}
        if response.get("tool_calls"):
            message["tool_calls"] = response["tool_calls"]
            message["content"] = response.get("content", "")
        prompt_chars = len(json.dumps(body.get("messages") or []))
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": self.model_name,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_chars // 4,
                "completion_tokens": len(json.dumps(response)) // 4,
                "total_tokens": (prompt_chars + len(json.dumps(response))) // 4,
            },
        }

    # -- lifecycle -------------------------------------------------------
    def start(self) -> str:
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send(self, code: int, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": model.model_name, "object": "model"}]})
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    self._send(400, {"error": {"message": "invalid json"}})
                    return
                model.requests.append(body)
                if model.reject_tools and body.get("tools"):
                    self._send(
                        400,
                        {"error": {"message": "this model does not support tools / function calling"}},
                    )
                    return
                response = model.answer(body)
                self._send(200, model.envelope(response, body))

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        if not self._httpd:
            raise RuntimeError("server not started")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self) -> "FakeModel":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def serve(
    rules: List[Tuple[Callable, Dict[str, Any]]],
    reject_tools: bool = False,
    default: Optional[Dict[str, Any]] = None,
) -> FakeModel:
    model = FakeModel(rules, reject_tools=reject_tools, default=default)
    model.start()
    return model
