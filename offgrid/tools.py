"""Tools: real file edits, real searches, real shell commands.

Everything is confined to the workspace and gated by a command policy, because
this thing runs on a person's actual laptop.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import Config
from .council import CouncilUnavailable
from .models import ToolResult
from .workspace import Workspace, WorkspaceError

# Never allowed, not even with --yes.
FORBIDDEN: Sequence[tuple] = (
    (r"rm\s+(-[a-zA-Z]*\s+)*-?[rfRF]*[rfRF]\s+/(\s|$)", "recursive delete of the filesystem root"),
    (r"rm\s+-[a-zA-Z]*[rf]\s+~/?(\s|$)", "recursive delete of the home directory"),
    (r"\bmkfs(\.\w+)?\b", "formatting a filesystem"),
    (r"\bdd\b[^\n]*\bof=/dev/", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|disk)", "raw write to a block device"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutting down the machine"),
    (r"\bformat\s+[a-zA-Z]:", "formatting a drive"),
    (r"\bdel\s+/[fsq]\b", "mass deletion"),
)

# Allowed, but only with explicit human approval, even in auto mode.
DESTRUCTIVE: Sequence[tuple] = (
    (r"\bsudo\b", "privilege escalation"),
    (r"\bgit\s+push\b", "publishing to a remote"),
    (r"\bgit\s+reset\s+--hard\b", "discarding uncommitted work"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[dfx]", "deleting untracked files"),
    (r"\brm\s+-[a-zA-Z]*[rf]", "recursive delete"),
    (r"\bchmod\s+-R\s+777\b", "world-writable permissions"),
    (r"\bchown\s+-R\b", "recursive ownership change"),
    (r"\bkill(all)?\b", "terminating processes"),
    (r"\bmv\b[^\n]*\s/(\s|$)", "moving files into the filesystem root"),
)

# Refused when the network is disabled (--offline).
NETWORK: Sequence[tuple] = (
    (r"\b(curl|wget|nc|ncat|telnet|ssh|scp|sftp|rsync)\b", "network transfer tool"),
    (r"\b(pip3?|pipx)\s+install\b", "package install"),
    (r"\b(npm|pnpm|yarn|bun)\s+(install|i|ci|add)\b", "package install"),
    (r"\b(apt|apt-get|yum|dnf|pacman|apk|brew|choco|winget)\s+(install|add)\b", "package install"),
    (r"\b(cargo\s+install|go\s+get|gem\s+install|dotnet\s+add)\b", "package install"),
    (r"\bgit\s+(push|pull|fetch|clone|ls-remote)\b", "remote git operation"),
    (r"\b(gh|hub)\b", "hosting CLI"),
    (r"\b(ollama)\s+pull\b", "model download"),
)

OFFLINE_SAFE = ("--no-index", "--offline", "--find-links", "--no-build-isolation")

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT = 8000


def _matches(patterns: Sequence[tuple], command: str) -> Optional[tuple]:
    for pattern, reason in patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern, reason
    return None


class ToolBox:
    """Owns the tool implementations and the permission checks around them."""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        approve: Optional[Callable[[str], bool]] = None,
        log: Optional[Callable[[str], None]] = None,
        memory=None,
        library=None,
        council=None,
    ) -> None:
        self.ws = workspace
        self.config = config
        self.approve = approve
        self.log = log or (lambda _msg: None)
        self.memory = memory
        self.library = library
        self.council = council
        self.council_recorder: Optional[Callable[[str, Any], None]] = None
        self.finished = False
        self.summary = ""
        self.commands: List[str] = []
        self._build()

    # -- registry --------------------------------------------------------
    def _build(self) -> None:
        self._impl: Dict[str, Callable[..., ToolResult]] = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "list_files": self.list_files,
            "search_files": self.search_files,
            "run_command": self.run_command,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "remember": self.remember,
            "recall": self.recall,
            "library_search": self.library_search,
            "library_read": self.library_read,
            "ask_council": self.ask_council,
            "finish": self.finish,
        }
        self._schemas: Dict[str, Dict[str, Any]] = {
            "read_file": {
                "description": "Read a text file. Returns numbered lines. Use offset/limit for big files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "workspace-relative file path"},
                        "offset": {"type": "integer", "description": "first line, 1-based", "default": 1},
                        "limit": {"type": "integer", "description": "max lines", "default": 300},
                    },
                    "required": ["path"],
                },
            },
            "write_file": {
                "description": "Create or overwrite a file with full content. Parent directories are created.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string", "description": "complete file content"},
                    },
                    "required": ["path", "content"],
                },
            },
            "edit_file": {
                "description": "Replace an exact snippet in an existing file. `old` must appear exactly once.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string", "description": "exact existing text, include context lines"},
                        "new": {"type": "string", "description": "replacement text"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "old", "new"],
                },
            },
            "list_files": {
                "description": "List files and directories under a path, recursively, ignoring junk directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "depth": {"type": "integer", "default": 3},
                    },
                },
            },
            "search_files": {
                "description": "Regex search across workspace text files. Returns path:line: match.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Python-style regex"},
                        "glob": {"type": "string", "description": "optional path filter, e.g. *.py"},
                        "path": {"type": "string", "default": "."},
                        "max_matches": {"type": "integer", "default": 60},
                    },
                    "required": ["pattern"],
                },
            },
            "run_command": {
                "description": "Run a shell command in the workspace and return stdout, stderr and exit code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer", "description": "seconds", "default": DEFAULT_TIMEOUT},
                        "cwd": {"type": "string", "description": "workspace-relative directory", "default": "."},
                    },
                    "required": ["command"],
                },
            },
            "git_status": {
                "description": "Show `git status --short` for the workspace.",
                "parameters": {"type": "object", "properties": {}},
            },
            "git_diff": {
                "description": "Show the current uncommitted `git diff`.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "default": "."}},
                },
            },
            "remember": {
                "description": "Save a durable note for future sessions (a fact about this repo, a rule, a correction).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "one short sentence"},
                        "kind": {
                            "type": "string",
                            "enum": ["fact", "repo", "lesson", "warning", "preference", "correction"],
                            "default": "fact",
                        },
                        "confidence": {"type": "number", "description": "0.0-1.0, be honest", "default": 0.6},
                        "key": {"type": "string", "description": "optional stable key so it updates instead of duplicating"},
                    },
                    "required": ["text"],
                },
            },
            "recall": {
                "description": "Search your own long-term memory from previous sessions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            "library_search": {
                "description": "Search the offline reference library (survival, medicine, coding docs).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 6},
                    },
                    "required": ["query"],
                },
            },
            "library_read": {
                "description": "Read a text file from the offline library, with numbered lines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "path relative to the library root"},
                        "offset": {"type": "integer", "default": 1},
                        "limit": {"type": "integer", "default": 200},
                    },
                    "required": ["path"],
                },
            },
            "ask_council": {
                "description": "Ask independent advisor models when you genuinely do not know. Returns disagreement as disagreement.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string", "description": "self-contained question"}},
                    "required": ["question"],
                },
            },
            "finish": {
                "description": "Declare your task complete. Call this as your last action.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string", "description": "one or two sentences"}},
                    "required": ["summary"],
                },
            },
        }

    def schemas(self, allowed: Sequence[str]) -> List[Dict[str, Any]]:
        out = []
        for name in allowed:
            if name in self._schemas:
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": self._schemas[name]["description"],
                            "parameters": self._schemas[name]["parameters"],
                        },
                    }
                )
        return out

    def available(self, allowed: Sequence[str]) -> List[str]:
        return [name for name in allowed if name in self._impl]

    def call(self, name: str, args: Dict[str, Any]) -> ToolResult:
        fn = self._impl.get(name)
        if not fn:
            return ToolResult(False, f"unknown tool '{name}'. Available: {', '.join(sorted(self._impl))}")
        try:
            return fn(**args)
        except TypeError as exc:
            return ToolResult(False, f"bad arguments for {name}: {exc}")
        except WorkspaceError as exc:
            return ToolResult(False, str(exc))
        except Exception as exc:  # a broken tool must not kill the run
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    # -- file tools ------------------------------------------------------
    def read_file(self, path: str, offset: int = 1, limit: int = 300) -> ToolResult:
        text, total = self.ws.read_lines(path, offset=offset, limit=min(int(limit), 1000))
        start = max(int(offset), 1)
        end = min(start + int(limit) - 1, total)
        header = f"{self.ws.rel(self.ws.resolve(path))} lines {start}-{end} of {total}\n"
        if total == 0:
            return ToolResult(True, f"{header}(empty file)")
        return ToolResult(True, header + text, {"lines": total})

    def write_file(self, path: str, content: str = "") -> ToolResult:
        note = self.ws.write_text(path, content if content is not None else "")
        return ToolResult(True, note)

    def edit_file(self, path: str, old: str = "", new: str = "", replace_all: bool = False) -> ToolResult:
        if not old:
            return ToolResult(False, "`old` must not be empty; use write_file to create a file")
        current = self.ws.read_text(path)
        count = current.count(old)
        if count == 0:
            return ToolResult(
                False,
                f"could not find that exact text in {path}. Re-read the file and copy the text verbatim, "
                "including indentation.",
            )
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                f"that text appears {count} times in {path}. Include more surrounding lines to make it unique, "
                "or pass replace_all=true.",
            )
        updated = current.replace(old, new, -1 if replace_all else 1)
        note = self.ws.write_text(path, updated)
        return ToolResult(True, note)

    def list_files(self, path: str = ".", depth: int = 3) -> ToolResult:
        target = self.ws.resolve(path)
        if target.is_file():
            size = target.stat().st_size
            return ToolResult(True, f"{self.ws.rel(target)} ({size} bytes)")
        rel = self.ws.rel(target)
        if rel == ".":
            listing = self.ws.tree(limit=250, max_depth=max(1, min(int(depth), 6)))
        else:
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if child.name.startswith(".") or child.name in {".offgrid", "node_modules", "__pycache__"}:
                    continue
                entries.append(child.name + ("/" if child.is_dir() else ""))
            listing = "\n".join(entries) or "(empty)"
        return ToolResult(True, listing)

    def search_files(
        self, pattern: str, glob: str = "", path: str = ".", max_matches: int = 60
    ) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(False, f"invalid regex: {exc}")
        limit = max(1, min(int(max_matches), 200))
        hits: List[str] = []
        scanned = 0
        for file_path in self.ws.iter_files(path, limit=1500):
            rel_path = self.ws.rel(file_path)
            if glob and not fnmatch.fnmatch(rel_path, glob) and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                if file_path.stat().st_size > 512 * 1024:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in text[:4096]:
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    snippet = line.strip()
                    if len(snippet) > 200:
                        snippet = snippet[:200] + "..."
                    hits.append(f"{rel_path}:{lineno}: {snippet}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        if not hits:
            return ToolResult(True, f"no matches for /{pattern}/ in {scanned} files")
        return ToolResult(True, "\n".join(hits), {"matches": len(hits)})

    # -- shell -----------------------------------------------------------
    def run_command(self, command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = ".") -> ToolResult:
        allowed, reason = self._authorize(command)
        if not allowed:
            return ToolResult(False, reason)

        workdir = self.ws.root
        try:
            candidate = self.ws.resolve(cwd or ".")
            if candidate.is_dir():
                workdir = candidate
        except WorkspaceError as exc:
            return ToolResult(False, str(exc))
        seconds = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        env = dict(os.environ)
        env.update(
            {
                "OFFGRID": "1",
                "CI": "1",
                "PYTHONUNBUFFERED": "1",
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "TERM": "dumb",
            }
        )
        self.commands.append(command)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workdir),
                env=env,
                capture_output=True,
                text=True,
                timeout=seconds,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"`{command}` timed out after {seconds}s")
        except OSError as exc:
            return ToolResult(False, f"could not run `{command}`: {exc}")

        body = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        body = body.strip() or "(no output)"
        if len(body) > MAX_OUTPUT:
            body = (
                body[: MAX_OUTPUT // 2]
                + f"\n... [{len(body) - MAX_OUTPUT} chars trimmed] ...\n"
                + body[-MAX_OUTPUT // 2 :]
            )
        text = f"$ {command}\nexit {proc.returncode}\n{body}"
        return ToolResult(proc.returncode == 0, text, {"exit_code": proc.returncode})

    def _authorize(self, command: str) -> tuple:
        command = (command or "").strip()
        if not command:
            return False, "empty command"
        if self.config.command_policy == "deny":
            return False, "shell access is disabled (command-policy=deny); edit files instead"

        hit = _matches(FORBIDDEN, command)
        if hit:
            return False, f"refused: {hit[1]} is never allowed. Find a narrower command."

        if self.config.network != "allow" and not any(flag in command for flag in OFFLINE_SAFE):
            hit = _matches(NETWORK, command)
            if hit:
                return (
                    False,
                    f"refused: {hit[1]} needs the network, and OffGrid is running offline. "
                    "Use what is already installed on this machine.",
                )

        destructive = _matches(DESTRUCTIVE, command)
        if destructive:
            if self.approve is None:
                return False, f"needs human approval ({destructive[1]}) and nobody is at the terminal"
            self.log(f"approval needed: {destructive[1]}")
            if not self.approve(command):
                return False, f"the user declined: {destructive[1]}"
            return True, ""

        if self.config.command_policy == "ask":
            if self.approve is None:
                return False, "needs approval and nobody is at the terminal"
            if not self.approve(command):
                return False, "the user declined this command"
        return True, ""

    # -- git -------------------------------------------------------------
    def git_status(self) -> ToolResult:
        if not (self.ws.root / ".git").exists():
            return ToolResult(True, "not a git repository")
        result = self._git("status --short --branch")
        return result

    def git_diff(self, path: str = ".") -> ToolResult:
        if not (self.ws.root / ".git").exists():
            return ToolResult(True, "not a git repository")
        return self._git("diff")

    def _git(self, args: str) -> ToolResult:
        try:
            proc = subprocess.run(
                f"git {args}",
                shell=True,
                cwd=str(self.ws.root),
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult(False, f"git failed: {exc}")
        body = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(proc.returncode == 0, body.strip() or "(no changes)")

    # -- memory, library, council ----------------------------------------
    def recall(self, query: str = "", limit: int = 5) -> ToolResult:
        if self.memory is None:
            return ToolResult(False, "memory is disabled for this run (--no-memory)")
        items = self.memory.recall(query, limit=max(1, min(int(limit), 20)))
        if not items:
            return ToolResult(True, "nothing remembered about that yet")
        self.memory.touch([item.id for item in items])
        return ToolResult(True, "\n".join(item.line() for item in items))

    def remember(
        self, text: str, kind: str = "fact", confidence: float = 0.6, key: str = ""
    ) -> ToolResult:
        if self.memory is None:
            return ToolResult(False, "memory is disabled for this run (--no-memory)")
        new_id = self.memory.remember(text, kind=kind, source="agent", confidence=confidence, key=key)
        if new_id is None:
            return ToolResult(True, "already remembered; strengthened the existing note")
        return ToolResult(True, f"remembered as #{new_id}")

    def library_search(self, query: str, limit: int = 6) -> ToolResult:
        if self.library is None:
            return ToolResult(False, "no offline library is configured")
        hits = self.library.search(query, limit=max(1, min(int(limit), 20)))
        if not hits:
            stats = self.library.stats()
            if not stats.get("files"):
                return ToolResult(
                    True,
                    "the library is empty. Say you could not check rather than guessing. "
                    "(It can be filled with: python3 scripts/fetch_library.py --get survival coding)",
                )
            return ToolResult(True, "no matches in the library for that. Say so rather than inventing an answer.")
        return ToolResult(True, "\n".join(hits))

    def library_read(self, path: str, offset: int = 1, limit: int = 200) -> ToolResult:
        if self.library is None:
            return ToolResult(False, "no offline library is configured")
        try:
            return ToolResult(True, self.library.read(path, offset=offset, limit=limit))
        except ValueError as exc:
            return ToolResult(False, str(exc))

    def ask_council(self, question: str) -> ToolResult:
        if self.council is None:
            return ToolResult(False, "no council is configured")
        try:
            answer = self.council.ask(question)
        except CouncilUnavailable as exc:
            return ToolResult(
                False,
                f"council unavailable: {exc}. You must tell the user you do not know and what would answer it, "
                "rather than inventing an answer.",
            )
        if not answer.usable:
            return ToolResult(False, "nobody on the council could answer. Say you do not know.")
        if self.council_recorder is not None:
            self.council_recorder(question, answer)
        return ToolResult(True, answer.render())

    # -- control ---------------------------------------------------------
    def finish(self, summary: str = "") -> ToolResult:
        self.finished = True
        self.summary = summary.strip()
        return ToolResult(True, "acknowledged: task marked complete")

    def reset_control(self) -> None:
        self.finished = False
        self.summary = ""
