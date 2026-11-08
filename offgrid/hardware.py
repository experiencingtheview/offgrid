"""What this machine can actually do.

OffGrid is meant to be carried between machines - a phone on a train, a laptop
at a desk, a desktop with a GPU at home. The same team has to know which one it
is running on, so it can spend exactly the effort the hardware supports and no
more. This module measures the machine with the standard library only, offline,
and turns that into a capability tier plus a recommended model size.

Everything reported here is measured or absent. If a probe fails the field stays
empty and says so; nothing invents a plausible number.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CACHE_FILENAME = "hardware.json"
CACHE_MAX_AGE = 60 * 60 * 24  # a day; hardware rarely changes faster than that


# --------------------------------------------------------------------------
# raw observation
# --------------------------------------------------------------------------
def _run(argv: List[str], timeout: int = 3) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (completed.stdout or "").strip()


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _linux_cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name") and ":" in line:
            return line.split(":", 1)[1].strip()
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("hardware") and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _windows_ram_gb() -> float:
    try:

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    return 0.0


def ram_gb() -> float:
    system = platform.system()
    if system == "Linux":
        for line in _read("/proc/meminfo").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return round(float(parts[1]) / (1024 ** 2), 1)
                    except ValueError:
                        return 0.0
        return 0.0
    if system == "Darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        try:
            return round(float(raw) / (1024 ** 3), 1)
        except ValueError:
            return 0.0
    if system == "Windows":
        return _windows_ram_gb()
    return 0.0


def cpu_model() -> str:
    system = platform.system()
    if system == "Linux":
        return _linux_cpu_model() or platform.processor()
    if system == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if system == "Windows":
        return os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor()
    return platform.processor()


def accelerators() -> Tuple[List[str], float]:
    """Return (labels, total VRAM in GB). Apple unified memory is not VRAM."""
    found: List[str] = []
    vram = 0.0

    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        for line in out.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                megabytes = float(parts[1])
            except ValueError:
                continue
            found.append(f"nvidia:{parts[0]} {round(megabytes / 1024)}GB")
            vram += megabytes / 1024

    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showproductname"])
        for line in out.splitlines():
            if "Card series" in line or "Card model" in line:
                name = line.split(":", 1)[-1].strip()
                if name:
                    found.append(f"amd:{name}")

    if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
        found.append("apple:unified-memory")

    return found, round(vram, 1)


def disk_free_gb(path: str) -> float:
    try:
        return round(shutil.disk_usage(path).free / (1024 ** 3), 1)
    except OSError:
        return 0.0


def observe(workspace: Optional[str] = None) -> Dict[str, Any]:
    """Measure this machine. Cheap, offline, no side effects."""
    labels, vram = accelerators()
    prefix = os.environ.get("PREFIX", "")
    android = "com.termux" in prefix or os.environ.get("ANDROID_ROOT") is not None
    return {
        "os": platform.system(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "cpu_model": cpu_model(),
        "ram_gb": ram_gb(),
        "disk_free_gb": disk_free_gb(workspace or os.getcwd()),
        "accelerators": labels,
        "vram_gb": vram,
        "android": android,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def fingerprint(raw: Dict[str, Any]) -> str:
    """Stable identity of the hardware, ignoring volatile numbers like disk."""
    stable = {
        key: raw.get(key)
        for key in ("os", "machine", "cpu_count", "cpu_model", "ram_gb", "accelerators", "vram_gb")
    }
    blob = json.dumps(stable, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Capability:
    os_name: str
    machine: str
    cpu_count: int
    cpu_model: str = ""
    ram_gb: float = 0.0
    disk_free_gb: float = 0.0
    accelerators: Tuple[str, ...] = ()
    vram_gb: float = 0.0
    android: bool = False
    tier: str = "modest"
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def gpu(self) -> bool:
        return bool(self.accelerators)

    @property
    def score(self) -> float:
        """Rough capability weight, used to rank machines against each other."""
        return round(self.ram_gb + 2.0 * self.vram_gb + self.cpu_count / 4.0, 2)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "os": self.os_name,
            "machine": self.machine,
            "cpu_count": self.cpu_count,
            "cpu_model": self.cpu_model,
            "ram_gb": self.ram_gb,
            "disk_free_gb": self.disk_free_gb,
            "accelerators": list(self.accelerators),
            "vram_gb": self.vram_gb,
            "android": self.android,
            "tier": self.tier,
            "score": self.score,
        }

    def line(self) -> str:
        """One line for a system prompt: what this machine really is."""
        bits = [f"{self.os_name} {self.machine}".strip(), f"{self.cpu_count} cores"]
        if self.ram_gb:
            bits.append(f"{self.ram_gb:g} GB RAM")
        bits.append(", ".join(self.accelerators) if self.accelerators else "no GPU")
        bits.append(f"tier={self.tier}")
        return "Machine: " + " | ".join(bits)

    def describe(self) -> List[str]:
        lines = [
            f"os          {self.os_name} {self.machine}" + (" (android/termux)" if self.android else ""),
            f"cpu         {self.cpu_count} cores  {self.cpu_model or '(unknown model)'}",
            f"memory      {self.ram_gb:g} GB RAM, {self.disk_free_gb:g} GB free disk"
            if self.ram_gb
            else f"memory      unknown, {self.disk_free_gb:g} GB free disk",
            "graphics    "
            + (", ".join(self.accelerators) + (f" ({self.vram_gb:g} GB VRAM)" if self.vram_gb else ""))
            if self.accelerators
            else "graphics    none detected (CPU only)",
            f"tier        {self.tier}",
        ]
        return lines + [f"note        {note}" for note in self.notes]


def classify(raw: Dict[str, Any]) -> Capability:
    cores = int(raw.get("cpu_count") or 1)
    ram = float(raw.get("ram_gb") or 0.0)
    vram = float(raw.get("vram_gb") or 0.0)
    gpus = tuple(str(item) for item in raw.get("accelerators") or ())
    android = bool(raw.get("android"))
    notes: List[str] = []

    if vram >= 16 or ram >= 64:
        tier = "workstation"
        notes.append("enough memory to run a large model and several attempts at once")
    elif vram >= 8 or ram >= 32 or (cores >= 12 and ram >= 24):
        tier = "capable"
        notes.append("comfortable for a mid-size coder model with room for context")
    elif ram >= 12 or cores >= 6 or vram >= 4:
        tier = "modest"
        notes.append("workable for a 7B model; long contexts will be slow")
    else:
        tier = "minimal"
        notes.append("small machine: fewer parallel attempts, more careful ones")

    if android:
        notes.append("phone/tablet class device: expect heat and battery limits")
    if not gpus:
        notes.append("no accelerator detected: model speed is CPU-bound")
    if ram and ram < 8:
        notes.append("under 8 GB RAM: a 3B model is the realistic ceiling")

    return Capability(
        os_name=str(raw.get("os") or platform.system()),
        machine=str(raw.get("machine") or platform.machine()),
        cpu_count=cores,
        cpu_model=str(raw.get("cpu_model") or ""),
        ram_gb=ram,
        disk_free_gb=float(raw.get("disk_free_gb") or 0.0),
        accelerators=gpus,
        vram_gb=vram,
        android=android,
        tier=tier,
        notes=tuple(notes),
    )


def recommend(capability: Capability) -> str:
    """The largest model this machine can run comfortably. Measured, not guessed."""
    vram = capability.vram_gb
    ram = capability.ram_gb
    if vram >= 24:
        return "32B q4 on the GPU, or 14B q6 for faster, higher-quality turns"
    if vram >= 16:
        return "14B q5 on the GPU, or 32B q4 if you can wait"
    if vram >= 8:
        return "14B q4 on the GPU, or 7B q6 for more context"
    if capability.gpu and vram >= 4:
        return "7B q4 on the GPU"
    if capability.gpu and "apple" in " ".join(capability.accelerators):
        return "14B q4 via Metal" if ram >= 32 else "7B q4 via Metal"
    if ram >= 32:
        return "14B q4 on the CPU (slow but capable)"
    if ram >= 16:
        return "7B q4 on the CPU"
    if ram >= 8:
        return "3B q4 on the CPU"
    return "1.5B q4, or run the model on a stronger device and join its fleet"


# --------------------------------------------------------------------------
# cached probe
# --------------------------------------------------------------------------
def _cache_path(workspace: str) -> Path:
    return Path(workspace) / ".offgrid" / CACHE_FILENAME


def probe(workspace: str = ".", refresh: bool = False) -> Capability:
    """Measure the machine, reusing a cached observation when it still fits."""
    path = _cache_path(workspace)
    if not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict):
            age = time.time() - float(cached.get("checked") or 0)
            raw = cached.get("raw")
            if isinstance(raw, dict) and age < CACHE_MAX_AGE:
                try:
                    if fingerprint(raw) == cached.get("fingerprint"):
                        return classify(raw)
                except (TypeError, ValueError):
                    pass

    raw = observe(workspace)
    capability = classify(raw)
    try:
        from .private import ensure_private_dir, harden_file

        ensure_private_dir(path.parent)
        path.write_text(
            json.dumps(
                {"checked": time.time(), "fingerprint": fingerprint(raw), "raw": raw},
                indent=2,
            ),
            encoding="utf-8",
        )
        harden_file(path)
    except OSError:
        pass  # a read-only USB stick still works, it just re-probes
    return capability
