"""The workspace: every file the agents may touch, and nothing else."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".cache",
    ".offgrid",
    ".idea",
    ".vscode",
    "vendor",
    "Pods",
    "runtime",
    "models",
}

IGNORE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".gguf",
    ".safetensors",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
)

MAX_FILE_BYTES = 2 * 1024 * 1024


class WorkspaceError(Exception):
    """Raised when a tool tries to reach outside the workspace."""


class Workspace:
    """Confines file access to a root directory and records every change."""

    def __init__(self, root: str, allow_outside: bool = False) -> None:
        self.root = Path(root).expanduser().resolve()
        self.allow_outside = allow_outside
        # path -> original content (None means the file did not exist)
        self.originals: Dict[str, Optional[str]] = {}
        self.deleted: List[str] = []

    # -- paths -----------------------------------------------------------
    def resolve(self, path: str) -> Path:
        if not path or not str(path).strip():
            raise WorkspaceError("empty path")
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not self.allow_outside:
            try:
                resolved.relative_to(self.root)
            except ValueError:
                raise WorkspaceError(
                    f"path '{path}' is outside the workspace ({self.root}); staying confined"
                ) from None
        return resolved

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).is_file()
        except WorkspaceError:
            return False

    def is_ignored(self, path: Path) -> bool:
        parts = set(path.parts)
        if parts & IGNORE_DIRS:
            return True
        return path.name.endswith(IGNORE_SUFFIXES)

    # -- reading ---------------------------------------------------------
    def read_text(self, path: str) -> str:
        resolved = self.resolve(path)
        if not resolved.is_file():
            raise WorkspaceError(f"no such file: {path}")
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES:
            raise WorkspaceError(f"file too large to read ({size} bytes): {path}")
        data = resolved.read_bytes()
        if b"\x00" in data[:4096]:
            raise WorkspaceError(f"binary file, refusing to read as text: {path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def read_lines(self, path: str, offset: int = 1, limit: int = 400) -> Tuple[str, int]:
        """Return (windowed numbered text, total line count)."""
        text = self.read_text(path)
        lines = text.splitlines()
        total = len(lines)
        start = max(int(offset) - 1, 0)
        end = min(start + max(int(limit), 1), total)
        width = len(str(total)) + 1
        chunk = [f"{idx + 1:>{width}}| {lines[idx]}" for idx in range(start, end)]
        return "\n".join(chunk), total

    # -- writing ---------------------------------------------------------
    def record(self, rel_path: str) -> None:
        if rel_path in self.originals:
            return
        target = self.root / rel_path
        if target.is_file():
            try:
                self.originals[rel_path] = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self.originals[rel_path] = ""
        else:
            self.originals[rel_path] = None

    def write_text(self, path: str, content: str) -> str:
        resolved = self.resolve(path)
        rel_path = self.rel(resolved)
        self.record(rel_path)
        existed = resolved.is_file()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        if rel_path in self.deleted:
            self.deleted.remove(rel_path)
        lines = content.count("\n") + (1 if content else 0)
        return f"{'updated' if existed else 'created'} {rel_path} ({lines} lines, {len(content)} bytes)"

    def delete(self, path: str) -> str:
        resolved = self.resolve(path)
        rel_path = self.rel(resolved)
        if not resolved.is_file():
            raise WorkspaceError(f"no such file: {path}")
        self.record(rel_path)
        resolved.unlink()
        self.deleted.append(rel_path)
        return f"deleted {rel_path}"

    # -- walking ---------------------------------------------------------
    def iter_files(self, subdir: str = ".", limit: int = 400) -> Iterator[Path]:
        base = self.resolve(subdir)
        if base.is_file():
            yield base
            return
        count = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or name.endswith(IGNORE_SUFFIXES):
                    continue
                path = Path(dirpath) / name
                yield path
                count += 1
                if count >= limit:
                    return

    def tree(self, limit: int = 200, max_depth: int = 3) -> str:
        """A compact indented listing, cheap enough to put in a prompt."""
        rows: List[str] = []
        base_depth = len(self.root.parts)

        def walk(directory: Path, depth: int) -> None:
            if len(rows) >= limit:
                return
            try:
                entries = sorted(
                    directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
                )
            except OSError:
                return
            for entry in entries:
                if len(rows) >= limit:
                    return
                if entry.name in IGNORE_DIRS or entry.name.endswith(IGNORE_SUFFIXES):
                    continue
                if entry.name.startswith(".") and entry.name not in (".offgrid.json",):
                    continue
                if entry.is_dir():
                    rows.append("  " * depth + entry.name + "/")
                    if depth + 1 < max_depth:
                        walk(entry, depth + 1)
                else:
                    rows.append("  " * depth + entry.name)

        walk(self.root, 0)
        if len(rows) >= limit:
            rows.append(f"... listing capped at {limit} entries")
        return "\n".join(rows) or "(empty workspace)"

    # -- change review ---------------------------------------------------
    def changed_files(self) -> List[str]:
        changed = []
        for rel_path, original in self.originals.items():
            target = self.root / rel_path
            if rel_path in self.deleted:
                changed.append(rel_path)
                continue
            if not target.is_file():
                changed.append(rel_path)
                continue
            try:
                current = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if current != (original or ""):
                changed.append(rel_path)
        return sorted(changed)

    def diff(self, max_chars: int = 20000) -> str:
        """Unified diff of everything the agents changed this run."""
        chunks: List[str] = []
        for rel_path in self.changed_files():
            original = self.originals.get(rel_path)
            target = self.root / rel_path
            if rel_path in self.deleted or not target.is_file():
                chunks.append(f"--- {rel_path}\n+++ /dev/null\n(deleted)")
                continue
            try:
                current = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            diff = difflib.unified_diff(
                (original or "").splitlines(),
                current.splitlines(),
                fromfile=f"a/{rel_path}" if original is not None else "/dev/null",
                tofile=f"b/{rel_path}",
                lineterm="",
                n=3,
            )
            chunks.append("\n".join(diff))
        text = "\n".join(chunk for chunk in chunks if chunk.strip())
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... diff trimmed at {max_chars} chars ..."
        return text

    def snapshot(self) -> Dict[str, Optional[str]]:
        """Current state of changed files, for feeding a reviewer."""
        out: Dict[str, Optional[str]] = {}
        for rel_path in self.changed_files():
            target = self.root / rel_path
            if rel_path in self.deleted or not target.is_file():
                out[rel_path] = None
                continue
            try:
                out[rel_path] = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                out[rel_path] = None
        return out
