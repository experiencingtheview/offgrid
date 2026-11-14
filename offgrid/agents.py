"""The team roster: who exists, what they may do, and how many turns they get."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import prompt
from .protocol import PROTOCOL_DOC

# Tool names each role may use.
READ_TOOLS: Tuple[str, ...] = (
    "list_files",
    "read_file",
    "search_files",
    "git_status",
    "git_diff",
    "recall",
    "library_search",
    "library_read",
    "devices_list",
    "device_read",
    "device_diagnose",
    "signals_status",
    "signal_read",
    "signal_scan",
    "research_query",
)
EDIT_TOOLS: Tuple[str, ...] = ("write_file", "edit_file")
RUN_TOOLS: Tuple[str, ...] = ("run_command", "run_fleet_command")
KNOWLEDGE_TOOLS: Tuple[str, ...] = ("remember", "recall", "library_search", "library_read", "ask_council")

ALL_TOOLS: Tuple[str, ...] = (
    "list_files",
    "read_file",
    "search_files",
    "write_file",
    "edit_file",
    "run_command",
    "run_fleet_command",
    "git_status",
    "git_diff",
    "remember",
    "recall",
    "library_search",
    "library_read",
    "ask_council",
    "devices_list",
    "device_read",
    "device_set",
    "device_diagnose",
    "signals_status",
    "signal_read",
    "signal_scan",
    "signal_send_ir",
    "research_query",
    "research_save",
    "finish",
)


@dataclass
class AgentSpec:
    key: str
    name: str
    title: str
    body: str
    tools: Tuple[str, ...] = ()
    max_turns: int = 10

    def system(self, primary: Optional[str] = None) -> str:
        text = prompt.system_prompt(self.name, self.title, self.body, primary=primary)
        if self.tools:
            text += "\n" + PROTOCOL_DOC.format(tools=prompt.tool_list(self.tools))
        # Always last: models weight the tail of the prompt most heavily.
        return text + "\n" + prompt.HONESTY


ROSTER: Dict[str, AgentSpec] = {
    "lead": AgentSpec(
        key="lead",
        name="lead",
        title="the coordinator who reports back",
        body=prompt.LEAD,
        tools=(),
        max_turns=2,
    ),
    "planner": AgentSpec(
        key="planner",
        name="planner",
        title="who decomposes the task",
        body=prompt.PLANNER,
        tools=(),
        max_turns=2,
    ),
    "coder": AgentSpec(
        key="coder",
        name="coder",
        title="who writes and edits the code",
        body=prompt.CODER,
        tools=ALL_TOOLS,
        max_turns=14,
    ),
    "reviewer": AgentSpec(
        key="reviewer",
        name="reviewer",
        title="who reviews the change",
        body=prompt.REVIEWER,
        tools=READ_TOOLS,
        max_turns=6,
    ),
    "tester": AgentSpec(
        key="tester",
        name="tester",
        title="who proves it runs",
        body=prompt.TESTER,
        tools=READ_TOOLS + EDIT_TOOLS + RUN_TOOLS + ("finish",),
        max_turns=10,
    ),
    "analyst": AgentSpec(
        key="analyst",
        name="analyst",
        title="who reads, researches and reports without editing",
        body=(
            "You are analyst. You investigate and report findings. You never modify files.\n"
            "You have three sources, in this order of trust:\n"
            "  1. the repository (`read_file`, `search_files`) - ground truth;\n"
            "  2. the offline library (`library_search`) - reference material, may be dated;\n"
            "  3. the council (`ask_council`) - outside opinions, NOT verified facts.\n"
            "Say which source each claim came from. If a source cannot answer, say so plainly\n"
            "and call `finish` reporting the gap. Never fill a gap with invention.\n"
            "Call `finish` with a dense, factual report: structure, key APIs, constraints,\n"
            "open questions, and the exact paths another agent would need."
        ),
        tools=READ_TOOLS + ("remember", "ask_council", "finish"),
        max_turns=8,
    ),
    "critic": AgentSpec(
        key="critic",
        name="critic",
        title="who chooses between competing plans",
        body=prompt.PLAN_CRITIC,
        tools=(),
        max_turns=2,
    ),
    "archivist": AgentSpec(
        key="archivist",
        name="archivist",
        title="who distils lessons from a finished run",
        body=prompt.ARCHIVIST,
        tools=(),
        max_turns=2,
    ),
}

# Planner may name roles we do not have; map them onto what exists.
ALIASES = {
    "writer": "coder",
    "developer": "coder",
    "implementer": "coder",
    "engineer": "coder",
    "test": "tester",
    "qa": "tester",
    "review": "reviewer",
    "auditor": "reviewer",
    "researcher": "analyst",
    "scout": "analyst",
    "reader": "analyst",
}


def _clean_line(value: Any, limit: int) -> str:
    """One display line, without newlines smuggled in from a config file."""
    return " ".join(str(value or "").split())[:limit]


def _custom_spec(
    raw: Any, position: int, existing: Dict[str, AgentSpec]
) -> Tuple[Optional[AgentSpec], str]:
    """Validate one entry from a workspace roster file."""
    if not isinstance(raw, dict):
        return None, f"custom agent #{position} must be an object"
    key = str(raw.get("key") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", key):
        return None, (
            f"custom agent #{position} needs a key like 'security' "
            "(lowercase letters, digits, - or _)"
        )
    if key in ROSTER or key in ALIASES:
        return None, f"custom agent '{key}' would replace a built-in role; choose another key"
    if key in existing:
        return None, f"custom agent '{key}' is defined twice"

    base: Optional[AgentSpec] = None
    like = str(raw.get("like") or "").strip().lower()
    if like:
        base = ROSTER.get(like)
        if base is None:
            return None, f"custom agent '{key}': unknown base role '{like}'"

    title = _clean_line(raw.get("title") or (base.title if base else ""), 120)
    if not title:
        return None, f"custom agent '{key}' needs a title"

    prompt = raw.get("prompt", raw.get("body"))
    body = base.body if (prompt is None and base is not None) else str(prompt or "").strip()
    if not body.strip():
        return None, f"custom agent '{key}' needs a prompt"
    if len(body) > 12000:
        return None, f"custom agent '{key}': prompt is too long (12000 characters maximum)"

    tools_raw = raw.get("tools")
    if tools_raw is None:
        tools = base.tools if base else READ_TOOLS + ("finish",)
    else:
        if not isinstance(tools_raw, (list, tuple)):
            return None, f"custom agent '{key}': tools must be a list"
        tools = tuple(
            str(item).strip() for item in tools_raw if str(item).strip()
        )
        unknown = sorted({tool for tool in tools if tool not in ALL_TOOLS})
        if unknown:
            return None, f"custom agent '{key}': unknown tool(s): {', '.join(unknown)}"
        tools = tuple(dict.fromkeys(tools))

    max_turns = raw.get("max_turns", base.max_turns if base else 8)
    if (
        isinstance(max_turns, bool)
        or not isinstance(max_turns, int)
        or not 1 <= max_turns <= 30
    ):
        return None, f"custom agent '{key}': max_turns must be a whole number from 1 to 30"

    name = _clean_line(raw.get("name") or (base.name if base else key), 40) or key
    return (
        AgentSpec(
            key=key,
            name=name,
            title=title,
            body=body,
            tools=tools,
            max_turns=max_turns,
        ),
        "",
    )


def load_custom_agents(path: Path) -> Tuple[Dict[str, AgentSpec], List[str]]:
    """Read a workspace roster: {"agents": [...]} or a bare list.

    A missing file is normal. A malformed entry is reported and skipped, so one
    typo never stops a run; the rest of the roster still loads.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"custom agents ignored ({path}): {exc}"]
    rows = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}, [
            f"custom agents ignored ({path}): expected a list or an object with an 'agents' list"
        ]
    specs: Dict[str, AgentSpec] = {}
    warnings: List[str] = []
    for position, raw in enumerate(rows, 1):
        spec, problem = _custom_spec(raw, position, specs)
        if spec is None:
            warnings.append(f"{path}: {problem}")
        else:
            specs[spec.key] = spec
    return specs, warnings


def get_agent(key: str) -> AgentSpec:
    key = (key or "coder").strip().lower()
    return ROSTER.get(key) or ROSTER.get(ALIASES.get(key, ""), ROSTER["coder"])


def describe_roster(extra: Optional[Dict[str, AgentSpec]] = None) -> str:
    extra = extra or {}
    rows = []
    for spec in list(ROSTER.values()) + list(extra.values()):
        tools = ", ".join(spec.tools) if spec.tools else "no tools"
        if spec.key in extra:
            suffix = "custom" if spec.name == spec.key else f"custom: {spec.key}"
            title = f"{spec.title} ({suffix})"
        else:
            title = spec.title
        rows.append(f"  {spec.name:<10} {title:<44} [{tools}]")
    return "\n".join(rows)
