"""Install a native desktop shortcut for OffGrid.

A shortcut points at the existing top-level launcher when OffGrid is running
from a folder. That matters for a USB bundle: the launcher starts the bundled
model server before opening chat. An installed wheel has no bundled server, so
its shortcut starts the current Python interpreter instead.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._bundle import source_root

MARKER = "OffGrid desktop shortcut"
LAUNCHERS = {
    "linux": "OffGrid.sh",
    "macos": "OffGrid.command",
    "windows": "OffGrid.bat",
}
SHORTCUT_NAMES = {
    "linux": "OffGrid.desktop",
    "macos": "OffGrid.command",
    "windows": "OffGrid.lnk",
}


class DesktopError(Exception):
    """A desktop shortcut could not be installed or removed."""


@dataclass(frozen=True)
class Shortcut:
    """A shortcut that was installed or found on the desktop."""

    path: Path
    platform: str
    target: Path
    arguments: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "platform": self.platform,
            "target": str(self.target),
            "arguments": list(self.arguments),
        }


@dataclass(frozen=True)
class _Launch:
    command: Path
    arguments: Tuple[str, ...]
    working_dir: Path


def platform_key(platform: Optional[str] = None) -> str:
    """Return ``linux``, ``macos`` or ``windows`` for a platform string."""
    value = (sys.platform if platform is None else platform).lower()
    if value.startswith("linux"):
        return "linux"
    if value in ("darwin", "mac", "macos"):
        return "macos"
    if value in ("win32", "win64", "windows", "cygwin", "msys"):
        return "windows"
    raise DesktopError("desktop shortcuts are supported on Linux, macOS and Windows")


def _home(env: Dict[str, str]) -> Path:
    raw = env.get("HOME") or env.get("USERPROFILE")
    return Path(raw).expanduser() if raw else Path.home()


def _windows_desktop() -> Optional[Path]:
    """Ask Windows for the current user's Desktop known folder."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        # CSIDL_DESKTOPDIRECTORY, SHGFP_TYPE_CURRENT
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def desktop_directory(
    platform: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return the current user's desktop directory without creating it."""
    key = platform_key(platform)
    environment = dict(os.environ) if env is None else env
    home_path = Path(home).expanduser() if home is not None else _home(environment)

    if key == "linux":
        explicit = environment.get("XDG_DESKTOP_DIR")
        if explicit:
            return Path(explicit).expanduser()
        config_home = Path(
            environment.get("XDG_CONFIG_HOME") or (home_path / ".config")
        ).expanduser()
        user_dirs = config_home / "user-dirs.dirs"
        try:
            for line in user_dirs.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith("XDG_DESKTOP_DIR="):
                    continue
                value = stripped.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                value = value.replace("${HOME}", str(home_path)).replace(
                    "$HOME", str(home_path)
                )
                if value:
                    return Path(value).expanduser()
        except OSError:
            pass
        return home_path / "Desktop"

    if key == "macos":
        return home_path / "Desktop"

    known = _windows_desktop()
    return known if known is not None else home_path / "Desktop"


def shortcut_path(
    desktop_dir: Optional[Path] = None,
    platform: Optional[str] = None,
) -> Path:
    """Return the shortcut path for this platform, without creating it."""
    key = platform_key(platform)
    base = Path(desktop_dir).expanduser() if desktop_dir is not None else desktop_directory(key)
    return base / SHORTCUT_NAMES[key]


def _launcher_root(key: str, root: Optional[Path]) -> Optional[Path]:
    name = LAUNCHERS[key]
    if root is not None:
        candidate = Path(root).expanduser().resolve()
        if not (candidate / name).is_file():
            raise DesktopError(f"{candidate / name} does not exist")
        return candidate

    cwd = Path.cwd()
    if (cwd / name).is_file():
        return cwd

    source = source_root()
    if source is not None and (source / name).is_file():
        return source
    return None


def _launch_spec(key: str, root: Optional[Path]) -> _Launch:
    folder = _launcher_root(key, root)
    if folder is not None:
        return _Launch(folder / LAUNCHERS[key], (), folder)

    python = Path(sys.executable) if sys.executable else Path("python3")
    return _Launch(python, ("-m", "offgrid", "chat"), Path.home())


def _desktop_arg(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise DesktopError("desktop shortcut paths cannot contain a newline")
    value = value.replace("%", "%%")
    value = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{value}"'


def _desktop_exec(command: Path, arguments: Tuple[str, ...]) -> str:
    parts = (str(command),) + tuple(arguments)
    return " ".join(_desktop_arg(part) for part in parts)


def _write_text(path: Path, text: str, executable: bool = False) -> None:
    try:
        path.write_text(text, encoding="utf-8")
        if executable and os.name != "nt":
            path.chmod(0o755)
    except OSError as exc:
        raise DesktopError(f"could not write {path}: {exc}") from exc


def _write_linux(path: Path, launch: _Launch) -> None:
    working_dir = str(launch.working_dir)
    if "\n" in working_dir or "\r" in working_dir:
        raise DesktopError("desktop shortcut paths cannot contain a newline")
    text = "\n".join(
        (
            f"# {MARKER}",
            "[Desktop Entry]",
            "Version=1.0",
            "Type=Application",
            "Name=OffGrid",
            "GenericName=Offline Coding Team",
            "Comment=Start OffGrid, a free offline multi-agent coding team",
            f"Exec={_desktop_exec(launch.command, launch.arguments)}",
            f"Path={working_dir}",
            "Terminal=true",
            "Icon=utilities-terminal",
            "Categories=Development;",
            "StartupNotify=true",
            "X-OffGrid-Shortcut=true",
            "",
        )
    )
    _write_text(path, text, executable=True)


def _write_macos(path: Path, launch: _Launch) -> None:
    command = shlex.quote(str(launch.command))
    arguments = "".join(" " + shlex.quote(argument) for argument in launch.arguments)
    text = (
        "#!/bin/sh\n"
        f"# {MARKER} created by `offgrid desktop install`.\n"
        f"cd {shlex.quote(str(launch.working_dir))} || exit 1\n"
        f"exec {command}{arguments}\n"
    )
    _write_text(path, text, executable=True)


def _powershell() -> str:
    for name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise DesktopError("PowerShell is required to create a Windows shortcut")


POWERSHELL_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:OFFGRID_SHORTCUT_PATH)
$shortcut.TargetPath = $env:OFFGRID_SHORTCUT_TARGET
$shortcut.Arguments = $env:OFFGRID_SHORTCUT_ARGUMENTS
$shortcut.WorkingDirectory = $env:OFFGRID_SHORTCUT_WORKDIR
$shortcut.Description = 'OffGrid offline coding team'
$shortcut.WindowStyle = 1
$shortcut.Save()
"""


def _write_windows(path: Path, launch: _Launch) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "OFFGRID_SHORTCUT_PATH": str(path),
            "OFFGRID_SHORTCUT_TARGET": str(launch.command),
            "OFFGRID_SHORTCUT_ARGUMENTS": subprocess.list2cmdline(
                list(launch.arguments)
            ),
            "OFFGRID_SHORTCUT_WORKDIR": str(launch.working_dir),
        }
    )
    command = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SCRIPT]
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopError(f"could not run PowerShell: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            detail = ": " + detail.splitlines()[-1][:300]
        raise DesktopError(f"PowerShell could not create {path}{detail}")


def _is_ours(path: Path, key: str) -> bool:
    if key == "windows":
        return path.suffix.lower() == ".lnk"
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install(
    desktop_dir: Optional[Path] = None,
    root: Optional[Path] = None,
    platform: Optional[str] = None,
    force: bool = False,
) -> Shortcut:
    """Create or replace a shortcut to this OffGrid copy."""
    key = platform_key(platform)
    path = shortcut_path(desktop_dir=desktop_dir, platform=key)
    launch = _launch_spec(key, root)

    if path.is_symlink():
        raise DesktopError(f"refusing to replace the symbolic link {path}")
    if path.exists() and not force and not _is_ours(path, key):
        raise DesktopError(f"{path} already exists; pass --force to replace it")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DesktopError(f"could not create {path.parent}: {exc}") from exc

    if key == "linux":
        _write_linux(path, launch)
    elif key == "macos":
        _write_macos(path, launch)
    else:
        _write_windows(path, launch)
    return Shortcut(path, key, launch.command, launch.arguments)


def remove(
    desktop_dir: Optional[Path] = None,
    platform: Optional[str] = None,
    force: bool = False,
) -> Optional[Path]:
    """Remove the OffGrid shortcut, returning the path that was removed."""
    key = platform_key(platform)
    path = shortcut_path(desktop_dir=desktop_dir, platform=key)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        try:
            path.unlink()
        except OSError as exc:
            raise DesktopError(f"could not remove {path}: {exc}") from exc
        return path
    if not path.is_file():
        raise DesktopError(f"{path} is not a shortcut file")
    if not force and not _is_ours(path, key):
        raise DesktopError(f"{path} was not created by OffGrid; pass --force to remove it")
    try:
        path.unlink()
    except OSError as exc:
        raise DesktopError(f"could not remove {path}: {exc}") from exc
    return path
