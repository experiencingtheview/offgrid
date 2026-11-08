"""The council: several independent opinions instead of one guess.

Two sources of members:

* **local** - other models on this machine (another Ollama model, a second
  llama-server, LM Studio). Free, private, and the default.
* **public** - keyless endpoints that need no account. Verified working today:
  Pollinations' classic text API. These are OFF by default, because using them
  means your question leaves the machine.

If no member can answer, the council says so. It never fabricates a consensus.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .providers import is_loopback

DEFAULT_TIMEOUT = 90

# Public, keyless, no sign-in. Every entry here was probed live before shipping;
# anything that required a key or returned an error is deliberately absent
# (Hack Club AI now returns 404, LLM7 rejected an anonymous probe, and
# gen.pollinations.ai requires a personal key).
PUBLIC_MEMBERS: Dict[str, Dict[str, Any]] = {
    "pollinations": {
        "endpoint": "https://text.pollinations.ai/openai",
        "model": "openai",
        "api_key": "",
        "note": "free keyless lane, one request at a time, roughly 30s apart per IP",
        "min_interval": 32.0,
    },
}

ADVISOR_SYSTEM = """\
You are one independent member of a council of advisors. You cannot run tools or
see the user's files; you only have what is in the question. Answer in at most
eight sentences.

Rules:
- If the question cannot be answered from general knowledge, say "I don't know"
  and name what information would be needed. Do not invent specifics: no made-up
  APIs, versions, numbers, names, URLs or citations.
- Separate what is well established from what is your opinion, and say which is
  which.
- If you suspect the premise of the question is wrong, say so.
"""


class CouncilUnavailable(Exception):
    """Raised when no member can be consulted. Callers must admit ignorance."""


# Phrases that mean "I would otherwise stop here without an answer". The
# orchestrator watches for these and asks the council before the agent gives up.
IGNORANCE_PATTERNS = (
    re.compile(r"\bi (?:do not|don't) know\b"),
    re.compile(
        r"\bi (?:do not|don't) have (?:enough |sufficient |any )?(?:information|context|access|data)\b"
    ),
    re.compile(
        r"\bi (?:cannot|can't|can not|am unable to|'m unable to)\s+"
        r"(?:determine|tell|verify|confirm|know|answer|find out)\b"
    ),
    re.compile(r"\bi (?:have no|have got no) (?:idea|information|data|access)\b"),
    re.compile(r"\bi lack (?:the )?(?:information|context|access|data|knowledge)\b"),
    re.compile(r"\bnot enough (?:information|context|evidence|data)\b"),
    re.compile(r"\bunable to (?:determine|verify|confirm|answer|tell)\b"),
    re.compile(r"\b(?:cannot|can't|can not) be (?:determined|verified|confirmed) (?:from|without)\b"),
    re.compile(r"\b(?:there is|there's) no way to (?:know|tell|verify|confirm)\b"),
    re.compile(r"\b(?:unknown|unclear) (?:to me|from (?:the|this))\b"),
    re.compile(r"\bwithout (?:more information|access to|the (?:source|docs|documentation))\b"),
    re.compile(r"\bno (?:documentation|information|data) (?:is )?(?:available|provided)\b"),
)


def looks_ignorant(text: str) -> bool:
    """True when a reply is admitting a gap rather than answering."""
    lowered = (text or "").lower()
    return any(pattern.search(lowered) for pattern in IGNORANCE_PATTERNS)


@dataclass
class Member:
    name: str
    endpoint: str
    model: str = ""
    api_key: str = ""
    public: bool = False
    note: str = ""
    min_interval: float = 0.0


@dataclass
class Answer:
    member: str
    text: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


@dataclass
class CouncilAnswer:
    question: str
    answers: List[Answer] = field(default_factory=list)
    sent_chars: int = 0
    skipped: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return any(answer.ok for answer in self.answers)

    @property
    def disagreement(self) -> bool:
        """Cheap signal: members that answered very differently in length/content."""
        texts = [answer.text.strip().lower() for answer in self.answers if answer.ok]
        if len(texts) < 2:
            return False
        shortest = min(len(text) for text in texts)
        longest = max(len(text) for text in texts)
        if shortest and longest / shortest > 4:
            return True
        negatives = sum(1 for text in texts if "i don't know" in text or "i do not know" in text)
        return 0 < negatives < len(texts)

    def render(self, limit: int = 1200) -> str:
        if not self.usable:
            return "COUNCIL: nobody could answer. Do not guess; say you do not know."
        parts = [f"COUNCIL ({len(self.answers)} member(s) consulted, question {self.sent_chars} chars)"]
        for answer in self.answers:
            if answer.ok:
                body = answer.text.strip()
                if len(body) > limit:
                    body = body[:limit] + "..."
                parts.append(f"--- {answer.member} ---\n{body}")
            else:
                parts.append(f"--- {answer.member} --- (failed: {answer.error[:160]})")
        if self.skipped:
            parts.append("not consulted: " + ", ".join(self.skipped))
        if self.disagreement:
            parts.append(
                "NOTE: the members do not agree. Report the disagreement to the user instead of "
                "picking the answer that sounds best."
            )
        parts.append("Treat these as outside opinions, not verified facts. Say which parts you could check.")
        return "\n".join(parts)


class Council:
    def __init__(
        self,
        config: Config,
        post: Optional[Callable[[Member, List[Dict[str, str]], int], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self._post = post or self._http_post
        self._last_call: Dict[str, float] = {}
        self._lock = threading.Lock()

    # -- members ---------------------------------------------------------
    def members(self) -> List[Member]:
        specs = self.config.council_members or ("local",)
        if isinstance(specs, str):
            specs = (specs,)
        out: List[Member] = []
        seen = set()
        for spec in specs:
            member = self._parse_spec(str(spec).strip())
            if not member or member.name in seen:
                continue
            seen.add(member.name)
            out.append(member)
        return out

    def _parse_spec(self, spec: str) -> Optional[Member]:
        if not spec:
            return None
        if spec in PUBLIC_MEMBERS:
            preset = PUBLIC_MEMBERS[spec]
            return Member(
                name=spec,
                endpoint=preset["endpoint"],
                model=preset.get("model", ""),
                api_key=preset.get("api_key", ""),
                public=True,
                note=preset.get("note", ""),
                min_interval=float(preset.get("min_interval", 0.0)),
            )
        if spec == "local":
            if not self.config.endpoint:
                return None
            return Member(
                name="local",
                endpoint=self.config.resolved_endpoint().rstrip("/") + "/chat/completions",
                model=self.config.model,
            )
        if spec.startswith("local-model:"):
            model = spec.split(":", 1)[1].strip()
            if not self.config.endpoint or not model:
                return None
            return Member(
                name=f"local:{model}",
                endpoint=self.config.resolved_endpoint().rstrip("/") + "/chat/completions",
                model=model,
            )
        if spec.startswith("url:"):
            rest = spec[4:].strip()
            model = ""
            if "#" in rest:
                rest, model = rest.split("#", 1)
            endpoint = rest.rstrip("/")
            if "/chat/completions" not in endpoint:
                endpoint += "/chat/completions"
            return Member(
                name="remote:" + (model or endpoint.split("/")[2]),
                endpoint=endpoint,
                model=model.strip(),
                api_key=self.config.api_key,
                public=not is_loopback(endpoint),
            )
        return None

    def selected(self) -> Tuple[List[Member], List[str]]:
        """Members allowed under the current privacy settings."""
        allowed: List[Member] = []
        skipped: List[str] = []
        for member in self.members():
            if member.public and not self.config.allow_public_council:
                skipped.append(member.name + " (public, needs --council-public)")
                continue
            if not member.public and not is_loopback(member.endpoint):
                skipped.append(member.name + " (not localhost)")
                continue
            allowed.append(member)
        return allowed, skipped

    def available(self) -> bool:
        members, _ = self.selected()
        return bool(members) and bool(self.config.council)

    def label(self) -> str:
        members, _ = self.selected()
        return ", ".join(member.name for member in members) or "(none)"

    # -- asking ----------------------------------------------------------
    def ask(self, question: str, context: str = "", timeout: Optional[int] = None) -> CouncilAnswer:
        question = (question or "").strip()
        if not question:
            raise CouncilUnavailable("empty question")
        if not self.config.council:
            raise CouncilUnavailable(
                "the council is disabled. Re-run with --council (and --council-public for public members)"
            )
        members, skipped = self.selected()
        if not members:
            raise CouncilUnavailable(
                "no council members are available. Configure a second local model "
                "(--council-members local-model:NAME) or allow public members with --council-public"
            )

        budget = int(timeout or self.config.council_timeout or DEFAULT_TIMEOUT)
        prompt = question
        if context:
            prompt = f"{question}\n\nCONTEXT (may be partial; do not assume more)\n{context}"
        limit = max(200, int(self.config.council_max_chars or 2000))
        if len(prompt) > limit:
            prompt = prompt[:limit] + "\n[truncated]"
        messages = [
            {"role": "system", "content": ADVISOR_SYSTEM},
            {"role": "user", "content": prompt},
        ]

        results: Dict[str, Answer] = {}
        threads: List[threading.Thread] = []

        def run(member: Member) -> None:
            started = time.time()
            delay = self._wait_turn(member)
            if delay > 0:
                time.sleep(min(delay, 35))
            try:
                payload = self._post(member, messages, budget)
                text = self._extract(payload)
                if not text.strip():
                    results[member.name] = Answer(member.name, error="empty answer")
                else:
                    results[member.name] = Answer(member.name, text=text.strip(), elapsed=time.time() - started)
            except Exception as exc:  # a dead member must not break the council
                results[member.name] = Answer(member.name, error=f"{type(exc).__name__}: {exc}")

        for member in members:
            thread = threading.Thread(target=run, args=(member,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=budget + 5)

        answers: List[Answer] = []
        for member in members:
            answers.append(results.get(member.name, Answer(member.name, error="no response in time")))
        return CouncilAnswer(question=question, answers=answers, sent_chars=len(prompt), skipped=skipped)

    def ping(self, timeout: int = 20) -> List[Tuple[str, bool, str]]:
        """Check each configured member with a trivial prompt."""
        out: List[Tuple[str, bool, str]] = []
        members, skipped = self.selected()
        for name in skipped:
            out.append((name, False, "skipped: " + name.split("(")[-1].rstrip(")")))
        for member in members:
            messages = [{"role": "user", "content": "Reply with exactly: OK"}]
            try:
                payload = self._post(member, messages, timeout)
                text = self._extract(payload)
                out.append((member.name, bool(text.strip()), (text.strip() or "(empty)")[:60]))
            except Exception as exc:
                out.append((member.name, False, f"{type(exc).__name__}: {exc}"[:120]))
        return out

    # -- internals -------------------------------------------------------
    def _wait_turn(self, member: Member) -> float:
        if member.min_interval <= 0:
            return 0.0
        with self._lock:
            last = self._last_call.get(member.name, 0.0)
            now = time.time()
            wait = member.min_interval - (now - last)
            self._last_call[member.name] = max(now, last + member.min_interval)
        return max(0.0, wait)

    @staticmethod
    def _extract(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices") or []
        if not choices:
            if isinstance(payload.get("text"), str):
                return payload["text"]
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content

    @staticmethod
    def _http_post(member: Member, messages: List[Dict[str, str]], timeout: int) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 700,
            "stream": False,
        }
        if member.model:
            body["model"] = member.model
        headers = {"Content-Type": "application/json", "User-Agent": "offgrid-council/0.1"}
        if member.api_key:
            headers["Authorization"] = f"Bearer {member.api_key}"
        request = urllib.request.Request(
            member.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code} {detail}".strip()) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"unreachable: {exc.reason}") from None
