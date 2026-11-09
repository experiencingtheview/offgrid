"""The first-run setup wizard behind ``offgrid setup``.

There is nothing to install: OffGrid is pure Python, so a checkout, a wheel and
a USB bundle all run as they are. What a new machine usually lacks is a local
model server, and a newcomer often does not know which one to pick or how to
start the app afterwards.

The wizard therefore checks the interpreter, looks for a server on localhost,
explains the two free ways to get one when nothing answers, and offers the
desktop shortcut through the same code as ``offgrid desktop install``. It never
downloads anything and never connects to anything but localhost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import __version__
from .config import Config
from .providers import discover

REQUIRED_PYTHON = (3, 9)


def python_too_old(
    version_info: Optional[Sequence[int]] = None,
) -> Optional[str]:
    """Return a message when this interpreter is older than OffGrid supports."""
    info = sys.version_info if version_info is None else version_info
    major, minor = int(info[0]), int(info[1])
    if (major, minor) < REQUIRED_PYTHON:
        return (
            f"Python {major}.{minor} is too old; OffGrid needs "
            f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} or newer"
        )
    return None


def run_wizard(
    config: Config,
    *,
    workspace: Optional[Path] = None,
    out: Callable[[str], None] = print,
    ask: Optional[Callable[[str], str]] = None,
    interactive: Optional[bool] = None,
    desktop_dir: Optional[Path] = None,
    as_json: bool = False,
    probe: Optional[Callable[[Config], List[tuple]]] = None,
    version_info: Optional[Sequence[int]] = None,
) -> int:
    """Run the setup steps and return 0 only when a model server answered."""
    root = Path(workspace) if workspace is not None else Path(config.workspace or ".")
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    problem = python_too_old(version_info)
    servers: List[Tuple[str, List[str]]] = []
    if problem is None:
        scanner = discover if probe is None else probe
        servers = [(endpoint, list(names)) for endpoint, names in scanner(config)]
    ready = problem is None and bool(servers)

    # Never prompt for JSON (it is read by programs) or when there is no terminal
    # to answer: a wizard that blocks on a pipe is worse than one that just says
    # what it would do.
    prompt = input if ask is None else ask
    can_prompt = sys.stdin.isatty() if interactive is None else bool(interactive)
    can_prompt = can_prompt and not as_json

    shortcut = ""
    shortcut_error = ""
    if can_prompt and problem is None:
        try:
            answer = prompt("  Create a desktop shortcut? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in ("y", "yes"):
            from .desktop import DesktopError, install

            try:
                shortcut = str(install(desktop_dir=desktop_dir).path)
            except (DesktopError, OSError) as exc:
                shortcut_error = str(exc)

    payload = {
        "version": __version__,
        "python": python_version,
        "platform": sys.platform,
        "workspace": str(root),
        "ready": ready,
        "servers": [
            {"endpoint": endpoint, "model": names[0] if names else ""}
            for endpoint, names in servers
        ],
        "shortcut": shortcut,
        "problems": [text for text in (problem, shortcut_error) if text],
    }
    if as_json:
        payload["exit_code"] = 0 if ready else 1
        out(json.dumps(payload, indent=2))
        return payload["exit_code"]

    out("")
    out(f"  OffGrid {__version__} setup")
    out("")
    out(f"  python      {python_version}  (needs 3.9 or newer)")
    out(f"  platform    {sys.platform}")
    out(f"  workspace   {root}")
    out("")
    if problem:
        out(f"  {problem}")
        out("  install a newer Python, then run `offgrid setup` again")
        return 1

    out("  looking for a local model server...")
    if servers:
        for endpoint, names in servers:
            model = names[0] if names else "(no model listed)"
            out(f"  found       {endpoint}  {model}")
    else:
        out("  none answering")
        out("")
        out("  A model server is the one thing OffGrid needs, and both are free:")
        out("    Ollama     https://ollama.com/download")
        out("               ollama pull qwen2.5-coder:7b  &&  ollama serve")
        out("    llama.cpp  https://github.com/ggml-org/llama.cpp/releases")
        out("               llama-server -m models/model.gguf --port 8080 --jinja")
    out("")

    if shortcut:
        out(f"  shortcut    {shortcut}")
    elif shortcut_error:
        out(f"  shortcut    not created: {shortcut_error}")
        out("              run `offgrid desktop install` when you want one")
    elif can_prompt:
        out("  shortcut    skipped")
    else:
        out("  shortcut    not offered without a terminal")
        out("              `offgrid desktop install` adds one")
    out("")

    if ready:
        out("  ready. start it with:")
        out("    offgrid chat")
        out('    offgrid run "add a /health endpoint to app.py"')
    else:
        out("  not ready yet: no model server answered on localhost.")
        out("  start one, then run `offgrid setup` again, or `offgrid doctor` for details")
    return 0 if ready else 1
