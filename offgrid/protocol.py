"""Fallback tool protocol for models without native function calling.

Most small local models handle a fenced JSON block better than the OpenAI
`tool_calls` field. Both work: native when the server supports it, this when
it does not.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, List, Sequence

from .models import ToolCall

TOOL_BLOCK_RE = re.compile(r"```(?:tool|offgrid|json)?\s*\n?(.*?)```", re.DOTALL)

PROTOCOL_DOC = """\
TOOL PROTOCOL
You act by calling tools. To call one, reply with exactly one fenced block and
no other text:

```tool
{{"tool": "read_file", "args": {{"path": "src/app.py"}}}}
```

Rules:
- One tool per reply. Wait for the result before the next call.
- `args` must always be a JSON object.
- Never invent file contents; read them first.
- When the task is complete, call the `finish` tool with a short summary.

Available tools:
{tools}
"""


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"value": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def parse_text_tool_calls(text: str, allowed: Iterable[str]) -> List[ToolCall]:
    """Extract tool calls from fenced blocks in a plain-text reply."""
    allowed_set = set(allowed)
    calls: List[ToolCall] = []
    for match in TOOL_BLOCK_RE.finditer(text or ""):
        body = match.group(1).strip()
        if not body.startswith("{"):
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("tool") or data.get("name") or data.get("function")
        if isinstance(name, dict):  # {"function": {"name": ...}}
            name = name.get("name")
        if not isinstance(name, str) or name not in allowed_set:
            continue
        args = data.get("args")
        if args is None:
            args = data.get("arguments")
        if args is None:
            args = data.get("params")
        calls.append(ToolCall(name=name, arguments=_coerce_args(args)))
    return calls


def strip_tool_blocks(text: str) -> str:
    """Remove tool blocks so only the human-readable part is displayed."""
    cleaned = TOOL_BLOCK_RE.sub("", text or "").strip()
    return cleaned
