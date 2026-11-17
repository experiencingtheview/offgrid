"""Build and verify the ``install-manifest.json`` shipped with OffGrid.

Schema 1 deliberately separates three kinds of files:

* immutable app files, each with a size and SHA-256;
* mutable user state such as ``config.json``, recorded but allowed to change;
* large runtimes and models, recorded for on-demand verification so an
  ordinary launch never hashes a multi-gigabyte GGUF.

The manifest never lists itself.  It is written atomically and can be verified
with :func:`verify_manifest` for an unpacked bundle or :func:`verify_zip` for
the downloadable portable archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import __version__

MANIFEST_NAME = "install-manifest.json"
SCHEMA = "offgrid.install-manifest/1"
RUNTIME_FINGERPRINT_NAME = ".offgrid-runtime.json"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ENTRYPOINT_NAMES = ("app", "linux", "macos", "windows")
ARTIFACT_KINDS = frozenset({"portable", "full"})
FILE_KINDS = frozenset({"app", "launcher", "script", "doc", "pyz", "test", "file"})
USER_STATE_KINDS = frozenset({"config", "metadata"})
RUNTIME_KINDS = frozenset({"python", "llama"})
EXCLUDED_PATHS = (
    ".downloads/**",
    ".git/**",
    ".offgrid/**",
    ".offgrid-install/**",
    ".pytest_cache/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "install-manifest.json",
    "install-state.json",
    "models/**",
    "runtime/**",
)
USER_STATE_NAMES = {
    "config.json": "config",
    "MODEL.txt": "metadata",
}


class ManifestError(ValueError):
    """The install manifest is malformed or does not match its artifact."""


@dataclass(frozen=True)
class VerificationReport:
    """The result of checking an artifact against its manifest."""

    checked_files: int
    checked_bytes: int
    checked_assets: int
    errors: Tuple[str, ...]
    user_state_changed: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 of *path* without loading it in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes, used for both writing and the manifest digest."""
    return (
        json.dumps(dict(manifest), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical manifest bytes."""
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{label} must be true or false")
    return value


def _size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{label} must be a non-negative integer")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ManifestError(f"{label} must be a lowercase SHA-256")
    return value


def _optional_hash(entry: Mapping[str, Any], key: str, label: str) -> Optional[str]:
    if key not in entry:
        return None
    return _hash(entry[key], f"{label}.{key}")


def _relative_path(value: Any, label: str) -> str:
    path = _string(value, label)
    if "\x00" in path:
        raise ManifestError(f"{label} contains a NUL byte: {path!r}")
    if "\\" in path:
        raise ManifestError(f"{label} must use POSIX separators: {path!r}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ManifestError(f"{label} must be relative: {path!r}")
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts:
        raise ManifestError(f"{label} must be a relative path: {path!r}")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise ManifestError(f"{label} contains an unsafe component: {path!r}")
    if str(relative) != path:
        raise ManifestError(f"{label} is not a canonical path: {path!r}")
    if relative.parts[0].endswith(":"):
        raise ManifestError(f"{label} must not name a drive: {path!r}")
    return path


def _under(path: str, parent: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(parent))
    except ValueError:
        return False
    return True


def _register_path(
    path: str,
    label: str,
    seen: Dict[str, str],
) -> None:
    _relative_path(path, label)
    key = path.casefold()
    if key in seen:
        raise ManifestError(
            f"case-folded path collision between {seen[key]!r} and {path!r}"
        )
    seen[key] = path


def _validate_entrypoints(
    value: Any,
    files_by_path: Mapping[str, Mapping[str, Any]],
) -> None:
    entrypoints = _mapping(value, "entrypoints")
    if set(entrypoints) != set(ENTRYPOINT_NAMES):
        raise ManifestError(
            "entrypoints must name exactly " + ", ".join(ENTRYPOINT_NAMES)
        )
    for name in ENTRYPOINT_NAMES:
        path = _relative_path(
            entrypoints[name], f"entrypoints.{name}"
        )
        entry = files_by_path.get(path)
        if entry is None:
            raise ManifestError(
                f"entrypoint {name!r} is not in the immutable file list: {path}"
            )
        if not entry["required"]:
            raise ManifestError(
                f"entrypoint {name!r} must be a required file: {path}"
            )


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the complete schema and internal totals.

    Raises :class:`ManifestError` with a specific reason.  Validation is
    intentionally independent of the filesystem so a downloaded manifest can
    be checked before any path is opened.
    """
    data = _mapping(manifest, "manifest")
    if data.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema: {data.get('schema')!r}")
    _string(data.get("generator_version"), "generator_version")

    artifact = _mapping(data.get("artifact"), "artifact")
    kind = _string(artifact.get("kind"), "artifact.kind")
    if kind not in ARTIFACT_KINDS:
        raise ManifestError(f"unsupported artifact kind: {kind!r}")
    _string(artifact.get("version"), "artifact.version")
    _string(artifact.get("platform"), "artifact.platform")
    _string(artifact.get("created"), "artifact.created")
    source = artifact.get("source_sha256")
    if not isinstance(source, str):
        raise ManifestError("artifact.source_sha256 must be a string")
    if source:
        _hash(source, "artifact.source_sha256")

    seen: Dict[str, str] = {}
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("files must be a non-empty list")
    file_paths: List[str] = []
    files_by_path: Dict[str, Mapping[str, Any]] = {}
    total_bytes = 0
    for index, raw in enumerate(files):
        label = f"files[{index}]"
        entry = _mapping(raw, label)
        path = _relative_path(entry.get("path"), f"{label}.path")
        if path == MANIFEST_NAME:
            raise ManifestError("the manifest must not list itself")
        _register_path(path, f"{label}.path", seen)
        entry_kind = _string(entry.get("kind"), f"{label}.kind")
        if entry_kind not in FILE_KINDS:
            raise ManifestError(f"unsupported file kind: {entry_kind!r}")
        _size(entry.get("size"), f"{label}.size")
        _boolean(entry.get("executable"), f"{label}.executable")
        _boolean(entry.get("required"), f"{label}.required")
        check = _string(entry.get("check"), f"{label}.check")
        if check not in ("sha256", "presence"):
            raise ManifestError(f"unsupported file check mode: {check!r}")
        if check == "sha256":
            _hash(entry.get("sha256"), f"{label}.sha256")
        total_bytes += int(entry["size"])
        file_paths.append(path)
        files_by_path[path] = entry
    if file_paths != sorted(file_paths):
        raise ManifestError("files must be sorted by path")

    _validate_entrypoints(data.get("entrypoints"), files_by_path)

    user_state = data.get("user_state")
    if not isinstance(user_state, list):
        raise ManifestError("user_state must be a list")
    user_paths: List[str] = []
    for index, raw in enumerate(user_state):
        label = f"user_state[{index}]"
        entry = _mapping(raw, label)
        path = _relative_path(entry.get("path"), f"{label}.path")
        _register_path(path, f"{label}.path", seen)
        entry_kind = _string(entry.get("kind"), f"{label}.kind")
        if entry_kind not in USER_STATE_KINDS:
            raise ManifestError(f"unsupported user-state kind: {entry_kind!r}")
        present = _boolean(entry.get("present"), f"{label}.present")
        _size(entry.get("size"), f"{label}.size")
        if present:
            _hash(entry.get("sha256"), f"{label}.sha256")
        user_paths.append(path)
    if user_paths != sorted(user_paths):
        raise ManifestError("user_state must be sorted by path")

    runtimes = data.get("runtimes")
    if not isinstance(runtimes, list):
        raise ManifestError("runtimes must be a list")
    runtime_paths: List[str] = []
    for index, raw in enumerate(runtimes):
        label = f"runtimes[{index}]"
        entry = _mapping(raw, label)
        path = _relative_path(entry.get("path"), f"{label}.path")
        if not path.startswith("runtime/"):
            raise ManifestError(f"{label}.path must be under runtime/")
        _register_path(path, f"{label}.path", seen)
        runtime_kind = _string(entry.get("kind"), f"{label}.kind")
        if runtime_kind not in RUNTIME_KINDS:
            raise ManifestError(f"unsupported runtime kind: {runtime_kind!r}")
        _boolean(entry.get("required"), f"{label}.required")
        if entry.get("verification") != "on-demand":
            raise ManifestError(f"{label}.verification must be 'on-demand'")
        for key in ("version", "platform"):
            if key in entry:
                _string(entry[key], f"{label}.{key}", allow_empty=True)
        _optional_hash(entry, "archive_sha256", label)
        fingerprint = entry.get("fingerprint")
        if fingerprint is not None:
            fingerprint_path = _relative_path(
                fingerprint, f"{label}.fingerprint"
            )
            if not _under(fingerprint_path, path):
                raise ManifestError(f"{label}.fingerprint must be inside the runtime")
            _hash(entry.get("fingerprint_sha256"), f"{label}.fingerprint_sha256")
        binary = entry.get("binary")
        if binary is not None:
            binary_path = _relative_path(binary, f"{label}.binary")
            combined = str(PurePosixPath(path) / PurePosixPath(binary_path))
            if not _under(combined, path):
                raise ManifestError(f"{label}.binary must be inside the runtime")
            _size(entry.get("binary_size"), f"{label}.binary_size")
            _hash(entry.get("binary_sha256"), f"{label}.binary_sha256")
        runtime_paths.append(path)
    if runtime_paths != sorted(runtime_paths):
        raise ManifestError("runtimes must be sorted by path")

    models = data.get("models")
    if not isinstance(models, list):
        raise ManifestError("models must be a list")
    model_paths: List[str] = []
    for index, raw in enumerate(models):
        label = f"models[{index}]"
        entry = _mapping(raw, label)
        path = _relative_path(entry.get("path"), f"{label}.path")
        if not path.startswith("models/") or not path.lower().endswith(".gguf"):
            raise ManifestError(f"{label}.path must be a GGUF under models/")
        _register_path(path, f"{label}.path", seen)
        if entry.get("kind") != "model":
            raise ManifestError(f"{label}.kind must be 'model'")
        _boolean(entry.get("required"), f"{label}.required")
        if entry.get("verification") != "on-demand":
            raise ManifestError(f"{label}.verification must be 'on-demand'")
        _size(entry.get("size"), f"{label}.size")
        _hash(entry.get("sha256"), f"{label}.sha256")
        model_paths.append(path)
    if model_paths != sorted(model_paths):
        raise ManifestError("models must be sorted by path")

    excluded = data.get("excluded")
    if not isinstance(excluded, list):
        raise ManifestError("excluded must be a list")
    if any(not isinstance(item, str) or not item for item in excluded):
        raise ManifestError("excluded entries must be non-empty strings")
    if len(set(excluded)) != len(excluded):
        raise ManifestError("excluded entries must be unique")

    totals = _mapping(data.get("totals"), "totals")
    expected = {
        "files": len(files),
        "bytes": total_bytes,
        "user_state": len(user_state),
        "runtimes": len(runtimes),
        "models": len(models),
    }
    if set(totals) != set(expected):
        raise ManifestError(
            "totals must contain exactly " + ", ".join(sorted(expected))
        )
    for key, value in expected.items():
        _size(totals.get(key), f"totals.{key}")
        if totals[key] != value:
            raise ManifestError(
                f"totals.{key} is {totals[key]!r}, expected {value}"
            )


def parse_manifest(data: bytes) -> Dict[str, Any]:
    """Parse and validate manifest bytes."""
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid install manifest JSON: {exc}") from exc
    manifest = _mapping(decoded, "manifest")
    validate_manifest(manifest)
    return dict(manifest)


def load_manifest(path: Path) -> Dict[str, Any]:
    """Read, parse and validate a manifest file."""
    path = Path(path)
    if path.is_symlink():
        raise ManifestError(f"install manifest must not be a symlink: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"could not read install manifest {path}: {exc}") from exc
    return parse_manifest(data)


def _is_excluded_dir(relative: PurePosixPath) -> bool:
    if relative.parts[0] in (
        ".downloads",
        ".git",
        ".offgrid",
        ".offgrid-install",
        ".pytest_cache",
        "models",
        "runtime",
    ):
        return True
    return any(
        part in ("__pycache__", ".pytest_cache")
        for part in relative.parts
    )


def _is_excluded_file(relative: PurePosixPath) -> bool:
    if relative.parts[0] in (
        ".downloads",
        ".git",
        ".offgrid",
        ".offgrid-install",
        ".pytest_cache",
        "models",
        "runtime",
    ):
        return True
    if any(part in ("__pycache__", ".pytest_cache") for part in relative.parts):
        return True
    name = relative.name
    if relative in (PurePosixPath(MANIFEST_NAME), PurePosixPath("install-state.json")):
        return True
    if relative.parts and relative.parts[0].startswith(f".{MANIFEST_NAME}."):
        return True
    if name == ".DS_Store" or name.endswith(".pyc"):
        return True
    return len(relative.parts) == 1 and name in USER_STATE_NAMES


def _unexpected_files(
    root: Path, manifest: Mapping[str, Any]
) -> List[str]:
    """Return regular files that a manifest does not account for.

    Runtime and model trees are intentionally opaque here; their internals are
    handled by their asset entries.  Symlinks are left to the installer, which
    resolves internal links and rejects escapes.
    """
    expected = {
        str(entry["path"])
        for section in ("files", "user_state")
        for entry in manifest[section]
    }
    expected.update((MANIFEST_NAME, "install-state.json"))
    unexpected: List[str] = []
    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        relative_dir = current.relative_to(root)
        kept: List[str] = []
        for name in sorted(dirnames):
            candidate = current / name
            relative = PurePosixPath(*(relative_dir / name).parts)
            if _is_excluded_dir(relative) or candidate.is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = current / name
            relative_path = PurePosixPath(*(relative_dir / name).parts)
            if _is_excluded_file(relative_path):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            relative = relative_path.as_posix()
            if relative not in expected:
                unexpected.append(relative)
    return sorted(unexpected)


def _classify(path: PurePosixPath) -> str:
    if path.name == "offgrid.pyz":
        return "pyz"
    if path.parts[0] == "launch" or path.name in (
        "OffGrid.sh",
        "OffGrid.command",
        "OffGrid.bat",
    ):
        return "launcher"
    if path.parts[0] == "scripts":
        return "script"
    if path.parts[0] == "tests":
        return "test"
    if path.parts[0] == "offgrid":
        return "app"
    if path.suffix.lower() in (".md", ".rst", ".txt") or path.name in (
        "LICENSE",
        "pyproject.toml",
    ):
        return "doc"
    return "file"


def _collect_files(root: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        relative_dir = current.relative_to(root)
        kept: List[str] = []
        for name in sorted(dirnames):
            candidate = current / name
            relative = PurePosixPath(*(relative_dir / name).parts)
            if _is_excluded_dir(relative):
                continue
            if candidate.is_symlink():
                raise ManifestError(
                    f"cannot manifest a symlinked directory: {relative}"
                )
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            path = current / name
            relative_path = path.relative_to(root)
            relative = PurePosixPath(*relative_path.parts)
            if _is_excluded_file(relative):
                continue
            if path.is_symlink():
                raise ManifestError(f"cannot manifest a symlink: {relative}")
            if not path.is_file():
                continue
            info = path.stat()
            entries.append(
                {
                    "path": relative.as_posix(),
                    "kind": _classify(relative),
                    "size": info.st_size,
                    "sha256": sha256_file(path),
                    "executable": bool(
                        info.st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    ),
                    "required": True,
                    "check": "sha256",
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _user_state_entries(root: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for name in sorted(USER_STATE_NAMES):
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise ManifestError(f"cannot manifest a symlinked user-state file: {name}")
        if not path.is_file():
            raise ManifestError(f"user state is not a regular file: {name}")
        entries.append(
            {
                "path": name,
                "kind": USER_STATE_NAMES[name],
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "present": True,
            }
        )
    return entries


def _runtime_binary(directory: Path, kind: str) -> Optional[Path]:
    if kind == "python":
        candidates = (
            "python.exe",
            "bin/python3",
            "bin/python",
            "python3",
        )
    elif kind == "llama":
        candidates = (
            "llama-server.exe",
            "llama-server",
        )
    else:
        return None
    for name in candidates:
        candidate = directory / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    for name in candidates:
        found = sorted(
            path
            for path in directory.rglob(name)
            if path.is_file() and not path.is_symlink()
        )
        if found:
            return found[0]
    return None


def _runtime_entries(root: Path) -> List[Dict[str, Any]]:
    runtime_root = root / "runtime"
    if not runtime_root.exists():
        return []
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ManifestError("runtime must be a directory, not a symlink")
    entries: List[Dict[str, Any]] = []
    for directory in sorted(runtime_root.iterdir(), key=lambda item: item.name):
        if directory.is_symlink():
            raise ManifestError(
                f"cannot manifest a symlinked runtime: {directory.name}"
            )
        if not directory.is_dir():
            continue
        if directory.name.startswith(".") or directory.name.endswith(("-tmp", ".old")):
            continue
        kind = directory.name
        if kind not in RUNTIME_KINDS:
            continue
        relative = PurePosixPath("runtime") / directory.name
        entry: Dict[str, Any] = {
            "path": relative.as_posix(),
            "kind": kind,
            "required": True,
            "verification": "on-demand",
        }
        fingerprint = directory / RUNTIME_FINGERPRINT_NAME
        if fingerprint.is_file() and not fingerprint.is_symlink():
            try:
                payload = json.loads(fingerprint.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManifestError(
                    f"invalid runtime fingerprint {fingerprint}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ManifestError(
                    f"runtime fingerprint is not an object: {fingerprint}"
                )
            for key in ("version", "platform", "archive_sha256"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    entry[key] = value
            entry["fingerprint"] = (
                relative / RUNTIME_FINGERPRINT_NAME
            ).as_posix()
            entry["fingerprint_sha256"] = sha256_file(fingerprint)
        binary = _runtime_binary(directory, kind)
        if binary is not None:
            entry["binary"] = binary.relative_to(directory).as_posix()
            entry["binary_size"] = binary.stat().st_size
            entry["binary_sha256"] = sha256_file(binary)
        entries.append(entry)
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _model_entries(
    root: Path,
    model_hashes: Optional[Mapping[str, str]],
) -> List[Dict[str, Any]]:
    models_root = root / "models"
    if not models_root.exists():
        return []
    if models_root.is_symlink() or not models_root.is_dir():
        raise ManifestError("models must be a directory, not a symlink")
    known = {
        str(key): str(value).lower()
        for key, value in (model_hashes or {}).items()
    }
    entries: List[Dict[str, Any]] = []
    for path in sorted(models_root.rglob("*.gguf")):
        if path.is_symlink():
            raise ManifestError(f"cannot manifest a symlinked model: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        expected = known.get(relative) or known.get(path.name) or ""
        if expected:
            _hash(expected, f"model hash for {relative}")
            digest = expected
        else:
            digest = sha256_file(path)
        entries.append(
            {
                "path": relative,
                "kind": "model",
                "size": path.stat().st_size,
                "sha256": digest,
                "required": True,
                "verification": "on-demand",
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def build_manifest(
    root: Path,
    *,
    artifact_kind: str,
    platform: str = "any",
    version: Optional[str] = None,
    created: Optional[str] = None,
    source_sha256: str = "",
    entrypoints: Optional[Mapping[str, str]] = None,
    model_hashes: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Describe an already-staged artifact tree.

    ``model_hashes`` lets a builder reuse a checksum it obtained while
    downloading a large GGUF.  Without an entry the file is hashed here.
    """
    if artifact_kind not in ARTIFACT_KINDS:
        raise ManifestError(f"unsupported artifact kind: {artifact_kind!r}")
    root = Path(root)
    if root.is_symlink():
        raise ManifestError(f"artifact root must not be a symlink: {root}")
    if not root.is_dir():
        raise ManifestError(f"artifact root is not a directory: {root}")

    names = {
        "app": "offgrid.pyz" if artifact_kind == "portable" else "offgrid/cli.py",
        "linux": "OffGrid.sh",
        "macos": "OffGrid.command",
        "windows": "OffGrid.bat",
    }
    if entrypoints is not None:
        names.update({str(key): str(value) for key, value in entrypoints.items()})

    files = _collect_files(root)
    user_state = _user_state_entries(root)
    runtimes = _runtime_entries(root)
    models = _model_entries(root, model_hashes)
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "generator_version": __version__,
        "artifact": {
            "kind": artifact_kind,
            "version": version or __version__,
            "platform": platform,
            "created": created
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_sha256": source_sha256.strip().lower(),
        },
        "entrypoints": names,
        "files": files,
        "user_state": user_state,
        "runtimes": runtimes,
        "models": models,
        "excluded": list(EXCLUDED_PATHS),
        "totals": {
            "files": len(files),
            "bytes": sum(entry["size"] for entry in files),
            "user_state": len(user_state),
            "runtimes": len(runtimes),
            "models": len(models),
        },
    }
    validate_manifest(manifest)
    return manifest


def write_manifest(root: Path, manifest: Mapping[str, Any]) -> Path:
    """Validate and atomically write ``install-manifest.json`` under *root*."""
    validate_manifest(manifest)
    root = Path(root)
    if root.is_symlink():
        raise ManifestError(f"artifact root must not be a symlink: {root}")
    if not root.is_dir():
        raise ManifestError(f"artifact root is not a directory: {root}")
    target = root / MANIFEST_NAME
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{MANIFEST_NAME}.", suffix=".tmp", dir=root
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(manifest_bytes(manifest))
        try:
            os.chmod(temporary_name, 0o644)
        except OSError:
            pass
        os.replace(temporary_name, target)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise ManifestError(
            f"could not write install manifest {target}: {exc}"
        ) from exc
    return target


def verify_manifest(
    root: Path,
    manifest: Optional[Mapping[str, Any]] = None,
    *,
    check_hashes: bool = True,
    check_assets: bool = False,
    check_extra: bool = False,
    check_required_assets: bool = True,
) -> VerificationReport:
    """Verify an unpacked artifact against its manifest.

    Mutable user state is reported separately and never makes the artifact
    invalid.  Runtimes and models are checked for presence and size by default;
    pass ``check_assets=True`` to hash them as well.  ``check_extra=True``
    also rejects unlisted regular files; runtime and model trees remain
    opaque, and symlinks are left to the installer.  Recovery restores may
    pass ``check_required_assets=False`` while staging app-only files, then
    verify the preserved assets in the completed stage.
    """
    root = Path(root)
    if root.is_symlink():
        raise ManifestError(f"artifact root must not be a symlink: {root}")
    if not root.is_dir():
        raise ManifestError(f"artifact root is not a directory: {root}")
    if manifest is None:
        manifest = load_manifest(root / MANIFEST_NAME)
    else:
        validate_manifest(manifest)

    errors: List[str] = []
    changed: List[str] = []
    checked_files = 0
    checked_bytes = 0
    checked_assets = 0

    for entry in manifest["files"]:
        relative = str(entry["path"])
        path = root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            if entry["required"]:
                errors.append(f"missing file: {relative}")
            continue
        size = path.stat().st_size
        checked_files += 1
        checked_bytes += int(entry["size"])
        if size != int(entry["size"]):
            errors.append(
                f"size mismatch: {relative} expected {entry['size']} got {size}"
            )
            continue
        if check_hashes and entry["check"] == "sha256":
            actual = sha256_file(path)
            if actual != entry["sha256"]:
                errors.append(f"SHA-256 mismatch: {relative}")

    if check_extra:
        errors.extend(
            f"unexpected file: {relative}"
            for relative in _unexpected_files(root, manifest)
        )

    for entry in manifest["user_state"]:
        relative = str(entry["path"])
        path = root.joinpath(*PurePosixPath(relative).parts)
        if entry.get("present"):
            if not path.is_file() or path.is_symlink():
                changed.append(relative)
                continue
            if path.stat().st_size != int(entry["size"]):
                changed.append(relative)
            elif check_hashes and sha256_file(path) != entry.get("sha256"):
                changed.append(relative)

    for entry in manifest["runtimes"]:
        relative = str(entry["path"])
        directory = root.joinpath(*PurePosixPath(relative).parts)
        if directory.is_symlink() or not directory.is_dir():
            if entry["required"] and check_required_assets:
                errors.append(f"missing runtime: {relative}")
            continue
        binary = entry.get("binary")
        if not binary:
            continue
        binary_path = directory.joinpath(*PurePosixPath(binary).parts)
        if not binary_path.is_file() or binary_path.is_symlink():
            if entry["required"] and check_required_assets:
                errors.append(f"missing runtime binary: {relative}/{binary}")
            continue
        size = binary_path.stat().st_size
        if size != int(entry["binary_size"]):
            errors.append(f"size mismatch: {relative}/{binary}")
            continue
        if check_assets:
            checked_assets += 1
            if sha256_file(binary_path) != entry["binary_sha256"]:
                errors.append(f"SHA-256 mismatch: {relative}/{binary}")

        fingerprint = entry.get("fingerprint")
        if fingerprint:
            fingerprint_path = root.joinpath(
                *PurePosixPath(str(fingerprint)).parts
            )
            if fingerprint_path.is_symlink() or not fingerprint_path.is_file():
                if entry["required"] and check_required_assets:
                    errors.append(
                        f"missing runtime fingerprint: {fingerprint}"
                    )
            elif check_assets:
                checked_assets += 1
                if (
                    sha256_file(fingerprint_path)
                    != entry["fingerprint_sha256"]
                ):
                    errors.append(f"SHA-256 mismatch: {fingerprint}")

    for entry in manifest["models"]:
        relative = str(entry["path"])
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            if entry["required"] and check_required_assets:
                errors.append(f"missing model: {relative}")
            continue
        size = path.stat().st_size
        if size != int(entry["size"]):
            errors.append(
                f"size mismatch: {relative} expected {entry['size']} got {size}"
            )
            continue
        if check_assets:
            checked_assets += 1
            if sha256_file(path) != entry["sha256"]:
                errors.append(f"SHA-256 mismatch: {relative}")

    return VerificationReport(
        checked_files=checked_files,
        checked_bytes=checked_bytes,
        checked_assets=checked_assets,
        errors=tuple(errors),
        user_state_changed=tuple(changed),
    )


def _zip_member_hash(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(info) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_member_path(name: str) -> PurePosixPath:
    if not name:
        raise ManifestError("zip member has an empty name")
    if "\x00" in name or "\\" in name:
        raise ManifestError(f"unsafe zip member name: {name!r}")
    clean = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(clean)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or str(path) != clean
        or re.match(r"^[A-Za-z]:", clean)
        or path.parts[0].endswith(":")
    ):
        raise ManifestError(f"unsafe zip member name: {name!r}")
    return path


def _zip_member_sha256(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    errors: List[str],
    label: str,
) -> Optional[str]:
    try:
        return _zip_member_hash(archive, info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        errors.append(f"could not read {label}: {exc}")
        return None


def verify_zip(
    path: Path,
    *,
    check_hashes: bool = True,
    check_assets: bool = False,
) -> VerificationReport:
    """Verify a manifest zip, including its member set and assets.

    The manifest must sit at the archive root or inside one top-level
    directory.  Every file in the archive must be accounted for by the
    manifest (runtime internals are the one exception, since only the primary
    runtime binary is recorded).  This keeps a verified zip from carrying
    unlisted extra files into an install.
    """
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise ManifestError(f"{path} is an empty zip archive")
            seen: Dict[str, str] = {}
            members = []
            for info in infos:
                member = _zip_member_path(info.filename)
                members.append((info, member))
                key = member.as_posix().casefold()
                if key in seen:
                    raise ManifestError(
                        f"{path} contains duplicate zip member "
                        f"{info.filename!r}"
                    )
                seen[key] = info.filename
                if info.flag_bits & 0x1:
                    raise ManifestError(
                        f"{path} contains an encrypted zip member: "
                        f"{info.filename}"
                    )
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ManifestError(
                        f"{path} contains a symlink: {info.filename}"
                    )

            candidates = [
                (info, member)
                for info, member in members
                if not info.is_dir()
                and member.name == MANIFEST_NAME
                and len(member.parts) <= 2
            ]
            if len(candidates) != 1:
                raise ManifestError(
                    f"{path} must contain exactly one {MANIFEST_NAME} "
                    "at its root or inside one top-level directory"
                )
            manifest_info, manifest_member = candidates[0]
            prefix = (
                ""
                if len(manifest_member.parts) == 1
                else f"{manifest_member.parts[0]}/"
            )
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise ManifestError(
                    f"{MANIFEST_NAME} in {path} is unreasonably large"
                )
            try:
                manifest = parse_manifest(archive.read(manifest_info))
            except (OSError, KeyError, RuntimeError) as exc:
                raise ManifestError(
                    f"could not read {MANIFEST_NAME} from {path}: {exc}"
                ) from exc

            errors: List[str] = []
            changed: List[str] = []
            checked_files = 0
            checked_bytes = 0
            checked_assets = 0

            files: Dict[str, Mapping[str, Any]] = {}
            for entry in manifest["files"]:
                files[prefix + str(entry["path"])] = entry
            user_state: Dict[str, Mapping[str, Any]] = {
                prefix + str(entry["path"]): entry
                for entry in manifest["user_state"]
                if entry.get("present")
            }
            models: Dict[str, Mapping[str, Any]] = {
                prefix + str(entry["path"]): entry
                for entry in manifest["models"]
            }
            runtime_roots: List[str] = []
            runtime_assets: Dict[str, Mapping[str, Any]] = {}
            for entry in manifest["runtimes"]:
                base = prefix + str(entry["path"])
                runtime_roots.append(f"{base}/")
                binary = entry.get("binary")
                if binary:
                    runtime_assets[f"{base}/{binary}"] = entry
                fingerprint = entry.get("fingerprint")
                if fingerprint:
                    runtime_assets[prefix + str(fingerprint)] = entry

            manifest_name = prefix + MANIFEST_NAME
            allowed = (
                set(files)
                | set(user_state)
                | set(models)
                | set(runtime_assets)
                | {manifest_name}
            )
            for info in infos:
                name = info.filename
                if info.is_dir():
                    if prefix and name != prefix and not name.startswith(prefix):
                        errors.append(f"unexpected directory: {name}")
                    continue
                if name in allowed:
                    continue
                if any(name.startswith(root) for root in runtime_roots):
                    continue
                display = (
                    name[len(prefix):]
                    if prefix and name.startswith(prefix)
                    else name
                )
                errors.append(f"unexpected file: {display}")

            for name, entry in files.items():
                relative = str(entry["path"])
                try:
                    info = archive.getinfo(name)
                except KeyError:
                    if entry["required"]:
                        errors.append(f"missing file: {relative}")
                    continue
                if info.is_dir():
                    if entry["required"]:
                        errors.append(f"missing file: {relative}")
                    continue
                if info.file_size != int(entry["size"]):
                    errors.append(
                        f"size mismatch: {relative} expected "
                        f"{entry['size']} got {info.file_size}"
                    )
                    continue
                checked_files += 1
                checked_bytes += int(entry["size"])
                if check_hashes and entry["check"] == "sha256":
                    actual = _zip_member_sha256(
                        archive, info, errors, relative
                    )
                    if actual is not None and actual != entry["sha256"]:
                        errors.append(f"SHA-256 mismatch: {relative}")

            for name, entry in user_state.items():
                relative = str(entry["path"])
                try:
                    info = archive.getinfo(name)
                except KeyError:
                    changed.append(relative)
                    continue
                if info.is_dir() or info.file_size != int(entry["size"]):
                    changed.append(relative)
                    continue
                if check_hashes:
                    actual = _zip_member_sha256(
                        archive, info, errors, relative
                    )
                    if actual is not None and actual != entry.get("sha256"):
                        changed.append(relative)

            for entry in manifest["runtimes"]:
                relative = str(entry["path"])
                base = prefix + relative
                present = any(
                    info.filename == base
                    or info.filename.startswith(f"{base}/")
                    for info in infos
                )
                if not present:
                    if entry["required"]:
                        errors.append(f"missing runtime: {relative}")
                    continue
                binary = entry.get("binary")
                if binary:
                    label = f"{relative}/{binary}"
                    try:
                        info = archive.getinfo(f"{base}/{binary}")
                    except KeyError:
                        if entry["required"]:
                            errors.append(f"missing runtime binary: {label}")
                    else:
                        if info.is_dir():
                            if entry["required"]:
                                errors.append(f"missing runtime binary: {label}")
                        elif info.file_size != int(entry["binary_size"]):
                            errors.append(f"size mismatch: {label}")
                        elif check_assets:
                            checked_assets += 1
                            actual = _zip_member_sha256(
                                archive, info, errors, label
                            )
                            if (
                                actual is not None
                                and actual != entry["binary_sha256"]
                            ):
                                errors.append(f"SHA-256 mismatch: {label}")

                fingerprint = entry.get("fingerprint")
                if fingerprint:
                    label = str(fingerprint)
                    try:
                        info = archive.getinfo(prefix + label)
                    except KeyError:
                        if entry["required"]:
                            errors.append(f"missing runtime fingerprint: {label}")
                    else:
                        if info.is_dir():
                            if entry["required"]:
                                errors.append(
                                    f"missing runtime fingerprint: {label}"
                                )
                        elif check_assets:
                            checked_assets += 1
                            actual = _zip_member_sha256(
                                archive, info, errors, label
                            )
                            if (
                                actual is not None
                                and actual != entry["fingerprint_sha256"]
                            ):
                                errors.append(f"SHA-256 mismatch: {label}")

            for name, entry in models.items():
                relative = str(entry["path"])
                try:
                    info = archive.getinfo(name)
                except KeyError:
                    if entry["required"]:
                        errors.append(f"missing model: {relative}")
                    continue
                if info.is_dir():
                    if entry["required"]:
                        errors.append(f"missing model: {relative}")
                    continue
                if info.file_size != int(entry["size"]):
                    errors.append(
                        f"size mismatch: {relative} expected "
                        f"{entry['size']} got {info.file_size}"
                    )
                    continue
                if check_assets:
                    checked_assets += 1
                    actual = _zip_member_sha256(
                        archive, info, errors, relative
                    )
                    if actual is not None and actual != entry["sha256"]:
                        errors.append(f"SHA-256 mismatch: {relative}")

            return VerificationReport(
                checked_files=checked_files,
                checked_bytes=checked_bytes,
                checked_assets=checked_assets,
                errors=tuple(errors),
                user_state_changed=tuple(changed),
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManifestError(f"not a valid zip archive: {path}: {exc}") from exc
