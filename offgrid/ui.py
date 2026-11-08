"""Terminal output. Coloured, quiet when piped, no dependencies."""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from .config import Config


class _Colors:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"


AGENT_COLORS = {
    "lead": _Colors.MAGENTA,
    "planner": _Colors.CYAN,
    "coder": _Colors.GREEN,
    "reviewer": _Colors.YELLOW,
    "tester": _Colors.BLUE,
    "analyst": _Colors.CYAN,
    "offgrid": _Colors.MAGENTA,
    "tool": _Colors.GREY,
}


class UI:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.verbose = config.verbose
        self.color = bool(config.color) and sys.stdout.isatty()
        self._spinner_stop = threading.Event()
        self._spinner_thread: Optional[threading.Thread] = None
        self._spinner_quiet = False

    # -- primitives ------------------------------------------------------
    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + _Colors.RESET

    def dim(self, text: str) -> str:
        return self.paint(text, _Colors.DIM)

    def raw(self, text: str = "") -> None:
        print(text, flush=True)

    def rule(self) -> None:
        if self.verbose >= 1:
            print(self.dim("-" * min(72, max(40, _width()))), flush=True)

    # -- messages --------------------------------------------------------
    def banner(self, task: str, model: str, endpoint: str) -> None:
        print()
        print(self.paint("  OffGrid", _Colors.BOLD, _Colors.MAGENTA) + self.dim("  offline coding team"))
        print(self.dim(f"  model {model or '(auto)'} @ {endpoint}"))
        print(self.dim(f"  task: {task[:160]}{'...' if len(task) > 160 else ''}"))
        self.rule()

    def agent(self, name: str, note: str = "") -> None:
        if self.verbose < 1:
            return
        color = AGENT_COLORS.get(name, _Colors.CYAN)
        label = self.paint(f"{name:>8}", color, _Colors.BOLD)
        print(f"{label} {self.dim(note)}" if note else label, flush=True)

    def say(self, text: str, indent: int = 9) -> None:
        if self.verbose < 1 or not text.strip():
            return
        pad = " " * indent
        for line in text.rstrip().splitlines():
            print(pad + line, flush=True)

    def info(self, text: str) -> None:
        if self.verbose < 1:
            return
        print(self.dim("  " + text), flush=True)

    def ok(self, text: str) -> None:
        if self.verbose < 1:
            return
        print("  " + self.paint("ok ", _Colors.GREEN, _Colors.BOLD) + text, flush=True)

    def warn(self, text: str) -> None:
        if self.verbose < 1:
            return
        print("  " + self.paint("!! ", _Colors.YELLOW, _Colors.BOLD) + text, flush=True)

    def error(self, text: str) -> None:
        print("  " + self.paint("xx ", _Colors.RED, _Colors.BOLD) + text, file=sys.stderr, flush=True)

    def tool(self, call) -> None:
        if self.verbose < 2:
            return
        print(f"{self.dim('        >')} {self.paint(call.name, _Colors.GREY)} {self.dim(call.pretty_args())}", flush=True)

    def tool_result(self, name: str, ok: bool, content: str) -> None:
        if self.verbose < 2:
            return
        first = content.strip().splitlines()[0] if content.strip() else "(empty)"
        mark = self.paint("ok", _Colors.GREEN) if ok else self.paint("no", _Colors.RED)
        if len(first) > 110:
            first = first[:110] + "..."
        print(f"{self.dim('        <')} {mark} {self.dim(first)}", flush=True)

    # -- spinner ---------------------------------------------------------
    class _Spinner:
        def __init__(self, ui: "UI", text: str) -> None:
            self.ui = ui
            self.text = text

        def __enter__(self) -> "UI._Spinner":
            if not (self.ui.color and self.ui.verbose >= 2) or self.ui._spinner_quiet:
                return self
            self.ui._spinner_stop.clear()
            thread = threading.Thread(target=self._spin, daemon=True)
            self.ui._spinner_thread = thread
            thread.start()
            return self

        def _spin(self) -> None:
            frames = "|/-\\"
            index = 0
            while not self.ui._spinner_stop.is_set():
                frame = frames[index % len(frames)]
                sys.stdout.write(f"\r{self.ui.dim('        ' + frame + ' ' + self.text)}")
                sys.stdout.flush()
                index += 1
                time.sleep(0.12)

        def __exit__(self, *exc) -> None:
            self.ui._spinner_stop.set()
            thread = self.ui._spinner_thread
            if thread and thread.is_alive():
                thread.join(timeout=0.5)
            if self.ui.color and self.ui.verbose >= 2 and not self.ui._spinner_quiet:
                sys.stdout.write("\r" + " " * (len(self.text) + 16) + "\r")
                sys.stdout.flush()

    def spinner(self, text: str) -> "UI._Spinner":
        return UI._Spinner(self, text)

    # -- interaction -----------------------------------------------------
    def approve(self, command: str) -> bool:
        """Ask a human before a risky command. Denies when non-interactive."""
        if not sys.stdin.isatty():
            self.warn(f"needs approval, no terminal: {command}")
            return False
        print()
        print("  " + self.paint("run this command?", _Colors.YELLOW, _Colors.BOLD))
        for line in command.splitlines():
            print("    " + line)
        try:
            answer = input("  [y]es / [n]o / [a]ll: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("a", "all", "always"):
            self.config.command_policy = "auto"
            return True
        return answer in ("", "y", "yes")

    def ask(self, text: str) -> str:
        try:
            return input(text)
        except (EOFError, KeyboardInterrupt):
            print()
            return "/quit"

    # -- summaries -------------------------------------------------------
    def stats(self, text: str) -> None:
        if self.verbose < 1 or not text:
            return
        print()
        print(self.dim("  " + text))


def _width() -> int:
    try:
        return int(__import__("shutil").get_terminal_size().columns)
    except Exception:
        return 80
