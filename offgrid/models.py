"""Core data types shared by every part of OffGrid."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """A request from the model to run one of our tools."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "call_" + uuid.uuid4().hex[:12]

    def to_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    def pretty_args(self, limit: int = 100) -> str:
        """Short one-line rendering for the UI: read_file("src/app.py")."""
        bits = []
        for key, value in self.arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            text = text.replace("\n", "\\n")
            if len(text) > limit:
                text = text[: limit - 1] + "\u2026"
            bits.append(text if key in ("path", "command", "pattern", "old", "file") else f"{key}={text}")
        return ", ".join(bits)


@dataclass
class Message:
    """One chat message, in the shape the OpenAI-compatible API expects."""

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def to_api(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [call.to_api() for call in self.tool_calls]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Message":
        calls: List[ToolCall] = []
        for raw_call in raw.get("tool_calls") or []:
            fn = raw_call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"value": args}
            calls.append(
                ToolCall(
                    name=str(fn.get("name") or ""),
                    arguments=args,
                    id=str(raw_call.get("id") or ""),
                )
            )
        return cls(
            role=str(raw.get("role") or "assistant"),
            content=raw.get("content") or "",
            tool_calls=calls,
        )


@dataclass
class Completion:
    """Normalised reply from a local model server."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    elapsed: float = 0.0


@dataclass
class ToolResult:
    ok: bool
    content: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def render(self, limit: int = 4000) -> str:
        body = self.content
        if len(body) > limit:
            head = body[: limit // 2]
            tail = body[-limit // 2 :]
            body = f"{head}\n... [{len(self.content) - limit} chars trimmed] ...\n{tail}"
        if self.ok:
            return body
        return f"ERROR: {body}" if body else "ERROR"


@dataclass
class Stats:
    """Cheap token accounting so users can see what a run cost them (nothing)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    started: float = field(default_factory=time.time)
    by_agent: Dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def add(self, agent: str, completion: Completion) -> None:
        self.prompt_tokens += completion.prompt_tokens
        self.completion_tokens += completion.completion_tokens
        self.model_calls += 1
        self.by_agent[agent] = self.by_agent.get(agent, 0) + 1

    def summary(self) -> str:
        mins, secs = divmod(int(self.elapsed), 60)
        tokens = self.total_tokens
        tok_text = f"{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"{tokens} tokens"
        return (
            f"{self.model_calls} model calls, {self.tool_calls} tool calls, "
            f"{tok_text}, {mins}m{secs:02d}s"
        )
