"""Mechanical quality gates.

Small local models fail in predictable, boring ways: they truncate a file, leave
`# rest of the code here`, stub a function with `pass`, emit invalid JSON, or
ship a syntax error. None of that needs a model to detect - and catching it
before review is what makes "quality first" more than a slogan.

These checks are deterministic, offline and language-partial on purpose: Python
and JSON get real parsers, everything else gets placeholder scanning.
"""

from __future__ import annotations

import json
import re
from typing import List

from .workspace import Workspace, WorkspaceError

MAX_FILE_CHARS = 400_000

PLACEHOLDERS = (
    (
        re.compile(
            r"^[ \t]*(?:#|//|/\*)[ \t]*(?:\.\.\.|<snip>|snip|rest of (?:the )?(?:code|file|implementation)"
            r"|implementation (?:here|omitted|goes here)|your code here|code goes here)"
            r"(?:[ \t]+(?:here|below|follows|omitted))?[ \t]*(?:\*/)?[ \t]*$",
            re.IGNORECASE | re.MULTILINE,
        ),
        "placeholder comment standing in for real code",
    ),
    (re.compile(r"^[ \t]*\.\.\.[ \t]*$", re.MULTILINE), "bare ellipsis where code should be"),
    (re.compile(r"\bTODO\b"), "unfinished TODO"),
    (re.compile(r"\bFIXME\b"), "unfinished FIXME"),
    (re.compile(r"\bNotImplementedError\b"), "unimplemented branch"),
    (re.compile(r"<your[-_ ]?(?:api[-_ ]?)?key>", re.IGNORECASE), "placeholder credential"),
    (re.compile(r"\bpass\s*#\s*(?:stub|todo|implement)", re.IGNORECASE), "stubbed function body"),
)


def check_text(rel_path: str, text: str) -> List[str]:
    """Find mechanical defects in one changed file."""
    issues: List[str] = []
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS]

    for pattern, reason in PLACEHOLDERS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            issues.append(f"{rel_path}:{line}: {reason} — finish the file completely")
            break  # one report per pattern per file is enough

    if rel_path.endswith(".py"):
        try:
            compile(text, rel_path, "exec")
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "unknown line"
            issues.append(f"{rel_path}:{exc.lineno or 0}: python syntax error at {where}: {exc.msg}")
    elif rel_path.endswith(".json"):
        try:
            json.loads(text)
        except ValueError as exc:
            issues.append(f"{rel_path}: invalid JSON: {exc}")

    return issues


def check_workspace(workspace: Workspace, max_files: int = 40) -> List[str]:
    """Run the checks over everything this run changed."""
    issues: List[str] = []
    try:
        changed = workspace.changed_files()
    except Exception:
        return issues
    for rel_path in changed[:max_files]:
        try:
            text = workspace.read_text(rel_path)
        except (WorkspaceError, OSError):
            continue
        issues.extend(check_text(rel_path, text))
    return issues
