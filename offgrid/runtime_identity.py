"""Deterministic runtime identity for the Python and llama.cpp trees.

R0 is deliberately read-only with respect to installation and updates.  The
module can describe a runtime tree, build and persist its version-2 fingerprint,
and compare fingerprints without starting either runtime binary.

A fingerprint separates three kinds of identity:

* ``tree_digest`` identifies the immutable runtime payload with
  ``offgrid-tree-v1``;
* ``runtime_id`` is a deterministic hash of the runtime artifact fields;
* ``installation_id`` is a random local identifier that must not affect
  ``runtime_id``;
* ``identity_id`` combines the app version, app manifest digest, and both
  runtime IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import __version__, install_manifest

SCHEMA = "offgrid.runtime-identity/2"
TREE_SCHEMA = "offgrid-tree-v1"
FINGERPRINT_NAME = ".offgrid-runtime.json"
KINDS = ("python", "llama")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PLATFORM_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
MAX_FINGERPRINT_BYTES = 64 * 1024
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

EXCLUDED_NAMES = {FINGERPRINT_NAME}
EXCLUDED_DIRECTORIES = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


class IdentityError(ValueError):
    """A runtime tree, fingerprint, or identity is malformed."""


@dataclass(frozen=True)
class TreeReport:
    schema: str
    digest: str
    files: int
    bytes: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "digest": self.digest,
            "files": self.files,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class RuntimeReport:
    kind: str
    root: Optional[Path]
    present: bool
    recorded: bool
    legacy: bool
    verified: bool
    version: str
    platform: str
    runtime_id: str
    tree_digest: str
    tree_files: int
    binary_path: str
    tree_verified: bool
    binary_verified: bool
    error: str

    def summary(self) -> str:
        if not self.present:
            return "not installed"
        if self.verified:
            return f"{self.version or 'present'} verified ({self.runtime_id[:12]})"
        if not self.recorded:
            if self.legacy:
                return f"{self.version or 'present'} legacy fingerprint; not recorded"
            return f"{self.version or 'present'} present; no version-2 fingerprint"
        return f"unverified: {self.error}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "root": str(self.root) if self.root is not None else "",
            "present": self.present,
            "recorded": self.recorded,
            "legacy": self.legacy,
            "verified": self.verified,
            "version": self.version,
            "platform": self.platform,
            "runtime_id": self.runtime_id,
            "tree_digest": self.tree_digest,
            "tree_files": self.tree_files,
            "binary_path": self.binary_path,
            "tree_verified": self.tree_verified,
            "binary_verified": self.binary_verified,
            "error": self.error,
            "summary": self.summary(),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise IdentityError(f"could not read {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise IdentityError(f"{label} must be a non-empty string")
    return value


def _hash(value: Any, label: str) -> str:
    text = _string(value, label)
    if not HASH_RE.fullmatch(text):
        raise IdentityError(f"{label} must be a lowercase SHA-256")
    return text


def _optional_hash(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise IdentityError(f"{label} must be a string")
    if not value:
        return ""
    return _hash(value, label)


def _safe_relative(value: Any, label: str) -> str:
    text = _string(value, label)
    if "\x00" in text or "\\" in text or "\t" in text or "\n" in text:
        raise IdentityError(f"{label} contains an unsafe character")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise IdentityError(f"{label} must be relative: {text!r}")
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts:
        raise IdentityError(f"{label} must be a relative path: {text!r}")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise IdentityError(f"{label} contains an unsafe component: {text!r}")
    if str(relative) != text:
        raise IdentityError(f"{label} is not a canonical path: {text!r}")
    return text


def _created(value: Optional[str]) -> str:
    if value is None:
        value = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = _string(value, "created")
    if not CREATED_RE.fullmatch(text):
        raise IdentityError("created must be an RFC 3339 UTC timestamp")
    return text


def _default_capabilities(kind: str) -> Dict[str, Any]:
    if kind == "python":
        return {"python_min": "3.9", "python_max": "3.12"}
    if kind == "llama":
        return {"api": "openai-compatible", "server": "llama.cpp"}
    raise IdentityError(f"unsupported runtime kind: {kind!r}")


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise IdentityError(f"unsupported runtime kind: {kind!r}")
    return kind


def _walk_entries(root: Path) -> Tuple[List[Tuple[str, str, bytes]], int]:
    """Return canonical tree records and total regular-file bytes."""
    if root.is_symlink() or not root.is_dir():
        raise IdentityError(f"runtime is not a real directory: {root}")

    records: List[Tuple[str, str, bytes]] = []
    total_bytes = 0

    def walk_error(exc: OSError) -> None:
        raise IdentityError(f"could not walk runtime tree: {exc}") from exc

    for current, dirnames, filenames in os.walk(
        str(root), followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        kept: List[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            if path.is_symlink():
                raise IdentityError(
                    f"runtime contains a symlinked directory: "
                    f"{path.relative_to(root).as_posix()}"
                )
            if name not in EXCLUDED_DIRECTORIES:
                kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if (
                name in EXCLUDED_NAMES
                or name.endswith(EXCLUDED_SUFFIXES)
                or any(part in EXCLUDED_DIRECTORIES for part in PurePosixPath(relative).parts)
            ):
                continue
            if "\t" in relative or "\n" in relative or "\x00" in relative:
                raise IdentityError(f"runtime path contains an unsafe character: {relative!r}")
            try:
                if path.is_symlink():
                    target = os.readlink(path)
                    if (
                        not target
                        or target.startswith("/")
                        or re.match(r"^[A-Za-z]:", target)
                        or "\t" in target
                        or "\n" in target
                    ):
                        raise IdentityError(
                            f"runtime symlink has an unsafe target: {relative}"
                        )
                    records.append(("l", relative, target.encode("utf-8")))
                elif path.is_file():
                    size = path.stat().st_size
                    mode = stat.S_IMODE(path.stat().st_mode)
                    executable = "x" if mode & 0o111 else "-"
                    digest = sha256_file(path)
                    total_bytes += size
                    records.append(
                        (
                            "f",
                            relative,
                            (
                                f"f\t{relative}\t{executable}\t{size}\t{digest}\n"
                            ).encode("utf-8"),
                        )
                    )
                else:
                    raise IdentityError(
                        f"runtime contains a non-regular entry: {relative}"
                    )
            except OSError as exc:
                raise IdentityError(f"could not inspect runtime entry {relative}: {exc}") from exc
    return records, total_bytes


def tree_digest(root: Path) -> TreeReport:
    """Return the deterministic ``offgrid-tree-v1`` digest of *root*.

    Local metadata (``.offgrid-runtime.json``) and generated bytecode caches
    are excluded.  Regular files and symlinks are included.  The walk is
    top-down: each directory's files and symlinks are sorted bytewise and
    emitted before its sorted subdirectories.  Executable bits are normalized
    to ``x`` or ``-``.
    """
    root = Path(root).expanduser()
    if root.is_symlink():
        raise IdentityError(f"runtime must not be a symlink: {root}")
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise IdentityError(f"could not resolve runtime root: {exc}") from exc
    records, total_bytes = _walk_entries(root)
    body = [f"{TREE_SCHEMA}\n".encode("ascii")]
    body.extend(record[2] for record in records)
    return TreeReport(
        schema=TREE_SCHEMA,
        digest=_sha256(b"".join(body)),
        files=len(records),
        bytes=total_bytes,
    )


def _binary_file(root: Path, relative: str) -> Path:
    """Return the file to hash, rejecting a symlink that leaves *root*."""
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise IdentityError(
                f"runtime binary symlink leaves the runtime: {relative}"
            ) from exc
        if not resolved.is_file():
            raise IdentityError(f"runtime binary is not a file: {relative}")
        return resolved
    if not path.is_file():
        raise IdentityError(f"runtime binary is missing: {relative}")
    return path


def _find_binary(root: Path, kind: str) -> str:
    if kind == "python":
        candidates = ["python.exe", "bin/python3", "bin/python", "python3"]
    elif kind == "llama":
        candidates = ["llama-server.exe", "llama-server"]
    else:
        raise IdentityError(f"unsupported runtime kind: {kind!r}")
    if kind == "python":
        for directory in (root / "bin", root):
            for path in sorted(directory.glob("python3.*")):
                if re.fullmatch(r"python3\.\d+", path.name):
                    candidates.append(path.relative_to(root).as_posix())
    for name in candidates:
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            _binary_file(root, name)
        except IdentityError:
            continue
        return candidate.relative_to(root).as_posix()
    for name in candidates:
        found = sorted(path for path in root.rglob(name) if path.is_file())
        for path in found:
            relative = path.relative_to(root).as_posix()
            try:
                _binary_file(root, relative)
            except IdentityError:
                continue
            return relative
    raise IdentityError(f"no primary {kind} binary found in {root}")


def _runtime_material(fingerprint: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "kind": str(fingerprint["kind"]),
        "platform": str(fingerprint["platform"]),
        "version": str(fingerprint["version"]),
        "source_release": str(fingerprint.get("source_release") or ""),
        "source_asset": str(fingerprint.get("source_asset") or ""),
        "archive_sha256": str(fingerprint.get("archive_sha256") or ""),
        "binary_path": str(fingerprint["binary_path"]),
        "binary_size": int(fingerprint["binary_size"]),
        "binary_sha256": str(fingerprint["binary_sha256"]),
        "tree_schema": str(fingerprint["tree_schema"]),
        "tree_digest": str(fingerprint["tree_digest"]),
        "tree_files": int(fingerprint["tree_files"]),
        "capabilities": dict(fingerprint["capabilities"]),
    }


def runtime_id(fingerprint: Mapping[str, Any]) -> str:
    """Return the deterministic runtime ID for a validated fingerprint."""
    return _sha256(_canonical_json(_runtime_material(fingerprint)))


def identity_id(
    app_version: str,
    app_manifest_digest: str,
    python_runtime_id: str,
    llama_runtime_id: str,
) -> str:
    """Return the deterministic combined app/runtime identity, if complete."""
    values = (
        app_version,
        app_manifest_digest,
        python_runtime_id,
        llama_runtime_id,
    )
    if any(not isinstance(value, str) or not value for value in values):
        return ""
    for value, label in (
        (app_manifest_digest, "app_manifest_digest"),
        (python_runtime_id, "python_runtime_id"),
        (llama_runtime_id, "llama_runtime_id"),
    ):
        if not HASH_RE.fullmatch(value):
            raise IdentityError(f"{label} must be a lowercase SHA-256")
    return _sha256(
        _canonical_json(
            {
                "app_version": app_version,
                "app_manifest_digest": app_manifest_digest,
                "python_runtime_id": python_runtime_id,
                "llama_runtime_id": llama_runtime_id,
            }
        )
    )


def build_fingerprint(
    directory: Path,
    *,
    kind: str,
    platform: str,
    version: str,
    binary: Optional[str] = None,
    source_release: str = "",
    source_asset: str = "",
    archive_sha256: str = "",
    capabilities: Optional[Mapping[str, Any]] = None,
    installer_version: str = __version__,
    created: Optional[str] = None,
    installation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a version-2 fingerprint from an extracted runtime tree."""
    kind = _check_kind(kind)
    root = Path(directory).expanduser()
    if root.is_symlink():
        raise IdentityError(f"runtime must not be a symlink: {root}")
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise IdentityError(f"could not resolve runtime root: {exc}") from exc
    if not root.is_dir() or root.is_symlink():
        raise IdentityError(f"runtime is not a real directory: {root}")
    platform_text = _string(platform, "platform")
    if not PLATFORM_RE.fullmatch(platform_text):
        raise IdentityError("platform contains unsupported characters")
    version_text = _string(version, "version")
    if binary is None:
        binary = _find_binary(root, kind)
    binary_path = _safe_relative(binary, "binary_path")
    binary_file = _binary_file(root, binary_path)
    tree = tree_digest(root)
    archive_text = _optional_hash(archive_sha256, "archive_sha256")
    if capabilities is None:
        caps = _default_capabilities(kind)
    else:
        if not isinstance(capabilities, Mapping):
            raise IdentityError("capabilities must be an object")
        caps = dict(capabilities)
    try:
        json.dumps(caps, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise IdentityError(f"capabilities are not JSON-serializable: {exc}") from exc
    _string(installer_version, "installer_version")
    source_release = _string(source_release, "source_release", allow_empty=True)
    source_asset = _string(source_asset, "source_asset", allow_empty=True)
    created_text = _created(created)
    installation = installation_id or uuid.uuid4().hex
    if not isinstance(installation, str) or not installation:
        raise IdentityError("installation_id must be a non-empty string")
    payload: Dict[str, Any] = {
        "schema": SCHEMA,
        "kind": kind,
        "platform": platform_text,
        "version": version_text,
        "source_release": source_release,
        "source_asset": source_asset,
        "archive_sha256": archive_text,
        "binary_path": binary_path,
        "binary_size": binary_file.stat().st_size,
        "binary_sha256": sha256_file(binary_file),
        "tree_schema": tree.schema,
        "tree_digest": tree.digest,
        "tree_files": tree.files,
        "capabilities": caps,
        "installer_version": installer_version,
        "created": created_text,
        "installation_id": installation,
    }
    payload["runtime_id"] = runtime_id(payload)
    validate_fingerprint(payload)
    return payload


def validate_fingerprint(payload: Mapping[str, Any]) -> None:
    """Validate a version-2 fingerprint and all deterministic IDs."""
    expected = {
        "schema",
        "kind",
        "platform",
        "version",
        "source_release",
        "source_asset",
        "archive_sha256",
        "binary_path",
        "binary_size",
        "binary_sha256",
        "tree_schema",
        "tree_digest",
        "tree_files",
        "capabilities",
        "installer_version",
        "created",
        "installation_id",
        "runtime_id",
    }
    if set(payload) != expected:
        raise IdentityError(
            "runtime fingerprint must contain exactly: " + ", ".join(sorted(expected))
        )
    if payload.get("schema") != SCHEMA:
        raise IdentityError(f"unsupported runtime identity schema: {payload.get('schema')!r}")
    _check_kind(str(payload.get("kind")))
    platform_text = _string(payload.get("platform"), "platform")
    if not PLATFORM_RE.fullmatch(platform_text):
        raise IdentityError("platform contains unsupported characters")
    _string(payload.get("version"), "version")
    _string(payload.get("source_release"), "source_release", allow_empty=True)
    _string(payload.get("source_asset"), "source_asset", allow_empty=True)
    _optional_hash(payload.get("archive_sha256"), "archive_sha256")
    _safe_relative(payload.get("binary_path"), "binary_path")
    _size(payload.get("binary_size"), "binary_size")
    _hash(payload.get("binary_sha256"), "binary_sha256")
    if payload.get("tree_schema") != TREE_SCHEMA:
        raise IdentityError(f"unsupported tree schema: {payload.get('tree_schema')!r}")
    _hash(payload.get("tree_digest"), "tree_digest")
    _size(payload.get("tree_files"), "tree_files")
    if not isinstance(payload.get("capabilities"), dict):
        raise IdentityError("capabilities must be an object")
    _string(payload.get("installer_version"), "installer_version")
    created = _string(payload.get("created"), "created")
    if not CREATED_RE.fullmatch(created):
        raise IdentityError("created must be an RFC 3339 UTC timestamp")
    _string(payload.get("installation_id"), "installation_id")
    expected_id = _string(payload.get("runtime_id"), "runtime_id")
    if not HASH_RE.fullmatch(expected_id):
        raise IdentityError("runtime_id must be a lowercase SHA-256")
    actual_id = runtime_id(payload)
    if actual_id != expected_id:
        raise IdentityError("runtime_id does not match the runtime fields")


def _size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IdentityError(f"{label} must be a non-negative integer")
    return value


def write_fingerprint(directory: Path, payload: Mapping[str, Any], *, force: bool = True) -> Path:
    """Atomically write a validated version-2 fingerprint into a runtime."""
    validate_fingerprint(payload)
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise IdentityError(f"runtime is not a real directory: {root}")
    target = root / FINGERPRINT_NAME
    if target.exists() and not force:
        raise IdentityError(f"{target} already exists; pass force=True to replace it")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_canonical_json(payload))
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise IdentityError(f"could not write runtime fingerprint {target}: {exc}") from exc
    return target


def read_fingerprint(directory: Path) -> Dict[str, Any]:
    """Read and validate a version-2 fingerprint from a runtime tree."""
    root = Path(directory)
    target = root / FINGERPRINT_NAME
    if target.is_symlink() or not target.is_file():
        raise IdentityError(f"runtime has no regular {FINGERPRINT_NAME}: {root}")
    try:
        if target.stat().st_size > MAX_FINGERPRINT_BYTES:
            raise IdentityError("runtime fingerprint is too large")
        data = target.read_bytes()
    except OSError as exc:
        raise IdentityError(f"could not read runtime fingerprint {target}: {exc}") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityError(f"invalid runtime fingerprint JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise IdentityError("runtime fingerprint must be an object")
    validate_fingerprint(payload)
    return dict(payload)


def _legacy_fingerprint(directory: Path) -> Optional[Dict[str, Any]]:
    target = directory / FINGERPRINT_NAME
    if target.is_symlink() or not target.is_file():
        return None
    try:
        if target.stat().st_size > MAX_FINGERPRINT_BYTES:
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return None
    return payload


def inspect_runtime(directory: Path, kind: str, *, full: bool = True) -> RuntimeReport:
    """Describe a runtime without executing its binary.

    ``full=True`` recomputes the tree digest, which is what install, update and
    doctor use.  ``full=False`` is the quick launch check: schema, platform,
    binary hash and recorded runtime ID only.
    """
    kind = _check_kind(kind)
    root = Path(directory).expanduser()
    if not root.is_dir() or root.is_symlink():
        return RuntimeReport(
            kind=kind,
            root=root,
            present=False,
            recorded=False,
            legacy=False,
            verified=False,
            version="",
            platform="",
            runtime_id="",
            tree_digest="",
            tree_files=0,
            binary_path="",
            tree_verified=False,
            binary_verified=False,
            error="",
        )

    tree: Optional[TreeReport] = None
    tree_error = ""
    if full:
        try:
            tree = tree_digest(root)
        except IdentityError as exc:
            tree_error = str(exc)

    payload: Optional[Dict[str, Any]] = None
    fingerprint_error = ""
    try:
        payload = read_fingerprint(root)
    except IdentityError as exc:
        fingerprint_error = str(exc)

    legacy = False
    version = str((payload or {}).get("version") or "")
    platform_text = str((payload or {}).get("platform") or "")
    runtime_id_value = str((payload or {}).get("runtime_id") or "")
    binary_path = str((payload or {}).get("binary_path") or "")
    if payload is None:
        old = _legacy_fingerprint(root)
        if old is not None:
            legacy = True
            version = str(old.get("version") or "")
            platform_text = str(old.get("platform") or "")
            fingerprint_error = "version-2 fingerprint is not recorded"

    tree_verified = bool(
        full
        and tree is not None
        and payload is not None
        and tree.digest == payload["tree_digest"]
    )
    binary_verified = False
    if payload is not None:
        try:
            binary = _binary_file(root, str(payload["binary_path"]))
            binary_verified = (
                binary.stat().st_size == payload["binary_size"]
                and sha256_file(binary) == payload["binary_sha256"]
            )
        except (IdentityError, OSError):
            binary_verified = False
    error = tree_error or fingerprint_error
    if payload is not None and not tree_verified and not error:
        error = "runtime tree digest differs from the fingerprint"
    if payload is not None and not binary_verified and not error:
        error = "runtime binary digest differs from the fingerprint"
    verified = bool(
        payload is not None
        and binary_verified
        and (tree_verified if full else True)
    )
    if verified:
        error = ""
    return RuntimeReport(
        kind=kind,
        root=root,
        present=True,
        recorded=payload is not None,
        legacy=legacy,
        verified=verified,
        version=version,
        platform=platform_text,
        runtime_id=runtime_id_value,
        tree_digest=(
            tree.digest
            if tree is not None
            else (str((payload or {}).get("tree_digest") or ""))
        ),
        tree_files=(
            tree.files
            if tree is not None
            else int((payload or {}).get("tree_files") or 0)
        ),
        binary_path=binary_path,
        tree_verified=tree_verified,
        binary_verified=binary_verified,
        error=error,
    )


def find_app_root(workspace: Optional[Path] = None) -> Optional[Path]:
    """Find a directory containing either private runtime."""
    candidates: List[Path] = []
    if workspace is not None:
        candidates.append(Path(workspace))
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parent.parent)
    candidates.append(Path.home() / ".offgrid" / "easy")
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "runtime" / "python").is_dir() or (
            resolved / "runtime" / "llama"
        ).is_dir():
            return resolved
    return None


def inspect_app(workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Return the current app identity and both runtime reports."""
    root = find_app_root(workspace)
    app_digest = ""
    manifest_error = ""
    if root is not None:
        try:
            manifest = install_manifest.load_manifest(root / install_manifest.MANIFEST_NAME)
            app_digest = install_manifest.manifest_digest(manifest)
        except (OSError, install_manifest.ManifestError) as exc:
            manifest_error = str(exc)
    if root is None:
        python_root = llama_root = None
    else:
        python_root = root / "runtime" / "python"
        llama_root = root / "runtime" / "llama"
    python = inspect_runtime(python_root, "python") if python_root is not None else None
    llama = inspect_runtime(llama_root, "llama") if llama_root is not None else None
    python_id = python.runtime_id if python and python.verified else ""
    llama_id = llama.runtime_id if llama and llama.verified else ""
    combined = identity_id(__version__, app_digest, python_id, llama_id)
    return {
        "app_version": __version__,
        "app_manifest_digest": app_digest,
        "identity_id": combined,
        "root": str(root) if root is not None else "",
        "runtimes": {
            "python": (
                python.as_dict()
                if python is not None
                else {
                    "kind": "python",
                    "root": "",
                    "present": False,
                    "recorded": False,
                    "legacy": False,
                    "verified": False,
                    "version": "",
                    "platform": "",
                    "runtime_id": "",
                    "tree_digest": "",
                    "tree_files": 0,
                    "binary_path": "",
                    "tree_verified": False,
                    "binary_verified": False,
                    "error": "",
                    "summary": "not installed",
                }
            ),
            "llama": (
                llama.as_dict()
                if llama is not None
                else {
                    "kind": "llama",
                    "root": "",
                    "present": False,
                    "recorded": False,
                    "legacy": False,
                    "verified": False,
                    "version": "",
                    "platform": "",
                    "runtime_id": "",
                    "tree_digest": "",
                    "tree_files": 0,
                    "binary_path": "",
                    "tree_verified": False,
                    "binary_verified": False,
                    "error": "",
                    "summary": "not installed",
                }
            ),
        },
        "manifest_error": manifest_error,
    }
