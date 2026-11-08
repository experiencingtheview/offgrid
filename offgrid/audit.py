"""Consistent, deterministic security reviews.

A local model cannot be trusted to notice that it just wrote an API key into a
file, or that a token is world-readable. These checks are boring on purpose:
secrets in text, permissions, exposure, and the handful of configuration
choices that quietly send data off the machine. No network, no model, same
answer every time.

Every review is appended to `.offgrid/security.jsonl`, so "consistently" is
visible as a history rather than a promise.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .private import append_private_text, ensure_private_dir

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".offgrid",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "offgrid-library",
    "models",
    "runtime",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".rst", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".env", ".sql", ".html", ".css", ".rs", ".go", ".java", ".rb", ".php", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".swift", ".kt", ".lua", ".pl", ".tf",
}
MAX_FILE_BYTES = 400_000
MAX_FILES = 800
IGNORE_MARKERS = ("audit:ignore", "noqa: secret", "allowlist secret", "pragma: allowlist")


@dataclass(frozen=True)
class SecretRule:
    name: str
    pattern: re.Pattern
    severity: str
    message: str
    group: int = 0


SECRET_RULES: Sequence[SecretRule] = (
    SecretRule(
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
        "critical",
        "private key material in a text file",
    ),
    SecretRule("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "critical", "AWS access key id"),
    SecretRule(
        "github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
        "critical",
        "GitHub token",
    ),
    SecretRule("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "high", "Slack token"),
    SecretRule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "high", "Google API key"),
    SecretRule("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "high", "API secret key"),
    SecretRule(
        "generic-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
        ),
        "high",
        "hard-coded credential",
        group=1,
    ),
    SecretRule(
        "url-password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s:@]+)@", re.IGNORECASE),
        "high",
        "password embedded in a URL",
        group=1,
    ),
    SecretRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "medium",
        "a JWT, which may still be valid",
    ),
)


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str
    path: str = ""
    line: int = 0
    evidence: str = ""
    remediation: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 9)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "check": self.check,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }

    def line_text(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path or "(configuration)"
        mark = {"critical": "!!", "high": "!!", "medium": " *", "low": "  "}.get(self.severity, "  ")
        evidence = f" [{self.evidence}]" if self.evidence else ""
        return f"{mark} {self.severity:<8} {where}  {self.message}{evidence}"


# A finding's location is part of its identity: moving a leaked key to a new line
# is a changed finding worth seeing again.
FINGERPRINT_FIELDS = ("severity", "check", "message", "path", "line", "evidence")


def finding_key(finding: Union[Finding, Dict[str, Any]]) -> Tuple[str, ...]:
    """A stable comparison key for a live finding or a history entry."""
    data = finding.as_dict() if isinstance(finding, Finding) else finding
    if not isinstance(data, dict):
        return ()
    return tuple(str(data.get(field, "")) for field in FINGERPRINT_FIELDS)


def new_findings(
    previous: Sequence[Union[Finding, Dict[str, Any]]],
    current: Sequence[Finding],
) -> List[Finding]:
    """Return current findings absent from the previous review, in order."""
    baseline = {finding_key(item) for item in previous}
    seen = set()
    fresh: List[Finding] = []
    for finding in current:
        key = finding_key(finding)
        if key in baseline or key in seen:
            continue
        seen.add(key)
        fresh.append(finding)
    return fresh


def mask(secret: str) -> str:
    """Show enough to recognise the leak, never enough to use it."""
    secret = (secret or "").strip()
    if len(secret) <= 6:
        return "***"
    return f"{secret[:4]}...{secret[-2:]}"


def _line_ignored(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in IGNORE_MARKERS)


def _iter_text_files(root: Path, limit: int = MAX_FILES) -> Iterable[Path]:
    count = 0
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            suffix = path.suffix.lower()
            if suffix not in TEXT_SUFFIXES and name not in (".env", ".gitignore", "Dockerfile"):
                continue
            if ".env." in name and name.endswith((".example", ".sample", ".template")):
                pass  # templates are allowed to contain placeholders
            count += 1
            if count > limit:
                return
            yield path


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:2048]:
        return ""
    return raw.decode("utf-8", errors="replace")


def scan_text(rel_path: str, text: str) -> List[Finding]:
    """Secret scan for one file, with the ignore marker respected."""
    findings: List[Finding] = []
    for number, line in enumerate(text.splitlines()[:20000], 1):
        if _line_ignored(line):
            continue
        for rule in SECRET_RULES:
            found = rule.pattern.search(line)
            if not found:
                continue
            shown = mask(found.group(rule.group) if rule.group else found.group(0))
            findings.append(
                Finding(
                    severity=rule.severity,
                    check="secret",
                    message=rule.message,
                    path=rel_path,
                    line=number,
                    evidence=shown,
                    remediation=(
                        "Remove it from the file and rotate the credential; move it to an "
                        "untracked environment variable or a secrets store."
                    ),
                )
            )
            break  # one finding per line is enough
    return findings


def _check_permissions(root: Path) -> List[Finding]:
    findings: List[Finding] = []
    token = root / ".offgrid" / "fleet-token"
    if token.is_file() and os.name == "posix":
        mode = stat.S_IMODE(token.stat().st_mode)
        if mode & 0o077:
            findings.append(
                Finding(
                    severity="high",
                    check="permissions",
                    message="fleet token is readable by other users",
                    path=".offgrid/fleet-token",
                    evidence=oct(mode),
                    remediation="chmod 600 .offgrid/fleet-token",
                )
            )
    database = root / ".offgrid" / "memory.db"
    if database.is_file() and os.name == "posix":
        mode = stat.S_IMODE(database.stat().st_mode)
        if mode & 0o002:
            findings.append(
                Finding(
                    severity="medium",
                    check="permissions",
                    message="memory database is world-writable",
                    path=".offgrid/memory.db",
                    evidence=oct(mode),
                    remediation="chmod 600 .offgrid/memory.db",
                )
            )
    return findings


def _check_gitignore(root: Path) -> List[Finding]:
    findings: List[Finding] = []
    gitignore = root / ".gitignore"
    if not (root / ".git").exists():
        return findings
    text = _read_text(gitignore)
    for required, why in ((".offgrid", "memory and fleet tokens live there"), (".env", "secrets often live there")):
        if text and required in text:
            continue
        findings.append(
            Finding(
                severity="medium",
                check="gitignore",
                message=f".gitignore does not ignore {required} ({why})",
                path=".gitignore",
                remediation=f"add `{required}` to .gitignore",
            )
        )
    return findings


def _check_posture(config: Any) -> List[Finding]:
    findings: List[Finding] = []
    if config is None:
        return findings
    endpoint = str(getattr(config, "endpoint", "") or "")
    if endpoint and getattr(config, "allow_remote", False):
        findings.append(
            Finding(
                severity="medium",
                check="exposure",
                message="the model endpoint is allowed to be a non-local host",
                path=".offgrid.json",
                evidence=endpoint[:60],
                remediation="keep the endpoint on loopback unless you trust the host",
            )
        )
    if getattr(config, "allow_public_council", False):
        findings.append(
            Finding(
                severity="medium",
                check="privacy",
                message="public council members may receive parts of your prompts",
                path=".offgrid.json",
                remediation="drop --council-public unless you accept that",
            )
        )
    if getattr(config, "fleet", False):
        findings.append(
            Finding(
                severity="info",
                check="exposure",
                message="fleet routing is on: model calls may run on LAN peers",
                path=".offgrid.json",
                remediation="keep peer tokens private and the LAN trusted",
            )
        )
    return findings


def review_workspace(
    root: Path,
    changed: Optional[Sequence[str]] = None,
    config: Any = None,
) -> List[Finding]:
    """Review the workspace (or just what changed) plus this run's posture."""
    root = Path(root)
    findings: List[Finding] = []
    targets: List[Path] = []
    if changed is not None:
        for rel in changed:
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                targets.append(candidate)
    else:
        targets = list(_iter_text_files(root))

    for path in targets:
        text = _read_text(path)
        if not text:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        findings.extend(scan_text(rel, text))

    findings.extend(_check_permissions(root))
    findings.extend(_check_gitignore(root))
    findings.extend(_check_posture(config))
    findings.sort(key=lambda item: (item.rank, item.path, item.line))
    return findings


def counts(findings: Sequence[Finding]) -> Dict[str, int]:
    out: Dict[str, int] = {level: 0 for level in SEVERITY_ORDER}
    for finding in findings:
        out[finding.severity] = out.get(finding.severity, 0) + 1
    out["total"] = len(findings)
    return out


def verdict(findings: Sequence[Finding]) -> str:
    worst = min((finding.rank for finding in findings), default=9)
    for level, rank in SEVERITY_ORDER.items():
        if rank == worst:
            return level
    return "clean"


def record(path: Path, findings: Sequence[Finding], source: str = "run") -> None:
    """Append one line per review so consistency can be checked later."""
    path = Path(path)
    entry = {
        "t": round(time.time(), 3),
        "source": source,
        "verdict": verdict(findings),
        "counts": counts(findings),
        "findings": [finding.as_dict() for finding in findings[:40]],
    }
    try:
        ensure_private_dir(path.parent)
        append_private_text(path, json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def history(path: Path, limit: int = 10) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    entries.append(data)
    except OSError:
        return []
    return entries[-limit:]
