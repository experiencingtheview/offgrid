"""Client for any OpenAI-compatible model server on localhost.

Covers llama.cpp (`llama-server`), Ollama, LM Studio, vLLM and
llama-cpp-python. Nothing here ever contacts a remote host unless the user has
explicitly opted in with --allow-remote.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Union

from .config import CANDIDATE_ENDPOINTS, Config
from .models import Completion, Message, ToolCall
from .protocol import parse_text_tool_calls, strip_tool_blocks

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}


class ProviderError(Exception):
    """Something went wrong talking to the local model server."""


def is_loopback(endpoint: str) -> bool:
    try:
        host = urllib.parse.urlparse(endpoint).hostname or ""
    except ValueError:
        return False
    return host.lower() in LOOPBACK_HOSTS


class LocalProvider:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.endpoint = config.resolved_endpoint()
        self.model = config.model
        self.native_tools: Optional[bool] = config.native_tools
        self._probed = False

        if not is_loopback(self.endpoint) and not config.allow_remote:
            raise ProviderError(
                f"endpoint {self.endpoint} is not on this machine. OffGrid is offline by design; "
                "pass --allow-remote only if you really want to talk to another host."
            )

    # -- transport -------------------------------------------------------
    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None):
        url = f"{self.endpoint}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.request_timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            raise ProviderError(f"HTTP {exc.code} from {url}: {detail or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"cannot reach {url} ({exc.reason}). Is the model server running? "
                "Try `offgrid doctor`."
            ) from None
        except (socket.timeout, TimeoutError):
            raise ProviderError(f"{url} timed out after {timeout or self.config.request_timeout}s") from None
        try:
            return json.loads(body)
        except ValueError:
            raise ProviderError(f"{url} did not return JSON: {body[:300]}") from None

    # -- discovery -------------------------------------------------------
    def models(self) -> List[str]:
        payload = self._request("/models", timeout=10)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        return [str(item.get("id")) for item in entries if isinstance(item, dict) and item.get("id")]

    def health(self) -> tuple:
        try:
            names = self.models()
        except ProviderError as exc:
            return False, str(exc)
        if not names:
            return False, "server answered but reported no models"
        if not self.model:
            self.model = names[0]
        elif self.model not in names:
            return True, (
                f"reachable, but model '{self.model}' is not loaded. Available: {', '.join(names[:6])}"
            )
        return True, f"ok, model '{self.model}'"

    def _ensure_model(self) -> None:
        if self.model or self._probed:
            return
        self._probed = True
        try:
            names = self.models()
        except ProviderError:
            names = []
        self.model = names[0] if names else ""

    # -- chat ------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_names: Sequence[str] = (),
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Completion:
        self._ensure_model()
        payload: Dict[str, Any] = {
            "messages": [message.to_api() for message in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        if self.model:
            payload["model"] = self.model
        if tools and self.native_tools is not False:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.time()
        try:
            data = self._request("/chat/completions", payload)
        except ProviderError as exc:
            message = str(exc).lower()
            if tools and self.native_tools is None and (
                "tool" in message or "function" in message or "400" in message or "422" in message
            ):
                # Server refuses native tools: fall back to the text protocol.
                self.native_tools = False
                data = self._request(
                    "/chat/completions",
                    {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")},
                )
            else:
                raise

        return self._parse(data, text_protocol=self.native_tools is False, tool_names=tool_names, started=started)

    def _parse(
        self,
        data: Dict[str, Any],
        text_protocol: bool,
        tool_names: Sequence[str],
        started: float,
    ) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"empty response from model server: {json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))

        calls: List[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            parsed = Message.from_api({"role": "assistant", "tool_calls": [raw]})
            calls.extend(parsed.tool_calls)

        # Some small models emit a fenced tool block even when native tools were offered.
        # Parse it either way: a missed block would otherwise look like a silent stop.
        if not calls and tool_names:
            calls = parse_text_tool_calls(text, tool_names)
            if calls:
                text = strip_tool_blocks(text)

        usage = data.get("usage") or {}
        return Completion(
            text=text,
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self.model or ""),
            finish_reason=str(choices[0].get("finish_reason") or ""),
            elapsed=time.time() - started,
        )


class LoadBalancedProvider:
    """Round-robin across configured OpenAI-compatible model servers.

    The interface matches ``LocalProvider``. Each call goes to the next
    endpoint in order; if that endpoint fails, the call tries the others before
    reporting that every backend is unavailable. This is deliberately simple:
    it distributes calls and survives a dead server, but does not claim to
    measure queue depth or latency.
    """

    def __init__(self, config: Config, endpoints: Optional[Sequence[str]] = None) -> None:
        raw = endpoints if endpoints is not None else config.endpoints
        if isinstance(raw, str):
            raw = (raw,)
        seen = set()
        normalized = []
        for item in raw or ():
            endpoint = str(item or "").strip().rstrip("/")
            if endpoint and endpoint not in seen:
                seen.add(endpoint)
                normalized.append(endpoint)
        if not normalized:
            raise ProviderError("no endpoints configured for load balancing")

        self.config = config
        self.endpoints = tuple(normalized)
        self.backends: List[LocalProvider] = []
        for endpoint in self.endpoints:
            probe = Config(**config.as_dict())
            probe.endpoint = endpoint
            probe.endpoints = ()
            try:
                self.backends.append(LocalProvider(probe))
            except ProviderError as exc:
                raise ProviderError(f"balanced endpoint {endpoint}: {exc}") from None
        self.endpoint = self.endpoints[0]
        self.model = config.model
        self.native_tools = config.native_tools
        self._next = 0
        self._lock = threading.Lock()
        self.routes: Dict[str, int] = {}
        self.failures = 0

    @property
    def label(self) -> str:
        return f"{len(self.backends)} balanced endpoint(s)"

    def models(self) -> List[str]:
        names: List[str] = []
        for backend in self.backends:
            try:
                for name in backend.models():
                    if name not in names:
                        names.append(name)
            except ProviderError:
                continue
        return names

    def health(self) -> tuple:
        healthy = []
        unavailable = 0
        for backend in self.backends:
            ok, _message = backend.health()
            if ok:
                healthy.append(backend)
            else:
                unavailable += 1
        if not healthy:
            return False, "no balanced endpoint answered; check the servers and `offgrid doctor`"
        first = healthy[0]
        if first.model:
            self.model = first.model
        if self.native_tools is None and first.native_tools is not None:
            self.native_tools = first.native_tools
        detail = f"{len(healthy)}/{len(self.backends)} endpoint(s) ready"
        if unavailable:
            detail += f", {unavailable} unavailable"
        return True, detail

    def chat(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_names: Sequence[str] = (),
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Completion:
        if not self.backends:
            raise ProviderError("no balanced endpoints are available")
        count = len(self.backends)
        with self._lock:
            start = self._next
        errors: List[str] = []
        for offset in range(count):
            index = (start + offset) % count
            backend = self.backends[index]
            try:
                completion = backend.chat(
                    messages,
                    tools=tools,
                    tool_names=tool_names,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except ProviderError as exc:
                self.failures += 1
                errors.append(f"{backend.endpoint}: {exc}")
                continue
            with self._lock:
                self._next = (index + 1) % count
                self.routes[backend.endpoint] = self.routes.get(backend.endpoint, 0) + 1
            self.native_tools = backend.native_tools
            if completion.model:
                self.model = completion.model
            return completion
        raise ProviderError("all balanced endpoints failed: " + "; ".join(errors))

    def _parse(
        self,
        data: Dict[str, Any],
        text_protocol: bool,
        tool_names: Sequence[str],
        started: float,
    ) -> Completion:
        """Match LocalProvider for FleetProvider's peer-response parsing."""
        return self.backends[0]._parse(data, text_protocol, tool_names, started)

    def summary(self) -> str:
        if not self.routes:
            return ""
        parts = [f"{count} on {endpoint}" for endpoint, count in sorted(self.routes.items())]
        text = "balance: " + ", ".join(parts)
        if self.failures:
            text += f" ({self.failures} failed attempt(s))"
        return text


def discover(config: Config, candidates: Sequence[str] = CANDIDATE_ENDPOINTS) -> List[tuple]:
    """Probe localhost for a running OpenAI-compatible server."""
    found: List[tuple] = []
    for endpoint in candidates:
        probe = Config(**config.as_dict())
        probe.endpoint = endpoint
        probe.endpoints = ()
        probe.request_timeout = 2
        try:
            provider = LocalProvider(probe)
        except ProviderError:
            continue
        try:
            names = provider.models()
        except ProviderError:
            continue
        found.append((endpoint, names))
    return found


def open_provider(config: Config) -> Union[LocalProvider, LoadBalancedProvider]:
    """Build a provider, autodiscovering a local endpoint if none was given."""
    if config.endpoints:
        return LoadBalancedProvider(config)
    if config.endpoint:
        return LocalProvider(config)
    for endpoint, names in discover(config):
        config.endpoint = endpoint
        if not config.model and names:
            config.model = names[0]
        return LocalProvider(config)
    # Nothing listening: hand back the default so the error message is useful.
    return LocalProvider(config)
