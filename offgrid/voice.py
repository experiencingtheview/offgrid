"""Offline text-to-speech using voices already installed on this machine.

No network, no API key and no Python dependency: macOS uses ``say``, Windows
uses SAPI through PowerShell, and Linux uses espeak-ng, espeak or Festival.
Only the final response is spoken; microphone input is deliberately not part
of this module because it needs a separate local speech-to-text model.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

MAX_SPEECH_CHARS = 4000

NO_ENGINE_MESSAGE = (
    "no offline speech engine found; install espeak-ng on Linux, "
    "or use macOS say / Windows PowerShell with System.Speech"
)

# System.Speech ships with Windows PowerShell. The response arrives on stdin so
# it never appears in a command line, and the voice name travels in the
# environment for the same reason.
_WINDOWS_SCRIPT = (
    "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try { "
    "if ($env:OFFGRID_SPEAK_VOICE) { $s.SelectVoice($env:OFFGRID_SPEAK_VOICE) }; "
    "$text = [Console]::In.ReadToEnd(); "
    "if ($text) { $s.Speak($text) } "
    "} finally { $s.Dispose() }"
)


@dataclass(frozen=True)
class VoiceEngine:
    """One local speech command and how it accepts a voice name."""

    name: str
    executable: str
    args: Tuple[str, ...] = ()
    voice_args: Tuple[str, ...] = ()
    voice_env: str = ""

    def command(self, voice: str = "") -> List[str]:
        """Build the argv list without a shell or string interpolation."""
        parts = list(self.args)
        selected = (voice or "").strip()
        if selected and self.voice_args:
            parts[0:0] = list(self.voice_args) + [selected]
        return [self.executable] + parts


_Which = Callable[[str], Optional[str]]
_Runner = Callable[..., Any]


def find_engine(
    system: Optional[str] = None, which: Optional[_Which] = None
) -> Optional[VoiceEngine]:
    """Find the best available built-in speech command for this OS."""
    which = which or shutil.which
    system = system or platform.system()

    if system == "Darwin":
        path = which("say")
        if path:
            return VoiceEngine("macOS say", path, (), ("-v",))
        return None

    if system == "Windows":
        for executable in ("powershell", "powershell.exe", "pwsh"):
            path = which(executable)
            if path:
                return VoiceEngine(
                    "Windows SAPI",
                    path,
                    ("-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT),
                    voice_env="OFFGRID_SPEAK_VOICE",
                )
        return None

    # Linux and other POSIX systems: prefer the actively maintained eSpeak NG,
    # then the original eSpeak, then Festival.
    candidates = (
        ("espeak-ng", ("--stdin",), ("-v",)),
        ("espeak", ("--stdin",), ("-v",)),
        ("festival", ("--tts",), ()),
    )
    for executable, args, voice_args in candidates:
        path = which(executable)
        if path:
            return VoiceEngine(executable, path, args, voice_args)
    return None


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_NUMBER_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*|__|~~|\*")


def clean_for_speech(text: str, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Turn a Markdown report into short prose worth listening to.

    Code blocks are omitted rather than read symbol by symbol. Links keep their
    label, emphasis markers disappear, and very long reports stop at a natural
    boundary with a spoken note rather than a silent cut.
    """
    if not text:
        return ""
    clean = _ANSI_RE.sub("", text)
    clean = _FENCE_RE.sub(" Code block omitted. ", clean)
    clean = _INLINE_CODE_RE.sub(r"\1", clean)
    clean = _IMAGE_RE.sub(r"\1", clean)
    clean = _LINK_RE.sub(r"\1", clean)
    clean = _HTML_RE.sub("", clean)
    clean = _HEADING_RE.sub("", clean)
    clean = _BULLET_RE.sub("", clean)
    clean = _NUMBER_RE.sub("", clean)
    clean = _QUOTE_RE.sub("", clean)
    clean = _EMPHASIS_RE.sub("", clean)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r" *\n *", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    clean = clean.strip()
    if len(clean) <= max_chars:
        return clean

    head = clean[:max_chars]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= max_chars // 2:
        head = head[: cut + 1]
    else:
        cut = head.rfind(" ")
        head = head[:cut] if cut > 0 else head
    return head.rstrip(" ,;:") + ". The rest was not read aloud."


def speak(
    text: str,
    voice: str = "",
    *,
    engine: Optional[VoiceEngine] = None,
    system: Optional[str] = None,
    which: Optional[_Which] = None,
    runner: Optional[_Runner] = None,
    timeout: int = 600,
) -> Tuple[bool, str]:
    """Read ``text`` aloud. Return ``(ok, message)``; never raise on no engine."""
    spoken = clean_for_speech(text)
    if not spoken:
        return True, ""

    engine = engine or find_engine(system=system, which=which)
    if engine is None:
        return False, NO_ENGINE_MESSAGE

    runner = runner or subprocess.run
    command = engine.command(voice)
    env = os.environ.copy()
    if engine.voice_env:
        if voice.strip():
            env[engine.voice_env] = voice.strip()
        else:
            env.pop(engine.voice_env, None)

    try:
        completed = runner(
            command,
            input=spoken,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return False, f"{engine.name} is not available: {engine.executable}"
    except subprocess.TimeoutExpired:
        return False, f"{engine.name} took too long to speak"
    except OSError as exc:
        return False, f"{engine.name} could not speak: {exc}"

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1][:300] if detail else f"exit code {completed.returncode}"
        return False, f"{engine.name} could not speak: {reason}"
    return True, ""
