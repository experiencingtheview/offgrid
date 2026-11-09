"""Learning from itself: turn finished runs into durable, honest notes.

Two kinds of lesson:

* **deterministic** - facts we can prove from the run itself (the verify command
  that passed, the command that failed twice, the files that carried a task).
  These never depend on the model being smart.
* **reflective** - the archivist agent reads the run and extracts at most three
  generalisable lessons. It is explicitly allowed to return none, and its output
  is validated before it is stored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .agents import get_agent
from .config import Config
from .council import looks_ignorant
from .memory import MemoryStore, clip_evidence, make_key
from .models import Message
from .orchestrator import RunResult
from .primary import primary_block
from .providers import LocalProvider, ProviderError
from .ui import UI

MAX_LESSON_CHARS = 320


@dataclass
class Lesson:
    text: str
    kind: str = "lesson"
    confidence: float = 0.6
    key: str = ""
    id: Optional[int] = None
    source: str = ""
    verify_command: str = ""
    verify_output: str = ""


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r"^(lesson|note|learned)[:\-]\s*", "", text, flags=re.IGNORECASE)
    if len(text) > MAX_LESSON_CHARS:
        text = text[: MAX_LESSON_CHARS - 1].rstrip() + "\u2026"
    return text


def _council_answer_text(entry: Dict[str, Any]) -> str:
    """The usable member answer, not the rendered council envelope."""
    raw = entry.get("answer_text")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    for line in str(entry.get("answer") or "").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(
            ("COUNCIL", "---", "NOTE:", "Treat these", "not consulted:")
        ):
            continue
        return candidate
    return ""


def council_lessons(result: RunResult) -> List[Lesson]:
    """Offer council answers that the run's verification later supported.

    A disagreement is not eligible: verification can prove that the task works,
    but it does not identify which member was right. These lessons enter the
    same pending queue as every other automatic lesson and name the member as
    their source.
    """
    if result.verify_ok is not True:
        return []
    out: List[Lesson] = []
    seen = set()
    for entry in result.council:
        if not isinstance(entry, dict) or entry.get("disagreement"):
            continue
        answer = _clean(_council_answer_text(entry))
        if len(answer) < 12 or looks_ignorant(answer) or answer in seen:
            continue
        seen.add(answer)
        question = _clean(str(entry.get("question") or ""))
        if len(question) > 70:
            question = question[:69].rstrip() + "\u2026"
        if len(answer) > 170:
            answer = answer[:169].rstrip() + "\u2026"
        members = [
            str(item).strip() for item in (entry.get("members") or []) if str(item).strip()
        ]
        member = str(entry.get("member") or "").strip()
        if not member and members:
            member = members[0]
        source = f"council: {member}" if member else "council"
        context = f' to "{question}"' if question else ""
        out.append(
            Lesson(
                text=f"Council answer{context}, later confirmed by verification: {answer}",
                kind="lesson",
                confidence=0.7,
                source=source,
                verify_command=(result.verify_command or "").strip(),
                verify_output=clip_evidence(result.verify_output),
            )
        )
        if len(out) >= 3:
            break
    return out


def deterministic_lessons(result: RunResult, repo_name: str = "") -> List[Lesson]:
    """Lessons that follow from the run record alone."""
    out: List[Lesson] = []
    command = (result.verify_command or "").strip()

    if command and result.verify_ok is True:
        out.append(
            Lesson(
                text=f"Verification command for this project: `{command}` (it passed).",
                kind="repo",
                confidence=0.9,
                key="verify-command",
            )
        )
    elif command and result.verify_ok is False:
        out.append(
            Lesson(
                text=(
                    f"`{command}` failed after {1 + len(result.runs) - 1} attempt(s); "
                    "the approach needs rethinking, not another patch."
                ),
                kind="warning",
                confidence=0.8,
            )
        )

    if result.review_verdict == "changes" and result.review_issues:
        joined = "; ".join(result.review_issues[:3])
        out.append(
            Lesson(
                text=f"Reviewer rejected a change because: {_clean(joined)}",
                kind="correction",
                confidence=0.75,
            )
        )

    if result.changed:
        files = ", ".join(result.changed[:6])
        out.append(
            Lesson(
                text=f"For \"{_clean(result.task)[:120]}\" the relevant files were: {files}.",
                kind="fact",
                confidence=0.5,
            )
        )

    if result.verify_ok is None and command == "" and result.changed:
        out.append(
            Lesson(
                text=(
                    f"No test command was found for this project after: {_clean(result.task)[:100]}. "
                    "Consider adding a verify command in .offgrid.json."
                ),
                kind="warning",
                confidence=0.6,
            )
        )
    out.extend(council_lessons(result))
    return out


class Retrospective:
    """After a run, decide what (if anything) is worth remembering."""

    def __init__(
        self,
        provider: LocalProvider,
        memory: MemoryStore,
        config: Config,
        ui: UI,
    ) -> None:
        self.provider = provider
        self.memory = memory
        self.config = config
        self.ui = ui
        self.primary = primary_block(
            getattr(config, "principal", "EarthAngel"),
            getattr(config, "household", ("the dog",)),
            getattr(config, "caretaker", ""),
            getattr(config, "caretaker_household", ()),
        )

    def record(self, result: RunResult, source: str = "") -> List[Lesson]:
        lessons = deterministic_lessons(result)
        lessons.extend(self._reflective(result, source=source))

        saved: List[Lesson] = []
        # A fixed key makes repo facts overwrite themselves instead of piling up.
        for lesson in lessons:
            text = _clean(lesson.text)
            if len(text) < 8:
                continue
            if self.memory.was_rejected(text):
                self.ui.info(f"skipping a previously rejected lesson: {text[:80]}")
                continue
            row_key = lesson.key or make_key(lesson.kind, text)
            new_id = self.memory.remember(
                text,
                kind=lesson.kind,
                source=lesson.source or source or "run",
                confidence=lesson.confidence,
                key=lesson.key,
                status="pending",
                verify_command=lesson.verify_command,
                verify_output=lesson.verify_output,
            )
            if new_id is not None:
                lesson.id = new_id
                lesson.text = text
                saved.append(lesson)
                continue
            # A duplicate is only worth showing again if it is still unreviewed.
            existing = self.memory.get_by_key(row_key)
            if existing is not None and existing.status == "pending":
                lesson.id = existing.id
                lesson.text = existing.text
                saved.append(lesson)
        return saved

    def _reflective(self, result: RunResult, source: str = "") -> List[Lesson]:
        if not self.config.memory or self.config.verbose < 0:
            return []
        spec = get_agent("archivist")
        notes = "\n".join(f"- {note}" for note in result.notes[-8:]) or "- (none)"
        rejected = self.memory.rejected(limit=8)
        rejected_text = "\n".join(f"- {item.text}" for item in rejected) or "(none)"
        facts = (
            f"TASK\n{result.task}\n\n"
            f"CHANGED FILES\n{', '.join(result.changed) or '(none)'}\n\n"
            f"VERIFICATION\n{result.verify_ok} via `{result.verify_command or '(none)'}`\n\n"
            f"REVIEW\n{result.review_verdict or '(not run)'}\n\n"
            f"AGENT NOTES\n{notes}\n\n"
            f"REJECTED BY THE HUMAN (do not propose these again, even reworded)\n{rejected_text}\n\n"
            "Write the lessons JSON now."
        )
        try:
            with self.ui.spinner("archivist reflecting"):
                completion = self.provider.chat(
                    [Message("system", spec.system(self.primary)), Message("user", facts)],
                    temperature=0.1,
                    max_tokens=500,
                )
        except ProviderError:
            return []

        data = self._extract_json(completion.text)
        if not data:
            return []
        raw = data.get("lessons")
        if not isinstance(raw, list):
            return []

        out: List[Lesson] = []
        for item in raw[:3]:
            if isinstance(item, str):
                text, kind, confidence = item, "lesson", 0.55
            elif isinstance(item, dict):
                text = str(item.get("text") or "")
                kind = str(item.get("kind") or "lesson")
                try:
                    confidence = float(item.get("confidence") or 0.55)
                except (TypeError, ValueError):
                    confidence = 0.55
            else:
                continue
            text = _clean(text)
            if len(text) < 12 or self._is_noise(text):
                continue
            if kind not in ("fact", "lesson", "warning", "correction", "preference", "decision"):
                kind = "lesson"
            out.append(Lesson(text=text, kind=kind, confidence=max(0.3, min(0.9, confidence))))
        return out

    @staticmethod
    def _is_noise(text: str) -> bool:
        lowered = text.lower()
        bad = ("i cannot", "as an ai", "no lessons", "nothing to", "the task was", "json", "sure,")
        return any(marker in lowered for marker in bad)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        if not text:
            return None
        candidates = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        candidates.append(text)
        for candidate in candidates:
            start = candidate.find("{")
            while start != -1:
                depth = 0
                for index in range(start, len(candidate)):
                    char = candidate[index]
                    if char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                parsed = json.loads(candidate[start : index + 1])
                                if isinstance(parsed, dict):
                                    return parsed
                            except ValueError:
                                pass
                            break
                start = candidate.find("{", start + 1)
        return None
