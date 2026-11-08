"""Systemd user units for workspace security reviews.

Two modes are supported: a periodic timer that runs one review at a time, and
a long-running user service that keeps the review loop alive in the background.
Both are deliberately user units, not system services: they need no root, write
only the user's own journal and workspace history, and can be removed without
touching the rest of the machine.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_INTERVAL = 300
MIN_INTERVAL = 1
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class TimerError(Exception):
    """A systemd user timer could not be installed or removed."""


@dataclass(frozen=True)
class TimerInfo:
    workspace: str
    interval: int
    service_name: str
    timer_name: str
    service_path: str
    timer_path: str
    command: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "workspace": self.workspace,
            "interval": self.interval,
            "service_name": self.service_name,
            "timer_name": self.timer_name,
            "service_path": self.service_path,
            "timer_path": self.timer_path,
            "command": list(self.command),
        }


@dataclass(frozen=True)
class ServiceInfo:
    workspace: str
    interval: int
    service_name: str
    service_path: str
    command: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "workspace": self.workspace,
            "interval": self.interval,
            "mode": "continuous",
            "service_name": self.service_name,
            "service_path": self.service_path,
            "command": list(self.command),
        }


def user_unit_dir(env: Optional[Dict[str, str]] = None) -> Path:
    env = env if env is not None else os.environ
    configured = env.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "systemd" / "user"


def watcher_command() -> List[str]:
    """Return a command that can run from a systemd user service.

    A console script is best.  A portable ``.pyz`` needs to be invoked through
    its interpreter, while an uninstalled source checkout falls back to
    ``python -m offgrid``.
    """
    argv0 = (sys.argv[0] or "").strip()
    if argv0:
        candidate = Path(argv0).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if (
            resolved is not None
            and resolved.suffix.lower() in {".pyz", ".zip"}
            and resolved.is_file()
        ):
            return [sys.executable, str(resolved)]
    installed = shutil.which("offgrid")
    if installed:
        return [installed]
    return [sys.executable, "-m", "offgrid"]


def unit_stem(workspace: Path) -> str:
    root = Path(workspace).expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip(".-")[:36] or "workspace"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"offgrid-security-watch-{slug}-{digest}"


def continuous_unit_stem(workspace: Path) -> str:
    """Use a distinct service name so a timer cannot overwrite it."""
    return f"{unit_stem(workspace)}-continuous"


def _duration(seconds: int) -> str:
    if seconds % 60 == 0:
        return f"{seconds // 60}min"
    return f"{seconds}s"


def _unit_arg(value: str) -> str:
    """Quote one ExecStart argument for systemd's unit parser."""
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _description(value: str) -> str:
    return str(value).replace("%", "%%").replace("\n", " ")


def _service_text(info: TimerInfo) -> str:
    exec_start = " ".join(_unit_arg(part) for part in info.command)
    return "\n".join(
        [
            "[Unit]",
            f"Description=OffGrid security review for {_description(Path(info.workspace).name)}",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={_unit_arg(info.workspace)}",
            f"ExecStart={exec_start}",
            "Environment=PYTHONUNBUFFERED=1",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "Nice=10",
            "",
        ]
    )


def _timer_text(info: TimerInfo) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=Periodic security review for {_description(Path(info.workspace).name)}",
            "",
            "[Timer]",
            "OnBootSec=2min",
            f"OnUnitActiveSec={_duration(info.interval)}",
            "Persistent=true",
            f"Unit={info.service_name}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def _continuous_service_text(info: ServiceInfo) -> str:
    exec_start = " ".join(_unit_arg(part) for part in info.command)
    return "\n".join(
        [
            "[Unit]",
            f"Description=Continuous security review for {_description(Path(info.workspace).name)}",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={_unit_arg(info.workspace)}",
            f"ExecStart={exec_start}",
            "Environment=PYTHONUNBUFFERED=1",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "Nice=10",
            "Restart=on-failure",
            "RestartSec=10s",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _seconds(interval: Any) -> int:
    try:
        seconds = int(float(interval))
    except (TypeError, ValueError) as exc:
        raise TimerError("interval must be a number of seconds") from exc
    if seconds < MIN_INTERVAL:
        raise TimerError(f"interval must be at least {MIN_INTERVAL} second(s)")
    return seconds


def _service_info(
    workspace: Path,
    interval: int,
    unit_dir: Optional[Path] = None,
    command: Optional[Sequence[str]] = None,
    service_file: Optional[Path] = None,
) -> ServiceInfo:
    root = Path(workspace).expanduser().resolve()
    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir()
    path = (
        Path(service_file).expanduser().resolve()
        if service_file is not None
        else directory / f"{continuous_unit_stem(root)}.service"
    )
    launcher = list(command or watcher_command())
    argv = tuple(
        launcher
        + [
            "security",
            "watch",
            "--review",
            "--interval",
            str(int(interval)),
            "--workspace",
            str(root),
            "--no-color",
        ]
    )
    return ServiceInfo(
        workspace=str(root),
        interval=int(interval),
        service_name=path.name,
        service_path=str(path),
        command=argv,
    )


def _timer_info(
    workspace: Path,
    interval: int,
    unit_dir: Optional[Path] = None,
    command: Optional[Sequence[str]] = None,
) -> TimerInfo:
    root = Path(workspace).expanduser().resolve()
    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir()
    stem = unit_stem(root)
    launcher = list(command or watcher_command())
    argv = tuple(
        launcher
        + [
            "security",
            "watch",
            "--review",
            "--once",
            "--workspace",
            str(root),
            "--no-color",
        ]
    )
    return TimerInfo(
        workspace=str(root),
        interval=int(interval),
        service_name=f"{stem}.service",
        timer_name=f"{stem}.timer",
        service_path=str(directory / f"{stem}.service"),
        timer_path=str(directory / f"{stem}.timer"),
        command=argv,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _run(command: Sequence[str], runner: Optional[Runner] = None) -> subprocess.CompletedProcess:
    try:
        result = (runner or _default_runner)(command)
    except OSError as exc:
        raise TimerError(f"could not run {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail[:500]}" if detail else ""
        raise TimerError(f"{' '.join(command)} failed (exit {result.returncode}){suffix}")
    return result


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )


def _systemctl_command() -> Optional[str]:
    return shutil.which("systemctl")


def install_timer(
    workspace: Path,
    interval: int = DEFAULT_INTERVAL,
    *,
    unit_dir: Optional[Path] = None,
    command: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
    env: Optional[Dict[str, str]] = None,
) -> TimerInfo:
    """Write and enable a systemd user timer for this workspace."""
    if not sys.platform.startswith("linux"):
        raise TimerError("systemd user timers are available on Linux only")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise TimerError(f"workspace is not a directory: {root}")
    seconds = _seconds(interval)

    systemctl = _systemctl_command()
    if not systemctl:
        raise TimerError(
            "systemctl was not found; a systemd user timer needs systemd and "
            "the systemctl command"
        )

    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir(env)
    info = _timer_info(root, seconds, directory, command)
    _write(Path(info.service_path), _service_text(info))
    _write(Path(info.timer_path), _timer_text(info))
    _run([systemctl, "--user", "daemon-reload"], runner)
    _run([systemctl, "--user", "enable", "--now", info.timer_name], runner)
    return info


def remove_timer(
    workspace: Path,
    *,
    unit_dir: Optional[Path] = None,
    runner: Optional[Runner] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Disable and remove the timer units for this workspace."""
    root = Path(workspace).expanduser().resolve()
    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir(env)
    info = _timer_info(root, DEFAULT_INTERVAL, directory)
    paths = [Path(info.service_path), Path(info.timer_path)]
    if not any(path.exists() for path in paths):
        raise TimerError(f"no OffGrid security timer is installed for {root}")

    systemctl = _systemctl_command()
    if systemctl:
        # A timer that was never enabled should not block removal.
        for args in (
            ["disable", "--now", info.timer_name],
            ["daemon-reload"],
        ):
            try:
                _run([systemctl, "--user", *args], runner)
            except TimerError:
                pass
    for path in paths:
        path.unlink(missing_ok=True)
    return {
        "workspace": str(root),
        "timer_name": info.timer_name,
        "removed": [str(path) for path in paths if not path.exists()],
        "systemctl": bool(systemctl),
    }


def write_service(
    workspace: Path,
    interval: int = DEFAULT_INTERVAL,
    *,
    unit_dir: Optional[Path] = None,
    command: Optional[Sequence[str]] = None,
    service_file: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> ServiceInfo:
    """Generate a continuous systemd user service file.

    Generating a file does not require systemd or systemctl, which makes it
    useful for review, packaging, or copying to another machine. Use
    :func:`install_service` to enable it on this machine.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise TimerError(f"workspace is not a directory: {root}")
    seconds = _seconds(interval)
    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir(env)
    info = _service_info(root, seconds, directory, command, service_file)
    try:
        _write(Path(info.service_path), _continuous_service_text(info))
    except OSError as exc:
        raise TimerError(f"could not write {info.service_path}: {exc}") from exc
    return info


def install_service(
    workspace: Path,
    interval: int = DEFAULT_INTERVAL,
    *,
    unit_dir: Optional[Path] = None,
    command: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
    env: Optional[Dict[str, str]] = None,
) -> ServiceInfo:
    """Write, enable and start a continuous systemd user service."""
    if not sys.platform.startswith("linux"):
        raise TimerError("systemd user services are available on Linux only")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise TimerError(f"workspace is not a directory: {root}")
    systemctl = _systemctl_command()
    if not systemctl:
        raise TimerError(
            "systemctl was not found; a systemd user service needs systemd and "
            "the systemctl command"
        )
    info = write_service(
        root,
        interval,
        unit_dir=unit_dir,
        command=command,
        env=env,
    )
    _run([systemctl, "--user", "daemon-reload"], runner)
    _run([systemctl, "--user", "enable", "--now", info.service_name], runner)
    return info


def remove_service(
    workspace: Path,
    *,
    unit_dir: Optional[Path] = None,
    runner: Optional[Runner] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Disable and remove the continuous service for this workspace."""
    root = Path(workspace).expanduser().resolve()
    directory = Path(unit_dir) if unit_dir is not None else user_unit_dir(env)
    info = _service_info(root, DEFAULT_INTERVAL, directory)
    path = Path(info.service_path)
    if not path.exists():
        raise TimerError(f"no OffGrid continuous security service is installed for {root}")

    systemctl = _systemctl_command()
    if systemctl:
        # A service that was never enabled should not block removal.
        for args in (
            ["disable", "--now", info.service_name],
            ["daemon-reload"],
        ):
            try:
                _run([systemctl, "--user", *args], runner)
            except TimerError:
                pass
    path.unlink(missing_ok=True)
    return {
        "workspace": str(root),
        "service_name": info.service_name,
        "service_path": str(path),
        "removed": not path.exists(),
        "systemctl": bool(systemctl),
    }
