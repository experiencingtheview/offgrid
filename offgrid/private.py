"""Private permissions for local state that can contain personal material.

Session logs, memory, security history and connection baselines live under
``.offgrid``. On POSIX systems the directory is made ``0700`` and files are
created or hardened to ``0600``; on Windows the chmod calls are best-effort and
quietly skipped because ACLs, not POSIX mode bits, control access there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def ensure_private_dir(path: Path) -> Path:
    """Create a directory and make it owner-only on POSIX."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def harden_file(path: Path, mode: int = 0o600) -> None:
    """Make an existing file owner-only on POSIX; ignore unsupported systems."""
    if os.name != "posix":
        return
    try:
        os.chmod(Path(path), mode)
    except OSError:
        pass


def _open_flags(extra: int) -> int:
    flags = extra
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    return flags


def write_private_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text with owner-only permissions."""
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = _open_flags(os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    harden_file(path)


def append_private_text(path: Path, text: str) -> None:
    """Append UTF-8 text, creating the file and its directory privately."""
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = _open_flags(os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    harden_file(path)


def create_private_file(path: Path) -> None:
    """Create an empty owner-only file without truncating an existing one."""
    path = Path(path)
    ensure_private_dir(path.parent)
    if path.exists():
        harden_file(path)
        return
    flags = _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    descriptor = os.open(str(path), flags, 0o600)
    os.close(descriptor)
    harden_file(path)


def mode_of(path: Path) -> Optional[int]:
    """Return POSIX permission bits, or ``None`` on other platforms."""
    if os.name != "posix":
        return None
    try:
        return os.stat(Path(path)).st_mode & 0o777
    except OSError:
        return None
