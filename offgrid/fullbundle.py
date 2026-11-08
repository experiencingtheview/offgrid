"""Build a self-contained OffGrid bundle you can carry on a USB stick.

It downloads, once, while you still have internet:

  1. a portable CPython (so the target machine needs nothing installed)
  2. a llama.cpp release for this platform (the local model server)
  3. a quantised open-weight GGUF model (the brain)

...then writes launchers and a config, so the target laptop needs no network,
no install, no account. ``scripts/setup_usb.py`` is the standalone entry point;
the distribution wizard calls :func:`build_full_bundle` directly.

Everything is stdlib. Run it again later and finished files are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ._bundle import assets_root as _assets_dir, package_root
from .hardware import ram_gb as _detected_ram_gb

UA = "offgrid-setup/0.1 (+https://example.invalid)"
DEFAULT_PORT = 8089
FREE_SPACE_MARGIN_GB = 1.0

MODELS: Dict[str, Dict[str, object]] = {
    "3b": {
        "repo": "bartowski/Qwen2.5-Coder-3B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf",
        "sha256": "3da3afe6cf5c674ac195803ea0dd6fee7e1c228c2105c1ce8c66890d1d4ab460",
        "gb": 2.0,
        "ram": "6 GB",
        "note": "smallest useful option; fine for scripts and single files",
    },
    "7b": {
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        "sha256": "1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6",
        "gb": 4.7,
        "ram": "10 GB",
        "note": "recommended default; good at multi-file edits and tool calls",
    },
    "14b": {
        "repo": "bartowski/Qwen2.5-Coder-14B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "sha256": "2946d28c9e1bb2bcae6d42e8678863a31775df6f740315c7d7e6d6b6411f5937",
        "gb": 9.0,
        "ram": "20 GB",
        "note": "noticeably better plans and reviews, needs a big machine",
    },
    "35b": {
        "repo": "ggml-org/Qwen3.6-35B-A3B-GGUF",
        "file": "Qwen3.6-35B-A3B-Q4_K_M.gguf",
        "sha256": "f4ffe03c9139e6a905a5d346f77d9fabdd179367c3280dfd005d1e75ca5ed8c8",
        "gb": 20.5,
        "ram": "32 GB",
        "note": (
            "high-end option: 35B total with ~3B active (MoE), "
            "Apache-2.0; needs a 32 GB machine and a fast disk"
        ),
    },
    "80b": {
        "repo": "Qwen/Qwen3-Next-80B-A3B-Instruct-GGUF",
        "file": "Qwen3-Next-80B-A3B-Instruct-Q4_K_M.gguf",
        "sha256": "d103b2733ec1012a52d01edda66b7e5c24ae50508c9f99f5297ea459ef3c061a",
        "gb": 48.4,
        "ram": "64 GB minimum (96 GB recommended)",
        "min_ram_gb": 64,
        "recommended_ram_gb": 96,
        "note": (
            "opt-in maximum: 80B total with ~3B active (MoE), Apache-2.0; "
            "needs 64 GB+ RAM and a very fast disk"
        ),
    },
}

# llama.cpp renames its assets every few months; these are anchored so that
# accelerator builds (cuda / rocm / sycl / vulkan / openvino) never match.
LLAMA_PATTERNS = {
    ("linux", "x64"): (r"^llama-\S*bin-(ubuntu|linux)-(x64|amd64)\.(tar\.gz|zip)$",),
    ("linux", "arm64"): (r"^llama-\S*bin-(ubuntu|linux)-arm64\.(tar\.gz|zip)$",),
    ("darwin", "arm64"): (r"^llama-\S*bin-(macos|osx)-arm64\.(tar\.gz|zip)$",),
    ("darwin", "x64"): (r"^llama-\S*bin-(macos|osx)-(x64|amd64)\.(tar\.gz|zip)$",),
    ("windows", "x64"): (
        r"^llama-\S*bin-win-cpu-x64\.zip$",
        r"^llama-\S*bin-win-x64\.zip$",
    ),
    ("windows", "arm64"): (r"^llama-\S*bin-win-(cpu-)?arm64\.zip$",),
}

PYTHON_TRIPLES = {
    ("linux", "x64"): "x86_64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x64"): "x86_64-apple-darwin",
    ("windows", "x64"): "x86_64-pc-windows-msvc",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def platform_key() -> Tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(system, system)
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return os_name, arch


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fetch_json(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_download(path: Path, expected_sha256: str) -> None:
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return
    actual = _sha256_file(path).lower()
    if actual != expected:
        try:
            Path(path).unlink()
        except OSError:
            pass
        raise OSError(
            f"SHA-256 mismatch for {Path(path).name}: expected {expected}, got {actual}; "
            "the download was deleted"
        )


def download(
    url: str,
    dest: Path,
    timeout: int = 120,
    expected_sha256: str = "",
) -> None:
    """Resumable download with a progress line and optional SHA-256 check."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = (expected_sha256 or "").strip().lower()
    if dest.is_file():
        if expected:
            try:
                if _sha256_file(dest).lower() == expected:
                    print(f"  already downloaded and verified: {dest.name}")
                    return
            except OSError:
                pass
        elif dest.stat().st_size > 0:
            print(f"  already downloaded: {dest.name}")
            return

    part = dest.with_name(dest.name + ".part")
    resume = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": UA}
    if resume:
        headers["Range"] = f"bytes={resume}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # already complete
            part.replace(dest)
            _verify_download(dest, expected)
            print(f"  already downloaded: {dest.name}")
            return
        raise
    with response:
        total_header = response.headers.get("Content-Length")
        total = (int(total_header) + resume) if total_header else None
        done = resume
        started = time.time()
        mode = "ab" if resume else "wb"
        with part.open(mode) as handle:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    percent = done * 100 // total
                    speed = done / max(time.time() - started, 0.5) / 1e6
                    sys.stderr.write(
                        f"\r  {dest.name}: {percent:3d}%  {human(done)} / {human(total)}  {speed:.1f} MB/s"
                    )
                    sys.stderr.flush()
                else:
                    sys.stderr.write(f"\r  {dest.name}: {human(done)}")
                    sys.stderr.flush()
    sys.stderr.write("\n")
    part.replace(dest)
    _verify_download(dest, expected)


def flatten_binary_dir(root: Path, binary: str) -> Optional[Path]:
    """Make `<root>/<binary>` exist whatever layout the archive used."""
    direct = root / binary
    if direct.is_file():
        return direct
    found = [path for path in root.rglob(binary) if path.is_file()]
    if not found:
        return None
    holder = found[0].parent
    if holder != root:
        for child in list(holder.iterdir()):
            target = root / child.name
            if target.exists():
                continue
            shutil.move(str(child), str(target))
    candidate = root / binary
    return candidate if candidate.is_file() else found[0]


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as tf:
            tf.extractall(destination, filter="data") if hasattr(tarfile, "data_filter") else tf.extractall(destination)
    else:
        raise SystemExit(f"unknown archive type: {archive.name}")


def find_asset(assets: List[dict], patterns: Tuple[str, ...]) -> Optional[dict]:
    for pattern in patterns:
        for asset in assets:
            if re.search(pattern, asset.get("name", "")):
                return asset
    return None


def fetch_release_retry(repo: str, attempts: int = 3) -> Optional[dict]:
    """`releases/latest` with a couple of retries; GitHub 5xx is common."""
    for attempt in range(attempts):
        try:
            return fetch_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout=60)
        except Exception as exc:
            if attempt == attempts - 1:
                print(f"  could not query {repo} ({exc})")
                return None
            time.sleep(2 * (attempt + 1))
    return None


def gh_releases(repo: str, pages: int = 1) -> List[dict]:
    """Recent releases, newest first.

    The newest release often has no binary assets (llama.cpp ships version tags
    with binaries in separate build releases), so callers scan for a real asset
    instead of trusting `releases/latest`.
    """
    releases: List[dict] = []
    for page in range(1, max(1, pages) + 1):
        try:
            batch = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page=50&page={page}")
        except Exception as exc:
            print(f"  could not query {repo} releases ({exc})")
            break
        if not isinstance(batch, list) or not batch:
            break
        releases.extend(batch)
    return releases


def make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def resolve_llama(os_name: str, arch: str, dry_run: bool) -> Optional[Tuple[str, str]]:
    patterns = LLAMA_PATTERNS.get((os_name, arch))
    if not patterns:
        print(f"  no llama.cpp build known for {os_name}/{arch}; install one manually")
        return None
    if dry_run:
        example = patterns[0].replace("\\", "").replace("$", "")
        return (f"https://github.com/ggml-org/llama.cpp/releases/latest/download/{example}", example)

    releases = gh_releases("ggml-org/llama.cpp", pages=2)
    if not releases:
        print("  manual: https://github.com/ggml-org/llama.cpp/releases")
        return None
    for release in releases:
        asset = find_asset(release.get("assets") or [], patterns)
        if asset and asset.get("browser_download_url"):
            return asset["browser_download_url"], asset.get("name", "llama.zip")
    print(f"  no release ships a binary matching {patterns}")
    print(f"  newest release was {releases[0].get('tag_name')}")
    print("  manual: https://github.com/ggml-org/llama.cpp/releases")
    print("  fallback: install Ollama instead; OffGrid autodetects it")
    return None


def resolve_python(os_name: str, arch: str, dry_run: bool) -> Optional[Tuple[str, str]]:
    triple = PYTHON_TRIPLES.get((os_name, arch))
    if not triple:
        print(f"  no portable python known for {os_name}/{arch}")
        return None
    if dry_run:
        return (
            f"https://github.com/astral-sh/python-build-standalone/releases/latest/download/"
            f"cpython-3.12-{triple}-install_only.tar.gz",
            "",
        )

    suffix = f"-{triple}-install_only.tar.gz"
    best: Optional[dict] = None
    best_key: Optional[tuple] = None

    def consider(assets: List[dict]) -> None:
        nonlocal best, best_key
        for asset in assets:
            name = asset.get("name", "")
            if not name.endswith(suffix) or "_stripped" in name:
                continue
            if re.search(r"cpython-\d+\.\d+\.\d+(a|b|rc)", name):
                continue  # alpha / beta / release candidate
            version = re.search(r"cpython-(\d+)\.(\d+)\.(\d+)", name)
            if not version:
                continue
            numbers = tuple(int(part) for part in version.groups())
            if numbers[0] != 3 or numbers[1] < 10:
                continue
            # prefer CPython 3.12 (widest compatibility), then the newest stable
            key = (numbers[1] == 12, numbers)
            if best_key is None or key > best_key:
                best_key, best = key, asset

    # The single-release endpoint is small and reliable; the release *list* for
    # this repo is enormous and regularly times out, so it is only a fallback.
    latest = fetch_release_retry("astral-sh/python-build-standalone")
    if latest:
        consider(latest.get("assets") or [])
    if best is None:
        for release in gh_releases("astral-sh/python-build-standalone", pages=1):
            consider(release.get("assets") or [])
    if not best:
        print(f"  no stable portable python asset ending in {suffix}")
        print("  manual: https://github.com/astral-sh/python-build-standalone/releases")
        return None
    return best["browser_download_url"], best["name"]


def resolve_model(model_key: str, dry_run: bool) -> Optional[Tuple[str, str, Dict[str, object]]]:
    if model_key not in MODELS:
        print(f"  unknown model '{model_key}'. choose from: {', '.join(MODELS)}")
        return None
    spec = MODELS[model_key]
    url = f"https://huggingface.co/{spec['repo']}/resolve/main/{spec['file']}?download=true"
    if dry_run:
        return url, str(spec["file"]), spec
    return url, str(spec["file"]), spec


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".offgrid", ".pytest_cache"),
    )


def _source_root() -> Path:
    """The directory containing the importable package (checkout or wheel)."""
    root = package_root()
    if root is None:
        raise FullBundleError(
            "run the full-bundle builder from an OffGrid source tree, a wheel install, "
            "or an unpacked bundle"
        )
    return root


def _assets_root() -> Path:
    root = _assets_dir()
    if root is None:
        raise FullBundleError(
            "bundle assets are missing from this installation; reinstall OffGrid "
            "from a wheel or use a source checkout"
        )
    return root


def write_launchers(target: Path, os_name: str, assets: Optional[Path] = None) -> None:
    launch_dir = target / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    assets = Path(assets) if assets is not None else _assets_root()
    for source in (assets / "launch").glob("*"):
        if source.is_file():
            shutil.copy2(source, launch_dir / source.name)
            make_executable(launch_dir / source.name)
    for name in ("offgrid.sh", "offgrid.command"):
        candidate = launch_dir / name
        if candidate.is_file():
            make_executable(candidate)


def write_config(target: Path, model_file: str, standalone: bool = True) -> None:
    config = {
        "endpoint": f"http://127.0.0.1:{DEFAULT_PORT}/v1" if standalone else "",
        "model": "",
        "temperature": 0.2,
        "max_tokens": 2048,
        "max_turns": 12,
        "parallel": 1,
        "command_policy": "ask",
        "network": "deny",
        "offline": True,
        "session_dir": ".offgrid",
    }
    (target / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if model_file:
        (target / "MODEL.txt").write_text(model_file + "\n", encoding="utf-8")


FIRST_RUN = """\
OffGrid - first run
===================

This folder is a complete, offline coding agent. Nothing to install, nothing to
sign up for, no internet needed from here on.

  Linux / macOS      ./launch/offgrid.sh chat
  macOS (double-click) launch/offgrid.command
  Windows            launch\\offgrid.bat chat

  One-shot task      ./launch/offgrid.sh run "add type hints to utils.py"
  Check the setup    ./launch/offgrid.sh doctor

The launcher starts the bundled model server by itself the first time, waits
for it to load (10-40 seconds on a laptop), then drops you into the agent.

Run it against a project:

  cd /path/to/your/project
  /path/to/offgrid/launch/offgrid.sh run "fix the failing login test"

Blue-screening your laptop is not on the menu: file writes stay inside the
project you point it at, shell commands ask permission first, and a list of
genuinely destructive commands is refused outright.

Full docs: README.md
"""


def summarise(target: Path, plan: List[str], model_file: str) -> None:
    print()
    print("Bundle contents")
    for line in plan:
        print(f"  - {line}")
    print()
    print(f"  target:  {target}")
    print(f"  model:   {model_file or '(none downloaded - pass --skip-model to silence this)'}")
    print()
    print("Next steps")
    print(f"  cd {target}")
    print("  ./launch/offgrid.sh run \"add a CLI entry point\"")
    print()
    print("Copy the whole folder to a USB stick and it works on another machine")
    print("with no internet, as long as the CPU architecture matches.")


class FullBundleError(Exception):
    """A complete full bundle could not be built."""


@dataclass(frozen=True)
class FullBundleResult:
    target: Path
    model: str
    model_file: str
    complete: bool


def estimate_download_gb(model: str) -> float:
    """Approximate the one-time download for a full bundle."""
    if model not in MODELS:
        raise FullBundleError(f"unknown model '{model}'; choose from: {', '.join(MODELS)}")
    return 0.35 + 0.06 + 0.15 + float(MODELS[model]["gb"])


def estimate_required_space_gb(
    model: str,
    *,
    skip_python: bool = False,
    skip_llama: bool = False,
    skip_model: bool = False,
) -> float:
    """Space to reserve for a build, including filesystem headroom."""
    if model not in MODELS:
        raise FullBundleError(f"unknown model '{model}'; choose from: {', '.join(MODELS)}")
    total = 0.35
    if not skip_python:
        total += 0.06
    if not skip_llama:
        total += 0.15
    if not skip_model:
        total += float(MODELS[model]["gb"])
    return round(total + FREE_SPACE_MARGIN_GB, 1)


def free_space_gb(path: Path) -> Optional[float]:
    """Free space at *path* or its nearest existing parent, or None if unknown."""
    probe = Path(path).expanduser()
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return round(shutil.disk_usage(probe).free / (1024 ** 3), 1)
    except OSError:
        return None


def ensure_free_space(target: Path, required_gb: float) -> None:
    """Refuse a build before downloading when the target is too small."""
    free = free_space_gb(target)
    if free is None:
        print(
            f"  warning: could not check free space for {target}; "
            f"need about {required_gb:.1f} GB"
        )
        return
    if free < required_gb:
        raise FullBundleError(
            f"not enough free space for {target}: need about {required_gb:.1f} GB, "
            f"have {free:.1f} GB"
        )


def ram_warning(model: str, available_gb: Optional[float] = None) -> str:
    """A warning when a model's minimum RAM exceeds this machine."""
    spec = MODELS.get(model)
    if not spec:
        return ""
    minimum = float(spec.get("min_ram_gb") or 0.0)
    if minimum <= 0:
        return ""
    recommended = spec.get("recommended_ram_gb")
    need = f"{minimum:g} GB minimum"
    if recommended:
        need += f" ({float(recommended):g} GB recommended)"
    if available_gb is None:
        try:
            available_gb = float(_detected_ram_gb())
        except Exception:
            available_gb = 0.0
    if available_gb is None or available_gb <= 0:
        return (
            f"{model} wants {need} RAM; RAM could not be detected. "
            "Use it only on a machine with enough memory."
        )
    if available_gb + 0.05 < minimum:
        return (
            f"{model} wants {need} RAM; this machine has {available_gb:.1f} GB. "
            "It may run only by paging weights from disk, which is very slow; "
            "choose 35b or a smaller model, or add RAM."
        )
    return ""


def missing_components(
    target: Path,
    *,
    require_python: bool = True,
    require_llama: bool = True,
    require_model: bool = True,
) -> List[str]:
    """Return the parts of a full bundle that are absent.

    Downloads are best-effort in the standalone builder so a network problem
    still leaves a useful source bundle. The distribution wizard uses this
    check to distinguish that fallback from a genuinely complete bundle.
    """
    target = Path(target).expanduser()
    os_name, _ = platform_key()
    missing: List[str] = []
    if not (target / "offgrid" / "cli.py").is_file():
        missing.append("the OffGrid source")
    if require_python:
        python = target / "runtime" / "python" / (
            "python.exe" if os_name == "windows" else "bin/python3"
        )
        if not python.is_file():
            missing.append("portable Python")
    if require_llama:
        binary = "llama-server.exe" if os_name == "windows" else "llama-server"
        llama = target / "runtime" / "llama" / binary
        if not llama.is_file() and not any((target / "runtime" / "llama").rglob(binary)):
            missing.append("llama.cpp")
    if require_model and not any((target / "models").glob("*.gguf")):
        missing.append("the GGUF model")
    launch = target / "launch"
    wanted = "offgrid.bat" if os_name == "windows" else "offgrid.sh"
    if not (launch / wanted).is_file():
        missing.append(f"{wanted} launcher")
    if not (target / "config.json").is_file():
        missing.append("config.json")
    return missing


def build_full_bundle(
    target: Path,
    model: str = "7b",
    *,
    skip_python: bool = False,
    skip_llama: bool = False,
    skip_model: bool = False,
    keep_downloads: bool = False,
    llama_url: str = "",
    dry_run: bool = False,
) -> FullBundleResult:
    """Build a full bundle and fail if any requested component is missing.

    This is the library entry point used by the distribution wizard. The
    standalone script keeps its historical best-effort behaviour for advanced
    flags; without skip flags this function requires Python, llama.cpp and the
    model to all be present.
    """
    if model not in MODELS:
        raise FullBundleError(f"unknown model '{model}'; choose from: {', '.join(MODELS)}")
    argv = ["--target", str(target), "--model", model]
    if skip_python:
        argv.append("--skip-python")
    if skip_llama:
        argv.append("--skip-llama")
    if skip_model:
        argv.append("--skip-model")
    if keep_downloads:
        argv.append("--keep-downloads")
    if llama_url:
        argv.extend(("--llama-url", llama_url))
    if dry_run:
        argv.append("--dry-run")

    code = main(argv)
    if code != 0:
        raise FullBundleError(f"full bundle builder exited with status {code}")
    destination = Path(target).expanduser().resolve()
    if dry_run:
        return FullBundleResult(destination, model, "", False)

    missing = missing_components(
        destination,
        require_python=not skip_python,
        require_llama=not skip_llama,
        require_model=not skip_model,
    )
    if missing:
        raise FullBundleError(
            "the bundle is not complete; missing " + ", ".join(missing)
        )
    model_files = sorted((destination / "models").glob("*.gguf"))
    return FullBundleResult(destination, model, model_files[0].name, True)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble a portable offline OffGrid bundle (USB drive / laptop)."
    )
    parser.add_argument("--target", default="offgrid-usb", help="where to build the bundle")
    parser.add_argument("--model", default="7b", choices=sorted(MODELS), help="model size to download")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and exit, no downloads")
    parser.add_argument("--skip-python", action="store_true", help="do not bundle a portable python")
    parser.add_argument("--skip-llama", action="store_true", help="do not bundle llama.cpp")
    parser.add_argument("--skip-model", action="store_true", help="do not download the GGUF model")
    parser.add_argument(
        "--llama-url",
        default="",
        help="use a specific llama.cpp archive (zip or tar.gz) instead of the GitHub release",
    )
    parser.add_argument("--keep-downloads", action="store_true", help="keep archives in <target>/.downloads")
    args = parser.parse_args(argv)

    os_name, arch = platform_key()
    target = Path(args.target).expanduser().resolve()
    downloads = target / ".downloads"

    print()
    print("  OffGrid bundle builder")
    print(f"  platform  {os_name}/{arch}")
    print(f"  target    {target}")
    print(f"  model     {args.model} (about {MODELS[args.model]['gb']} GB; RAM: {MODELS[args.model]['ram']})")
    if not args.skip_model:
        warning = ram_warning(args.model)
        if warning:
            print(f"  warning: {warning}")
    print()

    required_space = estimate_required_space_gb(
        args.model,
        skip_python=args.skip_python,
        skip_llama=args.skip_llama,
        skip_model=args.skip_model,
    )
    if not args.dry_run:
        try:
            ensure_free_space(target, required_space)
        except FullBundleError as exc:
            print(f"  {exc}")
            print("  choose another target or free space, then retry")
            return 1

    if args.llama_url:
        archive_name = os.path.basename(args.llama_url.split("?")[0]) or "llama-archive.zip"
        llama = (args.llama_url, archive_name)
    else:
        llama = None if args.skip_llama else resolve_llama(os_name, arch, args.dry_run)
    python = None if args.skip_python else resolve_python(os_name, arch, args.dry_run)
    model = None if args.skip_model else resolve_model(args.model, args.dry_run)

    total = 0.35
    if python:
        total += 0.06
    if llama:
        total += 0.15
    if model:
        total += float(model[2]["gb"])
    print(f"  total download: about {total:.1f} GB (once, while you have internet)")
    print()

    if args.dry_run:
        print("  plan")
        print(f"    llama.cpp  {llama[0] if llama else 'skipped'}")
        print(f"    python     {python[0] if python else 'skipped'}")
        print(f"    model      {model[0] if model else 'skipped'}")
        print()
        print("  dry run: nothing was downloaded or written")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    source_root = _source_root()
    bundle_assets = _assets_root()
    plan: List[str] = []

    # 1. the agent itself
    copy_tree(source_root / "offgrid", target / "offgrid")
    if (source_root / "tests").is_dir():
        copy_tree(source_root / "tests", target / "tests")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        source = bundle_assets / name
        if source.is_file():
            shutil.copy2(source, target / name)
    write_launchers(target, os_name, bundle_assets)
    plan.append("offgrid/            the agent package (pure stdlib)")
    plan.append("launch/             start scripts for sh, .bat and .command")

    # 2. model server
    if llama:
        url, name = llama
        archive = downloads / name
        print("  downloading llama.cpp...")
        try:
            download(url, archive)
            llama_root = target / "runtime" / "llama"
            extract(archive, llama_root)
            binary = "llama-server.exe" if os_name == "windows" else "llama-server"
            located = flatten_binary_dir(llama_root, binary)
            if located:
                make_executable(located)
                plan.append("runtime/llama/      llama.cpp model server")
            else:
                print(f"  extracted llama.cpp but found no {binary} inside it")
                llama = False
        except Exception as exc:
            print(f"  llama.cpp download failed: {exc}")
            llama = False

    # 3. portable python
    if python:
        url, name = python
        archive = downloads / name
        print("  downloading portable python...")
        try:
            download(url, archive)
            staging = target / "runtime" / "python-tmp"
            extract(archive, staging)
            # the archive contains a single top-level "python" directory
            inner = staging / "python"
            final = target / "runtime" / "python"
            if final.exists():
                shutil.rmtree(final)
            (inner if inner.is_dir() else staging).rename(final)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            plan.append("runtime/python/     portable CPython, no install needed")
            exe = final / ("python.exe" if os_name == "windows" else "bin/python3")
            if exe.is_file():
                make_executable(exe)
        except Exception as exc:
            print(f"  portable python download failed: {exc} (system python3 will be used)")

    # 4. the brain
    model_file = ""
    if model:
        url, name, spec = model
        archive = downloads / name
        print(f"  downloading {name} ({spec['gb']} GB, resumable, SHA-256 checked)...")
        try:
            download(
                url,
                archive,
                expected_sha256=str(spec.get("sha256") or ""),
            )
            models_dir = target / "models"
            models_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archive), str(models_dir / name))
            model_file = name
            plan.append(f"models/{name}   the model ({spec['note']})")
        except Exception as exc:
            print(f"  model download failed: {exc}")
            print("  you can drop any .gguf into models/ later")

    # 5. config + docs
    standalone = bool(llama)
    write_config(target, model_file, standalone=standalone)
    (target / "README-FIRST.txt").write_text(FIRST_RUN, encoding="utf-8")
    if not standalone:
        with (target / "README-FIRST.txt").open("a", encoding="utf-8") as handle:
            handle.write(
                "\nNOTE: no bundled llama.cpp binary was found for this platform, so the\n"
                "bundle will use an existing local server instead (Ollama, LM Studio).\n"
                "Install Ollama from https://ollama.com/download, run\n"
                "`ollama pull qwen2.5-coder:7b`, and the launcher will find it.\n"
            )
    plan.append("config.json        endpoint, offline mode, safe defaults")
    plan.append("README-FIRST.txt   how to start it")

    if not args.keep_downloads and downloads.is_dir():
        shutil.rmtree(downloads, ignore_errors=True)

    summarise(target, plan, model_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
