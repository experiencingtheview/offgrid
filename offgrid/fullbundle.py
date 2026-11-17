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
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import install_manifest, runtime_identity
from ._bundle import assets_root as _assets_dir, package_root
from .downloads import DownloadError, download as _verified_download, sha256_file
from .hardware import ram_gb as _detected_ram_gb

UA = "offgrid-setup/0.1 (+https://example.invalid)"
DEFAULT_PORT = 8089
FREE_SPACE_MARGIN_GB = 1.0
RUNTIME_FINGERPRINT_NAME = ".offgrid-runtime.json"
RUNTIME_FINGERPRINT_SCHEMA = 1
INPUT_MANIFEST_SCHEMA = "offgrid.fullbundle-inputs/1"
INPUT_ROLES = ("python", "llama")
_CURRENT_STAGE = None


def write_runtime_fingerprint(
    directory: Path,
    binary: Path,
    *,
    platform: str,
    version: str,
    archive_sha256: str = "",
) -> Path:
    """Record the installed runtime so setup knows the extraction completed."""
    target = Path(directory) / RUNTIME_FINGERPRINT_NAME
    payload = {
        "schema": RUNTIME_FINGERPRINT_SCHEMA,
        "platform": platform,
        "version": version,
        "archive_sha256": archive_sha256,
        "binary_sha256": sha256_file(Path(binary)),
    }
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return target


def write_python_tree_digest(target: Path, runtime: Path, *, platform: str) -> Path:
    """Write the one digest for the flattened Python tree in this bundle."""
    digest = runtime_identity.tree_digest(Path(runtime)).digest
    table = Path(target) / "launch" / "python-tree-digests.txt"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(
        "# Generated for this full bundle from its flattened Python runtime.\n"
        f"{platform} {digest}\n",
        encoding="utf-8",
    )
    return table


def _flatten_python_runtime(source: Path, destination: Path) -> None:
    """Dereference file links so the shipped digest matches exFAT-safe copies."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="offgrid-flat-", dir=str(destination.parent)
    ) as temporary:
        flattened = Path(temporary) / destination.name
        shutil.copytree(source, flattened, symlinks=False)
        _replace_tree(flattened, destination)


MODELS: Dict[str, Dict[str, object]] = {
    "0.5b": {
        "repo": "bartowski/Qwen2.5-Coder-0.5B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf",
        "sha256": "0128e77564e43d40682f82d7ebe8a9abdf0c24c8f55fa85629f8cc156b1b6560",
        "gb": 0.4,
        "ram": "2 GB",
        "note": "tiny starter; fastest download for trying OffGrid on any laptop",
    },
    "3b": {
        "repo": "bartowski/Qwen2.5-Coder-3B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf",
        "sha256": "3da3afe6cf5c674ac195803ea0dd6fee7e1c228c2105c1ce8c66890d1d4ab460",
        "gb": 2.0,
        "ram": "6 GB",
        "note": "entry-level model; fine for scripts and single files",
    },
    "7b": {
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        "sha256": "1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6",
        "gb": 4.7,
        "ram": "10 GB",
        "note": "recommended default; good at multi-file edits and tool calls",
    },
    "12b": {
        "repo": "JetBrains/Mellum2.1-12B-A2.5B-Thinking-GGUF",
        "file": "Mellum2.1-12B-A2.5B-Thinking-Q4_K_M.gguf",
        "sha256": "ecc4d5b8107fc219e23c494b2d107552137637b6c6002ee713e0ba8af7fa2755",
        "gb": 8.1,
        "ram": "15 GB minimum (16 GB recommended)",
        "min_ram_gb": 15,
        "recommended_ram_gb": 16,
        "note": (
            "agentic coder: 12B total with ~2.5B active (MoE), Apache-2.0; "
            "needs about 10 GB of memory, so a 16 GB laptop or a 12 GB GPU is enough"
        ),
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
    return sha256_file(Path(path))


def _asset_sha256(asset: dict) -> str:
    """Return a GitHub release asset's SHA-256 when the API publishes one."""
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        digest = digest.split(":", 1)[1]
    if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
        return digest
    return ""


def _resolution(value):
    """Normalize a resolver result to ``(url, name, sha256)``.

    Older callers and tests may still return a two-item tuple; keeping this
    adapter means the checksum can be added without changing their contract.
    """
    if not value:
        return None
    url = str(value[0])
    name = str(value[1] or "") if len(value) > 1 else ""
    sha256 = str(value[2] or "") if len(value) > 2 else ""
    return url, name, sha256


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


def _input_hash(value: object, label: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise FullBundleError(f"{label} must be a lowercase SHA-256")
    return digest


def _input_path(entry: dict, base: Path, label: str) -> Path:
    """Resolve a manifest-local file and make sure it exists."""
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise FullBundleError(f"{label}.path must be a non-empty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FullBundleError(f"{label}.path does not exist: {candidate}")
    return candidate


def _input_entry(entry: object, base: Path, label: str, *, role: str) -> dict:
    """Validate one pinned runtime or model entry."""
    if not isinstance(entry, dict):
        raise FullBundleError(f"{label} must be an object")
    normalized = dict(entry)
    filename = normalized.get("file")
    if not isinstance(filename, str) or not filename.strip():
        raise FullBundleError(f"{label}.file must be a non-empty string")
    if filename in (".", "..") or "/" in filename or "\\" in filename:
        raise FullBundleError(f"{label}.file must be a plain filename: {filename!r}")
    normalized["file"] = filename
    normalized["sha256"] = _input_hash(normalized.get("sha256"), f"{label}.sha256")

    size = normalized.get("size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size < 0
    ):
        raise FullBundleError(f"{label}.size must be a non-negative integer")

    path = normalized.get("path")
    url = normalized.get("url")
    if path is not None and url is not None:
        raise FullBundleError(f"{label} must pin either path or url, not both")
    if path is not None:
        normalized["path"] = _input_path(normalized, base, label)
        if size is not None and normalized["path"].stat().st_size != size:
            raise FullBundleError(
                f"{label}.size does not match {normalized['path']}"
            )
    elif url is not None:
        if not isinstance(url, str) or not url.strip():
            raise FullBundleError(f"{label}.url must be a non-empty string")
        if "/latest/" in url or url.rstrip("/").endswith("/latest"):
            raise FullBundleError(
                f"{label}.url must be pinned, not a latest-release URL: {url}"
            )
    else:
        raise FullBundleError(f"{label} must provide a pinned path or url")

    if role == "model":
        try:
            normalized["gb"] = float(normalized.get("gb", 0.0))
        except (TypeError, ValueError) as exc:
            raise FullBundleError(f"{label}.gb must be a number") from exc
        if normalized["gb"] < 0:
            raise FullBundleError(f"{label}.gb must not be negative")
        normalized["ram"] = str(normalized.get("ram") or "unknown")
        normalized["note"] = str(normalized.get("note") or "pinned model")
    else:
        normalized["version"] = str(normalized.get("version") or filename)
    return normalized


def load_input_manifest(path: Path) -> dict:
    """Load a pinned full-bundle input manifest.

    Every runtime and model entry names an exact local file or a non-latest URL
    and carries a SHA-256.  Local paths are resolved relative to the manifest,
    which lets CI build from tiny checked-in fixtures without network access.
    """
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FullBundleError(
            f"could not read full-bundle input manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise FullBundleError("full-bundle input manifest must be a JSON object")
    if data.get("schema") != INPUT_MANIFEST_SCHEMA:
        raise FullBundleError(
            "unsupported full-bundle input manifest schema: "
            f"{data.get('schema')!r}; expected {INPUT_MANIFEST_SCHEMA!r}"
        )

    base = manifest_path.parent
    result = {"schema": INPUT_MANIFEST_SCHEMA, "path": manifest_path}
    for role in INPUT_ROLES:
        table = data.get(role)
        if not isinstance(table, dict) or not table:
            raise FullBundleError(f"{role} must be a non-empty object")
        result[role] = {}
        for platform_name, entry in table.items():
            if not isinstance(platform_name, str) or not platform_name:
                raise FullBundleError(f"{role} platform keys must be non-empty strings")
            result[role][platform_name] = _input_entry(
                entry,
                base,
                f"{role}.{platform_name}",
                role=role,
            )

    models = data.get("models")
    if not isinstance(models, dict) or not models:
        raise FullBundleError("models must be a non-empty object")
    result["models"] = {}
    for model_key, entry in models.items():
        if not isinstance(model_key, str) or not model_key:
            raise FullBundleError("model keys must be non-empty strings")
        result["models"][model_key] = _input_entry(
            entry,
            base,
            f"models.{model_key}",
            role="model",
        )
    return result


def _pinned_runtime(inputs: dict, role: str, os_name: str, arch: str) -> dict:
    platform_name = f"{os_name}-{arch}"
    try:
        return inputs[role][platform_name]
    except KeyError as exc:
        raise FullBundleError(
            f"the input manifest has no pinned {role} for {platform_name}"
        ) from exc


def _stage_pinned_input(entry: dict, archive: Path) -> None:
    """Copy or download one pinned input, then verify its exact bytes."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    source = entry.get("path")
    if source is not None:
        try:
            shutil.copyfile(Path(source), archive)
        except OSError as exc:
            raise FullBundleError(
                f"could not stage pinned input {entry.get('file')}: {exc}"
            ) from exc
        try:
            _verify_download(archive, str(entry["sha256"]))
        except OSError as exc:
            raise FullBundleError(str(exc)) from exc
        return
    download(
        str(entry["url"]),
        archive,
        expected_sha256=str(entry["sha256"]),
    )


def _manifest_created(created: Optional[str] = None) -> str:
    """Return the install-manifest timestamp, honoring SOURCE_DATE_EPOCH."""
    if created is not None:
        return created
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise FullBundleError(
            "SOURCE_DATE_EPOCH must be an integer number of seconds"
        ) from exc
    if seconds < 0:
        raise FullBundleError("SOURCE_DATE_EPOCH must not be negative")
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise FullBundleError(
            f"SOURCE_DATE_EPOCH is out of range: {raw!r}"
        ) from exc


def download(
    url: str,
    dest: Path,
    timeout: int = 120,
    expected_sha256: str = "",
) -> None:
    """Resumable, verified download with a progress line.

    The shared downloader keeps a resumable ``.part`` file and only replaces
    *dest* after the SHA-256 check.  The progress line is kept here because the
    standalone builder is a terminal tool.
    """
    destination = Path(dest)
    started = time.time()

    def progress(done: int, total: Optional[int]) -> None:
        if total:
            percent = done * 100 // total
            speed = done / max(time.time() - started, 0.5) / 1e6
            sys.stderr.write(
                f"\r  {destination.name}: {percent:3d}%  {human(done)} / "
                f"{human(total)}  {speed:.1f} MB/s"
            )
        else:
            sys.stderr.write(f"\r  {destination.name}: {human(done)}")
        sys.stderr.flush()

    try:
        _verified_download(
            url,
            destination,
            expected_sha256,
            timeout=timeout,
            progress=progress,
            user_agent=UA,
        )
    except DownloadError as exc:
        sys.stderr.write("\n")
        raise OSError(str(exc)) from exc
    sys.stderr.write("\n")
    if expected_sha256:
        print(f"  downloaded and verified: {destination.name}")
    else:
        print(f"  downloaded: {destination.name}")


def _replace_tree(source: Path, destination: Path) -> None:
    """Move *source* into *destination*, restoring the previous tree on failure.

    Extraction happens in a staging directory.  Keeping one previous copy until
    the staged tree is in place means an interrupted or disk-full extraction
    does not leave a partially replaced runtime.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(destination.name + ".old")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    had_destination = destination.exists()
    try:
        if had_destination:
            os.replace(destination, backup)
        os.replace(source, destination)
    except OSError:
        if had_destination and backup.exists() and not destination.exists():
            try:
                os.replace(backup, destination)
            except OSError:
                pass
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _kept_downloads_path(staged: Any) -> Path:
    """Return the retry cache for archives from an unfinished build."""
    return Path(staged.root) / f"{staged.key}.downloads"


def _restore_kept_downloads(staged: Any, downloads: Path) -> None:
    """Move archives retained by an earlier refused build into this stage."""
    cached = _kept_downloads_path(staged)
    if cached.is_symlink() or not cached.is_dir() or downloads.exists():
        return
    try:
        downloads.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cached), str(downloads))
    except OSError as exc:
        print(f"  warning: kept downloads could not be reused: {exc}")
    else:
        print(f"  reusing downloads from the previous attempt: {downloads}")


def _keep_downloads(
    staged: Any, downloads: Path, models_dir: Path
) -> Optional[Path]:
    """Retain archives for a retry, including a model already moved into place."""
    if models_dir.is_dir():
        downloads.mkdir(parents=True, exist_ok=True)
        for model in sorted(models_dir.glob("*.gguf")):
            if model.is_symlink() or not model.is_file():
                continue
            retained = downloads / model.name
            try:
                os.replace(model, retained)
            except OSError:
                try:
                    shutil.move(str(model), str(retained))
                except OSError as exc:
                    print(
                        f"  warning: could not keep downloaded model "
                        f"{model.name}: {exc}"
                    )
    if not downloads.is_dir():
        return None
    try:
        if not any(downloads.iterdir()):
            return None
    except OSError:
        return None
    cached = _kept_downloads_path(staged)
    try:
        if cached.is_symlink() or cached.is_file():
            cached.unlink()
        elif cached.is_dir():
            shutil.rmtree(cached)
        shutil.move(str(downloads), str(cached))
    except OSError as exc:
        print(f"  warning: downloaded archives could not be kept: {exc}")
        return None
    return cached


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


def _validate_tar_member(member: tarfile.TarInfo, root: Path) -> None:
    """Refuse a tar entry that would write or link outside *root*."""
    raw = member.name.replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if not parts or raw.startswith("/") or ".." in parts or os.path.isabs(member.name):
        raise FullBundleError(f"refusing unsafe archive entry: {member.name!r}")
    target = (root / member.name).resolve()
    if target != root and root not in target.parents:
        raise FullBundleError(
            f"refusing archive entry outside the destination: {member.name!r}"
        )
    if member.isdev() or member.isfifo():
        raise FullBundleError(f"refusing special file in archive: {member.name!r}")
    if member.issym() or member.islnk():
        # Symlink targets are relative to the link's directory; hardlinks to the root.
        base = target.parent if member.issym() else root
        linked = (base / member.linkname.replace("\\", "/")).resolve()
        if linked != root and root not in linked.parents:
            raise FullBundleError(
                f"refusing archive link outside the destination: "
                f"{member.name!r} -> {member.linkname!r}"
            )


def _safe_extract_tar(tf: tarfile.TarFile, destination: Path) -> None:
    """Extract a tar archive without letting entries escape *destination*.

    Python 3.12's ``data`` filter does this, but OffGrid supports 3.9+, so the
    validation has to happen here as well.
    """
    root = destination.resolve()
    data_filter = hasattr(tarfile, "data_filter")
    # Validate one member at a time: a link extracted earlier must not redirect
    # a later member outside the destination (the 3.12 ``data`` filter does
    # this itself, but 3.9-3.11 have no filter at all).
    for member in tf.getmembers():
        # Archives made by some tools include a harmless "." root entry.
        if member.name in ("", ".", "./"):
            continue
        _validate_tar_member(member, root)
        if data_filter:
            tf.extract(member, root, filter="data")
        else:
            tf.extract(member, root)


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as tf:
            _safe_extract_tar(tf, destination)
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
def resolve_llama(
    os_name: str, arch: str, dry_run: bool, with_checksum: bool = False
):
    patterns = LLAMA_PATTERNS.get((os_name, arch))
    if not patterns:
        print(f"  no llama.cpp build known for {os_name}/{arch}; install one manually")
        return None
    if dry_run:
        example = patterns[0].replace("\\", "").replace("$", "")
        result = (f"https://github.com/ggml-org/llama.cpp/releases/latest/download/{example}", example)
        return result + ("",) if with_checksum else result

    releases = gh_releases("ggml-org/llama.cpp", pages=2)
    if not releases:
        print("  manual: https://github.com/ggml-org/llama.cpp/releases")
        return None
    for release in releases:
        asset = find_asset(release.get("assets") or [], patterns)
        if asset and asset.get("browser_download_url"):
            result = (asset["browser_download_url"], asset.get("name", "llama.zip"))
            return result + (_asset_sha256(asset),) if with_checksum else result
    print(f"  no release ships a binary matching {patterns}")
    print(f"  newest release was {releases[0].get('tag_name')}")
    print("  manual: https://github.com/ggml-org/llama.cpp/releases")
    print("  fallback: install Ollama instead; OffGrid autodetects it")
    return None


def resolve_python(
    os_name: str, arch: str, dry_run: bool, with_checksum: bool = False
):
    triple = PYTHON_TRIPLES.get((os_name, arch))
    if not triple:
        print(f"  no portable python known for {os_name}/{arch}")
        return None
    if dry_run:
        result = (
            f"https://github.com/astral-sh/python-build-standalone/releases/latest/download/"
            f"cpython-3.12-{triple}-install_only.tar.gz",
            "",
        )
        return result + ("",) if with_checksum else result

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
    result = (best["browser_download_url"], best["name"])
    return result + (_asset_sha256(best),) if with_checksum else result


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
    for name in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
        source = assets / name
        if not source.is_file():
            raise FullBundleError(f"launcher missing from bundle assets: {source}")
        shutil.copy2(source, target / name)
        make_executable(target / name)


def write_install_manifest(
    target: Path,
    *,
    model_file: str = "",
    model_sha256: str = "",
    created: Optional[str] = None,
    requested_components: Optional[Mapping[str, bool]] = None,
) -> Path:
    """Describe a completed full bundle and verify its app files.

    The caller commits this prepared tree through the install transaction,
    which writes the committed install state.
    """
    model_hashes = {model_file: model_sha256} if model_file and model_sha256 else None
    manifest = install_manifest.build_manifest(
        target,
        artifact_kind="full",
        platform="-".join(platform_key()),
        created=created if created is not None else _manifest_created(),
        model_hashes=model_hashes,
        requested_components=requested_components,
    )
    path = install_manifest.write_manifest(target, manifest)
    report = install_manifest.verify_manifest(
        target, manifest, check_extra=True
    )
    if not report.ok:
        raise FullBundleError(
            "full-bundle install manifest did not verify: "
            + "; ".join(report.errors)
        )
    return path


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

  macOS (double-click) OffGrid.command
  Windows (double-click) OffGrid.bat
  Linux / terminal     ./OffGrid.sh chat

Windows note: install Python from python.org and tick "Add python.exe to PATH"
for the portable zip; this full bundle carries its own runtime below. If Smart
App Control or SmartScreen blocks the download, unblock the zip before
extracting (Properties -> Unblock). A console that flashes and closes prints
its reason if you run OffGrid.bat from a Command Prompt in this folder.

  One-shot task        ./OffGrid.sh run "add type hints to utils.py"
  Check the setup      ./OffGrid.sh doctor
  Desktop shortcut     ./OffGrid.sh desktop install

The top-level launchers start chat with no arguments. The same scripts are in
launch/ for terminal use. They start the bundled model server by itself the
first time, wait for it to load (10-40 seconds on a laptop), then drop you into
the agent.

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
    print("  ./OffGrid.sh run \"add a CLI entry point\"")
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
            "choose a smaller model, or add RAM."
        )
    return ""


def missing_components(
    target: Path,
    *,
    require_python: bool = True,
    require_llama: bool = True,
    require_model: bool = True,
    require_install_state: bool = True,
) -> List[str]:
    """Return the parts of a full bundle that are absent.

    Downloads are best-effort while the builder stages a tree, but a requested
    component that is still missing is refused before commit. The distribution
    wizard uses this check to distinguish that fallback from a genuinely
    complete bundle. ``require_install_state=False`` is for checking a prepared
    transaction stage, which writes the state file only during the commit.
    """
    target = Path(target).expanduser()
    missing: List[str] = []
    if not (target / "offgrid" / "cli.py").is_file():
        missing.append("the OffGrid source")

    manifest_path = target / install_manifest.MANIFEST_NAME
    manifest = None
    if manifest_path.is_file():
        try:
            manifest = install_manifest.load_manifest(manifest_path)
        except (install_manifest.ManifestError, OSError):
            manifest = None
    if manifest is not None:
        missing.extend(
            install_manifest.missing_requested_components(
                target,
                manifest,
                requested={
                    "python": require_python,
                    "llama": require_llama,
                    "model": require_model,
                },
            )
        )
    else:
        os_name, _ = platform_key()
        if require_python:
            python = target / "runtime" / "python" / (
                "python.exe" if os_name == "windows" else "bin/python3"
            )
            if not python.is_file():
                missing.append("portable Python")
        if require_llama:
            binary = (
                "llama-server.exe" if os_name == "windows" else "llama-server"
            )
            llama = target / "runtime" / "llama" / binary
            if not llama.is_file() and not any(
                (target / "runtime" / "llama").rglob(binary)
            ):
                missing.append("llama.cpp")
        if require_model and not any((target / "models").glob("*.gguf")):
            missing.append("the GGUF model")

    launch = target / "launch"
    for wanted in ("offgrid.sh", "offgrid.command", "offgrid.bat"):
        if not (launch / wanted).is_file():
            missing.append(f"{wanted} launcher")
    for wanted in ("OffGrid.sh", "OffGrid.command", "OffGrid.bat"):
        if not (target / wanted).is_file():
            missing.append(f"{wanted} launcher")
    if not (target / "config.json").is_file():
        missing.append("config.json")
    if not manifest_path.is_file():
        missing.append("install manifest")
    if require_install_state and not (target / "install-state.json").is_file():
        missing.append("install state")
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
    inputs: Optional[Path] = None,
    dry_run: bool = False,
) -> FullBundleResult:
    """Build a full bundle and fail if any requested component is missing.

    This is the library entry point used by the distribution wizard. The
    standalone builder may stage partial downloads, but it refuses to commit
    unless every requested component is present; without skip flags this
    function requires Python, llama.cpp and the model to all be present.  Pass
    ``inputs`` to build from an exact pinned manifest instead of resolving
    GitHub/Hugging Face downloads.
    """
    if model not in MODELS and inputs is None:
        raise FullBundleError(f"unknown model '{model}'; choose from: {', '.join(MODELS)}")
    argv = ["--target", str(target), "--model", model]
    if inputs is not None:
        argv.extend(("--inputs", str(inputs)))
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
def _run_main(argv: Optional[List[str]] = None) -> int:
    global _CURRENT_STAGE
    parser = argparse.ArgumentParser(
        description="Assemble a portable offline OffGrid bundle (USB drive / laptop)."
    )
    parser.add_argument("--target", default="offgrid-usb", help="where to build the bundle")
    parser.add_argument("--model", default="7b", help="model size to download")
    parser.add_argument(
        "--inputs",
        "--input-manifest",
        dest="inputs",
        type=Path,
        help="pinned JSON manifest of exact runtime and model inputs",
    )
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

    inputs = None
    if args.inputs is not None:
        try:
            inputs = load_input_manifest(args.inputs)
        except FullBundleError as exc:
            print(f"  {exc}")
            return 1
    if inputs is None:
        if args.model not in MODELS:
            print(f"  unknown model '{args.model}'. choose from: {', '.join(MODELS)}")
            return 1
        model_spec = MODELS[args.model]
    else:
        model_spec = inputs["models"].get(args.model)
        if model_spec is None:
            print(
                f"  the input manifest has no pinned model '{args.model}'. "
                f"choose from: {', '.join(inputs['models'])}"
            )
            return 1

    os_name, arch = platform_key()
    final_target = Path(args.target).expanduser().resolve()
    target = final_target
    try:
        _manifest_created()
    except FullBundleError as exc:
        print(f"  {exc}")
        return 1

    print()
    print("  OffGrid bundle builder")
    print(f"  platform  {os_name}/{arch}")
    print(f"  target    {final_target}")
    print(
        f"  model     {args.model} (about {model_spec['gb']} GB; "
        f"RAM: {model_spec['ram']})"
    )
    if not args.skip_model:
        warning = ram_warning(args.model)
        if warning:
            print(f"  warning: {warning}")
    print()

    if inputs is None:
        required_space = estimate_required_space_gb(
            args.model,
            skip_python=args.skip_python,
            skip_llama=args.skip_llama,
            skip_model=args.skip_model,
        )
    else:
        required_space = round(
            0.35
            + (0.0 if args.skip_python else 0.06)
            + (0.0 if args.skip_llama else 0.15)
            + (0.0 if args.skip_model else float(model_spec["gb"]))
            + FREE_SPACE_MARGIN_GB,
            1,
        )
    if not args.dry_run:
        from . import install_transaction

        # Reclaim bounded abandoned assembly trees before the free-space check;
        # a previous interrupted build can otherwise consume the space needed
        # for this one.  Active builds are protected by their stage lease.
        try:
            cleanup = install_transaction.cleanup_orphan_stages(final_target)
        except (install_transaction.InstallError, OSError) as exc:
            print(f"  warning: could not clean abandoned staging: {exc}")
        else:
            if cleanup.removed:
                print(
                    f"  reclaimed {len(cleanup.removed)} abandoned staging "
                    f"director{'y' if len(cleanup.removed) == 1 else 'ies'} "
                    f"({human(cleanup.reclaimed_bytes)})"
                )
            for error in cleanup.errors:
                print(f"  warning: stage cleanup: {error}")
        try:
            ensure_free_space(final_target, required_space)
        except FullBundleError as exc:
            print(f"  {exc}")
            print("  choose another target or free space, then retry")
            return 1

    llama_input = None
    python_input = None
    model_input = None
    if args.llama_url:
        archive_name = os.path.basename(args.llama_url.split("?")[0]) or "llama-archive.zip"
        llama = (args.llama_url, archive_name, "")
    elif inputs is not None:
        if args.skip_llama:
            llama = None
        else:
            llama_input = _pinned_runtime(inputs, "llama", os_name, arch)
            llama = (
                str(llama_input["path"] or llama_input["url"]),
                str(llama_input["file"]),
                str(llama_input["sha256"]),
            )
    else:
        llama = (
            None
            if args.skip_llama
            else resolve_llama(os_name, arch, args.dry_run, with_checksum=True)
        )
    if inputs is not None:
        if args.skip_python:
            python = None
        else:
            python_input = _pinned_runtime(inputs, "python", os_name, arch)
            python = (
                str(python_input["path"] or python_input["url"]),
                str(python_input["file"]),
                str(python_input["sha256"]),
            )
    else:
        python = (
            None
            if args.skip_python
            else resolve_python(os_name, arch, args.dry_run, with_checksum=True)
        )
    if args.skip_model:
        model = None
    elif inputs is not None:
        model_input = inputs["models"][args.model]
        model = (
            str(model_input["path"] or model_input["url"]),
            str(model_input["file"]),
            model_input,
        )
    else:
        model = resolve_model(args.model, args.dry_run)
    llama = _resolution(llama)
    python = _resolution(python)

    if inputs is not None:
        total_bytes = sum(
            int(entry.get("size") or 0)
            for entry in (llama_input, python_input, model_input)
            if entry is not None
        )
        print(
            f"  pinned inputs: {human(total_bytes)} local fixture bytes; "
            "no release lookup or large download"
        )
    else:
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

    # Assemble in the transaction's own stage. The commit below verifies and
    # activates that tree in place, so a multi-gigabyte model is never copied
    # into a second staging tree.
    final_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        staged = install_transaction.prepare_directory_stage(final_target)
    except (install_transaction.InstallError, OSError) as exc:
        print(f"  could not prepare the full bundle: {exc}")
        return 1
    _CURRENT_STAGE = staged

    target = staged.stage
    downloads = target / ".downloads"
    source_root = _source_root()
    bundle_assets = _assets_root()
    plan: List[str] = []
    incomplete = False

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
    plan.append("OffGrid.*          double-click launchers at the top level")

    _restore_kept_downloads(staged, downloads)

    # 2. model server
    if llama:
        url, name, expected = llama
        archive = downloads / name
        print("  downloading llama.cpp...")
        try:
            if llama_input is not None:
                _stage_pinned_input(llama_input, archive)
            else:
                download(url, archive, expected_sha256=expected)
            llama_tmp = target / "runtime" / "llama-tmp"
            llama_root = target / "runtime" / "llama"
            if llama_tmp.exists():
                shutil.rmtree(llama_tmp, ignore_errors=True)
            extract(archive, llama_tmp)
            binary = "llama-server.exe" if os_name == "windows" else "llama-server"
            located = flatten_binary_dir(llama_tmp, binary)
            if located:
                make_executable(located)
                write_runtime_fingerprint(
                    llama_tmp,
                    located,
                    platform=f"{os_name}-{arch}",
                    version=name or "bundle",
                    archive_sha256=expected,
                )
                _replace_tree(llama_tmp, llama_root)
                plan.append("runtime/llama/      llama.cpp model server")
            else:
                shutil.rmtree(llama_tmp, ignore_errors=True)
                print(f"  extracted llama.cpp but found no {binary} inside it")
                llama = False
        except Exception as exc:
            shutil.rmtree(target / "runtime" / "llama-tmp", ignore_errors=True)
            print(f"  llama.cpp download failed: {exc}")
            llama = False
            incomplete = True

    # 3. portable python
    if python:
        url, name, expected = python
        archive = downloads / name
        print("  downloading portable python...")
        try:
            if python_input is not None:
                _stage_pinned_input(python_input, archive)
            else:
                download(url, archive, expected_sha256=expected)
            python_tmp = target / "runtime" / "python-tmp"
            if python_tmp.exists():
                shutil.rmtree(python_tmp, ignore_errors=True)
            extract(archive, python_tmp)
            # The archive contains a single top-level "python" directory.
            # Dereference its aliases now: a full bundle may be copied to exFAT,
            # where symlinks cannot survive, and the launcher must verify the
            # one tree form that is actually shipped.
            inner = python_tmp / "python"
            final = target / "runtime" / "python"
            _flatten_python_runtime(
                inner if inner.is_dir() else python_tmp, final
            )
            if python_tmp.exists():
                shutil.rmtree(python_tmp, ignore_errors=True)
            plan.append("runtime/python/     portable CPython, no install needed")
            exe = final / ("python.exe" if os_name == "windows" else "bin/python3")
            if exe.is_file():
                make_executable(exe)
                write_runtime_fingerprint(
                    final,
                    exe,
                    platform=f"{os_name}-{arch}",
                    version=name or "portable-python",
                    archive_sha256=expected,
                )
                write_python_tree_digest(
                    target, final, platform=f"{os_name}-{arch}"
                )
        except Exception as exc:
            shutil.rmtree(target / "runtime" / "python-tmp", ignore_errors=True)
            print(f"  portable python download failed: {exc} (system python3 will be used)")
            incomplete = True

    # 4. the brain
    model_file = ""
    model_sha256 = ""
    if model:
        url, name, spec = model
        archive = downloads / name
        print(f"  downloading {name} ({spec['gb']} GB, resumable, SHA-256 checked)...")
        try:
            if model_input is not None:
                _stage_pinned_input(model_input, archive)
            else:
                download(
                    url,
                    archive,
                    expected_sha256=str(spec.get("sha256") or ""),
                )
            models_dir = target / "models"
            models_dir.mkdir(parents=True, exist_ok=True)
            final_model = models_dir / name
            os.replace(archive, final_model)
            model_file = name
            model_sha256 = str(spec.get("sha256") or "")
            plan.append(f"models/{name}   the model ({spec['note']})")
        except Exception as exc:
            print(f"  model download failed: {exc}")
            print("  you can drop any .gguf into models/ later")
            incomplete = True

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
    try:
        write_install_manifest(
            target,
            model_file=model_file,
            model_sha256=model_sha256,
            requested_components={
                "python": not args.skip_python,
                "llama": not args.skip_llama,
                "model": not args.skip_model,
            },
        )
    except (FullBundleError, install_manifest.ManifestError, OSError) as exc:
        print(f"  could not write the install manifest: {exc}")
        install_transaction.discard_directory_stage(staged)
        return 1
    plan.append("install-manifest.json  hashes for integrity checks")

    missing = missing_components(
        target,
        require_python=not args.skip_python,
        require_llama=not args.skip_llama,
        require_model=not args.skip_model,
        require_install_state=False,
    )
    if missing:
        print(
            "  refusing to commit an incomplete full bundle; missing "
            + ", ".join(missing)
        )
        print("  the existing destination was not changed")
        retained = _keep_downloads(staged, downloads, target / "models")
        if retained is not None:
            print(
                f"  downloaded archives kept in {retained}; rerun the same "
                "command to reuse them."
            )
        install_transaction.discard_directory_stage(staged)
        return 1

    try:
        verification = install_transaction.verify_staged_directory(target)
    except (
        install_transaction.InstallError,
        install_manifest.ManifestError,
        OSError,
    ) as exc:
        print(f"  staged full bundle did not verify: {exc}")
        print("  the existing destination was not changed")
        install_transaction.discard_directory_stage(staged)
        return 1

    if not (args.keep_downloads or incomplete) and downloads.is_dir():
        shutil.rmtree(downloads, ignore_errors=True)

    try:
        install_transaction.install_directory(
            target,
            final_target,
            force=True,
            staged=staged,
            verified=verification,
        )
    except (install_transaction.InstallError, OSError) as exc:
        print(f"  could not commit the full bundle: {exc}")
        install_transaction.discard_directory_stage(staged)
        return 1

    if incomplete and not args.keep_downloads:
        final_downloads = final_target / ".downloads"
        if final_downloads.is_dir():
            print(
                f"  partial downloads kept in {final_downloads}; rerun the same "
                "command to resume them."
            )

    summarise(final_target, plan, model_file)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Run the builder, marking any interrupted stage as immediately orphaned."""
    global _CURRENT_STAGE
    _CURRENT_STAGE = None
    try:
        return _run_main(argv)
    except BaseException:
        staged = _CURRENT_STAGE
        if staged is not None:
            from . import install_transaction

            install_transaction.abandon_directory_stage(staged)
        raise
    finally:
        _CURRENT_STAGE = None


if __name__ == "__main__":
    sys.exit(main())
