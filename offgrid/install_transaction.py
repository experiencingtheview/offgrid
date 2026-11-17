"""Transactional installation for portable OffGrid bundles and full installs.

The install is deliberately built around one destination and one lock:

1. preflight the destination and the source manifest;
2. acquire an exclusive install lock;
3. copy or extract the candidate into a sibling staging directory;
4. verify the staged manifest and run a small launch health check;
5. capture a verified recovery archive of the previous install while it is
   still untouched; a corrupt current install is moved to quarantine instead;
6. move the previous install aside and rename the staged tree into place;
7. health-check the activated tree and write ``install-state.json``;
8. prune old recovery archives to the newest three per destination;
9. remove the previous tree only after the new install is committed.

A failed first install removes the staged tree and leaves no destination. A
failed replacement restores the previous tree (or quarantined current tree) and
its state before returning. Large ``models/`` and ``runtime/`` trees are linked
from the previous install when the old and new manifests prove they are
unchanged, rather than copied. Reuse is verified against the manifest and falls
back to the bundle copy when a link or reuse check fails.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

from . import install_manifest, recovery

STATE_NAME = "install-state.json"
STATE_SCHEMA = "offgrid.install-state/1"
JOURNAL_SCHEMA = "offgrid.install-transaction/1"
TRANSACTION_DIR = ".offgrid-install"
HEALTH_TIMEOUT = 30
SKIP_COPY_NAMES = {
    "install-state.json",
    ".downloads",
    ".pytest_cache",
    "__pycache__",
}
SKIP_COPY_SUFFIXES = (".pyc", ".pyo")


class InstallError(RuntimeError):
    """An install could not be staged, verified, activated or rolled back."""


class InstallBusy(InstallError):
    """Another install holds the destination lock."""


class HealthCheckError(InstallError):
    """The staged or activated install failed its health check."""


@dataclass(frozen=True)
class InstallReport:
    destination: Path
    transaction_id: str
    install_id: str
    manifest_digest: str
    status: str
    previous_install: Dict[str, Any]
    recovery_bundle: str = ""
    recovery_generation: int = 0
    quarantine: str = ""
    quarantine_reason: str = ""
    retention: Optional["RecoveryRetentionReport"] = None

    @property
    def committed(self) -> bool:
        return self.status == "committed"


@dataclass(frozen=True)
class RecoveryRetentionReport:
    """The result of a best-effort recovery-archive pruning pass."""

    kept: Tuple[Path, ...]
    pruned: Tuple[Path, ...]
    errors: Tuple[str, ...]
    protected: Tuple[Path, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kept": [str(path) for path in self.kept],
            "pruned": [str(path) for path in self.pruned],
            "errors": list(self.errors),
            "protected": [str(path) for path in self.protected],
        }


@dataclass(frozen=True)
class RecoveryArchiveStatus:
    """One retained recovery archive and its restore state."""

    generation: int
    path: Path
    readiness: recovery.RecoveryReadinessReport

    @property
    def valid(self) -> bool:
        return self.readiness.report is not None

    @property
    def ready(self) -> bool:
        return self.readiness.ready

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "generation": self.generation,
            "path": str(self.path),
            "valid": self.valid,
            "ready": self.ready,
            "reason": self.readiness.reason,
        }
        report = self.readiness.report
        if report is not None:
            payload.update(
                {
                    "version": report.version,
                    "install_id": report.install_id,
                    "manifest_digest": report.manifest_digest,
                    "files": report.files,
                    "bytes": report.bytes,
                    "config_sanitized": report.config_sanitized,
                }
            )
        return payload


@dataclass(frozen=True)
class RecoveryInventory:
    """Retained recovery archives for one install destination."""

    destination: Path
    archives: Tuple[RecoveryArchiveStatus, ...]
    latest_valid: Optional[Path]
    ready: bool
    reason: str
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ready,
            "destination": str(self.destination),
            "ready": self.ready,
            "reason": self.reason,
            "latest_valid": str(self.latest_valid) if self.latest_valid else "",
            "archives": [archive.as_dict() for archive in self.archives],
            "error": self.error,
        }


@dataclass(frozen=True)
class StorageCleanupCandidate:
    """One transaction or archive temporary considered by cleanup."""

    path: Path
    kind: str
    bytes: int
    age_days: float
    selected: bool
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "bytes": self.bytes,
            "age_days": round(self.age_days, 3),
            "selected": self.selected,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StorageCleanupReport:
    """A read-only plan for removing abandoned transaction temporaries."""

    destination: Path
    dry_run: bool
    max_age_days: float
    max_size_bytes: int
    candidates: Tuple[StorageCleanupCandidate, ...]
    preserved: Tuple[StorageCleanupCandidate, ...]
    errors: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.bytes for item in self.candidates)

    @property
    def preserved_bytes(self) -> int:
        return sum(item.bytes for item in self.preserved)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "destination": str(self.destination),
            "max_age_days": self.max_age_days,
            "max_size_bytes": self.max_size_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "preserved_bytes": self.preserved_bytes,
            "candidates": [item.as_dict() for item in self.candidates],
            "preserved": [item.as_dict() for item in self.preserved],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class QuarantineRecord:
    """One quarantined install tree and its retained diagnostic metadata."""

    transaction_id: str
    kind: str
    path: Optional[Path]
    state_dir: Optional[Path]
    metadata_path: Optional[Path]
    reason: str
    created: str
    age_days: float
    bytes: int
    present: bool
    metadata_valid: bool
    referenced: bool
    latest: bool
    retention: str
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "kind": self.kind,
            "path": str(self.path),
            "state_dir": str(self.state_dir) if self.state_dir else "",
            "metadata_path": str(self.metadata_path) if self.metadata_path else "",
            "reason": self.reason,
            "created": self.created,
            "age_days": round(self.age_days, 3),
            "bytes": self.bytes,
            "present": self.present,
            "metadata_valid": self.metadata_valid,
            "referenced": self.referenced,
            "latest": self.latest,
            "retention": self.retention,
            "error": self.error,
        }


@dataclass(frozen=True)
class QuarantineInventory:
    """Read-only view of the quarantine lifecycle for one destination."""

    destination: Path
    quarantines: Tuple[QuarantineRecord, ...]
    latest: Optional[Path]
    reason: str
    errors: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "destination": str(self.destination),
            "latest": str(self.latest) if self.latest else "",
            "reason": self.reason,
            "quarantines": [record.as_dict() for record in self.quarantines],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class QuarantineRetentionReport:
    """Result of planning or applying quarantine retention."""

    destination: Path
    dry_run: bool
    keep: int
    max_age_days: float
    kept: Tuple[Path, ...]
    pruned: Tuple[Path, ...]
    metadata_retained: Tuple[Path, ...]
    errors: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "destination": str(self.destination),
            "keep": self.keep,
            "max_age_days": self.max_age_days,
            "kept": [str(path) for path in self.kept],
            "pruned": [str(path) for path in self.pruned],
            "metadata_retained": [
                str(path) for path in self.metadata_retained
            ],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class SnapshotResult:
    """A recovery archive created for one transaction."""

    path: str = ""
    generation: int = 0


@dataclass(frozen=True)
class CurrentInstall:
    """The state of the destination before a replacement starts."""

    state: str
    reason: str = ""

    @property
    def corrupt(self) -> bool:
        return self.state == "corrupt"

    @property
    def reusable(self) -> bool:
        return self.state == "valid"


@dataclass(frozen=True)
class TransactionPaths:
    destination: Path
    root: Path
    key: str
    transaction_id: str
    lock: Path
    journal: Path
    stage: Path
    previous: Path
    quarantine: Path
    quarantine_state_dir: Path
    quarantine_metadata: Path
    state_dir: Path
    previous_state_dir: Path
    recovery: Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _absolute(path: Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path.cwd() / value
    return Path(os.path.abspath(str(value)))


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return (cleaned or "install")[:40]


def _key(destination: Path) -> str:
    digest = hashlib.sha256(str(destination).encode("utf-8")).hexdigest()[:10]
    return f"{_slug(destination.name)}-{digest}"


def transaction_paths(destination: Path, transaction_id: Optional[str] = None) -> TransactionPaths:
    destination = _absolute(destination)
    txid = transaction_id or uuid.uuid4().hex
    root = destination.parent / TRANSACTION_DIR
    key = _key(destination)
    return TransactionPaths(
        destination=destination,
        root=root,
        key=key,
        transaction_id=txid,
        lock=root / f"{key}.lock",
        journal=root / f"{key}.journal.json",
        stage=root / f"{key}.stage-{txid}",
        previous=root / f"{key}.previous-{txid}",
        quarantine=root / f"{key}.quarantine-{txid}",
        quarantine_state_dir=root / f"{key}.quarantine-state-{txid}",
        quarantine_metadata=root / f"{key}.quarantine-meta-{txid}.json",
        state_dir=root / f"{key}.state",
        previous_state_dir=root / f"{key}.previous-state",
        recovery=root / f"{key}.recovery",
    )


def prepare_directory_stage(destination: Path) -> TransactionPaths:
    """Create the transaction stage for a caller-assembled directory.

    The caller writes the complete bundle into ``result.stage`` and later
    passes the returned paths to :func:`install_directory` as ``staged=``.
    The transaction then verifies and activates that same tree, avoiding a
    second copy of large assets such as a GGUF model.

    This only prepares the hidden transaction directory; the destination is
    not touched.  Call :func:`discard_directory_stage` if assembly fails.
    """
    destination = _absolute(destination)
    if destination.is_symlink():
        raise InstallError(f"refusing a symlinked install destination: {destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise InstallError(f"install parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise InstallError(f"install parent is not writable: {parent}")
    paths = transaction_paths(destination)
    _ensure_root(paths)
    if paths.stage.exists() or paths.stage.is_symlink():
        raise InstallError(f"transaction stage already exists: {paths.stage}")
    paths.stage.mkdir(parents=False, exist_ok=False)
    return paths


def discard_directory_stage(staged: TransactionPaths) -> None:
    """Best-effort removal of a prepared stage that was never committed."""
    try:
        _remove_path(staged.stage)
    except OSError:
        pass
    _cleanup_root(staged)


RECOVERY_KEEP = 3
QUARANTINE_SCHEMA = "offgrid.quarantine/1"
QUARANTINE_KEEP = 1
QUARANTINE_MAX_AGE_DAYS = 30.0
CLEANUP_MAX_AGE_DAYS = 30.0
CLEANUP_MAX_BYTES = 1024 * 1024 * 1024


def recovery_archive_path(destination: Path, generation: int) -> Path:
    """Return the per-destination recovery archive path for *generation*.

    The archive is intentionally outside the destination's tree.  Generation
    zero is reserved for the legacy fixed ``{key}.recovery`` path; callers
    should allocate from one upward.
    """
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise ValueError("recovery archive generation must be an integer")
    if generation < 0 or generation > 99_999_999:
        raise ValueError("recovery archive generation is out of range")
    paths = transaction_paths(destination)
    return paths.root / f"{paths.key}.recovery-{generation:08d}"


def plan_recovery_snapshot(
    destination: Path,
    *,
    file_restore: bool = False,
) -> SnapshotResult:
    """Describe the safety snapshot a restore would create, without writing.

    Only a reusable managed install gets a recovery bundle.  An unmanaged
    destination has no install metadata to snapshot, and a corrupt destination
    is quarantined instead.  This is read-only; callers can include the result
    in a restore plan before asking the operator to apply it.
    """
    destination = _absolute(destination)
    paths = transaction_paths(destination)
    kind = "file" if file_restore else "directory"
    current = _inspect_current_install(destination, paths, kind)
    if not current.reusable:
        return SnapshotResult()
    generation, archive = allocate_recovery_archive(destination)
    return SnapshotResult(str(archive), generation)


def _state_path_for_retention(destination: Path) -> Path:
    """Return the deployed state file, or the pyz sidecar for file installs."""
    destination = _absolute(destination)
    if destination.is_dir():
        return destination / STATE_NAME
    return transaction_paths(destination).state_dir / STATE_NAME


def committed_recovery_archive(destination: Path) -> Optional[Path]:
    """Return the snapshot referenced by the committed install state, if any."""
    value, _error = _read_retention_pointer(
        _state_path_for_retention(destination), "install state"
    )
    return value


def _recovery_archive_entries(
    destination: Path,
) -> Tuple[TransactionPaths, list, Optional[str]]:
    paths = transaction_paths(destination)
    if not paths.root.exists():
        return paths, [], None
    if paths.root.is_symlink() or not paths.root.is_dir():
        return paths, [], f"transaction root is not a directory: {paths.root}"
    entries = []
    pattern = re.compile(
        rf"^{re.escape(paths.key)}\.recovery-(\d{{8}})$"
    )
    try:
        children = list(paths.root.iterdir())
    except OSError as exc:
        return paths, [], f"could not inspect recovery archives: {exc}"
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name == f"{paths.key}.recovery":
            entries.append((0, child))
            continue
        match = pattern.fullmatch(child.name)
        if match is None:
            continue
        entries.append((int(match.group(1)), child))
    entries.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return paths, entries, None


def recovery_inventory(
    destination: Path, *, confirm: bool = False
) -> RecoveryInventory:
    """List, verify and report restore-readiness of retained archives.

    This is read-only. Every retained generation is verified; a corrupt or
    partially written archive is listed with its error instead of hiding the
    good generations around it.  *confirm* is forwarded to the shared
    readiness check for newer, older or different-lineage destinations.
    """
    destination = _absolute(destination)
    _paths, entries, error = _recovery_archive_entries(destination)
    if error:
        return RecoveryInventory(
            destination=destination,
            archives=(),
            latest_valid=None,
            ready=False,
            reason=error,
            error=error,
        )

    archives: List[RecoveryArchiveStatus] = []
    for generation, path in entries:
        archives.append(
            RecoveryArchiveStatus(
                generation=generation,
                path=path,
                readiness=recovery.recovery_readiness(
                    path, destination, confirm=confirm
                ),
            )
        )
    latest = next((archive for archive in archives if archive.valid), None)
    if latest is None:
        reason = (
            "no recovery archives retained"
            if not archives
            else "no retained recovery archive verified"
        )
        return RecoveryInventory(
            destination=destination,
            archives=tuple(archives),
            latest_valid=None,
            ready=False,
            reason=reason,
        )
    ready = latest.ready
    reason = latest.readiness.reason
    if ready:
        reason = (
            f"latest valid recovery archive is generation {latest.generation}"
            f" and is restore-ready"
        )
    else:
        reason = f"latest valid recovery archive is not restore-ready: {reason}"
    return RecoveryInventory(
        destination=destination,
        archives=tuple(archives),
        latest_valid=latest.path,
        ready=ready,
        reason=reason,
    )


def restore_latest_recovery_archive(
    destination: Path,
    *,
    force: bool = False,
    asset_source: Optional[Path] = None,
    confirm: bool = False,
    expect_plan: Optional[str] = None,
) -> recovery.RecoveryRestoreReport:
    """Restore the newest retained archive that verifies."""
    inventory = recovery_inventory(destination)
    if inventory.latest_valid is None:
        raise InstallError(
            f"no valid recovery archive for {inventory.destination}: "
            f"{inventory.reason}"
        )
    return recovery.restore_recovery_archive(
        inventory.latest_valid,
        inventory.destination,
        force=force,
        asset_source=asset_source,
        confirm=confirm,
        expect_plan=expect_plan,
    )


def _cleanup_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _cleanup_references(
    destination: Path, paths: TransactionPaths
) -> Tuple[set, bool, list]:
    """Return paths referenced by an active journal or committed state."""
    references: set = set()
    errors = []
    unknown = False

    def add(value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        try:
            references.add(_cleanup_path_key(Path(value).expanduser()))
        except (OSError, RuntimeError, ValueError):
            pass

    journal_path = paths.journal
    if journal_path.exists() or journal_path.is_symlink():
        journal = _read_json(journal_path)
        if journal is None:
            unknown = True
            errors.append(f"could not read active journal: {journal_path}")
        else:
            for field in (
                "staging",
                "previous",
                "previous_state_dir",
                "quarantine",
                "quarantine_state_dir",
            ):
                add(journal.get(field))

    for state_path in (
        destination / STATE_NAME,
        paths.state_dir / STATE_NAME,
    ):
        if not state_path.exists() and not state_path.is_symlink():
            continue
        state = _read_json(state_path)
        if state is None:
            unknown = True
            errors.append(f"could not read active install state: {state_path}")
            continue
        for field in ("quarantine", "quarantine_state_dir"):
            add(state.get(field))

    return references, unknown, errors


def _cleanup_size(path: Path) -> int:
    """Measure a tree without following symlinks."""
    total = 0
    pending = [Path(path)]
    while pending:
        current = pending.pop()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISREG(info.st_mode):
            total += max(0, int(info.st_size))
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)
    return total


def _cleanup_candidates(paths: TransactionPaths) -> Tuple[list, list]:
    """Find only known per-destination transaction and archive temporaries."""
    found = []
    errors = []
    key = re.escape(paths.key)
    transaction = r"[A-Za-z0-9_-]+"
    root_patterns = (
        (
            "quarantine-state",
            re.compile(rf"^{key}\.quarantine-state-{transaction}$"),
        ),
        ("quarantine", re.compile(rf"^{key}\.quarantine-{transaction}$")),
        ("stage", re.compile(rf"^{key}\.stage-{transaction}$")),
        ("previous-state", re.compile(rf"^{key}\.previous-state$")),
        ("previous", re.compile(rf"^{key}\.previous-{transaction}$")),
        (
            "transaction-temp",
            re.compile(rf"^\.{key}\.[A-Za-z0-9_.-]+\.tmp$"),
        ),
    )
    state_patterns = (
        re.compile(
            r"^\.(?:install-state\.json|install-manifest\.json)"
            r"\.[A-Za-z0-9_.-]+\.tmp$"
        ),
    )
    archive = rf"{key}\.recovery(?:-\d{{8}})?"
    archive_patterns = (
        (
            "archive-backup",
            re.compile(rf"^\.{archive}\.old-[0-9a-fA-F]{{32}}$"),
        ),
        ("archive-temp", re.compile(rf"^\.{archive}\.[A-Za-z0-9_]+$")),
    )

    def classify(name: str, patterns: tuple) -> Optional[str]:
        for kind, pattern in patterns:
            if pattern.fullmatch(name):
                return kind
        return None

    root = paths.root
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        errors.append(f"transaction root is not a directory: {root}")
    elif root.is_dir():
        try:
            children = list(root.iterdir())
        except OSError as exc:
            errors.append(f"could not inspect transaction root {root}: {exc}")
            children = []
        for child in children:
            kind = classify(child.name, root_patterns)
            if kind is not None:
                found.append((kind, child))

    state_dir = paths.state_dir
    if state_dir.is_dir() and not state_dir.is_symlink():
        try:
            state_children = list(state_dir.iterdir())
        except OSError as exc:
            errors.append(f"could not inspect transaction state {state_dir}: {exc}")
            state_children = []
        for child in state_children:
            if any(pattern.fullmatch(child.name) for pattern in state_patterns):
                found.append(("state-temp", child))

    parent = paths.destination.parent
    if parent.is_dir() and not parent.is_symlink():
        try:
            parent_children = list(parent.iterdir())
        except OSError as exc:
            errors.append(f"could not inspect archive directory {parent}: {exc}")
            parent_children = []
        for child in parent_children:
            kind = classify(child.name, archive_patterns)
            if kind is not None:
                found.append((kind, child))

    return found, errors


def _cleanup_latest_quarantine_keys(
    paths: TransactionPaths, found: list
) -> set:
    """Return the newest quarantine tree and its state sidecar."""
    groups: Dict[str, list] = {}
    for kind, path in found:
        if kind == "quarantine-state":
            prefix = f"{paths.key}.quarantine-state-"
        elif kind == "quarantine":
            prefix = f"{paths.key}.quarantine-"
        else:
            continue
        transaction = path.name[len(prefix) :]
        if not transaction:
            continue
        try:
            modified = path.lstat().st_mtime
        except OSError:
            modified = 0.0
        groups.setdefault(transaction, []).append((modified, path))
    if not groups:
        return set()
    latest = max(
        groups,
        key=lambda transaction: (
            max(item[0] for item in groups[transaction]),
            transaction,
        ),
    )
    return {_cleanup_path_key(path) for _modified, path in groups[latest]}


def storage_cleanup(
    destination: Path,
    *,
    max_age_days: float = CLEANUP_MAX_AGE_DAYS,
    max_size_bytes: int = CLEANUP_MAX_BYTES,
    now: Optional[float] = None,
) -> StorageCleanupReport:
    """Plan removal of old abandoned transaction and archive temporaries.

    This function is deliberately read-only.  It removes nothing and creates
    nothing.  The newest quarantine pair is always retained as the latest
    diagnostic tree, and paths named by an active journal or install state are
    never selected.  Only paths older than ``max_age_days`` are eligible, and
    no more than ``max_size_bytes`` of eligible data is selected per pass.
    """
    if (
        isinstance(max_age_days, bool)
        or not isinstance(max_age_days, (int, float))
        or max_age_days <= 0
    ):
        raise ValueError("max_age_days must be greater than zero")
    if (
        isinstance(max_size_bytes, bool)
        or not isinstance(max_size_bytes, int)
        or max_size_bytes <= 0
    ):
        raise ValueError("max_size_bytes must be a positive integer")

    destination = _absolute(destination)
    paths = transaction_paths(destination)
    found, scan_errors = _cleanup_candidates(paths)
    references, unknown_active, reference_errors = _cleanup_references(
        destination, paths
    )
    errors = list(scan_errors) + list(reference_errors)
    protected = references | _cleanup_latest_quarantine_keys(paths, found)
    current = time.time() if now is None else float(now)

    preserved = []
    eligible = []
    for kind, path in found:
        path_key = _cleanup_path_key(path)
        if path.is_symlink():
            preserved.append(
                StorageCleanupCandidate(
                    path=path,
                    kind=kind,
                    bytes=0,
                    age_days=0.0,
                    selected=False,
                    reason="symlink",
                )
            )
            continue
        try:
            info = path.lstat()
            size = _cleanup_size(path)
        except OSError as exc:
            errors.append(f"could not inspect cleanup candidate {path}: {exc}")
            preserved.append(
                StorageCleanupCandidate(
                    path=path,
                    kind=kind,
                    bytes=0,
                    age_days=0.0,
                    selected=False,
                    reason=f"could not inspect: {exc}",
                )
            )
            continue

        age_days = max(0.0, (current - info.st_mtime) / 86400.0)
        candidate = StorageCleanupCandidate(
            path=path,
            kind=kind,
            bytes=size,
            age_days=age_days,
            selected=False,
        )
        if path_key in protected:
            preserved.append(
                replace(
                    candidate,
                    reason="active transaction or latest diagnostic quarantine",
                )
            )
        elif unknown_active:
            preserved.append(
                replace(
                    candidate,
                    reason="active transaction state is unreadable",
                )
            )
        elif age_days < max_age_days:
            preserved.append(
                replace(
                    candidate,
                    reason=f"younger than {max_age_days:g}-day age limit",
                )
            )
        else:
            eligible.append(candidate)

    eligible.sort(key=lambda item: (-item.age_days, str(item.path)))
    selected = []
    selected_bytes = 0
    for candidate in eligible:
        if selected_bytes + candidate.bytes > max_size_bytes:
            preserved.append(
                replace(candidate, reason="size limit")
            )
            continue
        selected.append(replace(candidate, selected=True))
        selected_bytes += candidate.bytes

    preserved.sort(key=lambda item: str(item.path))
    return StorageCleanupReport(
        destination=destination,
        dry_run=True,
        max_age_days=float(max_age_days),
        max_size_bytes=max_size_bytes,
        candidates=tuple(selected),
        preserved=tuple(preserved),
        errors=tuple(errors),
    )


def _quarantine_metadata_path(
    paths: TransactionPaths, transaction_id: Optional[str] = None
) -> Path:
    txid = transaction_id or paths.transaction_id
    return paths.root / f"{paths.key}.quarantine-meta-{txid}.json"


def _quarantine_diagnostics(
    destination: Path, paths: TransactionPaths, kind: str
) -> Dict[str, Any]:
    """Collect best-effort identity details before the tree is moved."""
    root = destination if kind == "directory" else paths.state_dir
    state_path = Path(root) / STATE_NAME
    state: Dict[str, Any] = {}
    if state_path.is_file() and not state_path.is_symlink():
        state = _read_json(state_path) or {}
    artifact = state.get("artifact")
    manifest = _manifest_for(Path(root))
    return {
        "install_id": str(state.get("install_id") or ""),
        "status": str(state.get("status") or ""),
        "manifest_digest": str(state.get("manifest_digest") or ""),
        "artifact": dict(artifact) if isinstance(artifact, dict) else {},
        "manifest_present": bool(manifest),
    }


def _quarantine_metadata_payload(
    paths: TransactionPaths,
    transaction_id: str,
    *,
    kind: str,
    reason: str,
    diagnostics: Optional[Mapping[str, Any]] = None,
    status: str = "quarantined",
) -> Dict[str, Any]:
    return {
        "schema": QUARANTINE_SCHEMA,
        "transaction_id": transaction_id,
        "kind": kind,
        "destination": str(paths.destination),
        "quarantine": str(
            paths.root / f"{paths.key}.quarantine-{transaction_id}"
        ),
        "quarantine_state_dir": (
            str(paths.root / f"{paths.key}.quarantine-state-{transaction_id}")
            if kind == "file"
            else ""
        ),
        "reason": reason[:500],
        "status": status,
        "created": _now(),
        "updated": _now(),
        "source": dict(diagnostics or {}),
    }


def _write_quarantine_metadata(
    paths: TransactionPaths,
    *,
    kind: str,
    reason: str,
    diagnostics: Optional[Mapping[str, Any]] = None,
) -> Optional[Path]:
    """Record quarantine metadata without letting diagnostics block install."""
    target = _quarantine_metadata_path(paths)
    if target.is_symlink():
        return None
    payload = _quarantine_metadata_payload(
        paths,
        paths.transaction_id,
        kind=kind,
        reason=reason,
        diagnostics=diagnostics,
    )
    try:
        _write_json_atomic(target, payload, mode=0o600)
    except OSError:
        return None
    return target


def _remove_quarantine_metadata(
    paths: TransactionPaths, transaction_id: Optional[str] = None
) -> None:
    target = _quarantine_metadata_path(paths, transaction_id)
    try:
        _remove_path(target)
    except OSError:
        pass


def _remove_quarantine_metadata_for(
    paths: TransactionPaths, quarantine: object
) -> None:
    """Remove metadata when recovery restored the quarantined tree."""
    raw = str(quarantine or "")
    if not raw:
        return
    prefix = f"{paths.key}.quarantine-"
    name = Path(raw).name
    if not name.startswith(prefix):
        return
    transaction_id = name[len(prefix) :]
    if transaction_id:
        _remove_quarantine_metadata(paths, transaction_id)


def _quarantine_entries(
    paths: TransactionPaths,
) -> Tuple[Dict[str, Dict[str, Path]], list]:
    """Group quarantine tree, sidecar and metadata files by transaction."""
    groups: Dict[str, Dict[str, Path]] = {}
    errors = []
    root = paths.root
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return groups, [f"transaction root is not a directory: {root}"]
    if not root.exists():
        return groups, errors
    key = re.escape(paths.key)
    tree_pattern = re.compile(rf"^{key}\.quarantine-([A-Za-z0-9_-]+)$")
    state_pattern = re.compile(
        rf"^{key}\.quarantine-state-([A-Za-z0-9_-]+)$"
    )
    metadata_pattern = re.compile(
        rf"^{key}\.quarantine-meta-([A-Za-z0-9_-]+)\.json$"
    )
    try:
        children = list(root.iterdir())
    except OSError as exc:
        return groups, [f"could not inspect quarantine directory {root}: {exc}"]
    for child in children:
        name = child.name
        state_match = state_pattern.fullmatch(name)
        if state_match is not None:
            if child.is_symlink():
                errors.append(f"quarantine state is a symlink: {child}")
                continue
            groups.setdefault(state_match.group(1), {})["state_dir"] = child
            continue
        metadata_match = metadata_pattern.fullmatch(name)
        if metadata_match is not None:
            if child.is_symlink():
                errors.append(f"quarantine metadata is a symlink: {child}")
                continue
            groups.setdefault(metadata_match.group(1), {})["metadata"] = child
            continue
        tree_match = tree_pattern.fullmatch(name)
        if tree_match is not None:
            if child.is_symlink():
                errors.append(f"quarantine tree is a symlink: {child}")
                continue
            groups.setdefault(tree_match.group(1), {})["tree"] = child
    return groups, errors


def _quarantine_reasons(
    destination: Path, paths: TransactionPaths
) -> Dict[str, str]:
    """Recover reasons for legacy quarantines from journal or state."""
    reasons: Dict[str, str] = {}
    sources = []
    if paths.journal.exists() and not paths.journal.is_symlink():
        sources.append(_read_json(paths.journal) or {})
    for state_path in (
        destination / STATE_NAME,
        paths.state_dir / STATE_NAME,
    ):
        if state_path.is_file() and not state_path.is_symlink():
            state = _read_json(state_path)
            if state:
                sources.append(state)
    for source in sources:
        reason = str(source.get("quarantine_reason") or "")
        for field in ("quarantine", "quarantine_state_dir"):
            raw = str(source.get(field) or "")
            if raw:
                reasons[_cleanup_path_key(Path(raw))] = reason
    return reasons


def _iso_timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OSError, OverflowError, ValueError):
        return ""


def _quarantine_record(
    paths: TransactionPaths,
    transaction_id: str,
    group: Mapping[str, Path],
    *,
    latest: bool,
    referenced_keys: set,
    reasons: Mapping[str, str],
    max_age_days: float,
    current: float,
) -> QuarantineRecord:
    tree = group.get("tree")
    state_dir = group.get("state_dir")
    metadata_path = group.get("metadata")
    metadata: Mapping[str, Any] = {}
    metadata_valid = False
    error = ""
    if metadata_path is not None:
        if not metadata_path.is_file() or metadata_path.is_symlink():
            error = f"quarantine metadata is not a regular file: {metadata_path}"
        else:
            parsed = _read_json(metadata_path)
            if parsed and parsed.get("schema") == QUARANTINE_SCHEMA:
                metadata = parsed
                metadata_valid = True
            else:
                error = f"quarantine metadata is invalid: {metadata_path}"

    present_paths = [
        path
        for path in (tree, state_dir)
        if path is not None and (path.exists() or path.is_symlink())
    ]
    present = bool(present_paths)
    modified = 0.0
    for path in present_paths:
        try:
            modified = max(modified, path.lstat().st_mtime)
        except OSError:
            continue
    if metadata_path is not None and not present:
        try:
            modified = metadata_path.lstat().st_mtime
        except OSError:
            modified = 0.0
    age_days = max(0.0, (current - modified) / 86400.0) if modified else 0.0

    if metadata_valid:
        kind = str(metadata.get("kind") or "")
    else:
        kind = ""
    if not kind:
        if state_dir is not None:
            kind = "file"
        elif tree is not None and tree.is_file():
            kind = "file"
        elif tree is not None:
            kind = "directory"
        else:
            kind = "unknown"

    reason = ""
    if metadata_valid:
        reason = str(metadata.get("reason") or "")
    if not reason:
        for path in present_paths:
            reason = reasons.get(_cleanup_path_key(path), "")
            if reason:
                break
    if not reason:
        reason = "unrecorded quarantine"

    created = ""
    if metadata_valid:
        created = str(metadata.get("created") or "")
    if not created and modified:
        created = _iso_timestamp(modified)

    size = 0
    for path in present_paths:
        try:
            size += _cleanup_size(path)
        except OSError as exc:
            error = f"could not measure {path}: {exc}"

    keys = {
        _cleanup_path_key(path) for path in present_paths
    }
    referenced = bool(keys & referenced_keys)
    if not present:
        retention = "metadata-only"
    elif referenced:
        retention = "active"
    elif latest:
        retention = "retained-latest"
    elif age_days >= max_age_days:
        retention = "eligible"
    else:
        retention = f"younger than {max_age_days:g}-day age limit"

    return QuarantineRecord(
        transaction_id=transaction_id,
        kind=kind,
        path=tree or state_dir or metadata_path,
        state_dir=state_dir,
        metadata_path=metadata_path,
        reason=reason,
        created=created,
        age_days=age_days,
        bytes=size,
        present=present,
        metadata_valid=metadata_valid,
        referenced=referenced,
        latest=latest,
        retention=retention,
        error=error,
    )


def quarantine_inventory(
    destination: Path,
    *,
    max_age_days: float = QUARANTINE_MAX_AGE_DAYS,
    now: Optional[float] = None,
) -> QuarantineInventory:
    """Inspect retained quarantine trees, sidecars and metadata."""
    if (
        isinstance(max_age_days, bool)
        or not isinstance(max_age_days, (int, float))
        or max_age_days <= 0
    ):
        raise ValueError("max_age_days must be greater than zero")
    destination = _absolute(destination)
    paths = transaction_paths(destination)
    groups, errors = _quarantine_entries(paths)
    references, unknown_active, reference_errors = _cleanup_references(
        destination, paths
    )
    errors.extend(reference_errors)
    if unknown_active:
        errors.append("active install state is unreadable")
    reasons = _quarantine_reasons(destination, paths)
    current = time.time() if now is None else float(now)

    def modified(transaction_id: str) -> float:
        group = groups[transaction_id]
        values = []
        for key in ("tree", "state_dir"):
            path = group.get(key)
            if path is None:
                continue
            try:
                values.append(path.lstat().st_mtime)
            except OSError:
                continue
        return max(values) if values else 0.0

    present_ids = [
        transaction_id
        for transaction_id, group in groups.items()
        if any(
            path.exists() or path.is_symlink()
            for key, path in group.items()
            if key in ("tree", "state_dir")
        )
    ]
    latest_id = max(
        present_ids, key=lambda item: (modified(item), item), default=None
    )
    records = [
        _quarantine_record(
            paths,
            transaction_id,
            group,
            latest=transaction_id == latest_id,
            referenced_keys=references,
            reasons=reasons,
            max_age_days=float(max_age_days),
            current=current,
        )
        for transaction_id, group in sorted(groups.items())
    ]
    latest_record = next((record for record in records if record.latest), None)
    if not records:
        reason = "no quarantines retained"
    elif latest_record is None:
        reason = "no complete quarantine tree is retained"
    else:
        reason = (
            f"latest quarantine {latest_record.transaction_id}: "
            f"{latest_record.reason}"
        )
    return QuarantineInventory(
        destination=destination,
        quarantines=tuple(records),
        latest=latest_record.path if latest_record else None,
        reason=reason,
        errors=tuple(errors),
    )


def _quarantine_retention_record(
    paths: TransactionPaths,
    record: QuarantineRecord,
) -> Optional[Path]:
    """Ensure a quarantined tree has metadata before its contents are removed."""
    target = record.metadata_path or _quarantine_metadata_path(
        paths, record.transaction_id
    )
    if target.is_symlink():
        return None
    if record.metadata_valid:
        return target
    payload = _quarantine_metadata_payload(
        paths,
        record.transaction_id,
        kind=record.kind,
        reason=record.reason,
        diagnostics={"recovered": True},
    )
    try:
        _write_json_atomic(target, payload, mode=0o600)
    except OSError:
        return None
    return target


def prune_quarantines(
    destination: Path,
    *,
    keep: int = QUARANTINE_KEEP,
    max_age_days: float = QUARANTINE_MAX_AGE_DAYS,
    dry_run: bool = True,
    now: Optional[float] = None,
) -> QuarantineRetentionReport:
    """Plan or apply age-based quarantine retention.

    The newest ``keep`` complete trees are always retained, as are trees named
    by an active journal or install state.  Older trees are removed only after
    a metadata record exists, so a small diagnostic history remains after the
    bulk data is gone.  The default is a read-only plan; pass ``dry_run=False``
    to apply it.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    if (
        isinstance(max_age_days, bool)
        or not isinstance(max_age_days, (int, float))
        or max_age_days <= 0
    ):
        raise ValueError("max_age_days must be greater than zero")

    destination = _absolute(destination)
    paths = transaction_paths(destination)
    inventory = quarantine_inventory(
        destination, max_age_days=max_age_days, now=now
    )
    errors = list(inventory.errors)
    _references, unknown_active, reference_errors = _cleanup_references(
        destination, paths
    )
    errors.extend(reference_errors)
    records = [record for record in inventory.quarantines if record.present]
    if unknown_active:
        return QuarantineRetentionReport(
            destination=destination,
            dry_run=dry_run,
            keep=keep,
            max_age_days=float(max_age_days),
            kept=tuple(record.path for record in records if record.path),
            pruned=(),
            metadata_retained=(),
            errors=tuple(
                errors + ["active install state is unreadable; nothing pruned"]
            ),
        )
    ordered = sorted(
        records, key=lambda record: (record.age_days, record.transaction_id)
    )
    protected_ids = {
        record.transaction_id for record in ordered[:keep]
    }
    protected_ids.update(
        record.transaction_id for record in records if record.referenced
    )

    kept = []
    pruned = []
    retained_metadata = []

    def keep_path(record: QuarantineRecord) -> None:
        if record.path:
            kept.append(record.path)

    for record in ordered:
        if record.transaction_id in protected_ids:
            keep_path(record)
            continue
        if record.age_days < max_age_days:
            keep_path(record)
            continue

        if dry_run:
            if record.path:
                pruned.append(record.path)
            continue

        metadata_path = _quarantine_retention_record(paths, record)
        if metadata_path is None:
            errors.append(
                f"could not record quarantine metadata for "
                f"{record.transaction_id}; keeping the tree"
            )
            keep_path(record)
            continue
        payload = _read_json(metadata_path)
        if payload is None:
            errors.append(
                f"could not read quarantine metadata for {record.transaction_id}; "
                "keeping the tree"
            )
            keep_path(record)
            continue
        payload = dict(payload)
        payload["status"] = "pruned"
        payload["pruned_at"] = _now()
        payload["updated"] = payload["pruned_at"]
        try:
            _write_json_atomic(metadata_path, payload, mode=0o600)
        except OSError as exc:
            errors.append(
                f"could not update quarantine metadata for "
                f"{record.transaction_id}: {exc}; keeping the tree"
            )
            keep_path(record)
            continue

        removed = True
        for path in (record.path, record.state_dir):
            if path is None or not (path.exists() or path.is_symlink()):
                continue
            try:
                _remove_path(path)
            except OSError as exc:
                removed = False
                errors.append(f"could not remove quarantine {path}: {exc}")
        if removed:
            if record.path:
                pruned.append(record.path)
            if metadata_path.exists():
                retained_metadata.append(metadata_path)
        else:
            keep_path(record)

    # Metadata-only records are intentionally retained as the small history of
    # quarantines that have already had their trees pruned.
    return QuarantineRetentionReport(
        destination=destination,
        dry_run=dry_run,
        keep=keep,
        max_age_days=float(max_age_days),
        kept=tuple(kept),
        pruned=tuple(pruned),
        metadata_retained=tuple(retained_metadata),
        errors=tuple(errors),
    )


def _next_recovery_generation(destination: Path) -> int:
    """Allocate the next archive generation without reusing a gap."""
    _paths, entries, error = _recovery_archive_entries(destination)
    if error:
        raise InstallError(f"could not inspect recovery archives: {error}")
    generation = 1
    if entries:
        generation = max(int(entry[0]) for entry in entries) + 1
    if generation > 99_999_999:
        raise InstallError("recovery archive generation is exhausted")
    return generation


def allocate_recovery_archive(
    destination: Path,
    *,
    generation: Optional[int] = None,
) -> Tuple[int, Path]:
    """Return the next numeric recovery generation and its archive path.

    An explicit *generation* selects that archive path without allocating a
    new generation.  Otherwise the next free generation is based on the
    newest archive already present; gaps are not reused.
    """
    if generation is None:
        generation = _next_recovery_generation(destination)
    archive = recovery_archive_path(destination, generation)
    return generation, archive


def _inspect_current_install(
    destination: Path,
    paths: TransactionPaths,
    kind: str,
) -> CurrentInstall:
    """Classify the destination before the transaction copies or moves it.

    ``valid`` means a complete recovery archive can be made.  ``corrupt`` means
    install metadata exists but the tree cannot be archived safely; that tree
    is quarantined instead of deleted.  A plain directory with no install
    metadata is ``unmanaged`` and keeps the historical force-replacement path.
    """
    destination = Path(destination)
    if kind == "directory":
        if not destination.exists():
            return CurrentInstall("absent")
        if destination.is_symlink() or not destination.is_dir():
            return CurrentInstall("corrupt", "destination is not a real directory")
        manifest_path = destination / install_manifest.MANIFEST_NAME
        state_path = destination / STATE_NAME
    elif kind == "file":
        if not destination.exists():
            return CurrentInstall("absent")
        if destination.is_symlink() or not destination.is_file():
            return CurrentInstall("corrupt", "destination is not a real file")
        manifest_path = paths.state_dir / install_manifest.MANIFEST_NAME
        state_path = paths.state_dir / STATE_NAME
    else:
        raise InstallError(f"unsupported recovery source kind: {kind}")

    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    state_exists = state_path.exists() or state_path.is_symlink()
    if not manifest_exists and not state_exists:
        return CurrentInstall("unmanaged", "no install metadata")
    if not manifest_exists or not state_exists:
        return CurrentInstall("corrupt", "install metadata is incomplete")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return CurrentInstall("corrupt", "install manifest is not a regular file")
    if state_path.is_symlink() or not state_path.is_file():
        return CurrentInstall("corrupt", "install state is not a regular file")
    try:
        manifest = install_manifest.load_manifest(manifest_path)
    except (OSError, install_manifest.ManifestError) as exc:
        return CurrentInstall("corrupt", f"install manifest is invalid: {exc}")
    state = _read_json(state_path)
    if not state:
        return CurrentInstall("corrupt", "install state is invalid JSON")
    if state.get("schema") != STATE_SCHEMA:
        return CurrentInstall("corrupt", "install state has an unsupported schema")
    if state.get("status") != "committed":
        return CurrentInstall("corrupt", "install state is not committed")
    expected_digest = install_manifest.manifest_digest(manifest)
    if str(state.get("manifest_digest") or "") != expected_digest:
        return CurrentInstall("corrupt", "install state and manifest digest differ")
    if not str(state.get("install_id") or ""):
        return CurrentInstall("corrupt", "install state has no install ID")

    if kind == "file":
        entry = next(
            (
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict)
                and str(item.get("path") or "") == destination.name
            ),
            None,
        )
        try:
            if entry is None:
                raise OSError("the manifest does not list the pyz")
            if destination.stat().st_size != int(entry.get("size") or -1):
                raise OSError("the pyz size differs from the manifest")
            if install_manifest.sha256_file(destination) != str(
                entry.get("sha256") or ""
            ):
                raise OSError("the pyz hash differs from the manifest")
        except (OSError, ValueError) as exc:
            return CurrentInstall("corrupt", f"current pyz is invalid: {exc}")
        return CurrentInstall("valid")

    try:
        recovery.inspect_recovery_source(destination)
    except recovery.RecoveryError as exc:
        return CurrentInstall("corrupt", f"current install cannot be archived: {exc}")
    return CurrentInstall("valid")


def _read_retention_pointer(path: Path, label: str) -> Tuple[Optional[Path], str]:
    """Read a state/journal archive pointer, failing closed on corruption."""
    path = Path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"could not inspect {label} {path}: {exc}"
    if path.is_symlink() or not path.is_file():
        return None, f"{label} is not a regular file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"could not read {label} {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"{label} is not a JSON object: {path}"
    if "recovery_bundle" not in data:
        return None, ""
    raw = data.get("recovery_bundle")
    if not isinstance(raw, str):
        return None, f"{label} recovery pointer is invalid: {path}"
    text = raw.strip()
    if not text:
        return None, ""
    try:
        value = Path(text).expanduser()
        if not value.is_absolute():
            return None, f"{label} recovery pointer is not absolute: {path}"
        return Path(os.path.abspath(str(value))), ""
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"{label} recovery pointer is invalid: {path}: {exc}"


def _is_safety_snapshot(path: Path) -> bool:
    """Return whether a recovery archive is a pre-restore safety snapshot."""
    try:
        payload = recovery.load_recovery_manifest(
            Path(path) / recovery.RECOVERY_NAME
        )
    except (OSError, recovery.RecoveryError):
        return False
    return payload.get("safety_snapshot") is True


def _prune_after_commit(destination: Path) -> RecoveryRetentionReport:
    """Apply best-effort retention after the commit state is durable."""
    try:
        report = prune_recovery_archives(destination, keep=RECOVERY_KEEP)
    except Exception as exc:  # noqa: BLE001 - cleanup must never undo a commit
        report = RecoveryRetentionReport(
            kept=(),
            pruned=(),
            errors=(f"could not prune recovery archives: {exc}",),
            protected=(),
        )
    try:
        prune_quarantines(
            destination,
            keep=QUARANTINE_KEEP,
            max_age_days=QUARANTINE_MAX_AGE_DAYS,
            dry_run=False,
        )
    except Exception:  # noqa: BLE001 - retention must never undo a commit
        pass
    return report


def prune_recovery_archives(
    destination: Path,
    *,
    keep: int = RECOVERY_KEEP,
) -> RecoveryRetentionReport:
    """Best-effort prune completed recovery archives for one destination.

    The newest ``keep`` archives are retained.  Archives referenced by the
    committed ``install-state.json`` (and a live journal, when present) are
    protected even when older.  If a present state or journal file cannot be
    read, or its recovery pointer is malformed or unknown, no archive is
    removed.  Deletion failures never raise; they are returned in ``errors`` so
    a committed install is not rolled back because cleanup failed.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")

    paths, entries, inspect_error = _recovery_archive_entries(destination)
    if inspect_error:
        return RecoveryRetentionReport(
            kept=(),
            pruned=(),
            errors=(inspect_error,),
            protected=(),
        )
    if not entries:
        return RecoveryRetentionReport((), (), (), ())

    def key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    entry_keys = {key(entry[1]) for entry in entries}
    protected_keys = set()
    for _generation, path in entries:
        if _is_safety_snapshot(path):
            protected_keys.add(key(path))
    for label, source in (
        ("install state", _state_path_for_retention(destination)),
        ("journal", paths.journal),
    ):
        value, error = _read_retention_pointer(source, label)
        if error:
            # A present but unreadable pointer must never be treated as absent:
            # the archive it names may be the only committed recovery snapshot.
            return RecoveryRetentionReport(
                kept=tuple(entry[1] for entry in entries),
                pruned=(),
                errors=(error,),
                protected=(),
            )
        if value is None:
            continue
        value_key = key(value)
        if value_key not in entry_keys:
            return RecoveryRetentionReport(
                kept=tuple(entry[1] for entry in entries),
                pruned=(),
                errors=(
                    f"{label} recovery pointer does not name a known "
                    f"archive: {value}",
                ),
                protected=(),
            )
        protected_keys.add(value_key)

    selected = []
    for _generation, path in entries:
        if key(path) in protected_keys:
            selected.append((_generation, path))
    for entry in entries:
        if len(selected) >= keep:
            break
        if entry not in selected:
            selected.append(entry)

    selected.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    selected_paths = {key(entry[1]) for entry in selected}
    kept = tuple(entry[1] for entry in selected)
    protected_paths = tuple(
        entry[1] for entry in selected if key(entry[1]) in protected_keys
    )
    pruned = []
    errors = []
    for _generation, path in entries:
        if key(path) in selected_paths:
            continue
        try:
            _remove_path(path)
        except OSError as exc:
            errors.append(f"could not prune {path}: {exc}")
        else:
            pruned.append(path)
    return RecoveryRetentionReport(
        kept=kept,
        pruned=tuple(pruned),
        errors=tuple(errors),
        protected=protected_paths,
    )


def _fsync_directory(path: Path) -> None:
    """Persist directory entries after a rename, create, or unlink."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            unsupported = {
                getattr(errno, "EINVAL", 0),
                getattr(errno, "ENOTSUP", 0),
                getattr(errno, "EOPNOTSUPP", 0),
                getattr(errno, "ENOSYS", 0),
            }
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def _ensure_root(paths: TransactionPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    try:
        paths.root.chmod(0o700)
    except OSError:
        pass
    _fsync_directory(paths.root.parent)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_process_start(
    pid: int, *, kernel32: Any = None
) -> Optional[str]:
    """Return a Windows process creation-time marker via GetProcessTimes.

    The combined FILETIME value is stable for the lifetime of the process and
    changes when Windows recycles its PID for a different process.
    """
    if pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = (
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            )

        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = FILETIME()
            exited = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (int(created.dwHighDateTime) << 32) | int(
                created.dwLowDateTime
            )
            return str(value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _process_start(pid: int) -> Optional[str]:
    """Return a stable marker for when *pid* started, when available.

    A live PID alone is not enough to identify the process that wrote a lock:
    the operating system may have recycled the number for a new process. The
    marker is compared with the value recorded when the lock was acquired.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start(pid)
    if os.name == "posix":
        try:
            stat_path = Path(f"/proc/{pid}/stat")
            data = stat_path.read_text(encoding="utf-8")
            close = data.rfind(")")
            if close >= 0:
                fields = data[close + 2 :].split()
                if len(fields) > 19:
                    return fields[19]
        except OSError:
            pass
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            value = completed.stdout.strip()
            if value:
                return value
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _stale_lock(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # The owner may have created the file but not written its metadata yet.
        # Only reclaim a malformed lock after it has had time to become valid.
        try:
            return time.time() - path.stat().st_mtime > 60
        except OSError:
            return True
    if not isinstance(data, dict):
        return True
    host = str(data.get("host") or "")
    if host and host != platform.node():
        # A process on another machine cannot be inspected safely. A shared
        # install must never steal a lock just because the hostname differs.
        return False
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    recorded_start = data.get("pid_start")
    if recorded_start:
        current_start = _process_start(pid)
        if current_start is None:
            # If the platform cannot identify process start times, fall back
            # to the old liveness check rather than stealing a live lock.
            return not _process_alive(pid)
        return str(recorded_start) != current_start
    return not _process_alive(pid)


@contextmanager
def install_lock(destination: Path) -> Iterator[str]:
    """Hold the exclusive install lock for *destination*.

    A lock left by a dead process on this host is reclaimed, including after
    its PID has been recycled. A lock from another host is treated as live
    contention because that process cannot be inspected safely; a live local
    owner is always refused.
    """
    paths = transaction_paths(destination)
    _ensure_root(paths)
    token = uuid.uuid4().hex
    pid = os.getpid()
    payload = {
        "pid": pid,
        "host": platform.node(),
        "created": _now(),
        "destination": str(paths.destination),
        "token": token,
    }
    pid_start = _process_start(pid)
    if pid_start is not None:
        payload["pid_start"] = pid_start
    descriptor = -1
    for attempt in range(2):
        try:
            descriptor = os.open(
                str(paths.lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            break
        except FileExistsError:
            if attempt == 0 and _stale_lock(paths.lock):
                try:
                    paths.lock.unlink()
                except OSError:
                    pass
                continue
            raise InstallBusy(
                f"another install is already running for {paths.destination}; "
                f"lock file: {paths.lock}"
            ) from None
    if descriptor < 0:
        raise InstallBusy(
            f"could not acquire the install lock for {paths.destination}"
        )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        yield token
    finally:
        try:
            current = json.loads(paths.lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if isinstance(current, dict) and current.get("token") == token:
            try:
                paths.lock.unlink()
            except OSError:
                pass
        _cleanup_root(paths)


def _remove_path(path: Path) -> None:
    path = Path(path)
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if path.is_dir():
        shutil.rmtree(path)


def _write_json_atomic(path: Path, payload: Mapping[str, Any], mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _manifest_for(root: Path) -> Optional[Dict[str, Any]]:
    try:
        return install_manifest.load_manifest(Path(root) / install_manifest.MANIFEST_NAME)
    except (OSError, install_manifest.ManifestError):
        return None


def _is_candidate_state(
    root: Path,
    state_path: Path,
    journal: Mapping[str, Any],
) -> bool:
    """Return whether *state_path* still identifies this journal's candidate."""
    candidate_digest = str(journal.get("manifest_digest") or "")
    if not candidate_digest:
        return False
    state = _read_json(state_path)
    if str((state or {}).get("manifest_digest") or "") != candidate_digest:
        return False
    manifest = _manifest_for(root)
    return bool(
        manifest
        and install_manifest.manifest_digest(manifest) == candidate_digest
    )


def _previous_info(root: Path) -> Dict[str, Any]:
    state = _read_json(Path(root) / STATE_NAME)
    manifest = _manifest_for(root)
    if state:
        try:
            generation = int(state.get("recovery_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        return {
            "install_id": str(state.get("install_id") or ""),
            "transaction_id": str(state.get("transaction_id") or ""),
            "status": str(state.get("status") or ""),
            "manifest_digest": str(state.get("manifest_digest") or ""),
            "artifact": state.get("artifact") if isinstance(state.get("artifact"), dict) else {},
            "destination": str(state.get("destination") or ""),
            "recovery_bundle": str(state.get("recovery_bundle") or ""),
            "recovery_generation": generation,
            "quarantine": str(state.get("quarantine") or ""),
            "quarantine_state_dir": str(state.get("quarantine_state_dir") or ""),
            "quarantine_reason": str(state.get("quarantine_reason") or ""),
            "created": str(state.get("created") or ""),
            "updated": str(state.get("updated") or ""),
        }
    if manifest:
        return {
            "install_id": "",
            "transaction_id": "",
            "manifest_digest": install_manifest.manifest_digest(manifest),
            "artifact": manifest.get("artifact") or {},
            "destination": str(root),
        }
    return {}


def _recovery_created(state_path: Path) -> Optional[str]:
    """Use the previous state timestamp so the archive stays reproducible."""
    state = _read_json(state_path)
    if not state:
        return None
    value = str(state.get("updated") or state.get("created") or "")
    return value or None


def _create_recovery_bundle(
    destination: Path,
    paths: TransactionPaths,
    transaction_id: str,
    *,
    kind: str,
    generation: Optional[int] = None,
    safety_snapshot: bool = False,
) -> SnapshotResult:
    """Capture the committed previous install before the destination changes.

    Each replacement receives the next per-destination generation.  The archive
    is verified before this function returns; a failed write or verification
    leaves the destination untouched.
    """
    if kind == "directory":
        if not destination.is_dir():
            return SnapshotResult()
        manifest_path = destination / install_manifest.MANIFEST_NAME
        state_path = destination / STATE_NAME
        source = destination
    elif kind == "file":
        if not destination.is_file():
            return SnapshotResult()
        manifest_path = paths.state_dir / install_manifest.MANIFEST_NAME
        state_path = paths.state_dir / STATE_NAME
        source = destination
    else:
        raise InstallError(f"unsupported recovery source kind: {kind}")

    if not manifest_path.exists() and not state_path.exists():
        return SnapshotResult()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError(
            "existing install has no regular install-manifest.json; "
            "cannot create a recovery bundle"
        )
    if state_path.is_symlink() or not state_path.is_file():
        raise InstallError(
            "existing install has no regular install-state.json; "
            "cannot create a recovery bundle"
        )

    generation, archive = allocate_recovery_archive(
        destination, generation=generation
    )
    if archive.exists() or archive.is_symlink():
        raise InstallError(f"recovery archive already exists: {archive}")

    temporary_source: Optional[Path] = None
    try:
        if kind == "file":
            temporary_source = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.recovery-source-",
                    dir=str(paths.root),
                )
            )
            shutil.copy2(destination, temporary_source / destination.name)
            for name in (install_manifest.MANIFEST_NAME, STATE_NAME):
                shutil.copy2(
                    paths.state_dir / name,
                    temporary_source / name,
                )
            source = temporary_source

        reason = f"replacing {destination.name} in transaction {transaction_id}"
        kwargs: Dict[str, Any] = {}
        created_state = (
            temporary_source / STATE_NAME if temporary_source else state_path
        )
        created = _recovery_created(created_state)
        if created:
            kwargs["created"] = created
        recovery.write_recovery_archive(
            source,
            archive,
            reason=reason,
            transaction_id=transaction_id,
            force=False,
            safety_snapshot=safety_snapshot,
            **kwargs,
        )
    except recovery.RecoveryError as exc:
        _discard_recovery_archive(archive)
        raise InstallError(
            f"could not create a recovery bundle for {destination}: {exc}"
        ) from exc
    except OSError as exc:
        _discard_recovery_archive(archive)
        raise InstallError(
            f"could not stage a recovery bundle for {destination}: {exc}"
        ) from exc
    finally:
        if temporary_source is not None:
            shutil.rmtree(temporary_source, ignore_errors=True)

    if not archive.is_dir():
        raise InstallError(
            f"recovery bundle was not created for {destination}: {archive}"
        )
    try:
        recovery.verify_recovery_archive(archive)
    except recovery.RecoveryError as exc:
        _discard_recovery_archive(archive)
        raise InstallError(
            f"recovery bundle did not verify for {destination}: {exc}"
        ) from exc
    return SnapshotResult(str(archive), generation)


def _discard_recovery_archive(archive: Path) -> None:
    """Best-effort removal of an archive whose creation did not finish."""
    try:
        if archive.exists() or archive.is_symlink():
            _remove_path(archive)
    except OSError:
        pass


def _remove_recovery_bundle(value: str) -> None:
    """Remove an archive created for a transaction that did not commit."""
    raw = str(value or "")
    if not raw:
        return
    path = Path(raw)
    if path.exists() or path.is_symlink():
        _remove_path(path)


def _state_payload(
    destination: Path,
    manifest: Mapping[str, Any],
    *,
    transaction_id: str,
    install_id: str,
    previous: Mapping[str, Any],
    status: str,
    manifest_path: str = install_manifest.MANIFEST_NAME,
    recovery_bundle: str = "",
    recovery_generation: int = 0,
    quarantine: str = "",
    quarantine_state_dir: str = "",
    quarantine_reason: str = "",
    created: str = "",
    last_error: str = "",
) -> Dict[str, Any]:
    now = _now()
    artifact = manifest.get("artifact")
    return {
        "schema": STATE_SCHEMA,
        "install_id": install_id,
        "transaction_id": transaction_id,
        "status": status,
        "destination": str(destination),
        "manifest_digest": install_manifest.manifest_digest(manifest),
        "manifest_path": manifest_path,
        "artifact": artifact if isinstance(artifact, dict) else {},
        "previous_install": dict(previous),
        "recovery_bundle": recovery_bundle,
        "recovery_generation": recovery_generation,
        "quarantine": quarantine,
        "quarantine_state_dir": quarantine_state_dir,
        "quarantine_reason": quarantine_reason,
        "created": created or now,
        "updated": now,
        "last_error": last_error,
        "health": {
            "ok": status == "committed",
            "checked_at": now if status == "committed" else "",
        },
    }


def _write_state(root: Path, state: Mapping[str, Any], name: str = STATE_NAME) -> Path:
    path = Path(root) / name
    _write_json_atomic(path, state, mode=0o644)
    return path


def record_install(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    transaction_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a committed state file for an already-complete install tree.

    This is for callers that already have a complete tree and need the state
    contract without staging it. A later transactional replacement treats it
    as the previous install. Retention is best-effort and runs only after the
    committed state is durable.
    """
    root = _absolute(root)
    previous = _previous_info(root)
    state = _state_payload(
        root,
        manifest,
        transaction_id=transaction_id or uuid.uuid4().hex,
        install_id=uuid.uuid4().hex,
        previous=previous,
        status="committed",
    )
    _write_state(root, state)
    _prune_after_commit(root)
    return state


def preflight_destination(
    destination: Path,
    *,
    force: bool,
    kind: str = "directory",
) -> Path:
    destination = _absolute(destination)
    if destination.is_symlink():
        raise InstallError(f"refusing a symlinked install destination: {destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise InstallError(f"install parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise InstallError(f"install parent is not writable: {parent}")
    if destination.exists():
        if not force:
            raise InstallError(
                f"{destination} already exists; pass --force to replace it"
            )
        if kind == "directory" and not destination.is_dir():
            raise InstallError(
                f"{destination} exists and is not a directory; remove it first"
            )
        if kind == "file" and not destination.is_file():
            raise InstallError(
                f"{destination} exists and is not a file; remove it first"
            )
    return destination


def _source_manifest(
    source: Path,
    *,
    allow_missing_assets: bool = False,
) -> Dict[str, Any]:
    manifest_path = Path(source) / install_manifest.MANIFEST_NAME
    if not manifest_path.is_file():
        raise InstallError(
            f"bundle has no verified {install_manifest.MANIFEST_NAME}: {source}; "
            "rebuild the bundle before installing"
        )
    try:
        manifest = install_manifest.load_manifest(manifest_path)
    except install_manifest.ManifestError as exc:
        raise InstallError(str(exc)) from exc
    report = install_manifest.verify_manifest(
        source,
        manifest,
        check_assets=True,
        check_extra=True,
        check_required_assets=not allow_missing_assets,
    )
    if not report.ok:
        raise InstallError(
            "bundle manifest did not verify: " + "; ".join(report.errors)
        )
    return manifest


def _same_asset(new_entry: Mapping[str, Any], old_entry: Mapping[str, Any]) -> bool:
    return (
        str(new_entry.get("path") or "") == str(old_entry.get("path") or "")
        and int(new_entry.get("size") or -1) == int(old_entry.get("size") or -2)
        and str(new_entry.get("sha256") or "") == str(old_entry.get("sha256") or "")
    )


def _same_runtime(new_entry: Mapping[str, Any], old_entry: Mapping[str, Any]) -> bool:
    return dict(new_entry) == dict(old_entry)


def _reusable_asset(path: Path, entry: Mapping[str, Any]) -> bool:
    """Check a large asset before linking it from the previous install."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size != int(entry.get("size") or -1):
            return False
        return install_manifest.sha256_file(path) == str(entry.get("sha256") or "")
    except (OSError, TypeError, ValueError):
        return False


def _reusable_runtime(path: Path, entry: Mapping[str, Any]) -> bool:
    """Check every immutable runtime marker before reusing the tree.

    The binary is necessary but not sufficient: a stale or missing fingerprint
    must send the transaction back to the bundle copy rather than reuse a tree
    that the manifest no longer describes.
    """
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        binary = str(entry.get("binary") or "")
        if binary:
            target = path.joinpath(*PurePosixPath(binary).parts)
            if target.is_symlink() or not target.is_file():
                return False
            if target.stat().st_size != int(entry.get("binary_size") or -1):
                return False
            if install_manifest.sha256_file(target) != str(
                entry.get("binary_sha256") or ""
            ):
                return False
        fingerprint = str(entry.get("fingerprint") or "")
        if fingerprint:
            target = path.joinpath(*PurePosixPath(fingerprint).parts)
            if target.is_symlink() or not target.is_file():
                return False
            if install_manifest.sha256_file(target) != str(
                entry.get("fingerprint_sha256") or ""
            ):
                return False
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            expected_tree = str((payload or {}).get("tree_digest") or "")
            if expected_tree:
                from . import runtime_identity

                if runtime_identity.tree_digest(path).digest != expected_tree:
                    return False
        return True
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def _copy_file(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _link_or_copy(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    try:
        os.link(source, destination)
    except (OSError, NotImplementedError):
        shutil.copy2(source, destination)
    else:
        try:
            shutil.copystat(source, destination)
        except OSError:
            pass


def _reuse_tree(source: Path, destination: Path) -> None:
    """Hard-link a tree where possible, falling back to a normal copy."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        return
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        copy_function=_link_or_copy,
        symlinks=False,
    )


def _copy_bundle_tree(
    bundle_source: Optional[Path], relative: str, destination: Path
) -> None:
    """Copy a runtime tree from the candidate after reuse is not possible."""
    if bundle_source is None:
        raise InstallError(f"could not reuse {relative} and no bundle copy exists")
    source = Path(bundle_source).joinpath(*PurePosixPath(relative).parts)
    if source.is_symlink() or not source.is_dir():
        raise InstallError(
            f"could not reuse {relative} and the bundle has no replacement tree"
        )
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)


def _skip_rel(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts:
        return True
    if parts[0] in (
        ".offgrid",
        ".offgrid-install",
        ".downloads",
        ".pytest_cache",
        "__pycache__",
    ):
        return True
    if any(part in ("__pycache__", ".pytest_cache") for part in parts):
        return True
    if relative.name in SKIP_COPY_NAMES:
        return True
    return relative.name.endswith(SKIP_COPY_SUFFIXES)


def _validate_source_tree(source: Path) -> None:
    """Reject symlinks that escape a source directory before copying it."""
    source = Path(source).resolve()
    for path in source.rglob("*"):
        if not path.is_symlink():
            continue
        label = path.relative_to(source)
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InstallError(f"broken symlink in bundle: {label}") from exc
        try:
            target.relative_to(source)
        except ValueError:
            raise InstallError(
                f"symlink in bundle points outside the bundle: {label}"
            ) from None
        if target.is_dir():
            raise InstallError(
                f"symlinked directory in bundle: {label}; copy its contents into the bundle"
            )


def _copy_source_tree(
    source: Path,
    stage: Path,
    destination: Path,
    old_manifest: Optional[Mapping[str, Any]],
    new_manifest: Mapping[str, Any],
) -> None:
    old_models = {
        str(entry["path"]): entry
        for entry in (old_manifest or {}).get("models", [])
        if isinstance(entry, dict)
    }
    new_models = {
        str(entry["path"]): entry
        for entry in new_manifest.get("models", [])
        if isinstance(entry, dict)
    }
    old_runtimes = {
        str(entry["path"]): entry
        for entry in (old_manifest or {}).get("runtimes", [])
        if isinstance(entry, dict)
    }
    new_runtimes = {
        str(entry["path"]): entry
        for entry in new_manifest.get("runtimes", [])
        if isinstance(entry, dict)
    }
    reuse_runtimes = {
        relative
        for relative, entry in new_runtimes.items()
        if relative in old_runtimes
        and _same_runtime(entry, old_runtimes[relative])
        and (destination / relative).is_dir()
        and _reusable_runtime(destination / relative, entry)
    }
    reuse_models = {
        relative
        for relative, entry in new_models.items()
        if relative in old_models
        and _same_asset(entry, old_models[relative])
        and (destination / relative).is_file()
        and _reusable_asset(destination / relative, entry)
    }

    source = Path(source)
    stage = Path(stage)
    for directory, dirnames, filenames in os.walk(source):
        current = Path(directory)
        relative_dir = current.relative_to(source)
        kept_dirs = []
        for name in sorted(dirnames):
            relative = PurePosixPath(*(relative_dir / name).parts)
            if _skip_rel(relative):
                continue
            candidate = current / name
            if candidate.is_symlink():
                raise InstallError(f"symlinked directory in bundle: {relative}")
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            path = current / name
            relative_path = path.relative_to(source)
            relative = PurePosixPath(*relative_path.parts)
            if _skip_rel(relative):
                continue
            if path.is_symlink():
                target = path.resolve(strict=True)
                try:
                    target.relative_to(source.resolve())
                except ValueError:
                    raise InstallError(
                        f"symlink in bundle points outside the bundle: {relative}"
                    ) from None
                if target.is_dir():
                    raise InstallError(
                        f"symlinked directory in bundle: {relative}"
                    )
            relative_text = relative.as_posix()
            if any(
                relative_text == runtime or relative_text.startswith(runtime + "/")
                for runtime in reuse_runtimes
            ):
                continue
            target = stage.joinpath(*relative.parts)
            if relative_text in reuse_models:
                try:
                    _link_or_copy(destination.joinpath(*relative.parts), target)
                except OSError:
                    _remove_path(target)
                    _copy_file(path, target)
            else:
                _copy_file(path, target)


def _preserve_old_state(
    destination: Path,
    stage: Path,
    old_manifest: Optional[Mapping[str, Any]],
    new_manifest: Mapping[str, Any],
    *,
    bundle_source: Optional[Path] = None,
    preserve_unlisted_assets: bool = False,
) -> None:
    if not destination.is_dir():
        return

    # Sessions, config, memory and similar state are never replaced by a bundle.
    old_state_dir = destination / ".offgrid"
    if old_state_dir.is_dir() and not (stage / ".offgrid").exists():
        shutil.copytree(old_state_dir, stage / ".offgrid", dirs_exist_ok=True)

    old_runtimes = {
        str(entry["path"]): entry
        for entry in (old_manifest or {}).get("runtimes", [])
        if isinstance(entry, dict)
    }
    new_runtimes = {
        str(entry["path"]): entry
        for entry in new_manifest.get("runtimes", [])
        if isinstance(entry, dict)
    }
    old_runtime_dir = destination / "runtime"
    if old_runtime_dir.is_dir():
        for child in sorted(old_runtime_dir.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            relative = f"runtime/{child.name}"
            target = stage / "runtime" / child.name
            should_reuse = False
            if relative in new_runtimes:
                if relative not in old_runtimes:
                    should_reuse = preserve_unlisted_assets
                elif (
                    _same_runtime(new_runtimes[relative], old_runtimes[relative])
                    and _reusable_runtime(child, new_runtimes[relative])
                ):
                    should_reuse = True
            else:
                should_reuse = True
            if should_reuse and not target.exists():
                try:
                    _reuse_tree(child, target)
                except (OSError, RuntimeError):
                    _remove_path(target)
                    _copy_bundle_tree(bundle_source, relative, target)

    old_models = {
        str(entry["path"]): entry
        for entry in (old_manifest or {}).get("models", [])
        if isinstance(entry, dict)
    }
    new_models = {
        str(entry["path"]): entry
        for entry in new_manifest.get("models", [])
        if isinstance(entry, dict)
    }
    old_models_dir = destination / "models"
    if old_models_dir.is_dir():
        for path in sorted(old_models_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative_text = path.relative_to(destination).as_posix()
            target = stage.joinpath(*PurePosixPath(relative_text).parts)
            if target.exists():
                continue
            if (
                relative_text in old_models
                and relative_text in new_models
                and not _same_asset(new_models[relative_text], old_models[relative_text])
            ):
                continue
            _link_or_copy(path, target)

    for name in ("config.json", "MODEL.txt"):
        source = destination / name
        target = stage / name
        if source.is_file() and not source.is_symlink() and not target.exists():
            _copy_file(source, target)


def _verify_stage(stage: Path, manifest: Mapping[str, Any]) -> None:
    report = install_manifest.verify_manifest(
        stage, manifest, check_assets=True
    )
    if not report.ok:
        raise InstallError(
            "staged install did not verify: " + "; ".join(report.errors)
        )


def _run_health_command(command: list, root: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=HEALTH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HealthCheckError(f"could not start the candidate install: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit status {completed.returncode}"
        raise HealthCheckError(f"candidate install failed its launch check: {reason}")


def health_check(
    root: Path,
    manifest: Mapping[str, Any],
) -> None:
    """Verify the candidate and prove its main entrypoint can start.

    This is deliberately a smoke check, not a full model or Studio launch: it
    imports the app or runs ``offgrid.pyz --version`` with bytecode disabled.
    """
    root = Path(root)
    _verify_stage(root, manifest)
    for relative in set(str(value) for value in manifest.get("entrypoints", {}).values()):
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or path.is_symlink():
            raise HealthCheckError(f"entrypoint is missing: {relative}")

    config = root / "config.json"
    if config.is_file() and not config.is_symlink():
        try:
            parsed = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HealthCheckError(f"config.json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise HealthCheckError("config.json must contain a JSON object")

    pyz_relative = str(
        (manifest.get("entrypoints") or {}).get("app") or "offgrid.pyz"
    )
    pyz = root.joinpath(*PurePosixPath(pyz_relative).parts)
    if pyz.suffix.lower() != ".pyz":
        pyz = root / "offgrid.pyz"
    if pyz.is_file():
        try:
            with zipfile.ZipFile(pyz) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise HealthCheckError(f"offgrid.pyz is not a valid app archive: {exc}") from exc
        if "__main__.py" not in names or not any(
            name.startswith("offgrid/") for name in names
        ):
            raise HealthCheckError("offgrid.pyz is missing its application entrypoint")
        _run_health_command([sys.executable, str(pyz), "--version"], root)
        return

    package = root / "offgrid"
    if not (package / "__init__.py").is_file() or not (package / "cli.py").is_file():
        raise HealthCheckError("the app package is missing offgrid/__init__.py or cli.py")
    if (package / "__main__.py").is_file():
        _run_health_command([sys.executable, "-m", "offgrid", "--version"], root)
        return
    code = (
        "import sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "import offgrid; "
        "print(getattr(offgrid, '__version__', 'offgrid'))"
    )
    _run_health_command([sys.executable, "-c", code, str(root)], root)


def _journal_payload(
    paths: TransactionPaths,
    *,
    status: str,
    kind: str,
    manifest_digest: str,
    install_id: str,
    artifact: Mapping[str, Any],
    previous: Mapping[str, Any],
    manifest_path: str = install_manifest.MANIFEST_NAME,
    recovery_bundle: str = "",
    recovery_generation: int = 0,
    quarantine: str = "",
    quarantine_reason: str = "",
    created: str = "",
    retention: Optional[Mapping[str, Any]] = None,
    error: str = "",
) -> Dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA,
        "transaction_id": paths.transaction_id,
        "status": status,
        "kind": kind,
        "destination": str(paths.destination),
        "staging": str(paths.stage),
        "previous": str(paths.previous),
        "quarantine": quarantine,
        "quarantine_state_dir": (
            str(paths.quarantine_state_dir) if quarantine else ""
        ),
        "quarantine_reason": quarantine_reason,
        "state_dir": str(paths.state_dir),
        "previous_state_dir": str(paths.previous_state_dir),
        "manifest_path": manifest_path,
        "manifest_digest": manifest_digest,
        "install_id": install_id,
        "artifact": dict(artifact),
        "previous_install": dict(previous),
        "recovery_bundle": recovery_bundle,
        "recovery_generation": int(recovery_generation),
        "retention": dict(retention) if retention is not None else None,
        "created": created or _now(),
        "updated": _now(),
        "error": error[:500],
    }


def _write_journal(paths: TransactionPaths, payload: Mapping[str, Any]) -> None:
    values = dict(payload)
    existing = _read_json(paths.journal)
    existing_created = str((existing or {}).get("created") or "")
    if existing_created:
        values["created"] = existing_created
    elif not values.get("created"):
        values["created"] = _now()
    values["updated"] = _now()
    _write_json_atomic(paths.journal, values, mode=0o600)


def _cleanup_root(paths: TransactionPaths) -> None:
    try:
        paths.root.rmdir()
    except OSError:
        pass


def _remove_committed_backup(
    paths: TransactionPaths, journal: Optional[Mapping[str, Any]] = None
) -> bool:
    """Best-effort cleanup after commit; never turn success into rollback."""
    previous = paths.previous
    previous_state = paths.previous_state_dir
    if journal is not None:
        raw_previous = str(journal.get("previous") or "")
        raw_state = str(journal.get("previous_state_dir") or "")
        if raw_previous:
            previous = Path(raw_previous)
        if raw_state:
            previous_state = Path(raw_state)
    try:
        _remove_path(previous)
        _remove_path(previous_state)
    except OSError:
        return False
    return True


def _recover_file_candidate_state(
    destination: Path,
    paths: TransactionPaths,
    journal: Mapping[str, Any],
) -> bool:
    """Commit an activated pyz only after its state and launch verify."""
    state = _read_json(paths.state_dir / STATE_NAME)
    candidate_digest = str(journal.get("manifest_digest") or "")
    if str((state or {}).get("manifest_digest") or "") != candidate_digest:
        return False
    manifest_path = paths.state_dir / install_manifest.MANIFEST_NAME
    try:
        manifest = install_manifest.load_manifest(manifest_path)
    except (OSError, install_manifest.ManifestError):
        return False
    if install_manifest.manifest_digest(manifest) != candidate_digest:
        return False
    entry = next(
        (
            item
            for item in manifest.get("files", [])
            if isinstance(item, dict)
            and str(item.get("path") or "") == destination.name
        ),
        None,
    )
    try:
        if entry is None:
            return False
        if destination.stat().st_size != int(entry.get("size") or -1):
            return False
        if install_manifest.sha256_file(destination) != str(
            entry.get("sha256") or ""
        ):
            return False
        health_check(destination.parent, manifest)
    except (OSError, TypeError, ValueError, InstallError):
        return False
    quarantine = str(journal.get("quarantine") or "")
    try:
        generation = int(journal.get("recovery_generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    _write_state(
        paths.state_dir,
        _state_payload(
            destination,
            manifest,
            transaction_id=str(journal.get("transaction_id") or ""),
            install_id=str(journal.get("install_id") or ""),
            previous=journal.get("previous_install") or {},
            status="committed",
            manifest_path=str(
                journal.get("manifest_path")
                or (paths.state_dir / install_manifest.MANIFEST_NAME)
            ),
            recovery_bundle=str(journal.get("recovery_bundle") or ""),
            recovery_generation=generation,
            quarantine=quarantine,
            quarantine_state_dir=str(journal.get("quarantine_state_dir") or ""),
            quarantine_reason=str(journal.get("quarantine_reason") or ""),
            created=str(journal.get("created") or ""),
        ),
    )
    return True


def _journal_path(
    destination: Path,
    paths: TransactionPaths,
    raw: object,
    field: str,
    *,
    default: Optional[Path] = None,
    root_only: bool = True,
    allow_manifest_name: bool = False,
) -> str:
    """Validate one journal path and return a canonical path string."""
    if raw is None or raw == "":
        return str(default) if default is not None else ""
    if not isinstance(raw, str) or not raw.strip():
        raise InstallError(f"recovery journal {field} is not a path")
    if allow_manifest_name and raw == install_manifest.MANIFEST_NAME:
        return raw
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise InstallError(
            f"recovery journal {field} is not absolute: {raw!r}"
        )
    try:
        resolved = candidate.resolve(strict=False)
        root = paths.root.resolve(strict=False)
        destination_resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallError(
            f"recovery journal {field} could not be resolved: {raw!r}: {exc}"
        ) from exc

    bases = [root] if root_only else [root, destination_resolved]
    if not any(
        resolved == base or base in resolved.parents for base in bases
    ):
        raise InstallError(
            f"recovery journal {field} escapes the destination or "
            f"transaction root: {resolved}"
        )
    if resolved == root or resolved == destination_resolved:
        raise InstallError(
            f"recovery journal {field} names a protected root: {resolved}"
        )
    if root_only and (
        resolved == destination_resolved
        or destination_resolved in resolved.parents
    ):
        raise InstallError(
            f"recovery journal {field} points at the destination: {resolved}"
        )
    return str(resolved)


def _validated_recovery_journal(
    destination: Path,
    paths: TransactionPaths,
    journal: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a journal copy with every operational path validated.

    Crash recovery moves and deletes paths named by the journal.  A damaged
    or malicious journal must never be able to reach outside the transaction
    root (or the manifest inside the destination) or escape through a symlink.
    """
    values = dict(journal)
    raw_destination = values.get("destination")
    if raw_destination not in (None, ""):
        if not isinstance(raw_destination, str) or not raw_destination.strip():
            raise InstallError("recovery journal destination is not a path")
        candidate = Path(raw_destination).expanduser()
        if not candidate.is_absolute():
            raise InstallError(
                "recovery journal destination is not absolute: "
                f"{raw_destination!r}"
            )
        try:
            resolved_destination = candidate.resolve(strict=False)
            expected_destination = destination.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallError(
                "recovery journal destination could not be resolved: "
                f"{raw_destination!r}: {exc}"
            ) from exc
        if resolved_destination != expected_destination:
            raise InstallError(
                "recovery journal destination does not match the requested "
                f"destination: {resolved_destination}"
            )
        values["destination"] = str(resolved_destination)

    values["staging"] = _journal_path(
        destination, paths, values.get("staging"), "staging",
        default=paths.stage,
    )
    values["previous"] = _journal_path(
        destination, paths, values.get("previous"), "previous",
        default=paths.previous,
    )
    values["state_dir"] = _journal_path(
        destination, paths, values.get("state_dir"), "state_dir",
        default=paths.state_dir,
    )
    values["previous_state_dir"] = _journal_path(
        destination, paths, values.get("previous_state_dir"),
        "previous_state_dir", default=paths.previous_state_dir,
    )
    values["quarantine_state_dir"] = _journal_path(
        destination, paths, values.get("quarantine_state_dir"),
        "quarantine_state_dir", default=paths.quarantine_state_dir,
    )
    values["quarantine"] = _journal_path(
        destination, paths, values.get("quarantine"), "quarantine",
    )
    values["recovery_bundle"] = _journal_path(
        destination, paths, values.get("recovery_bundle"),
        "recovery_bundle",
    )
    values["manifest_path"] = _journal_path(
        destination, paths, values.get("manifest_path"), "manifest_path",
        root_only=False, allow_manifest_name=True,
    )
    return values


def _recover_file_transaction(
    destination: Path,
    paths: TransactionPaths,
    journal: Mapping[str, Any],
) -> None:
    """Recover a single-file transaction left by a crash."""
    status = str(journal.get("status") or "")
    previous = Path(str(journal.get("previous") or paths.previous))
    stage = Path(str(journal.get("staging") or paths.stage))
    state_dir = Path(str(journal.get("state_dir") or paths.state_dir))
    previous_state = Path(
        str(journal.get("previous_state_dir") or paths.previous_state_dir)
    )
    quarantine = _journal_quarantine(journal)
    quarantine_state = Path(
        str(journal.get("quarantine_state_dir") or paths.quarantine_state_dir)
    )
    try:
        if status == "committed":
            _prune_after_commit(destination)
            _remove_path(previous)
            _remove_path(previous_state)
            _remove_path(stage)
            _finish_recovery(paths)
            return
        if status in ("prepared", "staged"):
            if quarantine is None or not quarantine.exists():
                _remove_quarantine_metadata(paths)
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        if status == "rolling_back":
            source = previous if previous.exists() else quarantine
            source_exists = source is not None and source.exists()
            if destination.exists() and source_exists:
                _remove_path(destination)
            if not destination.exists() and source_exists:
                os.replace(source, destination)
                if source == quarantine:
                    _remove_quarantine_metadata_for(paths, quarantine)
            state_source = (
                previous_state if previous_state.exists() else quarantine_state
            )
            if state_dir.exists() and state_source.exists():
                _remove_path(state_dir)
            if not state_dir.exists() and state_source.exists():
                os.replace(state_source, state_dir)
            activated = journal.get("activated")
            had_current = journal.get("had_current")
            explicit_first_install = (
                had_current is False
                and activated is True
                and not source_exists
            )
            legacy_first_install = (
                had_current is None
                and not journal.get("previous_install")
                and not source_exists
                and _is_candidate_state(
                    state_dir, state_dir / STATE_NAME, journal
                )
            )
            if explicit_first_install or legacy_first_install:
                # A first install has no old file to restore. If its rollback
                # crashed after activation, remove the uncommitted candidate
                # and any sidecar left by a partially completed rollback.
                if destination.exists():
                    _remove_path(destination)
                _remove_path(state_dir)
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        if status in ("swapping", "committing", "quarantining"):
            staged_pyz = stage / "root" / destination.name
            if (
                destination.exists()
                and not staged_pyz.exists()
                and _recover_file_candidate_state(destination, paths, journal)
            ):
                _prune_after_commit(destination)
                _remove_path(stage)
                _remove_path(previous)
                _remove_path(previous_state)
                _finish_recovery(paths)
                return
            old_moved = previous.exists() or (
                quarantine is not None and quarantine.exists()
            )
            if (
                status == "quarantining"
                and destination.exists()
                and not staged_pyz.exists()
                and not old_moved
            ):
                if quarantine_state.exists():
                    if state_dir.exists():
                        _remove_path(state_dir)
                    os.replace(quarantine_state, state_dir)
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            if stage.exists() and destination.exists() and not old_moved:
                # The swap journal was written but no destination rename had
                # happened.  Keep the current file and restore its sidecar.
                if previous_state.exists():
                    if state_dir.exists():
                        _remove_path(state_dir)
                    os.replace(previous_state, state_dir)
                elif quarantine_state.exists():
                    if state_dir.exists():
                        _remove_path(state_dir)
                    os.replace(quarantine_state, state_dir)
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            if destination.exists():
                _remove_path(destination)
            if previous.exists():
                os.replace(previous, destination)
            elif quarantine is not None and quarantine.exists():
                os.replace(quarantine, destination)
                _remove_quarantine_metadata_for(paths, quarantine)
            if state_dir.exists():
                _remove_path(state_dir)
            if previous_state.exists():
                os.replace(previous_state, state_dir)
            elif quarantine_state.exists():
                os.replace(quarantine_state, state_dir)
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        _remove_path(stage)
        _drop_journal_bundle(journal)
        _finish_recovery(paths)
    except OSError as exc:
        raise InstallError(
            f"could not recover the interrupted file install {destination}: {exc}; "
            f"journal kept at {paths.journal}"
        ) from exc


def _journal_quarantine(journal: Mapping[str, Any]) -> Optional[Path]:
    raw = str(journal.get("quarantine") or "")
    return Path(raw) if raw else None


def _journal_bundle_path(journal: Mapping[str, Any]) -> Optional[Path]:
    raw = str(journal.get("recovery_bundle") or "")
    return Path(raw) if raw else None


def _drop_journal_bundle(journal: Mapping[str, Any]) -> None:
    path = _journal_bundle_path(journal)
    if path is not None and (path.exists() or path.is_symlink()):
        _remove_path(path)


def _recover_candidate_state(
    destination: Path, journal: Mapping[str, Any]
) -> Optional[Mapping[str, Any]]:
    """Commit an activated candidate only when its full state verifies."""
    state = _read_json(destination / STATE_NAME)
    candidate_digest = str(journal.get("manifest_digest") or "")
    state_digest = str((state or {}).get("manifest_digest") or "")
    manifest = _manifest_for(destination)
    if not (
        manifest
        and state_digest == candidate_digest
        and install_manifest.manifest_digest(manifest) == candidate_digest
    ):
        return None
    try:
        health_check(destination, manifest)
    except InstallError:
        return None
    quarantine = str(journal.get("quarantine") or "")
    try:
        generation = int(journal.get("recovery_generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    _write_state(
        destination,
        _state_payload(
            destination,
            manifest,
            transaction_id=str(journal.get("transaction_id") or ""),
            install_id=str(journal.get("install_id") or ""),
            previous=journal.get("previous_install") or {},
            status="committed",
            manifest_path=str(
                journal.get("manifest_path") or install_manifest.MANIFEST_NAME
            ),
            recovery_bundle=str(journal.get("recovery_bundle") or ""),
            recovery_generation=generation,
            quarantine=quarantine,
            quarantine_reason=str(journal.get("quarantine_reason") or ""),
            created=str(journal.get("created") or ""),
        ),
    )
    return manifest


def _finish_recovery(paths: TransactionPaths) -> None:
    try:
        paths.journal.unlink()
    except FileNotFoundError:
        pass
    _cleanup_root(paths)


def _recover_interrupted(destination: Path) -> None:
    """Resolve a journal left by a crash before starting a new transaction."""
    destination = _absolute(destination)
    paths = transaction_paths(destination)
    journal = _read_json(paths.journal)
    if not journal:
        return
    journal = _validated_recovery_journal(destination, paths, journal)
    if journal.get("kind") == "file":
        _recover_file_transaction(destination, paths, journal)
        return
    status = str(journal.get("status") or "")
    previous = Path(str(journal.get("previous") or paths.previous))
    stage = Path(str(journal.get("staging") or paths.stage))
    quarantine = _journal_quarantine(journal)
    try:
        if status in ("prepared", "staged"):
            if quarantine is None or not quarantine.exists():
                _remove_quarantine_metadata(paths)
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        if status in ("committed", "rolled_back"):
            if status == "committed":
                _prune_after_commit(destination)
            _remove_path(stage)
            if status == "committed":
                _remove_path(previous)
                previous_state = str(journal.get("previous_state_dir") or "")
                if previous_state:
                    _remove_path(Path(previous_state))
            else:
                _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        if status == "rolling_back":
            # A rollback writes this status before restoring anything, so a
            # crash cannot make a restored current tree look like a candidate.
            source = previous if previous.exists() else quarantine
            source_exists = source is not None and source.exists()
            if destination.exists() and source_exists:
                _remove_path(destination)
            if not destination.exists() and source_exists:
                os.replace(source, destination)
            activated = journal.get("activated")
            had_current = journal.get("had_current")
            explicit_first_install = (
                had_current is False
                and activated is True
                and not source_exists
            )
            legacy_first_install = (
                had_current is None
                and not journal.get("previous_install")
                and not source_exists
                and _is_candidate_state(
                    destination, destination / STATE_NAME, journal
                )
            )
            if (
                explicit_first_install or legacy_first_install
            ) and destination.exists():
                # A first install has no old tree to restore. If its rollback
                # crashed after activation, remove the uncommitted candidate.
                _remove_path(destination)
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
            return
        if status in ("swapping", "committing", "quarantining"):
            if (
                status == "quarantining"
                and destination.exists()
                and not stage.exists()
                and (quarantine is None or not quarantine.exists())
            ):
                # The quarantine rename never happened; the destination is the
                # current tree, not a candidate.  Never delete it on recovery.
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            restore = previous if previous.exists() else quarantine
            if not destination.exists() and restore is not None and restore.exists():
                os.replace(restore, destination)
                if restore == quarantine:
                    _remove_quarantine_metadata_for(paths, quarantine)
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            if destination.exists() and not stage.exists():
                if _recover_candidate_state(destination, journal):
                    _prune_after_commit(destination)
                    _remove_committed_backup(paths, journal)
                    _finish_recovery(paths)
                    return
                _remove_path(destination)
                if restore is not None and restore.exists():
                    os.replace(restore, destination)
                    if restore == quarantine:
                        _remove_quarantine_metadata_for(paths, quarantine)
                elif quarantine is not None:
                    # Never delete the current tree when the only surviving
                    # source was the quarantine directory.
                    if quarantine.exists():
                        os.replace(quarantine, destination)
                        _remove_quarantine_metadata_for(paths, quarantine)
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            if destination.exists():
                # The swap had not started. Leave the current tree in place.
                _remove_path(stage)
                _drop_journal_bundle(journal)
                _finish_recovery(paths)
                return
            _remove_path(stage)
            _drop_journal_bundle(journal)
            _finish_recovery(paths)
    except OSError as exc:
        raise InstallError(
            f"could not recover the interrupted install in {destination}: {exc}; "
            f"journal kept at {paths.journal}"
        ) from exc


def _activate_directory(
    paths: TransactionPaths, *, quarantine: Optional[Path] = None
) -> None:
    if quarantine is not None:
        if quarantine.exists() or quarantine.is_symlink():
            raise InstallError(f"quarantine path already exists: {quarantine}")
        if paths.destination.exists():
            os.replace(paths.destination, quarantine)
    elif paths.destination.exists():
        os.replace(paths.destination, paths.previous)
    os.replace(paths.stage, paths.destination)
    _fsync_directory(paths.destination.parent)
    _fsync_directory(paths.root)


def _rollback_directory(
    paths: TransactionPaths,
    error: BaseException,
    *,
    activated: bool,
    recovery_bundle: str = "",
    quarantine: str = "",
    journal: Optional[Mapping[str, Any]] = None,
) -> None:
    quarantine_path = Path(quarantine) if quarantine else None
    restore = paths.previous
    if not restore.exists() and quarantine_path is not None:
        restore = quarantine_path
    try:
        if paths.journal.exists() and journal is not None:
            rolling = dict(journal)
            rolling["status"] = "rolling_back"
            _write_journal(paths, rolling)
        if activated and paths.destination.exists():
            # The candidate was activated. Remove it before restoring the old
            # tree (or, for a first install, leave no destination at all).
            _remove_path(paths.destination)
        if restore.exists() and not paths.destination.exists():
            os.replace(restore, paths.destination)
        _remove_path(paths.stage)
        if recovery_bundle:
            _remove_recovery_bundle(recovery_bundle)
        if paths.journal.exists():
            if journal is not None:
                _write_journal(paths, journal)
            paths.journal.unlink()
        if quarantine:
            _remove_quarantine_metadata(paths)
        _cleanup_root(paths)
    except OSError as rollback_error:
        location = restore if restore.exists() else paths.previous
        raise InstallError(
            f"install failed ({error}); rollback also failed ({rollback_error}); "
            f"the previous tree is at {location}"
        ) from error


def install_directory(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    health_checker: Optional[Callable[[Path, Mapping[str, Any]], Any]] = None,
    allow_missing_assets: bool = False,
    preserve_unlisted_assets: bool = False,
    create_recovery_bundle: bool = True,
    safety_snapshot: bool = False,
    install_id: Optional[str] = None,
    staged: Optional[TransactionPaths] = None,
) -> InstallReport:
    """Install a verified bundle directory transactionally.

    When ``staged`` comes from :func:`prepare_directory_stage`, ``source`` and
    ``destination`` must be that stage and its destination.  The already
    assembled stage is verified with full asset hashes and activated in place
    instead of being copied into a second staging tree.
    """
    source = _absolute(source)
    destination = _absolute(destination)
    if not source.is_dir():
        raise InstallError(f"bundle directory not found: {source}")
    if staged is not None:
        if _absolute(staged.destination) != destination:
            raise InstallError("prepared stage destination does not match")
        if _absolute(staged.stage) != source:
            raise InstallError("source is not the prepared transaction stage")
    if source == destination:
        raise InstallError("source and destination are the same directory")
    if source in destination.parents or destination in source.parents:
        raise InstallError("source and destination must not contain each other")
    _validate_source_tree(source)
    manifest = _source_manifest(
        source,
        allow_missing_assets=allow_missing_assets,
    )
    manifest_digest = install_manifest.manifest_digest(manifest)
    if dry_run:
        preflight_destination(destination, force=force, kind="directory")
        return InstallReport(
            destination=destination,
            transaction_id="dry-run",
            install_id="",
            manifest_digest=manifest_digest,
            status="dry-run",
            previous_install=_previous_info(destination),
        )

    with install_lock(destination):
        # Recover a crash before deciding that an existing destination is a
        # user conflict. A first install interrupted after activation must not
        # become un-retryable without --force.
        _recover_interrupted(destination)
        preflight_destination(destination, force=force, kind="directory")
        old_manifest = _manifest_for(destination)
        old_previous = _previous_info(destination)
        tx_created = _now()
        install_id = install_id or uuid.uuid4().hex
        paths = staged if staged is not None else transaction_paths(destination)
        _ensure_root(paths)
        if staged is not None and (
            paths.stage.is_symlink() or not paths.stage.is_dir()
        ):
            raise InstallError(
                f"prepared transaction stage is missing: {paths.stage}"
            )
        txid = paths.transaction_id
        current = _inspect_current_install(destination, paths, "directory")
        checker = health_checker or (
            lambda root, current: health_check(root, current)
        )
        activated = False
        recovery = SnapshotResult()
        quarantine = ""
        quarantine_reason = ""
        retention: Optional[RecoveryRetentionReport] = None
        try:
            _write_journal(
                paths,
                _journal_payload(
                    paths,
                    status="prepared",
                    kind="directory",
                    manifest_digest=manifest_digest,
                    install_id=install_id,
                    artifact=manifest.get("artifact") or {},
                    previous=old_previous,
                    created=tx_created,
                ),
            )
            if staged is None:
                paths.stage.mkdir(parents=True, exist_ok=False)
                _copy_source_tree(
                    source, paths.stage, destination, old_manifest, manifest
                )
            _preserve_old_state(
                destination,
                paths.stage,
                old_manifest,
                manifest,
                bundle_source=source,
                preserve_unlisted_assets=preserve_unlisted_assets,
            )
            install_manifest.write_manifest(paths.stage, manifest)
            _verify_stage(paths.stage, manifest)
            result = checker(paths.stage, manifest)
            if result is False:
                raise HealthCheckError("candidate install health check returned false")

            # Snapshot the committed previous install while it is still in
            # place, before any rename can touch the destination.  A corrupt
            # current tree cannot be archived; preserve it in quarantine.
            if current.corrupt:
                quarantine = str(paths.quarantine)
                quarantine_reason = current.reason
                _write_quarantine_metadata(
                    paths,
                    kind="directory",
                    reason=quarantine_reason,
                    diagnostics=_quarantine_diagnostics(
                        destination, paths, "directory"
                    ),
                )
            elif create_recovery_bundle and current.reusable:
                recovery = _create_recovery_bundle(
                    destination,
                    paths,
                    txid,
                    kind="directory",
                    safety_snapshot=safety_snapshot,
                )

            staged_state = _state_payload(
                destination,
                manifest,
                transaction_id=txid,
                install_id=install_id,
                previous=old_previous,
                status="staged",
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                created=tx_created,
            )
            _write_state(paths.stage, staged_state)
            _write_journal(
                paths,
                _journal_payload(
                    paths,
                    status="staged",
                    kind="directory",
                    manifest_digest=manifest_digest,
                    install_id=install_id,
                    artifact=manifest.get("artifact") or {},
                    previous=old_previous,
                    recovery_bundle=recovery.path,
                    recovery_generation=recovery.generation,
                    quarantine=quarantine,
                    quarantine_reason=quarantine_reason,
                    created=tx_created,
                ),
            )
            swap_status = "quarantining" if quarantine else "swapping"
            _write_journal(
                paths,
                _journal_payload(
                    paths,
                    status=swap_status,
                    kind="directory",
                    manifest_digest=manifest_digest,
                    install_id=install_id,
                    artifact=manifest.get("artifact") or {},
                    previous=old_previous,
                    recovery_bundle=recovery.path,
                    recovery_generation=recovery.generation,
                    quarantine=quarantine,
                    quarantine_reason=quarantine_reason,
                    created=tx_created,
                ),
            )
            _activate_directory(
                paths,
                quarantine=Path(quarantine) if quarantine else None,
            )
            activated = True
            result = checker(destination, manifest)
            if result is False:
                raise HealthCheckError("activated install health check returned false")

            committed = _state_payload(
                destination,
                manifest,
                transaction_id=txid,
                install_id=install_id,
                previous=old_previous,
                status="committed",
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                created=tx_created,
            )
            _write_state(destination, committed)
            retention = _prune_after_commit(destination)
            journal_committed = False
            try:
                _write_journal(
                    paths,
                    _journal_payload(
                        paths,
                        status="committed",
                        kind="directory",
                        manifest_digest=manifest_digest,
                        install_id=install_id,
                        artifact=manifest.get("artifact") or {},
                        previous=old_previous,
                        recovery_bundle=recovery.path,
                        recovery_generation=recovery.generation,
                        quarantine=quarantine,
                        quarantine_reason=quarantine_reason,
                        created=tx_created,
                        retention=retention.as_dict() if retention else None,
                    ),
                )
                journal_committed = True
            except OSError:
                # The committed state is already durable. Leave the older
                # journal for crash recovery instead of undoing the install.
                pass
            try:
                if journal_committed and _remove_committed_backup(paths):
                    paths.journal.unlink()
            except OSError:
                # The install is committed. Leave the journal for the next
                # transaction to finish cleanup rather than rolling back a
                # healthy tree over a permission error in a hidden directory.
                pass
            _cleanup_root(paths)
            return InstallReport(
                destination=destination,
                transaction_id=txid,
                install_id=install_id,
                manifest_digest=manifest_digest,
                status="committed",
                previous_install=old_previous,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                retention=retention,
            )
        except BaseException as exc:
            rollback_journal = _journal_payload(
                paths,
                status="rolled_back",
                kind="directory",
                manifest_digest=manifest_digest,
                install_id=install_id,
                artifact=manifest.get("artifact") or {},
                previous=old_previous,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                created=tx_created,
                retention=retention.as_dict() if retention else None,
                error=str(exc),
            )
            rollback_journal["activated"] = activated
            rollback_journal["had_current"] = current.state != "absent"
            _rollback_directory(
                paths,
                exc,
                activated=activated,
                recovery_bundle=recovery.path,
                quarantine=quarantine,
                journal=rollback_journal,
            )
            if isinstance(exc, (InstallError, KeyboardInterrupt, SystemExit)):
                raise
            raise InstallError(f"install failed: {exc}") from exc


def _pyz_manifest(root: Path, name: str) -> Dict[str, Any]:
    manifest = install_manifest.build_manifest(
        root,
        artifact_kind="portable",
        platform="any",
        entrypoints={kind: name for kind in install_manifest.ENTRYPOINT_NAMES},
    )
    install_manifest.write_manifest(root, manifest)
    report = install_manifest.verify_manifest(
        root, manifest, check_extra=True
    )
    if not report.ok:
        raise InstallError("pyz manifest did not verify: " + "; ".join(report.errors))
    return manifest


def install_file(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    health_checker: Optional[Callable[[Path, Mapping[str, Any]], Any]] = None,
    create_recovery_bundle: bool = True,
    safety_snapshot: bool = False,
    install_id: Optional[str] = None,
) -> InstallReport:
    """Install a single-file ``.pyz`` with file-level atomic replacement."""
    source = _absolute(source)
    destination = _absolute(destination)
    if not source.is_file():
        raise InstallError(f"bundle file not found: {source}")
    if source == destination:
        raise InstallError("source and destination are the same file")
    if source.suffix.lower() != ".pyz":
        raise InstallError("single-file installs must be .pyz files")
    if dry_run:
        preflight_destination(destination, force=force, kind="file")
        return InstallReport(
            destination=destination,
            transaction_id="dry-run",
            install_id="",
            manifest_digest="",
            status="dry-run",
            previous_install=_previous_info(transaction_paths(destination).state_dir),
        )

    with install_lock(destination):
        _recover_interrupted(destination)
        preflight_destination(destination, force=force, kind="file")
        previous = _previous_info(transaction_paths(destination).state_dir)
        txid = uuid.uuid4().hex
        tx_created = _now()
        install_id = install_id or uuid.uuid4().hex
        paths = transaction_paths(destination, txid)
        _ensure_root(paths)
        current = _inspect_current_install(destination, paths, "file")
        checker = health_checker or health_check
        stage_root = paths.stage / "root"
        swapped = False
        recovery = SnapshotResult()
        quarantine = ""
        quarantine_reason = ""
        retention: Optional[RecoveryRetentionReport] = None
        manifest_path = str(paths.state_dir / install_manifest.MANIFEST_NAME)
        manifest: Dict[str, Any] = {}
        manifest_digest = ""

        def journal(status: str, *, error: str = "") -> Dict[str, Any]:
            return _journal_payload(
                paths,
                status=status,
                kind="file",
                manifest_digest=manifest_digest,
                install_id=install_id,
                artifact=manifest.get("artifact") or {},
                previous=previous,
                manifest_path=manifest_path,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                created=tx_created,
                retention=retention.as_dict() if retention else None,
                error=error,
            )

        try:
            _write_journal(paths, journal("prepared"))
            if current.state == "absent":
                # A stale sidecar can outlive its destination after an
                # interrupted first install. It cannot describe a usable
                # install, and leaving it would block the new sidecar.
                _remove_path(paths.state_dir)
            stage_root.mkdir(parents=True, exist_ok=False)
            staged_pyz = stage_root / destination.name
            _copy_file(source, staged_pyz)
            try:
                staged_pyz.chmod(staged_pyz.stat().st_mode | 0o111)
            except OSError:
                pass
            manifest = _pyz_manifest(stage_root, destination.name)
            manifest_digest = install_manifest.manifest_digest(manifest)
            result = checker(stage_root, manifest)
            if result is False:
                raise HealthCheckError("candidate pyz health check returned false")

            if current.corrupt:
                quarantine = str(paths.quarantine)
                quarantine_reason = current.reason
                _write_quarantine_metadata(
                    paths,
                    kind="file",
                    reason=quarantine_reason,
                    diagnostics=_quarantine_diagnostics(
                        destination, paths, "file"
                    ),
                )
            elif create_recovery_bundle and current.reusable:
                recovery = _create_recovery_bundle(
                    destination,
                    paths,
                    txid,
                    kind="file",
                    safety_snapshot=safety_snapshot,
                )

            _remove_path(paths.previous_state_dir)
            staged_state = _state_payload(
                destination,
                manifest,
                transaction_id=txid,
                install_id=install_id,
                previous=previous,
                status="staged",
                manifest_path=manifest_path,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_state_dir=(
                    str(paths.quarantine_state_dir) if quarantine else ""
                ),
                quarantine_reason=quarantine_reason,
                created=tx_created,
            )
            _write_state(stage_root, staged_state)
            _write_journal(paths, journal("staged"))
            _write_journal(
                paths, journal("quarantining" if quarantine else "swapping")
            )
            if quarantine:
                if paths.quarantine.exists() or paths.quarantine.is_symlink():
                    raise InstallError(
                        f"quarantine path already exists: {paths.quarantine}"
                    )
                if paths.quarantine_state_dir.exists():
                    raise InstallError(
                        f"quarantine state path already exists: "
                        f"{paths.quarantine_state_dir}"
                    )
                if paths.state_dir.exists():
                    os.replace(paths.state_dir, paths.quarantine_state_dir)
                if destination.exists():
                    os.replace(destination, paths.quarantine)
            else:
                if paths.state_dir.exists():
                    os.replace(paths.state_dir, paths.previous_state_dir)
                if destination.exists():
                    os.replace(destination, paths.previous)
            os.replace(staged_pyz, destination)
            swapped = True
            _fsync_directory(destination.parent)
            _fsync_directory(paths.root)
            paths.state_dir.mkdir(parents=True, exist_ok=False)
            shutil.copy2(
                stage_root / install_manifest.MANIFEST_NAME,
                paths.state_dir / install_manifest.MANIFEST_NAME,
            )
            committed = _state_payload(
                destination,
                manifest,
                transaction_id=txid,
                install_id=install_id,
                previous=previous,
                status="committed",
                manifest_path=manifest_path,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_state_dir=(
                    str(paths.quarantine_state_dir) if quarantine else ""
                ),
                quarantine_reason=quarantine_reason,
                created=tx_created,
            )
            _write_state(paths.state_dir, committed)
            _fsync_directory(paths.root)
            _write_journal(paths, journal("committing"))
            result = checker(destination.parent, manifest)
            if result is False:
                raise HealthCheckError("activated pyz health check returned false")

            retention = _prune_after_commit(destination)
            journal_committed = False
            try:
                _write_journal(paths, journal("committed"))
                journal_committed = True
            except OSError:
                # The committed state is durable; leave the journal for crash
                # recovery rather than undoing a healthy activated file.
                pass
            try:
                _remove_path(paths.stage)
                if journal_committed and _remove_committed_backup(paths):
                    paths.journal.unlink()
            except OSError:
                pass
            _cleanup_root(paths)
            return InstallReport(
                destination=destination,
                transaction_id=txid,
                install_id=install_id,
                manifest_digest=manifest_digest,
                status="committed",
                previous_install=previous,
                recovery_bundle=recovery.path,
                recovery_generation=recovery.generation,
                quarantine=quarantine,
                quarantine_reason=quarantine_reason,
                retention=retention,
            )
        except BaseException as exc:
            try:
                rollback_journal = journal("rolled_back", error=str(exc))
                rollback_journal["activated"] = swapped
                rollback_journal["had_current"] = current.state != "absent"
                rolling = dict(rollback_journal)
                rolling["status"] = "rolling_back"
                if paths.journal.exists():
                    _write_journal(paths, rolling)
                if swapped and destination.exists():
                    _remove_path(destination)
                if not destination.exists():
                    if paths.previous.exists():
                        os.replace(paths.previous, destination)
                    elif quarantine and Path(quarantine).exists():
                        os.replace(Path(quarantine), destination)
                if swapped:
                    if paths.state_dir.exists():
                        _remove_path(paths.state_dir)
                    if paths.previous_state_dir.exists():
                        os.replace(paths.previous_state_dir, paths.state_dir)
                    elif quarantine and paths.quarantine_state_dir.exists():
                        os.replace(paths.quarantine_state_dir, paths.state_dir)
                elif paths.previous_state_dir.exists():
                    if paths.state_dir.exists():
                        _remove_path(paths.state_dir)
                    os.replace(paths.previous_state_dir, paths.state_dir)
                elif (
                    quarantine
                    and paths.quarantine_state_dir.exists()
                    and not paths.state_dir.exists()
                ):
                    os.replace(paths.quarantine_state_dir, paths.state_dir)
                _remove_path(paths.stage)
                if recovery.path:
                    _remove_recovery_bundle(recovery.path)
                if paths.journal.exists():
                    _write_journal(paths, rollback_journal)
                    paths.journal.unlink()
                if quarantine:
                    _remove_quarantine_metadata(paths)
                _cleanup_root(paths)
            except OSError as rollback_error:
                raise InstallError(
                    f"pyz install failed ({exc}); rollback also failed "
                    f"({rollback_error}); previous file is at {paths.previous}"
                ) from exc
            if isinstance(exc, (InstallError, KeyboardInterrupt, SystemExit)):
                raise
            raise InstallError(f"pyz install failed: {exc}") from exc
