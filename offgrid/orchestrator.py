"""The orchestrator: plan, delegate, review, verify, report.

This is what turns one local model into a team:

    lead -> planner -> coder(s) -> reviewer -> tester -> lead

Every agent gets its own system prompt, its own tool allowlist, its own turn
budget and its own transcript. They share exactly two things: the workspace on
disk and a short blackboard of notes each finished agent leaves behind.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .agents import AgentSpec, get_agent
from .config import Config
from .council import CouncilUnavailable, looks_ignorant
from .effort import override, resolve as resolve_effort
from .hardware import probe
from .models import Message, Stats, ToolCall
from .scale import adjust as adjust_effort, capacity_for
from .prompt import SKEPTIC
from .providers import LocalProvider, ProviderError
from .tools import ToolBox
from .trust import disclosures as trust_disclosures
from .ui import UI
from .workspace import Workspace

MAX_DEPTH = 6


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------
@dataclass
class Step:
    id: int
    title: str
    agent: str
    task: str
    files: List[str] = field(default_factory=list)
    depends_on: List[int] = field(default_factory=list)


@dataclass
class AgentRun:
    agent: str
    text: str = ""
    finished: bool = False
    turns: int = 0
    summary: str = ""
    tool_calls: int = 0
    council: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RunResult:
    task: str
    summary: str = ""
    plan_summary: str = ""
    steps: List[Step] = field(default_factory=list)
    runs: List[AgentRun] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    diff: str = ""
    verify_command: str = ""
    verify_output: str = ""
    verify_ok: Optional[bool] = None
    review_verdict: str = ""
    review_issues: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    lessons: List[str] = field(default_factory=list)
    effort: str = ""
    hardware: str = ""
    capacity: str = ""
    plan_candidates: List[str] = field(default_factory=list)
    quality_issues: List[str] = field(default_factory=list)
    escalated: bool = False
    pending_lessons: List[Dict[str, Any]] = field(default_factory=list)
    council: List[Dict[str, Any]] = field(default_factory=list)
    security: List[Dict[str, Any]] = field(default_factory=list)
    disclosures: List[str] = field(default_factory=list)
    stats: Optional[Stats] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "summary": self.summary,
            "plan": self.plan_summary,
            "steps": [
                {"id": s.id, "title": s.title, "agent": s.agent, "task": s.task, "files": s.files}
                for s in self.steps
            ],
            "changed_files": self.changed,
            "verify": {
                "command": self.verify_command,
                "ok": self.verify_ok,
                "output": self.verify_output[:4000],
            },
            "review": {"verdict": self.review_verdict, "issues": self.review_issues},
            "lessons": self.lessons,
            "effort": self.effort,
            "hardware": self.hardware,
            "capacity": self.capacity,
            "plan_candidates": self.plan_candidates,
            "quality_issues": self.quality_issues,
            "escalated": self.escalated,
            "pending_lessons": self.pending_lessons,
            "council": self.council,
            "security": self.security,
            "disclosures": self.disclosures,
            "agents": [
                {
                    "agent": r.agent,
                    "turns": r.turns,
                    "tool_calls": r.tool_calls,
                    "finished": r.finished,
                    "summary": r.summary,
                }
                for r in self.runs
            ],
            "stats": {
                "model_calls": self.stats.model_calls if self.stats else 0,
                "tool_calls": self.stats.tool_calls if self.stats else 0,
                "tokens": self.stats.total_tokens if self.stats else 0,
                "seconds": round(self.stats.elapsed, 1) if self.stats else 0,
            },
            "diff": self.diff,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of a model reply, fences and all."""
    if not text:
        return None
    candidates: List[str] = []
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidates.extend(block.strip() for block in fenced)
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
        start = candidate.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = candidate[start : index + 1]
                        try:
                            parsed = json.loads(snippet)
                            if isinstance(parsed, dict):
                                return parsed
                        except ValueError:
                            break
            start = candidate.find("{", start + 1)
    return None


class Team:
    def __init__(
        self,
        provider: LocalProvider,
        workspace: Workspace,
        config: Config,
        ui: UI,
        memory=None,
        library=None,
        council=None,
    ) -> None:
        self.provider = provider
        self.ws = workspace
        self.cfg = config
        self.ui = ui
        self.memory = memory
        self.library = library
        self.council = council
        for warning in getattr(memory, "lexicon_warnings", []):
            self.ui.warn(warning)
        self.stats = Stats()
        self.notes: List[str] = []
        self.history: List[Tuple[str, str]] = []  # (task, summary) across REPL turns
        self.session_source = "session"

        # What this machine is, and therefore how much work it can afford.
        self.capability = None
        self.capacity = None
        if config.scale:
            try:
                self.capability = probe(config.workspace or ".")
                self.capacity = capacity_for(self.capability)
            except Exception:  # a probe failure must never stop a run
                self.capability = None
                self.capacity = None
        self.effort = resolve_effort(config)
        if self.capacity is not None:
            self.effort = adjust_effort(self.effort, self.capacity)
            if config.parallel <= 1:
                config.parallel = self.capacity.parallel_steps
        self.context_chars = (
            config.context_chars
            if config.context_chars >= 0
            else (self.capacity.context_chars if self.capacity else 16000)
        )
        self.last_plan_candidates: List[str] = []
        self.last_escalation_run: Optional[AgentRun] = None
        self.council_consulted: List[Dict[str, Any]] = []

    # -- one model call --------------------------------------------------
    def _chat(
        self,
        spec: AgentSpec,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_names: Sequence[str] = (),
        where: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        spinner = f"{spec.name} {where}".strip()
        try:
            with self.ui.spinner(spinner):
                completion = self.provider.chat(
                    messages,
                    tools=tools or None,
                    tool_names=tool_names,
                    temperature=temperature,
                    max_tokens=max_tokens or self.effort.max_tokens,
                )
        except ProviderError:
            raise
        self.stats.add(spec.name, completion)
        return completion

    # -- the agent loop --------------------------------------------------
    def run_agent(
        self,
        spec: AgentSpec,
        task: str,
        context: str = "",
        max_turns: Optional[int] = None,
        quiet: bool = False,
    ) -> AgentRun:
        """Give one agent a task and let it work until it stops or runs out."""
        tools = self._tools()
        council_notes: List[Dict[str, Any]] = []
        tools.council_recorder = lambda question, answer: self._record_manual_council(
            spec, question, answer, council_notes
        )
        allowed = tools.available(spec.tools)
        schemas = tools.schemas(allowed)
        budget = max_turns or max(2, int(spec.max_turns * self.effort.turn_multiplier))

        user = task if not context else f"{task}\n\nCONTEXT (already established, do not redo)\n{context}"
        messages: List[Message] = [Message("system", spec.system()), Message("user", user)]

        self.ui.agent(spec.name, f"· {spec.title}")
        calls_made = 0
        last_text = ""

        turn = 0
        while turn < budget:
            turn += 1
            completion = self._chat(
                spec, messages, tools=schemas, tool_names=allowed, where="" if quiet else "thinking"
            )
            tool_calls = completion.tool_calls
            last_text = completion.text.strip()

            if not tool_calls:
                if last_text:
                    self.ui.say(last_text)
                if self._should_consult(last_text, council_notes):
                    message, record = self.consult_council(spec, last_text, task)
                    if message is not None and record is not None:
                        council_notes.append(record)
                        messages.append(Message("assistant", last_text))
                        messages.append(Message("user", message))
                        budget += 1  # one turn to act on what the council said
                        continue
                return AgentRun(
                    spec.key, last_text, False, turn, last_text[:400], calls_made, council_notes
                )

            # Native tool calling keeps the calls on the assistant message.
            assistant = Message("assistant", completion.text or "", tool_calls=tool_calls)
            messages.append(assistant)

            for call in tool_calls:
                self.ui.tool(call)
                result = tools.call(call.name, call.arguments)
                self.stats.tool_calls += 1
                calls_made += 1
                self.ui.tool_result(call.name, result.ok, result.content)
                rendered = result.render(self.cfg.max_output)
                if self.provider.native_tools is False:
                    messages.append(Message("user", f"RESULT of {call.name}:\n{rendered}"))
                else:
                    messages.append(Message("tool", rendered, tool_call_id=call.id))
                if tools.finished:
                    summary = tools.summary or last_text
                    if summary:
                        self.ui.say(summary)
                    return AgentRun(
                        spec.key, last_text, True, turn, summary, calls_made, council_notes
                    )

            messages = self._trim(messages)

        self.ui.warn(f"{spec.name} used all {budget} turns without finishing")
        return AgentRun(
            spec.key,
            last_text,
            False,
            budget,
            last_text[:400] or "(no summary)",
            calls_made,
            council_notes,
        )

    # -- the council on demand -------------------------------------------
    def _should_consult(self, text: str, already: List[Dict[str, Any]]) -> bool:
        return bool(
            self.council is not None
            and self.cfg.council
            and self.cfg.council_auto
            and len(already) < max(1, self.cfg.council_auto_limit)
            and looks_ignorant(text)
            and self.council.available()
        )

    def consult_council(self, spec: AgentSpec, said: str, task: str):
        """Ask the council instead of letting an agent stop at "I don't know".

        Returns (message to feed back, record for the run log), or (None, None)
        when the council could not help. A failed consult never fails the run.
        """
        question = said.strip()[:1200]
        self.ui.agent("council", f"· {spec.key} does not know; asking the council")
        try:
            answer = self.council.ask(question, context=f"TASK FROM THE USER\n{task[:900]}")
        except CouncilUnavailable as exc:
            self.ui.info(f"council has nobody available: {exc}")
            return None, None
        except Exception as exc:  # noqa: BLE001 - the council is optional
            self.ui.warn(f"council failed: {exc}")
            return None, None

        if not answer.usable:
            self.ui.warn("council had no answer; the agent will say so")
            self.notes.append(f"[council] nobody answered for {spec.key}; the gap stays open")
            return None, None

        record = self._council_record(spec.key, question, answer, trigger="auto")
        if record is None:
            return None, None
        self.council_consulted.append(record)
        self.notes.append(f"[council for {spec.key}] {str(record['answer_text'])[:240]}")
        self.ui.info(
            f"council answered ({', '.join(record['members'])}); recorded in the run"
        )
        message = (
            "COUNCIL ANSWER (outside opinions, not verified facts)\n"
            f"{record['answer']}\n\n"
            "Use it if it resolves the gap. Verify any specific claim you can check with a tool. "
            "If it still does not settle the question, say plainly what remains unknown."
        )
        return message, record

    def _council_record(
        self, agent: str, question: str, answer: Any, trigger: str
    ) -> Optional[Dict[str, Any]]:
        """Build the durable description of one council answer."""
        used = [item for item in answer.answers if item.ok]
        if not used:
            return None
        first = used[0]
        return {
            "agent": agent,
            "question": (question or "").strip()[:1200],
            "members": [item.member for item in used],
            "member": first.member,
            "answer_text": first.text,
            "answer": answer.render(limit=1200),
            "disagreement": answer.disagreement,
            "skipped": list(answer.skipped),
            "trigger": trigger,
        }

    def _record_manual_council(
        self,
        spec: AgentSpec,
        question: str,
        answer: Any,
        notes: List[Dict[str, Any]],
    ) -> None:
        """Record an answer fetched explicitly through the ask_council tool."""
        record = self._council_record(spec.key, question, answer, trigger="manual")
        if record is None:
            return
        self.council_consulted.append(record)
        notes.append(record)
        self.notes.append(f"[council for {spec.key}] {str(record['answer_text'])[:240]}")
        self.ui.info(
            f"council answered ({', '.join(record['members'])}); recorded in the run"
        )

    def _trim(self, messages: List[Message]) -> List[Message]:
        """Keep the system message and the most recent exchange window."""
        keep = max(6, self.cfg.max_agent_history)
        if len(messages) <= keep:
            return messages
        head = messages[0]
        tail = messages[-(keep - 1) :]
        while tail and tail[0].role == "tool":
            tail = tail[1:]
        return [head] + tail

    def _tools(self) -> ToolBox:
        """A fresh toolbox per agent, sharing memory/library/council."""
        return ToolBox(
            self.ws,
            self.cfg,
            approve=self.ui.approve,
            log=self.ui.info,
            memory=self.memory,
            library=self.library,
            council=self.council,
        )

    # -- context ---------------------------------------------------------
    def briefing(self, task: str = "") -> str:
        parts: List[str] = []

        if self.memory is not None and self.cfg.memory:
            block = self.memory.prompt_block(task, limit=self.cfg.memory_limit)
            if block:
                parts.append(block)

        if self.library is not None:
            stats = self.library.stats()
            if stats.get("files"):
                categories = ", ".join(f"{name} ({count})" for name, count in stats["categories"].items())
                parts.append(
                    f"OFFLINE LIBRARY\n{stats['files']} document(s) available: {categories}.\n"
                    "Use library_search before claiming you do not know a reference fact."
                )
            else:
                parts.append(
                    "OFFLINE LIBRARY\n(empty - no reference documents installed. Say so rather than "
                    "answering from thin air; it can be filled with scripts/fetch_library.py)"
                )

        parts.append(f"WORKSPACE TREE\n{self.ws.tree(limit=120, max_depth=3)}")
        for name in (
            "README.md",
            "README.rst",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "requirements.txt",
            "Makefile",
        ):
            path = self.ws.root / name
            if path.is_file():
                try:
                    head = path.read_text(encoding="utf-8", errors="replace")[:900]
                except OSError:
                    continue
                parts.append(f"--- {name} ---\n{head}")
        if (self.ws.root / ".git").exists():
            try:
                import subprocess

                proc = subprocess.run(
                    "git log --oneline -5",
                    shell=True,
                    cwd=str(self.ws.root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.stdout.strip():
                    parts.append(f"RECENT COMMITS\n{proc.stdout.strip()}")
            except Exception:
                pass
        text = "\n\n".join(parts)
        return text[: self.context_chars]

    def _context_for(self, step: Step) -> str:
        bits = []
        if self.notes:
            recent = self.notes[-MAX_DEPTH:]
            bits.append("NOTES FROM TEAMMATES\n" + "\n".join(f"- {note}" for note in recent))
        if self.history:
            earlier = self.history[-3:]
            bits.append(
                "EARLIER TURNS\n"
                + "\n".join(f"- asked: {task[:120]}\n  result: {summary[:200]}" for task, summary in earlier)
            )
        return "\n\n".join(bits)

    # -- planning --------------------------------------------------------
    def plan(self, task: str, briefing: str) -> Tuple[List[Step], str, str]:
        spec = get_agent("planner")
        self.ui.agent(spec.name, "· splitting the task")
        user = (
            f"TASK FROM THE USER\n{task}\n\n"
            f"{briefing}\n\n"
            "Take the time you need to get this right. Return the JSON plan now, nothing else."
        )
        candidates = self._draft_plans(task, briefing, user, spec)
        data = self._choose_plan(task, briefing, candidates)
        steps: List[Step] = []
        summary = ""
        verify = self.cfg.verify_command
        if data:
            summary = str(data.get("summary") or "")[:400]
            verify = self.cfg.verify_command or str(data.get("verify") or "")
            raw_steps = data.get("steps")
            if isinstance(raw_steps, list):
                for index, raw in enumerate(raw_steps[:8], 1):
                    if not isinstance(raw, dict):
                        continue
                    body = str(raw.get("task") or raw.get("title") or "").strip()
                    if not body:
                        continue
                    depends: List[int] = []
                    for dep in raw.get("depends_on") or []:
                        try:
                            depends.append(int(dep))
                        except (TypeError, ValueError):
                            continue
                    files = [str(f) for f in (raw.get("files") or []) if isinstance(f, (str, int))]
                    try:
                        step_id = int(raw.get("id") or index)
                    except (TypeError, ValueError):
                        step_id = index
                    steps.append(
                        Step(
                            id=step_id,
                            title=str(raw.get("title") or body[:60]),
                            agent=str(raw.get("agent") or "coder"),
                            task=body,
                            files=files,
                            depends_on=[d for d in depends if d != step_id],
                        )
                    )

        if not steps:
            self.ui.warn("planner did not return a usable plan; falling back to one coder step")
            steps = [Step(id=1, title=task[:60], agent="coder", task=task)]
        else:
            self.ui.say(summary or "(no summary)", indent=9)
            for step in steps:
                self.ui.info(f"  {step.id}. [{step.agent}] {step.title}")
        return steps, summary, verify

    # -- effort knobs ----------------------------------------------------
    def _plan_candidates(self) -> int:
        return max(1, override(self.cfg.plan_candidates, self.effort.plan_candidates))

    def _review_passes(self) -> int:
        return max(1, override(self.cfg.review_passes, self.effort.review_passes))

    def _review_rounds(self) -> int:
        return max(0, override(self.cfg.review_rounds, self.effort.review_rounds))

    def _fix_rounds(self) -> int:
        return max(0, override(self.cfg.test_fix_rounds, self.effort.test_fix_rounds))

    def _quality_rounds(self) -> int:
        return max(0, override(self.cfg.quality_rounds, self.effort.quality_rounds))

    # -- planning with competing candidates ------------------------------
    def _draft_plans(
        self,
        task: str,
        briefing: str,
        user: str,
        spec: AgentSpec,
    ) -> List[Dict[str, Any]]:
        """Ask for several plans rather than accepting the first plausible one."""
        count = self._plan_candidates()
        if count > 1:
            self.ui.info(f"drafting {count} candidate plans, then choosing the strongest")
        found: List[Dict[str, Any]] = []
        for index in range(count):
            try:
                completion = self._chat(
                    spec,
                    [Message("system", spec.system()), Message("user", user)],
                    temperature=min(0.2 + 0.25 * index, 0.9),
                )
            except ProviderError:
                raise
            data = extract_json(completion.text)
            if data and isinstance(data.get("steps"), list) and data["steps"]:
                found.append(data)
        self.last_plan_candidates = [str(item.get("summary") or "(no summary)")[:160] for item in found]
        return found

    def _choose_plan(
        self,
        task: str,
        briefing: str,
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        if len(candidates) == 1 or not self.effort.plan_critique:
            return candidates[0]
        spec = get_agent("critic")
        self.ui.agent(spec.name, f"· comparing {len(candidates)} plans")
        options = json.dumps(
            [{"option": index + 1, "plan": plan} for index, plan in enumerate(candidates)],
            ensure_ascii=False,
        )
        user = (
            f"TASK FROM THE USER\n{task}\n\n{briefing}\n\n"
            f"CANDIDATE PLANS\n{options[:9000]}\n\n"
            "Choose the strongest plan, or merge the best parts. Reply with the final plan JSON only."
        )
        try:
            completion = self._chat(spec, [Message("system", spec.system()), Message("user", user)])
        except ProviderError:
            return candidates[0]
        data = extract_json(completion.text)
        if data and isinstance(data.get("steps"), list) and data["steps"]:
            return data
        self.ui.info("critic returned no usable plan; keeping the first candidate")
        return candidates[0]

    # -- mechanical quality gate -----------------------------------------
    def run_quality_gate(self, task: str, result: RunResult) -> List[str]:
        """Catch truncation, stubs and syntax errors before anyone reviews."""
        if not self.effort.quality_gate or not self.cfg.quality_gate:
            return []
        from .quality import check_workspace

        if not self.ws.changed_files():
            return []
        issues = check_workspace(self.ws)
        if not issues:
            self.ui.info("mechanical checks clean")
            return []

        rounds = 0
        while issues and rounds < self._quality_rounds():
            rounds += 1
            self.ui.rule()
            self.ui.warn(f"{len(issues)} mechanical defect(s); these must be fixed before review")
            for issue in issues[:6]:
                self.ui.info("  - " + issue)
            run = self.fix_quality(task, issues, rounds)
            if run is None:
                break
            result.runs.append(run)
            if run.summary:
                self.notes.append(f"[quality {rounds}] {run.summary[:280]}")
            issues = check_workspace(self.ws)

        if issues:
            self.ui.warn("mechanical defects remain; the reviewer will see them")
        else:
            self.ui.ok("mechanical checks clean")
        return issues

    def fix_quality(self, task: str, issues: List[str], attempt: int) -> Optional[AgentRun]:
        spec = get_agent("coder")
        body = (
            "Your change fails mechanical checks. These are not opinions; finish the work.\n\n"
            f"TASK\n{task}\n\nDEFECTS\n"
            + "\n".join(f"- {issue}" for issue in issues[:12])
            + "\n\nRewrite the incomplete parts completely: no ellipses standing in for code, no unfinished "
            "functions, no placeholders, valid syntax. Change nothing that already works. Then call finish."
        )
        self.ui.warn(f"sending mechanical defects back to a coder (round {attempt})")
        return self.run_agent(
            spec,
            body,
            context=self._context_for(Step(0, "", "coder", "")),
            max_turns=10,
        )

    # -- escalation -------------------------------------------------------
    def escalate(self, task: str, command: str, output: str) -> Tuple[Optional[bool], str]:
        """One stronger attempt before giving up. Never a fabricated success."""
        self.ui.rule()
        self.ui.agent("lead", "· escalating: a stronger attempt, not a guess")
        advice = ""
        if self.council is not None and self.cfg.council:
            try:
                answer = self.council.ask(
                    "A local coding agent keeps failing this verification command. "
                    "What are the most likely root causes, in order?",
                    context=f"COMMAND\n{command}\n\nOUTPUT\n{(output or '')[:1500]}",
                )
                if answer.usable:
                    advice = answer.render(limit=600)
                    self.ui.info("council diagnosis attached to the retry")
            except Exception:
                advice = ""
        elif self.library is not None and self.library.stats().get("files"):
            hits = self.library.search(" ".join((output or "").split()[:8]), limit=3)
            if hits:
                advice = "REFERENCE NOTES FROM THE OFFLINE LIBRARY\n" + "\n".join(hits)

        spec = get_agent("coder")
        body = (
            "The task still fails verification after several attempts. Do not repeat the last patch.\n\n"
            f"TASK\n{task}\n\nCOMMAND\n{command}\n\nFAILURE OUTPUT\n{(output or '')[:3000]}\n\n"
            + (advice + "\n\n" if advice else "")
            + "Re-read the current files first. Consider that the approach, not the implementation, may be "
            "wrong. If it genuinely cannot be fixed with what is here, say so plainly, name what is "
            "missing, and call finish - a clear failure is better than a fabricated success."
        )
        original_model = self.provider.model
        if self.cfg.escalation_model:
            self.provider.model = self.cfg.escalation_model
            self.ui.info(f"escalating to model {self.cfg.escalation_model}")
        try:
            run = self.run_agent(spec, body, max_turns=max(6, int(spec.max_turns * 2)))
        finally:
            self.provider.model = original_model
        if run.summary:
            self.notes.append(f"[escalation] {run.summary[:280]}")
        self.last_escalation_run = run
        return self.verify(command)

    # -- execution -------------------------------------------------------
    @staticmethod
    def _waves(steps: List[Step]) -> List[List[Step]]:
        remaining = {step.id: step for step in steps}
        done: set = set()
        waves: List[List[Step]] = []
        guard = 0
        while remaining and guard < 32:
            guard += 1
            ready = [
                step
                for step in remaining.values()
                if all(dep in done for dep in step.depends_on) or not step.depends_on
            ]
            if not ready:
                ready = list(remaining.values())
            ready.sort(key=lambda step: step.id)
            waves.append(ready)
            for step in ready:
                done.add(step.id)
                remaining.pop(step.id, None)
        return waves

    def _run_step(self, step: Step, quiet: bool) -> AgentRun:
        spec = get_agent(step.agent)
        context = self._context_for(step)
        hint = ""
        if step.files:
            hint = f"\n\nFiles this step is expected to touch: {', '.join(step.files)}"
        try:
            run = self.run_agent(spec, step.task + hint, context=context, quiet=quiet)
        except ProviderError as exc:
            self.ui.error(f"{spec.name} could not run: {exc}")
            run = AgentRun(spec.key, "", False, 0, f"provider error: {exc}")
        if run.summary:
            self.notes.append(f"[{step.id}/{spec.name}] {run.summary[:280]}")
        return run

    def execute(self, steps: List[Step]) -> List[AgentRun]:
        self.ui.rule()
        self.ui.agent("team", "· executing plan")
        waves = self._waves(steps)
        parallel = max(1, int(self.cfg.parallel or 1))
        runs: List[AgentRun] = []
        for wave in waves:
            if len(wave) > 1 and parallel > 1:
                self.ui.info(f"running {len(wave)} steps in parallel")
                self.ui._spinner_quiet = True
                try:
                    with ThreadPoolExecutor(max_workers=min(parallel, len(wave))) as pool:
                        wave_runs = list(pool.map(lambda step: self._run_step(step, True), wave))
                finally:
                    self.ui._spinner_quiet = False
                runs.extend(wave_runs)
            else:
                for step in wave:
                    runs.append(self._run_step(step, False))
        return runs

    # -- review ----------------------------------------------------------
    def review(self, task: str, diff: str) -> Tuple[str, List[str], str]:
        """One or more independent passes; any objection blocks approval."""
        for index in range(self._review_passes()):
            verdict, issues, instructions = self._review_once(task, diff, skeptic=index > 0)
            if verdict != "approve":
                return verdict, issues, instructions
        return "approve", [], ""

    def _review_once(self, task: str, diff: str, skeptic: bool = False) -> Tuple[str, List[str], str]:
        spec = get_agent("reviewer")
        self.ui.rule()
        self.ui.agent(spec.name, "· reviewing the change" + (" (adversarial pass)" if skeptic else ""))
        extra = ("\n\n" + SKEPTIC) if skeptic else ""
        user = (
            f"TASK THAT WAS REQUESTED\n{task}\n\n"
            f"THE CHANGE\n{diff[: self.context_chars]}\n\n"
            "Review it and reply with the JSON verdict now." + extra
        )
        try:
            completion = self._chat(
                spec,
                [Message("system", spec.system()), Message("user", user)],
                tools=None,
            )
        except ProviderError as exc:
            self.ui.warn(f"reviewer unavailable: {exc}")
            return "skipped", [], ""
        data = extract_json(completion.text)
        if not data:
            self.ui.say(completion.text.strip() or "(reviewer said nothing)")
            return "approve", [], ""
        verdict = str(data.get("verdict") or "approve").lower()
        issues = [str(item) for item in (data.get("issues") or []) if str(item).strip()]
        instructions = str(data.get("instructions") or "")
        if verdict.startswith("change") or verdict.startswith("reject"):
            self.ui.warn(f"reviewer wants changes ({len(issues)} issue(s))")
            for issue in issues[:6]:
                self.ui.info(f"  - {issue}")
            return "changes", issues, instructions
        self.ui.ok("reviewer approved")
        return "approve", issues, instructions

    # -- verification ----------------------------------------------------
    def _remembered_verify(self) -> str:
        """Self-learning: a verify command this project proved before."""
        if self.memory is None or not self.cfg.memory:
            return ""
        for item in self.memory.recall("verification command project", limit=8, kinds=("repo",)):
            match = re.search(r"`([^`]+)`", item.text)
            if match and "verification command" in item.text.lower():
                return match.group(1).strip()
        return ""

    def detect_verify_command(self) -> str:
        if self.cfg.verify_command:
            return self.cfg.verify_command
        remembered = self._remembered_verify()
        if remembered:
            self.ui.info(f"using remembered verify command: {remembered}")
            return remembered
        root = self.ws.root
        checks = (
            ("pyproject.toml", "python3 -m pytest -q"),
            ("pytest.ini", "python3 -m pytest -q"),
            ("tox.ini", "python3 -m pytest -q"),
            ("Cargo.toml", "cargo test -q"),
            ("go.mod", "go test ./..."),
            ("package.json", "npm test --silent"),
        )
        for filename, command in checks:
            if (root / filename).is_file():
                if filename == "pyproject.toml" and b"pytest" not in (root / filename).read_bytes()[:4000]:
                    if (root / "tests").is_dir():
                        return command
                    continue
                return command
        if (root / "tests").is_dir() or (root / "test").is_dir():
            return "python3 -m pytest -q"
        if list(root.glob("test_*.py")) or list(root.glob("*_test.py")):
            return "python3 -m pytest -q"
        return ""

    def verify(self, command: str) -> Tuple[Optional[bool], str]:
        if not command:
            return None, ""
        if self.cfg.command_policy == "deny":
            return None, "shell access disabled, so nothing was verified"
        tools = self._tools()
        self.ui.rule()
        self.ui.agent("tester", "· verifying with the shell")
        result = tools.run_command(command, timeout=300)
        self.ui.tool_result("run_command", result.ok, result.content)
        self.ui.say(result.content.strip()[:2000])
        return result.ok, result.content

    def fix_tests(self, task: str, command: str, output: str, attempt: int) -> Optional[AgentRun]:
        spec = get_agent("coder")
        followup = (
            f"Your earlier work on this task fails verification.\n\n"
            f"TASK\n{task}\n\n"
            f"COMMAND\n{command}\n\n"
            f"OUTPUT\n{output[:4000]}\n\n"
            "Fix the actual cause in the source. Do not weaken or delete the test to make it pass. "
            "Re-run the command to confirm, then call finish."
        )
        self.ui.warn(f"fixing verification failure (attempt {attempt})")
        return self.run_agent(spec, followup, context=self._context_for(Step(0, "", "coder", "")), max_turns=8)

    # -- final report ----------------------------------------------------
    def summarize(self, task: str, result: RunResult) -> str:
        spec = get_agent("lead")
        changed = ", ".join(result.changed) or "no files changed"
        if result.verify_ok is True:
            verify_line = f"verified: `{result.verify_command}` passed"
        elif result.verify_ok is False:
            verify_line = f"verification FAILED: `{result.verify_command}`"
        elif result.verify_command:
            verify_line = f"not verified (could not run `{result.verify_command}`)"
        else:
            verify_line = "not verified, no test command was found"

        agent_notes = "\n".join(f"- {note}" for note in self.notes[-8:]) or "- (none)"
        if result.council:
            council_lines = []
            for entry in result.council[:3]:
                members = ", ".join(entry.get("members") or [])
                lead = (
                    f"{entry.get('agent')} asked the council"
                    if entry.get("trigger") == "manual"
                    else f"{entry.get('agent')} did not know; council"
                )
                council_lines.append(
                    f"- {lead} ({members}) said: {self._council_gist(entry)}"
                )
            council_line = "\n".join(council_lines)
        else:
            council_line = "not consulted"
        if result.security:
            security_line = "; ".join(
                f"{item['severity']} {item['check']} in {item.get('path') or 'configuration'}"
                for item in result.security[:4]
            )
        else:
            security_line = "clean (no findings in the files that changed)"
        facts = (
            f"USER TASK\n{task}\n\n"
            f"FILES CHANGED\n{changed}\n\n"
            f"VERIFICATION\n{verify_line}\n\n"
            f"REVIEW\n{result.review_verdict or 'not run'}\n\n"
            f"COUNCIL\n{council_line}\n\n"
            f"SECURITY\n{security_line}\n\n"
            f"AGENT NOTES\n{agent_notes}\n\n"
            "Write the closing report now. Mention the council only if it was consulted, and say it "
            "was an outside opinion rather than a verified fact. Mention security findings only if "
            "there are any, and never call a change secure: the checks are limited."
        )
        self.ui.rule()
        self.ui.agent(spec.name, "· reporting")
        try:
            completion = self._chat(spec, [Message("system", spec.system()), Message("user", facts)])
            text = completion.text.strip()
        except ProviderError:
            text = ""
        if not text:
            text = (
                f"Changed {changed}.\n"
                f"{verify_line.capitalize()}.\n"
                f"{result.stats.summary() if result.stats else ''}"
            )
        return text

    @staticmethod
    def _council_gist(entry: Dict[str, Any]) -> str:
        for line in str(entry.get("answer") or "").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("COUNCIL") and not stripped.startswith("---"):
                return stripped[:220]
        return "(no usable text)"

    # -- the whole run ---------------------------------------------------
    def run(self, task: str, verify_override: str = "") -> RunResult:
        if verify_override:
            self.cfg.verify_command = verify_override

        self.council_consulted = []
        result = RunResult(task=task, stats=self.stats)
        briefing = self.briefing(task)
        steps, plan_summary, verify_command = self.plan(task, briefing)
        result.steps = steps
        result.plan_summary = plan_summary
        if verify_command and not self.cfg.verify_command:
            self.cfg.verify_command = verify_command

        result.runs = self.execute(steps)
        result.effort = self.effort.name
        result.hardware = self.capability.tier if self.capability else ""
        result.capacity = self.capacity.name if self.capacity else ""
        result.plan_candidates = list(self.last_plan_candidates)
        result.quality_issues = self.run_quality_gate(task, result)

        diff = self.ws.diff()
        if diff.strip():
            verdict, issues, instructions = self.review(task, diff)
            result.review_verdict = verdict
            result.review_issues = issues
            rounds = 0
            while verdict == "changes" and rounds < self._review_rounds():
                rounds += 1
                spec = get_agent("coder")
                followup = (
                    f"The reviewer found problems with your change to this task.\n\n"
                    f"TASK\n{task}\n\n"
                    f"ISSUES\n" + "\n".join(f"- {issue}" for issue in issues) + "\n\n"
                    f"INSTRUCTIONS\n{instructions}\n\n"
                    "Fix every issue. Keep the existing working behaviour. Then call finish."
                )
                run = self.run_agent(spec, followup, max_turns=8)
                result.runs.append(run)
                if run.summary:
                    self.notes.append(f"[review-fix {rounds}] {run.summary[:280]}")
                diff = self.ws.diff()
                verdict, issues, instructions = self.review(task, diff)
                result.review_verdict = verdict
                result.review_issues = issues
        else:
            result.review_verdict = "no changes to review"
            self.ui.warn("no files changed, so there is nothing to review")

        command = self.cfg.verify_command or self.detect_verify_command()
        result.verify_command = command
        ok, output = self.verify(command)
        result.verify_ok = ok
        result.verify_output = output

        attempts = 0
        while ok is False and attempts < self._fix_rounds():
            attempts += 1
            run = self.fix_tests(task, command, output, attempts)
            if run:
                result.runs.append(run)
                if run.summary:
                    self.notes.append(f"[test-fix {attempts}] {run.summary[:280]}")
            ok, output = self.verify(command)
            result.verify_ok = ok
            result.verify_output = output

        if ok is False and self.effort.escalate and self.cfg.escalate:
            self.last_escalation_run = None
            ok, output = self.escalate(task, command, output)
            if self.last_escalation_run is not None:
                result.runs.append(self.last_escalation_run)
            result.escalated = True
            result.verify_ok = ok
            result.verify_output = output

        result.changed = self.ws.changed_files()
        result.diff = self.ws.diff()
        result.notes = list(self.notes)
        result.council = list(self.council_consulted)
        self.security_review(result)
        result.disclosures = trust_disclosures(result, self.cfg)
        result.summary = self.summarize(task, result)
        self.history.append((task, result.summary))
        self.reflect(result)
        return result

    def security_review(self, result: RunResult) -> None:
        """Review the changed files, record it, and never fail the run over it."""
        if not self.cfg.security_review:
            return
        from .audit import record, review_workspace

        try:
            findings = review_workspace(
                self.ws.root, changed=result.changed or [], config=self.cfg
            )
        except Exception as exc:  # noqa: BLE001 - a review problem is not a run failure
            self.ui.info(f"security review could not run: {exc}")
            return
        result.security = [item.as_dict() for item in findings]
        try:
            record(
                self.ws.root / self.cfg.security_history,
                findings,
                source="run",
            )
        except Exception:  # noqa: BLE001 - history is best-effort
            pass
        critical = [item for item in findings if item.severity == "critical"]
        if critical:
            self.ui.error(f"security: {len(critical)} critical finding(s) in this change")
            for item in critical[:3]:
                self.ui.error("  " + item.line_text())
        elif findings:
            self.ui.warn(f"security: {len(findings)} finding(s); run `offgrid security review`")

    def reflect(self, result: RunResult) -> None:
        """Learn from the run. Failures here must never fail the run."""
        if self.memory is None or not self.cfg.memory or not self.cfg.reflect:
            return
        if not result.changed and result.verify_ok is None:
            return
        from .learn import Retrospective

        try:
            lessons = Retrospective(self.provider, self.memory, self.cfg, self.ui).record(
                result, source=self.session_source
            )
        except Exception as exc:  # noqa: BLE001 - reflection is best-effort
            self.ui.info(f"could not record lessons: {exc}")
            return
        result.lessons = [lesson.text for lesson in lessons]
        result.pending_lessons = [
            {
                "id": lesson.id,
                "text": lesson.text,
                "kind": lesson.kind,
                "confidence": lesson.confidence,
                "source": lesson.source or self.session_source or "run",
                "verify_command": lesson.verify_command,
                "verify_output": lesson.verify_output,
            }
            for lesson in lessons
            if lesson.id is not None
        ]
        if lessons:
            self.ui.info(f"{len(lessons)} new lesson(s) waiting for your review")
