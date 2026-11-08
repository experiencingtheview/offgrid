"""The team roster: who exists, what they may do, and how many turns they get."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

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
)
EDIT_TOOLS: Tuple[str, ...] = ("write_file", "edit_file")
RUN_TOOLS: Tuple[str, ...] = ("run_command",)
KNOWLEDGE_TOOLS: Tuple[str, ...] = ("remember", "recall", "library_search", "library_read", "ask_council")

ALL_TOOLS: Tuple[str, ...] = (
    "list_files",
    "read_file",
    "search_files",
    "write_file",
    "edit_file",
    "run_command",
    "git_status",
    "git_diff",
    "remember",
    "recall",
    "library_search",
    "library_read",
    "ask_council",
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

    def system(self) -> str:
        text = prompt.system_prompt(self.name, self.title, self.body)
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


def get_agent(key: str) -> AgentSpec:
    key = (key or "coder").strip().lower()
    return ROSTER.get(key) or ROSTER.get(ALIASES.get(key, ""), ROSTER["coder"])


def describe_roster() -> str:
    rows = []
    for spec in ROSTER.values():
        tools = ", ".join(spec.tools) if spec.tools else "no tools"
        rows.append(f"  {spec.name:<10} {spec.title:<44} [{tools}]")
    return "\n".join(rows)
