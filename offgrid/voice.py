"""Offline speech: a voice already on this machine, and optional listening.

No network, no API key and no Python dependency for speech output: macOS uses
``say``, Windows uses SAPI through PowerShell, and Linux uses espeak-ng, espeak
or Festival. Microphone input supports the Windows SAPI recognizer, a local
OpenAI Whisper or whisper.cpp installation, or a command you provide. The
built-in paths record locally with arecord, ffmpeg or SoX and transcribe
locally; a custom command is responsible for its own audio handling.
"""

from __future__ import annotations

import array
import math
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

MAX_SPEECH_CHARS = 4000

NO_ENGINE_MESSAGE = (
    "no offline speech engine found; install espeak-ng on Linux, "
    "or use macOS say / Windows PowerShell with System.Speech"
)

NO_INPUT_MESSAGE = (
    "no offline microphone recognizer found; install openai-whisper or "
    "whisper.cpp with a local model, or pass --stt-command"
)

NO_MODEL_MESSAGE = "Whisper needs a local model; pass --stt-model PATH"

# OpenAI Whisper uses .pt checkpoints; whisper.cpp uses GGML/GGUF files.  A
# model with one of these suffixes must never be handed to the other CLI.
WHISPER_MODEL_SUFFIXES = frozenset({".pt"})
WHISPER_CPP_MODEL_SUFFIXES = frozenset({".bin", ".gguf"})


def recognizer_for_model(model: str) -> str:
    """Return the recognizer implied by a model suffix, or ``""``.

    Model names such as ``tiny`` have no suffix and are accepted by OpenAI
    Whisper, so they remain unconstrained rather than being treated as errors.
    """
    suffix = Path(str(model or "").strip()).suffix.lower()
    if suffix in WHISPER_MODEL_SUFFIXES:
        return "whisper"
    if suffix in WHISPER_CPP_MODEL_SUFFIXES:
        return "whisper_cpp"
    return ""


def model_engine_compatible(model: str, kind: str) -> bool:
    """Whether *model* can be passed to a recognizer of this kind."""
    required = recognizer_for_model(model)
    return not required or required == kind


def stt_model_setup_message(model: str, selected_kind: str = "") -> str:
    """Explain a model/recognizer mismatch and the matching setup choice."""
    required = recognizer_for_model(model)
    name = str(model or "").strip()
    selected_name = {
        "whisper": "OpenAI Whisper",
        "whisper_cpp": "whisper.cpp",
    }.get(selected_kind)
    selected = (
        f"the selected recognizer is {selected_name}; "
        if selected_name
        else "no compatible recognizer is installed; "
    )
    if required == "whisper":
        return (
            f"--stt-model {name!r} is for OpenAI Whisper (.pt), but {selected}"
            "install openai-whisper or use a .bin/.gguf model with whisper.cpp"
        )
    if required == "whisper_cpp":
        return (
            f"--stt-model {name!r} is for whisper.cpp (.bin/.gguf), but {selected}"
            "install whisper.cpp or use a .pt model with openai-whisper"
        )
    return NO_MODEL_MESSAGE

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

# System.Speech includes an offline dictation recognizer on Windows. It waits
# for one utterance (or the timeout) and prints the recognized text on stdout.
_WINDOWS_INPUT_SCRIPT = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Speech; "
    "$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine; "
    "try { "
    "$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)); "
    "$r.SetInputToDefaultAudioDevice(); "
    "$seconds = 8; "
    "if ($env:OFFGRID_LISTEN_SECONDS) { $seconds = [int]$env:OFFGRID_LISTEN_SECONDS }; "
    "$result = $r.Recognize([TimeSpan]::FromSeconds($seconds)); "
    "if ($result) { [Console]::Out.Write($result.Text) } "
    "} finally { $r.Dispose() }"
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


@dataclass(frozen=True)
class VoiceRecorder:
    """One local microphone recorder that writes a mono 16 kHz WAV file."""

    name: str
    executable: str
    args: Tuple[str, ...]

    def command(self, path: Path, seconds: float) -> List[str]:
        """Build the argv list; the duration and destination are data, not code."""
        return [self.executable] + [
            item.replace("{path}", str(path)).replace("{seconds}", f"{seconds:g}")
            for item in self.args
        ]


@dataclass(frozen=True)
class VoiceInputEngine:
    """A local way to turn one spoken utterance into text."""

    name: str
    kind: str  # windows | whisper | whisper_cpp | command
    executable: str = ""
    command: str = ""


def find_recorder(
    system: Optional[str] = None, which: Optional[_Which] = None
) -> Optional[VoiceRecorder]:
    """Find a local recorder that can produce a WAV file without a network."""
    which = which or shutil.which
    system = system or platform.system()

    if system == "Windows":
        # Windows input uses SAPI directly; it does not need a separate recorder.
        return None

    if system == "Darwin":
        ffmpeg = which("ffmpeg")
        if ffmpeg:
            return VoiceRecorder(
                "ffmpeg",
                ffmpeg,
                (
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "avfoundation",
                    "-i",
                    ":0",
                    "-t",
                    "{seconds}",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-y",
                    "{path}",
                ),
            )
        rec = which("rec")
        if rec:
            return VoiceRecorder(
                "SoX rec",
                rec,
                (
                    "-q",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "{path}",
                    "trim",
                    "0",
                    "{seconds}",
                ),
            )
        return None

    arecord = which("arecord")
    if arecord:
        return VoiceRecorder(
            "arecord",
            arecord,
            (
                "-q",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-d",
                "{seconds}",
                "{path}",
            ),
        )
    ffmpeg = which("ffmpeg")
    if ffmpeg:
        return VoiceRecorder(
            "ffmpeg",
            ffmpeg,
            (
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "alsa",
                "-i",
                "default",
                "-t",
                "{seconds}",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-y",
                "{path}",
            ),
        )
    rec = which("rec")
    if rec:
        return VoiceRecorder(
            "SoX rec",
            rec,
            (
                "-q",
                "-r",
                "16000",
                "-c",
                "1",
                "{path}",
                "trim",
                "0",
                "{seconds}",
            ),
        )
    return None


def find_input_engine(
    system: Optional[str] = None,
    which: Optional[_Which] = None,
    command: str = "",
    model: str = "",
) -> Optional[VoiceInputEngine]:
    """Find a local speech-to-text path, preferring an explicit command."""
    if command.strip():
        return VoiceInputEngine(
            "custom speech command", "command", command=command.strip()
        )

    which = which or shutil.which
    system = system or platform.system()
    required = recognizer_for_model(model)

    if system == "Windows" and not required:
        for executable in ("powershell", "powershell.exe", "pwsh"):
            path = which(executable)
            if path:
                return VoiceInputEngine("Windows SAPI microphone", "windows", path)

    # A .pt model belongs to OpenAI Whisper and .bin/.gguf belongs to
    # whisper.cpp.  Never fall back to the other executable: the model would be
    # rejected by that engine, or worse, used with incompatible weights.
    if required:
        order = (required,)
    else:
        order = ("whisper", "whisper_cpp")
    for kind in order:
        if kind == "whisper":
            path = which("whisper")
            if path:
                return VoiceInputEngine("OpenAI Whisper (local)", "whisper", path)
        else:
            for executable in ("whisper-cli", "whisper-cpp"):
                path = which(executable)
                if path:
                    return VoiceInputEngine("whisper.cpp (local)", "whisper_cpp", path)
    return None


def _clean_transcript(value: str) -> str:
    """Whisper and custom commands may print several lines; read them as prose."""
    ignored = {"[blank_audio]", "[silence]", "[music]", "[applause]", "(silence)"}
    lines = [
        line.strip()
        for line in (value or "").splitlines()
        if line.strip() and line.strip().lower() not in ignored
    ]
    return " ".join(lines)


VOICE_SILENCE_SECONDS = 0.8
VOICE_SILENCE_WINDOW_SECONDS = 0.1
VOICE_SPEECH_RMS = 0.012
VOICE_SILENCE_POLL_SECONDS = 0.1


def _wav_pcm(path: Path) -> Tuple[int, List[int]]:
    """Read the available mono 16-bit PCM samples from a growing WAV file.

    Recorders write their WAV header before audio data, so this only needs the
    current bytes, not a finalized file.  Returning no samples lets the caller
    keep waiting for the recorder.  A non-PCM or multi-channel stream simply
    disables silence detection and falls back to the duration cap.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0, []
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0, []

    rate = 0
    channels = 0
    bits = 0
    format_tag = 0
    data_offset = None
    offset = 12
    try:
        while offset + 8 <= len(data):
            chunk = data[offset : offset + 4]
            size = struct.unpack_from("<I", data, offset + 4)[0]
            body = offset + 8
            if chunk == b"fmt " and size >= 16:
                format_tag, channels, rate, _byte_rate, _align, bits = (
                    struct.unpack_from("<HHIIHH", data, body)
                )
            elif chunk == b"data":
                data_offset = body
                break
            offset = body + size + (size & 1)
    except (struct.error, ValueError):
        return 0, []

    if (
        data_offset is None
        or format_tag != 1
        or channels != 1
        or bits != 16
        or rate <= 0
    ):
        return 0, []
    raw = data[data_offset:]
    raw = raw[: len(raw) - (len(raw) % 2)]
    if not raw:
        return rate, []
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return rate, list(samples)


def _silence_elapsed(samples: List[int], rate: int) -> float:
    """Seconds since the last speech-level window, or zero if none was heard."""
    if rate <= 0 or not samples:
        return 0.0
    window = max(1, int(rate * VOICE_SILENCE_WINDOW_SECONDS))
    heard_speech = False
    last_speech = 0
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        if not chunk:
            continue
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk))
        if rms / 32768.0 >= VOICE_SPEECH_RMS:
            heard_speech = True
            last_speech = start + len(chunk)
    if not heard_speech:
        return 0.0
    return max(0.0, (len(samples) - last_speech) / float(rate))


def _silence_finished(path: Path) -> bool:
    rate, samples = _wav_pcm(path)
    return _silence_elapsed(samples, rate) >= VOICE_SILENCE_SECONDS


def _finalize_wav(path: Path) -> None:
    """Make a WAV header match the bytes actually captured.

    A recorder stopped while it is still streaming might not get a chance to
    seek back and fill in the RIFF/data sizes.  The transcription tools still
    need a complete, readable WAV, so repair those two size fields best-effort.
    """
    try:
        data = bytearray(Path(path).read_bytes())
    except OSError:
        return
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return
    offset = 12
    data_offset = None
    data_size = 0
    try:
        while offset + 8 <= len(data):
            chunk = data[offset : offset + 4]
            size = struct.unpack_from("<I", data, offset + 4)[0]
            body = offset + 8
            if chunk == b"data":
                data_offset = body
                data_size = size
                break
            offset = body + size + (size & 1)
        if data_offset is None:
            return
        available = len(data) - data_offset
        if data_size in (0, 0xFFFFFFFF) or data_size > available:
            data_size = available
        if data_size % 2:
            data_size -= 1
        data = data[: data_offset + data_size]
        struct.pack_into("<I", data, data_offset - 4, data_size)
        struct.pack_into("<I", data, 4, len(data) - 8)
        Path(path).write_bytes(bytes(data))
    except (OSError, struct.error, ValueError):
        return


def _record_audio_live(
    recorder: VoiceRecorder,
    path: Path,
    seconds: float,
) -> Tuple[bool, str]:
    """Record up to *seconds*, stopping after a natural pause."""
    try:
        process = subprocess.Popen(
            recorder.command(path, seconds),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except FileNotFoundError:
        return False, f"{recorder.name} is not available: {recorder.executable}"
    except OSError as exc:
        return False, f"{recorder.name} could not record: {exc}"

    stopped_early = False
    stopped_at_limit = False
    # The recorder command also receives the same limit.  This small grace
    # covers process startup and final writes without extending the user's cap
    # by a whole extra second.
    deadline = time.monotonic() + max(1.0, float(seconds)) + 0.25
    try:
        while process.poll() is None:
            if _silence_finished(path):
                stopped_early = True
                process.terminate()
                break
            if time.monotonic() >= deadline:
                stopped_at_limit = True
                process.terminate()
                break
            time.sleep(VOICE_SILENCE_POLL_SECONDS)
        try:
            _stdout, stderr = process.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise
    except OSError as exc:
        if process.poll() is None:
            process.kill()
        return False, f"{recorder.name} could not record: {exc}"

    if not stopped_early and not stopped_at_limit and process.returncode != 0:
        detail = (stderr or "").strip().splitlines()
        reason = detail[-1][:300] if detail else f"exit code {process.returncode}"
        return False, f"{recorder.name} could not record: {reason}"
    _finalize_wav(path)
    try:
        if not path.is_file() or path.stat().st_size <= 44:
            return False, f"{recorder.name} produced no audio"
    except OSError as exc:
        return False, f"could not read recorded audio: {exc}"
    return True, ""


def _record_audio_once(
    recorder: VoiceRecorder,
    path: Path,
    seconds: float,
    runner: _Runner,
) -> Tuple[bool, str]:
    try:
        completed = runner(
            recorder.command(path, seconds),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5.0, seconds + 20.0),
            shell=False,
        )
    except FileNotFoundError:
        return False, f"{recorder.name} is not available: {recorder.executable}"
    except subprocess.TimeoutExpired:
        return False, f"{recorder.name} took too long to record"
    except OSError as exc:
        return False, f"{recorder.name} could not record: {exc}"

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1][:300] if detail else f"exit code {completed.returncode}"
        return False, f"{recorder.name} could not record: {reason}"
    try:
        if not path.is_file() or path.stat().st_size <= 44:
            return False, f"{recorder.name} produced no audio"
    except OSError as exc:
        return False, f"could not read recorded audio: {exc}"
    return True, ""


def _record_audio(
    recorder: VoiceRecorder,
    path: Path,
    seconds: float,
    runner: _Runner,
) -> Tuple[bool, str]:
    """Record a turn, using live silence detection for the real process path.

    Tests and callers can inject a runner to exercise the recorder command
    without starting a process.  The built-in path uses Popen so it can stop
    as soon as the audio contains speech followed by a pause; ``seconds`` is
    still the hard maximum passed to the recorder command.
    """
    if runner is subprocess.run:
        return _record_audio_live(recorder, path, seconds)
    return _record_audio_once(recorder, path, seconds, runner)


def _run_transcriber(
    command: List[str],
    runner: _Runner,
    *,
    timeout: float,
    env: Optional[dict] = None,
) -> Tuple[bool, str, str]:
    try:
        completed = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return False, "", f"speech command is not available: {command[0]}"
    except subprocess.TimeoutExpired:
        return False, "", "speech recognition took too long"
    except OSError as exc:
        return False, "", f"speech recognition failed: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1][:300] if detail else f"exit code {completed.returncode}"
        return False, "", f"speech recognition failed: {reason}"
    return True, _clean_transcript(completed.stdout or ""), ""


def _split_command(command: str) -> List[str]:
    """Split a user command without a shell, preserving Windows backslashes."""
    if os.name == "nt":
        parts = shlex.split(command, posix=False)
        return [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'"
            else part
            for part in parts
        ]
    return shlex.split(command)


def _custom_listen(
    engine: VoiceInputEngine,
    seconds: float,
    runner: _Runner,
    timeout: float,
) -> Tuple[bool, str, str]:
    try:
        parts = _split_command(engine.command)
    except ValueError as exc:
        return False, "", f"invalid --stt-command: {exc}"
    if not parts:
        return False, "", "empty --stt-command"
    needs_audio = any("{audio}" in part for part in parts)

    with tempfile.TemporaryDirectory(prefix="offgrid-listen-") as folder:
        audio = Path(folder) / "speech.wav"
        if needs_audio:
            recorder = find_recorder()
            if recorder is None:
                return (
                    False,
                    "",
                    "--stt-command needs {audio}, but no local recorder was found; "
                    "install arecord, ffmpeg or SoX",
                )
            ok, message = _record_audio(recorder, audio, seconds, runner)
            if not ok:
                return False, "", message
        command = [
            part.replace("{audio}", str(audio)).replace("{seconds}", f"{seconds:g}")
            for part in parts
        ]
        return _run_transcriber(command, runner, timeout=timeout)


def _whisper_listen(
    engine: VoiceInputEngine,
    seconds: float,
    model: str,
    language: str,
    runner: _Runner,
    timeout: float,
) -> Tuple[bool, str, str]:
    if engine.kind in ("whisper", "whisper_cpp") and not model.strip():
        return False, "", NO_MODEL_MESSAGE
    if engine.kind in ("whisper", "whisper_cpp") and not model_engine_compatible(
        model, engine.kind
    ):
        return False, "", stt_model_setup_message(model, engine.kind)
    recorder = find_recorder()
    if recorder is None:
        return (
            False,
            "",
            "no local microphone recorder found; install arecord, ffmpeg or SoX",
        )
    with tempfile.TemporaryDirectory(prefix="offgrid-listen-") as folder:
        audio = Path(folder) / "speech.wav"
        ok, message = _record_audio(recorder, audio, seconds, runner)
        if not ok:
            return False, "", message

        if engine.kind == "whisper_cpp":
            output_base = Path(folder) / "transcript"
            command = [
                engine.executable,
                "-m",
                model,
                "-f",
                str(audio),
                "-nt",
                "-otxt",
                "-of",
                str(output_base),
            ]
            ok, text, message = _run_transcriber(command, runner, timeout=timeout)
            if not ok:
                return ok, text, message
            output = Path(str(output_base) + ".txt")
        else:
            command = [
                engine.executable,
                str(audio),
                "--model",
                model,
                "--output_format",
                "txt",
                "--output_dir",
                folder,
                "--fp16",
                "False",
            ]
            if language.strip():
                command.extend(("--language", language.strip()))
            ok, text, message = _run_transcriber(command, runner, timeout=timeout)
            if not ok:
                return ok, text, message
            output = Path(folder) / (audio.stem + ".txt")

        try:
            spoken = output.read_text(encoding="utf-8")
        except OSError as exc:
            return False, "", f"speech recognizer wrote no transcript: {exc}"
        return True, _clean_transcript(spoken), ""


def _windows_listen(
    engine: VoiceInputEngine,
    seconds: float,
    runner: _Runner,
) -> Tuple[bool, str, str]:
    env = os.environ.copy()
    env["OFFGRID_LISTEN_SECONDS"] = str(max(1, int(seconds)))
    command = [
        engine.executable,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _WINDOWS_INPUT_SCRIPT,
    ]
    return _run_transcriber(command, runner, timeout=max(10.0, seconds + 15.0), env=env)


def listen(
    seconds: float = 8.0,
    *,
    command: str = "",
    model: str = "",
    language: str = "",
    engine: Optional[VoiceInputEngine] = None,
    system: Optional[str] = None,
    which: Optional[_Which] = None,
    runner: Optional[_Runner] = None,
    timeout: float = 600.0,
) -> Tuple[bool, str, str]:
    """Record one utterance locally and return ``(ok, text, message)``.

    An empty successful transcript means no speech was recognized. Errors never
    raise; the caller can show the message and fall back to the keyboard.
    """
    try:
        seconds = max(1.0, min(float(seconds), 60.0))
    except (TypeError, ValueError):
        seconds = 8.0
    engine = engine or find_input_engine(
        system=system, which=which, command=command, model=model
    )
    if engine is None:
        if recognizer_for_model(model):
            return False, "", stt_model_setup_message(model)
        return False, "", NO_INPUT_MESSAGE
    if engine.kind in ("whisper", "whisper_cpp") and not model_engine_compatible(
        model, engine.kind
    ):
        return False, "", stt_model_setup_message(model, engine.kind)

    runner = runner or subprocess.run
    try:
        if engine.kind == "command":
            return _custom_listen(engine, seconds, runner, timeout)
        if engine.kind == "windows":
            return _windows_listen(engine, seconds, runner)
        return _whisper_listen(engine, seconds, model, language, runner, timeout)
    except KeyboardInterrupt:
        return False, "", "listening interrupted"
    except OSError as exc:
        return False, "", f"speech recognition failed: {exc}"


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
