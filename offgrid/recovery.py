"""Deterministic, secret-free recovery archives for an installed tree.

This module is deliberately independent of the install transaction and the
command-line interface.  The transaction can call :func:`write_recovery_archive`
before it moves the previous tree aside, and a later ``--verify``/``--recover``
command can use :func:`verify_recovery_archive`.

A recovery archive is a directory so it can live on any filesystem.  Its
contents are:

``RECOVERY.json``
    The recovery metadata: install identity, previous install, manifest digest,
    reason, transaction and the restore command.
``install-manifest.json`` and ``install-state.json``
    The previous install's verified metadata.
``offgrid/``, ``launch/``, ``scripts/``, launchers and other app files
    The previous app tree, excluding runtimes, models, user state and caches.
``config.json``
    A sanitized copy with credentials removed.
``RECOVER.md``
    A short human-readable recovery note.
``SHA256SUMS``
    SHA-256 hashes over every other file in the archive.

The writer copies file bytes and uses modes derived from the install manifest,
not source mtimes.  It does not copy models, runtimes, ``.offgrid/``,
``.downloads/`` or the transaction directory.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import install_manifest

SCHEMA = "offgrid.recovery/1"
PLAN_SCHEMA = "offgrid.recovery-plan/1"
RECOVERY_NAME = "RECOVERY.json"
SUMS_NAME = "SHA256SUMS"
RECOVER_NAME = "RECOVER.md"
STATE_NAME = "install-state.json"
STATE_SCHEMA = "offgrid.install-state/1"
RESTORE_COMMAND = "offgrid protect --recover <this archive>"
MAX_REASON = 500
MAX_RECOVERY_BYTES = 256 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_SUMS_BYTES = 8 * 1024 * 1024
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SUMS_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")

CAPTURE_DIRECTORIES = ("offgrid", "launch", "scripts")
CAPTURE_FILES = (
    "OffGrid.sh",
    "OffGrid.command",
    "OffGrid.bat",
    "README-FIRST.txt",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "offgrid.pyz",
)
SKIP_DIRECTORIES = {
    ".downloads",
    ".git",
    ".offgrid",
    ".offgrid-install",
    ".pytest_cache",
    "__pycache__",
    "models",
    "runtime",
}
SKIP_SUFFIXES = (".pyc", ".pyo")
GENERATED_FILES = frozenset(
    {RECOVERY_NAME, SUMS_NAME, RECOVER_NAME, install_manifest.MANIFEST_NAME, STATE_NAME}
)

# Only these Config fields may enter a recovery archive.  This is an
# allowlist, not a secret-name denylist: an unknown or newly added field is
# dropped, so an unfamiliar nested credential spelling cannot leak.  Credential
# fields such as ``api_key`` and ``fleet_token`` are intentionally absent.
SAFE_CONFIG_FIELDS = frozenset(
    {
        "agents_file",
        "allow_dangerous_devices",
        "allow_public_council",
        "allow_remote",
        "caretaker",
        "caretaker_household",
        "color",
        "command_policy",
        "context_chars",
        "council",
        "council_auto",
        "council_auto_limit",
        "council_max_chars",
        "council_members",
        "council_timeout",
        "device_policy",
        "device_timeout",
        "devices_audit",
        "devices_file",
        "effort",
        "endpoint",
        "endpoints",
        "escalate",
        "escalation_model",
        "fleet",
        "fleet_exec",
        "fleet_file",
        "fleet_port",
        "fleet_timeout",
        "household",
        "lexicon_file",
        "library_dir",
        "listen",
        "listen_seconds",
        "maintenance_improve",
        "maintenance_interval",
        "max_agent_history",
        "max_output",
        "max_tokens",
        "max_turns",
        "memory",
        "memory_dir",
        "memory_limit",
        "model",
        "native_tools",
        "network",
        "offline",
        "parallel",
        "plan_candidates",
        "principal",
        "quality_gate",
        "quality_rounds",
        "recon",
        "reflect",
        "request_timeout",
        "research_cache",
        "research_cache_ttl",
        "research_online",
        "research_refresh",
        "research_timeout",
        "review_lessons",
        "review_passes",
        "review_rounds",
        "scale",
        "security_history",
        "security_review",
        "self_improve_idle",
        "self_improve_idle_minutes",
        "self_improve_keep",
        "self_improve_retention_days",
        "session_dir",
        "signals_audit",
        "signals_policy",
        "signals_port",
        "signals_timeout",
        "speak",
        "stt_command",
        "stt_language",
        "stt_model",
        "sync_allow_http",
        "sync_interval",
        "sync_log",
        "sync_peers",
        "sync_port",
        "sync_timeout",
        "sync_token_file",
        "temperature",
        "test_fix_rounds",
        "verbose",
        "verify_command",
        "voice",
        "watch_file",
        "workspace",
    }
)

# The identity metadata copied from install-state.json is not Config.  Keep
# only its documented fields and the manifest artifact fields we understand.
PREVIOUS_INSTALL_FIELDS = frozenset(
    {
        "artifact",
        "destination",
        "install_id",
        "manifest_digest",
        "transaction_id",
    }
)
SAFE_ARTIFACT_FIELDS = frozenset(
    {"created", "kind", "platform", "source_sha256", "version"}
)
_UNSAFE_VALUE = object()


class RecoveryError(ValueError):
    """A recovery archive is malformed, unsafe, modified or unsupported."""


@dataclass(frozen=True)
class RecoveryReport:
    """The result of verifying a recovery archive."""

    path: Path
    version: str
    install_id: str
    manifest_digest: str
    files: int
    bytes: int
    config_sanitized: bool

    @property
    def ok(self) -> bool:
        return True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "path": str(self.path),
            "version": self.version,
            "install_id": self.install_id,
            "manifest_digest": self.manifest_digest,
            "files": self.files,
            "bytes": self.bytes,
            "config_sanitized": self.config_sanitized,
        }


@dataclass(frozen=True)
class RecoveryRestoreReport:
    """The result of restoring a verified recovery archive."""

    archive: Path
    destination: Path
    version: str
    install_id: str
    manifest_digest: str
    files: int
    bytes: int
    safety_snapshot: str = ""
    safety_generation: int = 0
    asset_source: str = ""
    lineage: str = ""
    lineage_reason: str = ""
    confirmation_required: bool = False
    destination_newer: bool = False
    destination_older: bool = False

    @property
    def ok(self) -> bool:
        return True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "restored": True,
            "archive": str(self.archive),
            "destination": str(self.destination),
            "version": self.version,
            "install_id": self.install_id,
            "manifest_digest": self.manifest_digest,
            "files": self.files,
            "bytes": self.bytes,
            "safety_snapshot": self.safety_snapshot,
            "safety_generation": self.safety_generation,
            "asset_source": self.asset_source,
            "lineage": self.lineage,
            "lineage_reason": self.lineage_reason,
            "confirmation_required": self.confirmation_required,
            "destination_newer": self.destination_newer,
            "destination_older": self.destination_older,
        }


@dataclass(frozen=True)
class RecoveryReadinessReport:
    """Whether a verified archive can currently be restored."""

    archive: Path
    destination: Path
    ready: bool
    reason: str
    report: Optional[RecoveryReport] = None
    lineage: str = ""
    lineage_reason: str = ""
    confirmation_required: bool = False
    destination_newer: bool = False
    destination_older: bool = False

    @property
    def ok(self) -> bool:
        return self.ready

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "archive": str(self.archive),
            "destination": str(self.destination),
            "ready": self.ready,
            "reason": self.reason,
            "lineage": self.lineage,
            "lineage_reason": self.lineage_reason,
            "confirmation_required": self.confirmation_required,
            "destination_newer": self.destination_newer,
            "destination_older": self.destination_older,
        }
        if self.report is not None:
            payload.update(
                {
                    "version": self.report.version,
                    "install_id": self.report.install_id,
                    "manifest_digest": self.report.manifest_digest,
                    "files": self.report.files,
                    "bytes": self.report.bytes,
                    "config_sanitized": self.report.config_sanitized,
                }
            )
        return payload


@dataclass(frozen=True)
class RestorePlanFile:
    """One file that a restore would write or leave unchanged."""

    path: str
    action: str
    kind: str
    size: int
    executable: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "kind": self.kind,
            "size": self.size,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class RestorePlanAsset:
    """The destination status of one required runtime or model."""

    path: str
    kind: str
    required: bool
    present: bool
    status: str
    source: str = ""
    sha256: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "required": self.required,
            "present": self.present,
            "status": self.status,
            "source": self.source,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class RecoveryRestorePlan:
    """A read-only description of the changes a restore would make."""

    archive: Path
    destination: Path
    ready: bool
    reason: str
    file_restore: bool = False
    overwrite: bool = False
    force: bool = False
    drift: Tuple[str, ...] = ()
    affected_files: Tuple[RestorePlanFile, ...] = ()
    preserved_state: Tuple[str, ...] = ()
    required_assets: Tuple[RestorePlanAsset, ...] = ()
    asset_source: str = ""
    lineage: str = ""
    lineage_reason: str = ""
    confirmation_required: bool = False
    destination_newer: bool = False
    destination_older: bool = False
    report: Optional[RecoveryReport] = None
    plan_schema: str = PLAN_SCHEMA
    plan_id: str = ""
    blockers: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    destination_fingerprint: str = ""
    safety_snapshot: str = ""
    safety_generation: int = 0
    safety_snapshot_protected: bool = False
    available_bytes: int = 0
    required_bytes: int = 0
    artifact_platform: str = ""
    current_platform: str = ""
    existing_app_sha256: str = ""
    existing_app_size: int = 0

    @property
    def ok(self) -> bool:
        return self.ready

    @property
    def force_required(self) -> bool:
        """Recovery replacement is transactional, so --force is optional."""

        return False

    @property
    def force_status(self) -> str:
        if not self.overwrite:
            return "not needed"
        if self.force:
            return "supplied"
        return "not required; existing destination will be replaced"

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "dry_run": True,
            "ready": self.ready,
            "reason": self.reason,
            "archive": str(self.archive),
            "destination": str(self.destination),
            "file_restore": self.file_restore,
            "overwrite": self.overwrite,
            "force": self.force,
            "force_required": self.force_required,
            "force_status": self.force_status,
            "drift": list(self.drift),
            "affected_files": [item.as_dict() for item in self.affected_files],
            "preserved_state": list(self.preserved_state),
            "required_assets": [item.as_dict() for item in self.required_assets],
            "asset_source": self.asset_source,
            "lineage": self.lineage,
            "lineage_reason": self.lineage_reason,
            "confirmation_required": self.confirmation_required,
            "destination_newer": self.destination_newer,
            "destination_older": self.destination_older,
            "plan_schema": self.plan_schema,
            "plan_id": self.plan_id,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "destination_fingerprint": self.destination_fingerprint,
            "safety_snapshot": self.safety_snapshot,
            "safety_generation": self.safety_generation,
            "safety_snapshot_protected": self.safety_snapshot_protected,
            "available_bytes": self.available_bytes,
            "required_bytes": self.required_bytes,
            "artifact_platform": self.artifact_platform,
            "current_platform": self.current_platform,
            "existing_app_sha256": self.existing_app_sha256,
            "existing_app_size": self.existing_app_size,
        }
        if self.report is not None:
            payload.update(
                {
                    "version": self.report.version,
                    "install_id": self.report.install_id,
                    "manifest_digest": self.report.manifest_digest,
                    "files": self.report.files,
                    "bytes": self.report.bytes,
                    "config_sanitized": self.report.config_sanitized,
                }
            )
        return payload


@dataclass(frozen=True)
class _RestoreLineage:
    """How the destination install relates to the archived install."""

    relation: str
    reason: str
    confirmation_required: bool
    destination_newer: bool = False
    destination_older: bool = False
    archive_install_id: str = ""
    destination_install_id: str = ""
    archive_version: str = ""
    destination_version: str = ""
    archive_created: str = ""
    destination_created: str = ""


def _lineage_fields(lineage: Optional[_RestoreLineage]) -> Dict[str, Any]:
    """Flatten a lineage decision for public reports and plans."""
    if lineage is None:
        return {
            "lineage": "",
            "lineage_reason": "",
            "confirmation_required": False,
            "destination_newer": False,
            "destination_older": False,
        }
    return {
        "lineage": lineage.relation,
        "lineage_reason": lineage.reason,
        "confirmation_required": lineage.confirmation_required,
        "destination_newer": lineage.destination_newer,
        "destination_older": lineage.destination_older,
    }


@dataclass(frozen=True)
class _RestorePreflight:
    """The verified archive and destination decisions shared by status/restore."""

    verified: RecoveryReport
    payload: Mapping[str, Any]
    manifest: Mapping[str, Any]
    target: Path
    pyz: Path
    file_restore: bool
    missing_assets: Tuple[str, ...] = ()
    asset_source: Optional[Path] = None
    lineage: Optional[_RestoreLineage] = None


def recovery_readiness(
    archive: Path,
    destination: Optional[Path] = None,
    *,
    asset_source: Optional[Path] = None,
    confirm: bool = False,
) -> RecoveryReadinessReport:
    """Verify *archive* and report whether it can restore *destination*.

    This is read-only. It performs the same destination, lineage and
    required-asset checks that :func:`restore_recovery_archive` will perform,
    including an optional explicit *asset_source*, without creating or
    replacing anything.  When the destination is newer, older or from a
    different install lineage, *confirm* must be true for the report to be
    ready.
    """
    archive_path = Path(archive).expanduser()
    target_hint = (
        Path(destination).expanduser() if destination is not None else archive_path
    )
    verified: Optional[RecoveryReport] = None
    try:
        verified = verify_recovery_archive(archive_path)
        preflight = _restore_preflight(
            archive_path,
            destination,
            verified=verified,
            asset_source=asset_source,
        )
    except (RecoveryError, OSError) as exc:
        return RecoveryReadinessReport(
            archive=archive_path,
            destination=target_hint,
            ready=False,
            reason=str(exc),
            report=verified,
        )
    reason = "archive verifies and the destination has its required assets"
    if preflight.missing_assets:
        reason = (
            "archive verifies; required assets will be reused from "
            f"{preflight.asset_source}"
        )
    lineage = preflight.lineage
    ready = True
    if lineage is not None and lineage.confirmation_required and not confirm:
        ready = False
        reason = (
            "restore requires explicit confirmation: " + lineage.reason
        )
    return RecoveryReadinessReport(
        archive=preflight.verified.path,
        destination=preflight.target,
        ready=ready,
        reason=reason,
        report=preflight.verified,
        **_lineage_fields(lineage),
    )


def recovery_restore_plan(
    archive: Path,
    destination: Optional[Path] = None,
    *,
    force: bool = False,
    asset_source: Optional[Path] = None,
    confirm: bool = False,
) -> RecoveryRestorePlan:
    """Describe a restore without creating, replacing or staging anything.

    The plan uses the same preflight as :func:`restore_recovery_archive`, so
    a rejected plan means the real restore would reject the destination too.
    An unmanaged, newer, older or different-lineage destination is reported as
    not ready unless *confirm* is true.  Hashing and metadata inspection are
    read-only.
    The returned ``plan_id`` can be supplied to
    :func:`restore_recovery_archive` as ``expect_plan`` to ensure the state
    reviewed by the operator is still the state being restored.
    """
    archive_path = Path(archive).expanduser()
    target_hint = (
        Path(destination).expanduser() if destination is not None else archive_path
    )
    if destination is not None and not target_hint.is_absolute():
        target_hint = Path.cwd() / target_hint
    try:
        target_hint = target_hint.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        pass

    verified: Optional[RecoveryReport] = None
    try:
        verified = verify_recovery_archive(archive_path)
        preflight = _restore_preflight(
            archive_path,
            destination,
            verified=verified,
            asset_source=asset_source,
            allow_missing_assets=True,
        )
    except (RecoveryError, OSError) as exc:
        plan = RecoveryRestorePlan(
            archive=archive_path,
            destination=target_hint,
            ready=False,
            reason=str(exc),
            overwrite=target_hint.exists() or target_hint.is_symlink(),
            force=force,
            asset_source=(
                str(Path(asset_source).expanduser()) if asset_source else ""
            ),
            report=verified,
            blockers=(str(exc),),
        )
        return replace(plan, plan_id=_restore_plan_id(plan))

    return _build_restore_plan(
        preflight,
        force=force,
        confirm=confirm,
    )


def _plan_archive_entries(
    preflight: _RestorePreflight,
) -> List[Mapping[str, Any]]:
    entries: List[Mapping[str, Any]] = []
    if preflight.file_restore:
        app_relative = preflight.pyz.relative_to(
            preflight.verified.path
        ).as_posix()
        for entry in preflight.manifest.get("files") or []:
            if (
                isinstance(entry, dict)
                and str(entry.get("path") or "") == app_relative
            ):
                entries.append(entry)
        return entries

    for section in ("files", "user_state"):
        for entry in preflight.manifest.get(section) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("present") is False:
                continue
            entries.append(entry)
    return entries


def _plan_current_manifest(
    target: Path, file_restore: bool
) -> Optional[Mapping[str, Any]]:
    """Read current install metadata for plan comparisons, if it exists."""
    if file_restore:
        from . import install_transaction

        root = install_transaction.transaction_paths(target).state_dir
    else:
        root = target
    manifest_path = root / install_manifest.MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        return install_manifest.load_manifest(manifest_path)
    except (OSError, install_manifest.ManifestError):
        return None


def _plan_affected_files(
    preflight: _RestorePreflight,
) -> Tuple[RestorePlanFile, ...]:
    result: List[RestorePlanFile] = []
    target = preflight.target
    archived: set = set()
    for entry in _plan_archive_entries(preflight):
        relative = str(entry.get("path") or "")
        if not relative:
            continue
        archived.add(relative)
        source = preflight.verified.path.joinpath(
            *PurePosixPath(relative).parts
        )
        if not source.is_file() or source.is_symlink():
            continue
        destination = (
            target
            if preflight.file_restore
            else target.joinpath(*PurePosixPath(relative).parts)
        )
        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            action = "replace"
        elif not destination.is_file():
            action = "create"
        elif _asset_hash_matches(destination, str(entry.get("sha256") or "")):
            action = "unchanged"
        else:
            action = "replace"
        result.append(
            RestorePlanFile(
                path=target.name if preflight.file_restore else relative,
                action=action,
                kind=str(entry.get("kind") or "file"),
                size=int(entry.get("size") or 0),
                executable=bool(entry.get("executable")),
            )
        )

    if not preflight.file_restore:
        current_manifest = _plan_current_manifest(target, False)
        if current_manifest is not None:
            for entry in current_manifest.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                relative = str(entry.get("path") or "")
                if not relative or relative in archived:
                    continue
                result.append(
                    RestorePlanFile(
                        path=relative,
                        action="remove",
                        kind=str(entry.get("kind") or "file"),
                        size=int(entry.get("size") or 0),
                        executable=bool(entry.get("executable")),
                    )
                )
    result.sort(key=lambda item: item.path)
    return tuple(result)


def _plan_preserved_state(
    target: Path,
    file_restore: bool,
    manifest: Mapping[str, Any],
) -> Tuple[str, ...]:
    root = target.parent if file_restore else target
    if not root.is_dir():
        return ()
    archived: set = set()
    if not file_restore:
        for section in ("files", "user_state"):
            for entry in manifest.get(section) or []:
                if isinstance(entry, dict):
                    archived.add(str(entry.get("path") or ""))
    preserved: List[str] = []
    for relative in (".offgrid", "models", "runtime"):
        path = root / relative
        if path.exists() or path.is_symlink():
            preserved.append(relative + "/" if path.is_dir() else relative)
    for relative in ("config.json", "MODEL.txt"):
        path = root / relative
        if relative in archived:
            continue
        if path.exists() or path.is_symlink():
            preserved.append(relative)
    return tuple(preserved)


def _plan_runtime_asset(
    root: Path,
    entry: Mapping[str, Any],
    *,
    source: str = "destination",
) -> RestorePlanAsset:
    relative = str(entry.get("path") or "")
    directory = root.joinpath(*PurePosixPath(relative).parts)
    required = bool(entry.get("required"))
    present = directory.is_dir() and not directory.is_symlink()
    issues: List[str] = []
    if not present:
        issues.append("missing")
    else:
        binary = str(entry.get("binary") or "")
        if binary:
            binary_path = directory.joinpath(*PurePosixPath(binary).parts)
            if binary_path.is_symlink() or not binary_path.is_file():
                issues.append("missing binary")
            else:
                expected = int(entry.get("binary_size") or 0)
                if binary_path.stat().st_size != expected:
                    issues.append("size mismatch")
                elif not _asset_hash_matches(
                    binary_path, str(entry.get("binary_sha256") or "")
                ):
                    issues.append("hash mismatch")
        fingerprint = str(entry.get("fingerprint") or "")
        if fingerprint:
            fingerprint_path = root.joinpath(
                *PurePosixPath(fingerprint).parts
            )
            if fingerprint_path.is_symlink() or not fingerprint_path.is_file():
                issues.append("missing fingerprint")
            elif not _asset_hash_matches(
                fingerprint_path, str(entry.get("fingerprint_sha256") or "")
            ):
                issues.append("fingerprint hash mismatch")
    return RestorePlanAsset(
        path=relative,
        kind="runtime",
        required=required,
        present=present,
        status=", ".join(issues) if issues else "ready",
        source=source,
        sha256=str(entry.get("binary_sha256") or ""),
    )


def _plan_model_asset(
    root: Path,
    entry: Mapping[str, Any],
    *,
    source: str = "destination",
) -> RestorePlanAsset:
    relative = str(entry.get("path") or "")
    path = root.joinpath(*PurePosixPath(relative).parts)
    required = bool(entry.get("required"))
    present = path.is_file() and not path.is_symlink()
    issues: List[str] = []
    if not present:
        issues.append("missing")
    elif path.stat().st_size != int(entry.get("size") or 0):
        issues.append("size mismatch")
    elif not _asset_hash_matches(path, str(entry.get("sha256") or "")):
        issues.append("hash mismatch")
    return RestorePlanAsset(
        path=relative,
        kind="model",
        required=required,
        present=present,
        status=", ".join(issues) if issues else "ready",
        source=source,
        sha256=str(entry.get("sha256") or ""),
    )


def _plan_required_assets(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    source: str = "destination",
) -> Tuple[RestorePlanAsset, ...]:
    assets: List[RestorePlanAsset] = []
    for entry in manifest.get("runtimes") or []:
        if isinstance(entry, dict) and entry.get("required"):
            assets.append(_plan_runtime_asset(root, entry, source=source))
    for entry in manifest.get("models") or []:
        if isinstance(entry, dict) and entry.get("required"):
            assets.append(_plan_model_asset(root, entry, source=source))
    assets.sort(key=lambda item: (item.kind, item.path))
    return tuple(assets)


def _plan_drift(target: Path, file_restore: bool) -> Tuple[str, ...]:
    """Compare a current managed install with its own committed metadata."""
    if not target.exists():
        return ()
    if file_restore:
        from . import install_transaction

        root = install_transaction.transaction_paths(target).state_dir
    else:
        root = target
    manifest_path = root / install_manifest.MANIFEST_NAME
    state_path = root / STATE_NAME
    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    state_exists = state_path.exists() or state_path.is_symlink()
    if not manifest_exists and not state_exists:
        return ()
    if not manifest_exists or not state_exists:
        return ("current install metadata is incomplete",)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return (f"current install manifest is not a regular file: {manifest_path}",)
    if state_path.is_symlink() or not state_path.is_file():
        return (f"current install state is not a regular file: {state_path}",)
    try:
        manifest = install_manifest.load_manifest(manifest_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError, install_manifest.ManifestError) as exc:
        return (f"current install metadata is invalid: {exc}",)
    if not isinstance(state, dict):
        return ("current install state is not a JSON object",)
    if state.get("schema") != STATE_SCHEMA:
        return ("current install state has an unsupported schema",)
    if state.get("status") != "committed":
        return ("current install state is not committed",)
    if str(state.get("manifest_digest") or "") != install_manifest.manifest_digest(
        manifest
    ):
        return ("current install state and manifest digest differ",)
    if not str(state.get("install_id") or ""):
        return ("current install state has no install ID",)

    if file_restore:
        entry = next(
            (
                item
                for item in manifest.get("files") or []
                if isinstance(item, dict)
                and str(item.get("path") or "") == target.name
            ),
            None,
        )
        if entry is None:
            return (f"current install manifest does not list {target.name}",)
        if target.is_symlink() or not target.is_file():
            return (f"current install app is missing or a symlink: {target.name}",)
        if target.stat().st_size != int(entry.get("size") or 0):
            return (f"size mismatch: {target.name}",)
        if not _asset_hash_matches(target, str(entry.get("sha256") or "")):
            return (f"SHA-256 mismatch: {target.name}",)
        return ()

    try:
        report = install_manifest.verify_manifest(
            target,
            manifest,
            check_hashes=True,
            check_assets=False,
            check_extra=True,
            check_required_assets=False,
        )
    except (OSError, ValueError, RuntimeError, install_manifest.ManifestError) as exc:
        return (f"current install could not be checked: {exc}",)
    return tuple(report.errors)


def _restore_plan_id(plan: RecoveryRestorePlan) -> str:
    """Return a stable identifier for the reviewed plan content.

    Free-space numbers and the free-space warning are deliberately excluded:
    available disk space changes constantly and must not make an otherwise
    identical reviewed plan look stale.  Other warnings, blockers, hashes and
    destination identity are included.
    """
    payload = plan.as_dict()
    payload.pop("plan_id", None)
    payload.pop("available_bytes", None)
    payload["warnings"] = [
        warning
        for warning in payload.get("warnings", [])
        if not warning.startswith("low free space:")
    ]
    return hashlib.sha256(_json_bytes(payload)).hexdigest()[:32]


def _plan_tree_fingerprint(target: Path) -> str:
    """Fingerprint app-sized destination files without reading models."""
    digest = hashlib.sha256()
    skip = {
        ".downloads",
        ".offgrid",
        ".offgrid-install",
        "__pycache__",
        "models",
        "runtime",
    }
    try:
        walker = os.walk(target, followlinks=False)
        for raw_root, dirnames, filenames in walker:
            root = Path(raw_root)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in skip and not (root / name).is_symlink()
            )
            for name in sorted(filenames):
                path = root / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(target)
                    info = path.stat()
                except OSError:
                    continue
                digest.update(
                    f"{relative.as_posix()}\0{info.st_size}\0{info.st_mtime_ns}\n".encode(
                        "utf-8"
                    )
                )
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def _plan_destination_fingerprint(
    target: Path, file_restore: bool
) -> str:
    """Describe the current destination state without modifying it."""
    if not target.exists() and not target.is_symlink():
        return "absent"
    if target.is_symlink():
        return "symlink"
    tree = _plan_tree_fingerprint(target)
    state, _manifest, problem = _current_install_metadata(target, file_restore)
    if state is None:
        return f"{problem or 'unmanaged'}:{tree}"
    parts = [
        str(state.get("install_id") or ""),
        str(state.get("manifest_digest") or ""),
        tree,
    ]
    if file_restore:
        try:
            parts.append(install_manifest.sha256_file(target))
        except OSError:
            parts.append("unreadable")
    return "managed:" + ":".join(parts)


def _plan_safety_snapshot(
    preflight: _RestorePreflight,
) -> Tuple[str, int, bool]:
    """Return the safety snapshot a restore would create, if any."""
    from . import install_transaction

    try:
        snapshot = install_transaction.plan_recovery_snapshot(
            preflight.target,
            file_restore=preflight.file_restore,
        )
    except (install_transaction.InstallError, OSError):
        return "", 0, False
    if not snapshot.path:
        return "", 0, False
    return snapshot.path, snapshot.generation, True


def _current_platform_slug() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    aliases = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    machine = aliases.get(machine, machine)
    if not system or not machine:
        return ""
    return f"{system}-{machine}"


def _plan_required_bytes(preflight: _RestorePreflight) -> int:
    """Estimate staging plus safety-snapshot space for the plan."""
    app_bytes = max(0, int(preflight.verified.bytes))
    return app_bytes * 2


def _plan_feasibility_warnings(
    preflight: _RestorePreflight,
    *,
    available_bytes: int,
    required_bytes: int,
) -> List[str]:
    warnings: List[str] = []
    if required_bytes > 0 and available_bytes > 0 and available_bytes < required_bytes:
        warnings.append(
            "low free space: restore may need about "
            f"{required_bytes} bytes but only {available_bytes} are available"
        )
    from . import install_transaction

    try:
        journal = install_transaction.transaction_paths(
            preflight.target
        ).journal
        if journal.exists() or journal.is_symlink():
            warnings.append(
                "a pending install transaction journal exists; the restore "
                "will reconcile it before changing the destination"
            )
    except (install_transaction.InstallError, OSError, ValueError):
        pass
    return warnings


def _build_restore_plan(
    preflight: _RestorePreflight,
    *,
    force: bool,
    confirm: bool,
) -> RecoveryRestorePlan:
    """Build the read-only plan shared by preview and apply validation."""
    target = preflight.target
    missing_assets = list(preflight.missing_assets)
    asset_source_root = preflight.asset_source
    if missing_assets and asset_source_root is None:
        reason = (
            "recovery destination is missing required assets: "
            + "; ".join(missing_assets)
        )
    elif missing_assets:
        reason = (
            "archive verifies; required assets will be reused from "
            f"{asset_source_root}"
        )
    else:
        reason = "archive verifies and the destination has its required assets"

    blockers: List[str] = []
    if missing_assets and asset_source_root is None:
        blockers.append(reason)
    lineage = preflight.lineage
    if (
        lineage is not None
        and lineage.confirmation_required
        and not confirm
    ):
        blockers.append(
            "restore requires explicit confirmation: " + lineage.reason
        )
    if not os.access(target.parent, os.W_OK):
        blockers.append(f"destination parent is not writable: {target.parent}")
    if target.exists() and not preflight.file_restore:
        if not os.access(target, os.W_OK):
            blockers.append(f"destination is not writable: {target}")
    ready = not blockers
    if blockers:
        reason = blockers[0]

    asset_root = (
        asset_source_root
        if asset_source_root is not None
        else (target.parent if preflight.file_restore else target)
    )
    asset_source_label = (
        "asset-source" if asset_source_root is not None else "destination"
    )
    artifact = preflight.manifest.get("artifact")
    if not isinstance(artifact, Mapping):
        artifact = {}
    artifact_platform = str(artifact.get("platform") or "")
    current_platform = _current_platform_slug()
    safety_path, safety_generation, safety_protected = _plan_safety_snapshot(
        preflight
    )
    existing_app_sha256 = ""
    existing_app_size = 0
    if (
        preflight.file_restore
        and target.is_file()
        and not target.is_symlink()
    ):
        try:
            existing_app_sha256 = install_manifest.sha256_file(target)
            existing_app_size = target.stat().st_size
        except OSError:
            existing_app_size = 0

    try:
        available_bytes = int(shutil.disk_usage(target.parent).free)
    except OSError:
        available_bytes = 0
    required_bytes = _plan_required_bytes(preflight)

    warnings: List[str] = []
    if lineage is not None and lineage.relation in {"unmanaged", "unknown"}:
        warnings.append(
            "destination has no managed install metadata; existing app files "
            "may be replaced without an install-history safety check"
        )
    if not safety_protected and target.exists():
        warnings.append(
            "restore will not create a recovery safety snapshot because the "
            "current destination is not a reusable managed install"
        )
    if (
        artifact_platform
        and artifact_platform != "any"
        and current_platform
        and artifact_platform != current_platform
    ):
        warnings.append(
            f"archive targets {artifact_platform}, but this device is "
            f"{current_platform}"
        )
    warnings.extend(
        _plan_feasibility_warnings(
            preflight,
            available_bytes=available_bytes,
            required_bytes=required_bytes,
        )
    )

    plan = RecoveryRestorePlan(
        archive=preflight.verified.path,
        destination=target,
        ready=ready,
        reason=reason,
        file_restore=preflight.file_restore,
        overwrite=target.exists() or target.is_symlink(),
        force=force,
        drift=_plan_drift(target, preflight.file_restore),
        affected_files=_plan_affected_files(preflight),
        preserved_state=_plan_preserved_state(
            target, preflight.file_restore, preflight.manifest
        ),
        required_assets=_plan_required_assets(
            asset_root,
            preflight.manifest,
            source=asset_source_label,
        ),
        asset_source=str(asset_source_root or ""),
        report=preflight.verified,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        destination_fingerprint=_plan_destination_fingerprint(
            target, preflight.file_restore
        ),
        safety_snapshot=safety_path,
        safety_generation=safety_generation,
        safety_snapshot_protected=safety_protected,
        available_bytes=available_bytes,
        required_bytes=required_bytes,
        artifact_platform=artifact_platform,
        current_platform=current_platform,
        existing_app_sha256=existing_app_sha256,
        existing_app_size=existing_app_size,
        **_lineage_fields(lineage),
    )
    return replace(plan, plan_id=_restore_plan_id(plan))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RecoveryError(f"could not read {path}: {exc}") from exc
    return digest.hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError(f"{label} must be an object")
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise RecoveryError(f"{label} must be a non-empty string")
    return value


def _safe_relative(value: Any, label: str) -> str:
    path = _string(value, label)
    if "\x00" in path or "\\" in path:
        raise RecoveryError(f"{label} must use POSIX separators")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise RecoveryError(f"{label} must be relative: {path!r}")
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts:
        raise RecoveryError(f"{label} must be a relative path: {path!r}")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise RecoveryError(f"{label} contains an unsafe component: {path!r}")
    if str(relative) != path:
        raise RecoveryError(f"{label} is not a canonical path: {path!r}")
    return path


def _created(value: Optional[str]) -> str:
    if value is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
        if epoch:
            try:
                seconds = int(epoch)
            except ValueError as exc:
                raise RecoveryError("SOURCE_DATE_EPOCH must be an integer") from exc
            if seconds < 0:
                raise RecoveryError("SOURCE_DATE_EPOCH must not be negative")
            try:
                value = datetime.fromtimestamp(seconds, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except (OverflowError, OSError, ValueError) as exc:
                raise RecoveryError("SOURCE_DATE_EPOCH is out of range") from exc
        else:
            value = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = _string(value, "created")
    if not CREATED_RE.fullmatch(text):
        raise RecoveryError("created must be an RFC 3339 UTC timestamp")
    return text


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RecoveryError(f"{label} must be true or false")
    return value


def _nonnegative_size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryError(f"{label} must be a non-negative integer")
    return value


def _optional_hash(entry: Mapping[str, Any], key: str, label: str) -> None:
    if key in entry and not HASH_RE.fullmatch(str(entry[key])):
        raise RecoveryError(f"{label}.{key} must be a lowercase SHA-256")


def _validate_assets(value: Any) -> None:
    assets = _mapping(value, "assets")
    if set(assets) != {"runtimes", "models"}:
        raise RecoveryError("assets must contain exactly runtimes and models")
    seen: Dict[str, str] = {}
    for group, prefix in (("runtimes", "runtime/"), ("models", "models/")):
        entries = assets.get(group)
        if not isinstance(entries, list):
            raise RecoveryError(f"assets.{group} must be a list")
        paths: List[str] = []
        for index, raw in enumerate(entries):
            label = f"assets.{group}[{index}]"
            entry = _mapping(raw, label)
            path = _safe_relative(entry.get("path"), f"{label}.path")
            if not path.startswith(prefix):
                raise RecoveryError(f"{label}.path must start with {prefix}")
            if group == "models" and not path.lower().endswith(".gguf"):
                raise RecoveryError(f"{label}.path must name a GGUF model")
            if path.casefold() in seen:
                raise RecoveryError(f"duplicate recovery asset path: {path}")
            seen[path.casefold()] = path
            _string(entry.get("kind"), f"{label}.kind")
            _boolean(entry.get("required"), f"{label}.required")
            if entry.get("verification") != "on-demand":
                raise RecoveryError(f"{label}.verification must be on-demand")
            if group == "models":
                _nonnegative_size(entry.get("size"), f"{label}.size")
                _optional_hash(entry, "sha256", label)
            else:
                for key in ("version", "platform"):
                    if key in entry:
                        _string(entry[key], f"{label}.{key}", allow_empty=True)
                _optional_hash(entry, "archive_sha256", label)
                _optional_hash(entry, "fingerprint_sha256", label)
                _optional_hash(entry, "binary_sha256", label)
                if "binary" in entry:
                    _safe_relative(entry["binary"], f"{label}.binary")
                if "fingerprint" in entry:
                    _safe_relative(entry["fingerprint"], f"{label}.fingerprint")
                if "binary_size" in entry:
                    _nonnegative_size(entry["binary_size"], f"{label}.binary_size")
            paths.append(path)
        if paths != sorted(paths):
            raise RecoveryError(f"assets.{group} must be sorted by path")


def _reason(value: Any) -> str:
    text = _string(value, "reason")
    text = " ".join(text.split())
    if not text:
        raise RecoveryError("reason must not be blank")
    if len(text) > MAX_REASON:
        raise RecoveryError(f"reason must be at most {MAX_REASON} characters")
    return text


def _scrub_url_credentials(value: str) -> str:
    """Remove user:password@ from an HTTP(S) URL without changing its path."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(value)
    except (TypeError, ValueError):
        return value
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return value
    if "@" not in parts.netloc:
        return value
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, host + port, parts.path, parts.query, parts.fragment))


def _clean_config_value(value: Any) -> Any:
    """Keep JSON scalars and scalar lists; mark nested objects unsafe.

    Config fields are scalars or tuples of scalars.  A mapping under any field
    would therefore be an unknown extension, and allowing it would reopen the
    denylist problem inside that object.  Such a value is dropped instead.
    """
    if isinstance(value, str):
        return _scrub_url_credentials(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        cleaned: List[Any] = []
        for entry in value:
            if isinstance(entry, str):
                cleaned.append(_scrub_url_credentials(entry))
            elif isinstance(entry, (bool, int, float)) or entry is None:
                cleaned.append(entry)
            else:
                return _UNSAFE_VALUE
        return cleaned
    return _UNSAFE_VALUE


def _allowlisted_fields(
    value: Mapping[str, Any], allowed: frozenset
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for field in sorted(allowed):
        if field not in value:
            continue
        cleaned = _clean_config_value(value[field])
        if cleaned is not _UNSAFE_VALUE:
            result[field] = cleaned
    return result


def sanitize_config(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Return only known-safe Config fields, with URL credentials removed.

    Unknown fields and nested objects are discarded rather than filtered by
    their key names.  That keeps an unfamiliar credential key from leaking:
    if it is not a known Config field, it never reaches the archive.  URL
    userinfo is scrubbed as well, because an endpoint can carry a password
    without a secret-looking key.  Missing fields fall back to normal config
    defaults when the archive is restored.
    """

    if not isinstance(value, Mapping):
        raise RecoveryError("config must be a JSON object")
    return _allowlisted_fields(value, SAFE_CONFIG_FIELDS)


def _sanitize_previous_install(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep known install metadata while dropping unknown nested values."""
    if not isinstance(value, Mapping):
        raise RecoveryError("previous_install must be a JSON object")
    result = _allowlisted_fields(
        value, PREVIOUS_INSTALL_FIELDS - {"artifact"}
    )
    artifact = value.get("artifact")
    if isinstance(artifact, Mapping):
        result["artifact"] = _allowlisted_fields(artifact, SAFE_ARTIFACT_FIELDS)
    return result


def sanitize_config_bytes(data: bytes) -> bytes:
    """Parse, sanitize and canonically serialize ``config.json`` bytes."""
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"config.json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RecoveryError("config.json must contain a JSON object")
    return _json_bytes(sanitize_config(parsed))


def _load_state(source: Path) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    manifest_path = source / install_manifest.MANIFEST_NAME
    try:
        manifest = install_manifest.load_manifest(manifest_path)
    except install_manifest.ManifestError as exc:
        raise RecoveryError(str(exc)) from exc
    digest = install_manifest.manifest_digest(manifest)

    state_path = source / STATE_NAME
    if state_path.is_symlink() or not state_path.is_file():
        raise RecoveryError(f"previous install has no regular {STATE_NAME}: {source}")
    try:
        raw = state_path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"could not read {state_path}: {exc}") from exc
    if len(raw) > MAX_STATE_BYTES:
        raise RecoveryError(f"{STATE_NAME} is too large")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"{state_path} is not valid JSON: {exc}") from exc
    state = _mapping(state, STATE_NAME)
    if state.get("schema") != STATE_SCHEMA:
        raise RecoveryError(f"unsupported {STATE_NAME} schema: {state.get('schema')!r}")
    if state.get("status") != "committed":
        raise RecoveryError(f"previous install is not committed: {state.get('status')!r}")
    if str(state.get("manifest_digest") or "") != digest:
        raise RecoveryError("previous install state and manifest digest differ")
    install_id = _string(state.get("install_id"), "install-state.install_id")
    return manifest, dict(state), install_id


def _app_manifest_files(manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for entry in manifest.get("files") or []:
        if isinstance(entry, dict):
            path = str(entry.get("path") or "")
            if path:
                result[path] = entry
    return result


def _app_entrypoint(manifest: Mapping[str, Any]) -> str:
    """Return the required app entrypoint path from a validated manifest.

    Do not fall back to the default ``offgrid.pyz`` name: a malformed or
    incompatible manifest must be rejected instead of silently verifying a
    different file from the one the manifest names.
    """
    entrypoints = manifest.get("entrypoints")
    if not isinstance(entrypoints, Mapping):
        raise RecoveryError("install manifest has no app entrypoint")
    raw = entrypoints.get("app")
    if not isinstance(raw, str) or not raw:
        raise RecoveryError("install manifest has no app entrypoint")
    return _safe_relative(raw, "install.entrypoints.app")


def _add_copied_file(
    source: Path,
    relative: str,
    entries: Mapping[str, Mapping[str, Any]],
    files: Dict[str, Tuple[bytes, int]],
) -> None:
    entry = entries.get(relative)
    if entry is None:
        raise RecoveryError(
            f"refusing an app file not listed in install-manifest.json: {relative}"
        )
    path = source.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"app file is missing or a symlink: {relative}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"could not read app file {relative}: {exc}") from exc
    size = int(entry.get("size") or 0)
    if len(data) != size:
        raise RecoveryError(f"app file size differs from install manifest: {relative}")
    if str(entry.get("check") or "") == "sha256":
        expected = str(entry.get("sha256") or "")
        if _sha256(data) != expected:
            raise RecoveryError(f"app file hash differs from install manifest: {relative}")
    mode = 0o755 if bool(entry.get("executable")) else 0o644
    files[relative] = (data, mode)


def _collect_app_files(
    source: Path,
    manifest: Mapping[str, Any],
    files: Dict[str, Tuple[bytes, int]],
) -> None:
    entries = _app_manifest_files(manifest)
    app_relative = _app_entrypoint(manifest)
    seen: set = set()

    for directory in CAPTURE_DIRECTORIES:
        root = source / directory
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise RecoveryError(f"capture path is not a real directory: {directory}")
        for current, dirnames, filenames in os.walk(str(root), followlinks=False):
            current_path = Path(current)
            kept_dirs = []
            for name in sorted(dirnames):
                path = current_path / name
                if path.is_symlink():
                    raise RecoveryError(
                        "refusing a symlinked directory in the recovery app tree: "
                        f"{path.relative_to(source).as_posix()}"
                    )
                if name not in SKIP_DIRECTORIES:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                path = current_path / name
                if name.endswith(SKIP_SUFFIXES) or name in SKIP_DIRECTORIES:
                    continue
                if path.is_symlink():
                    raise RecoveryError(
                        "refusing a symlink in the recovery app tree: "
                        f"{path.relative_to(source).as_posix()}"
                    )
                relative = path.relative_to(source).as_posix()
                _safe_relative(relative, relative)
                _add_copied_file(source, relative, entries, files)
                seen.add(relative)

    capture_files = list(CAPTURE_FILES)
    if app_relative not in capture_files:
        capture_files.append(app_relative)
    for name in capture_files:
        path = source / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise RecoveryError(f"capture path is not a real file: {name}")
        _add_copied_file(source, name, entries, files)
        seen.add(name)

    if not any(path == app_relative or path.startswith("offgrid/") for path in seen):
        raise RecoveryError("previous install contains no offgrid app files")


def _missing_required_app_file(
    manifest: Mapping[str, Any], files: Mapping[str, Tuple[bytes, int]]
) -> Optional[str]:
    for relative, entry in _app_manifest_files(manifest).items():
        if entry.get("required") and relative not in files:
            return relative
    return None


def inspect_recovery_source(source: Path) -> None:
    """Raise :class:`RecoveryError` unless *source* can be archived.

    The install transaction uses this to distinguish a corrupt current install
    (which must be quarantined) from a transient archive-write failure (which
    must abort while the current tree is still untouched).  No files are
    written.
    """
    source = Path(source).expanduser()
    try:
        source = source.resolve()
    except OSError as exc:
        raise RecoveryError(f"could not resolve recovery source: {exc}") from exc
    if not source.is_dir():
        raise RecoveryError(f"previous install directory not found: {source}")
    manifest, _state, _install_id = _load_state(source)
    files: Dict[str, Tuple[bytes, int]] = {}
    _collect_app_files(source, manifest, files)
    missing = _missing_required_app_file(manifest, files)
    if missing is not None:
        raise RecoveryError(f"required app file is missing: {missing}")
    config = source / "config.json"
    if config.exists():
        if config.is_symlink() or not config.is_file():
            raise RecoveryError("config.json must be a regular file")
        try:
            sanitize_config_bytes(config.read_bytes())
        except OSError as exc:
            raise RecoveryError(f"could not read config.json: {exc}") from exc


def _recovery_payload(
    source: Path,
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
    install_id: str,
    *,
    transaction_id: str,
    created: str,
    reason: str,
    safety_snapshot: bool = False,
) -> Dict[str, Any]:
    artifact = manifest.get("artifact") or {}
    version = _string(artifact.get("version"), "install manifest version")
    destination = str(state.get("destination") or source)
    previous = state.get("previous_install") or {}
    if not isinstance(previous, dict):
        raise RecoveryError("install-state.previous_install must be an object")
    # State is local and mutable.  Keep only its documented identity fields,
    # but do not let a credential that was accidentally written there enter
    # the archive through an unknown key.
    previous = _sanitize_previous_install(previous)
    digest = install_manifest.manifest_digest(manifest)
    payload = {
        "schema": SCHEMA,
        "created": created,
        "reason": reason,
        "transaction_id": transaction_id,
        "install": {
            "install_id": install_id,
            "version": version,
            "destination": destination,
            "manifest_digest": digest,
            "status": str(state.get("status") or ""),
        },
        "previous_install": dict(previous),
        "assets": {
            "runtimes": [dict(entry) for entry in manifest.get("runtimes") or []],
            "models": [dict(entry) for entry in manifest.get("models") or []],
        },
        "manifest_digest": digest,
        "restore_command": RESTORE_COMMAND,
    }
    if safety_snapshot:
        payload["safety_snapshot"] = True
    return payload


def _validate_recovery_payload(payload: Mapping[str, Any]) -> None:
    # Check compatibility before the exact field set.  A newer archive may
    # legitimately carry extra fields, and it should fail as unsupported rather
    # than as a malformed older-format archive.
    schema = payload.get("schema")
    if schema != SCHEMA:
        raise RecoveryError(
            f"unsupported recovery archive schema: {schema!r}; "
            f"this version supports {SCHEMA!r}"
        )
    expected = {
        "schema",
        "created",
        "reason",
        "transaction_id",
        "install",
        "previous_install",
        "assets",
        "manifest_digest",
        "restore_command",
    }
    if "safety_snapshot" in payload:
        if payload.get("safety_snapshot") is not True:
            raise RecoveryError(
                "RECOVERY.json safety_snapshot must be true when present"
            )
        expected.add("safety_snapshot")
    if set(payload) != expected:
        raise RecoveryError(
            "RECOVERY.json must contain exactly: " + ", ".join(sorted(expected))
        )
    _created(_string(payload.get("created"), "created"))
    _reason(payload.get("reason"))
    _string(payload.get("transaction_id"), "transaction_id")
    install = _mapping(payload.get("install"), "install")
    if set(install) != {
        "install_id",
        "version",
        "destination",
        "manifest_digest",
        "status",
    }:
        raise RecoveryError(
            "RECOVERY.json install must contain exactly install_id, version, "
            "destination, manifest_digest and status"
        )
    _string(install.get("install_id"), "install.install_id")
    _string(install.get("version"), "install.version")
    _string(install.get("destination"), "install.destination")
    digest = _string(install.get("manifest_digest"), "install.manifest_digest")
    if not HASH_RE.fullmatch(digest):
        raise RecoveryError("install.manifest_digest must be a lowercase SHA-256")
    if install.get("status") != "committed":
        raise RecoveryError("recovery install status must be committed")
    if _string(payload.get("manifest_digest"), "manifest_digest") != digest:
        raise RecoveryError("recovery manifest digest fields differ")
    previous = _mapping(payload.get("previous_install"), "previous_install")
    if _sanitize_previous_install(previous) != previous:
        raise RecoveryError("previous_install contains unsanitized credentials")
    _validate_assets(payload.get("assets"))
    if payload.get("restore_command") != RESTORE_COMMAND:
        raise RecoveryError("recovery restore command is not the supported command")


def _recover_markdown(
    payload: Mapping[str, Any], *, reason: str, version: str
) -> bytes:
    text = f"""\
# OffGrid recovery archive

This archive preserves the previous OffGrid app install before a replacement
transaction. It does not contain models, runtimes, workspaces or private user
state.

- Version: {version}
- Install ID: {payload['install']['install_id']}
- Manifest SHA-256: {payload['manifest_digest']}
- Reason: {reason}
- Restore command: `{RESTORE_COMMAND}`

Verify the archive before restoring it. The `SHA256SUMS` file covers every
file in this archive except itself. `config.json` has had credentials removed;
you may need to re-enter an API key or fleet token after recovery.
"""
    return text.encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """Persist the entries in a recovery archive directory."""
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


def _write_tree(root: Path, files: Mapping[str, Tuple[bytes, int]]) -> None:
    for relative in sorted(files):
        path = root.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as handle:
                handle.write(files[relative][0])
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(files[relative][1])
        except OSError as exc:
            raise RecoveryError(f"could not write recovery file {relative}: {exc}") from exc
    # Directory modes are part of the archive's reproducible shape.  Walk
    # bottom-up so a parent is not made inaccessible before its children.
    for current, dirnames, _filenames in os.walk(str(root), topdown=False):
        current_path = Path(current)
        for name in dirnames:
            child = current_path / name
            if not child.is_symlink():
                try:
                    child.chmod(0o700)
                except OSError:
                    pass
        try:
            current_path.chmod(0o700)
        except OSError:
            pass
        try:
            _fsync_directory(current_path)
        except OSError as exc:
            raise RecoveryError(
                f"could not sync recovery directory {current_path}: {exc}"
            ) from exc


def _sums_bytes(files: Mapping[str, Tuple[bytes, int]]) -> bytes:
    lines = [
        f"{_sha256(files[relative][0])}  {relative}\n"
        for relative in sorted(files)
        if relative != SUMS_NAME
    ]
    return "".join(lines).encode("ascii")


def _remove_any(path: Path) -> None:
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _replace_directory(temporary: Path, destination: Path, *, force: bool) -> None:
    destination = Path(destination)
    if destination.is_symlink():
        raise RecoveryError(f"recovery destination must not be a symlink: {destination}")
    if destination.exists() and not force:
        raise RecoveryError(
            f"{destination} already exists; pass force=True to replace it"
        )
    backup = destination.with_name(f".{destination.name}.old-{uuid.uuid4().hex}")
    if destination.exists():
        if backup.exists():
            _remove_any(backup)
        os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except OSError:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        _remove_any(backup)
        _fsync_directory(destination.parent)


def write_recovery_archive(
    source: Path,
    destination: Path,
    *,
    reason: str,
    transaction_id: Optional[str] = None,
    created: Optional[str] = None,
    force: bool = False,
    safety_snapshot: bool = False,
) -> Path:
    """Write a deterministic recovery archive from a committed install.

    ``source`` must be the previous installed tree with its
    ``install-manifest.json`` and committed ``install-state.json``.  The caller
    supplies the transaction ID and, when reproducibility matters, ``created``.
    No transaction is started or modified here.
    """
    source = Path(source).expanduser()
    destination = Path(destination).expanduser()
    try:
        source = source.resolve()
        destination = Path(os.path.abspath(str(destination)))
    except OSError as exc:
        raise RecoveryError(f"could not resolve recovery paths: {exc}") from exc
    if not source.is_dir():
        raise RecoveryError(f"previous install directory not found: {source}")
    if source == destination:
        raise RecoveryError("recovery destination must not be the install directory")
    if destination in source.parents or source in destination.parents:
        raise RecoveryError("recovery destination must be outside the install tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise RecoveryError(
            f"{destination} already exists; pass force=True to replace it"
        )

    manifest, state, install_id = _load_state(source)
    created_text = _created(created)
    reason_text = _reason(reason)
    txid = _string(
        transaction_id or state.get("transaction_id") or "",
        "transaction_id",
    )
    payload = _recovery_payload(
        source,
        manifest,
        state,
        install_id,
        transaction_id=txid,
        created=created_text,
        reason=reason_text,
        safety_snapshot=safety_snapshot,
    )
    _validate_recovery_payload(payload)

    files: Dict[str, Tuple[bytes, int]] = {}
    _collect_app_files(source, manifest, files)
    missing = _missing_required_app_file(manifest, files)
    if missing is not None:
        raise RecoveryError(f"required app file is missing: {missing}")
    app_relative = _app_entrypoint(manifest)
    if app_relative not in files:
        raise RecoveryError(
            f"required app entrypoint is missing: {app_relative}"
        )

    for generated in (install_manifest.MANIFEST_NAME, STATE_NAME):
        path = source / generated
        if path.is_symlink() or not path.is_file():
            raise RecoveryError(f"previous install has no regular {generated}")
        try:
            files[generated] = (path.read_bytes(), 0o600)
        except OSError as exc:
            raise RecoveryError(f"could not read {generated}: {exc}") from exc

    config = source / "config.json"
    if config.is_file():
        if config.is_symlink():
            raise RecoveryError("config.json must not be a symlink")
        try:
            raw_config = config.read_bytes()
        except OSError as exc:
            raise RecoveryError(f"could not read config.json: {exc}") from exc
        files["config.json"] = (sanitize_config_bytes(raw_config), 0o600)

    version = str(payload["install"]["version"])
    files[RECOVERY_NAME] = (_json_bytes(payload), 0o600)
    files[RECOVER_NAME] = (
        _recover_markdown(payload, reason=reason_text, version=version),
        0o600,
    )
    files[SUMS_NAME] = (_sums_bytes(files), 0o644)

    parent = destination.parent
    try:
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    except OSError as exc:
        raise RecoveryError(f"could not create recovery staging directory: {exc}") from exc
    try:
        _write_tree(temporary, files)
        _replace_directory(temporary, destination, force=force)
    except OSError as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise RecoveryError(f"could not write recovery archive: {exc}") from exc
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def load_recovery_manifest(path: Path) -> Dict[str, Any]:
    """Read and validate ``RECOVERY.json`` without trusting any paths."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"recovery manifest is not a regular file: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"could not read recovery manifest {path}: {exc}") from exc
    if len(data) > MAX_RECOVERY_BYTES:
        raise RecoveryError("recovery manifest is too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"invalid recovery manifest JSON: {exc}") from exc
    payload = _mapping(payload, "recovery manifest")
    _validate_recovery_payload(payload)
    return dict(payload)


def _parse_sums(path: Path) -> List[Tuple[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"{SUMS_NAME} is missing or is a symlink")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"could not read {SUMS_NAME}: {exc}") from exc
    if not data or len(data) > MAX_SUMS_BYTES:
        raise RecoveryError(f"{SUMS_NAME} is empty or too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RecoveryError(f"{SUMS_NAME} is not ASCII") from exc
    if "\r" in text or not text.endswith("\n"):
        raise RecoveryError(f"{SUMS_NAME} must use LF line endings")
    entries: List[Tuple[str, str]] = []
    seen = set()
    for number, line in enumerate(text.splitlines(), start=1):
        match = SUMS_RE.fullmatch(line)
        if not match:
            raise RecoveryError(f"malformed {SUMS_NAME} line {number}")
        digest, relative = match.groups()
        _safe_relative(relative, f"{SUMS_NAME} line {number}")
        if relative == SUMS_NAME:
            raise RecoveryError(f"{SUMS_NAME} must not list itself")
        key = relative.casefold()
        if key in seen:
            raise RecoveryError(f"duplicate path in {SUMS_NAME}: {relative}")
        seen.add(key)
        entries.append((digest, relative))
    paths = [relative for _digest, relative in entries]
    if paths != sorted(paths):
        raise RecoveryError(f"{SUMS_NAME} paths must be sorted")
    return entries


def _archive_files(root: Path) -> List[str]:
    paths: List[str] = []
    for current, dirnames, filenames in os.walk(str(root), followlinks=False):
        current_path = Path(current)
        for name in dirnames:
            if (current_path / name).is_symlink():
                raise RecoveryError(
                    "recovery archive contains a symlink: "
                    f"{(current_path / name).relative_to(root).as_posix()}"
                )
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = current_path / name
            if path.is_symlink():
                raise RecoveryError(
                    "recovery archive contains a symlink: "
                    f"{path.relative_to(root).as_posix()}"
                )
            if not path.is_file():
                raise RecoveryError(
                    "recovery archive contains a non-regular file: "
                    f"{path.relative_to(root).as_posix()}"
                )
            paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def _verify_app_file(
    root: Path,
    relative: str,
    entries: Mapping[str, Mapping[str, Any]],
) -> None:
    entry = entries.get(relative)
    if entry is None:
        raise RecoveryError(
            f"recovery archive contains an app file not in install-manifest.json: "
            f"{relative}"
        )
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"recovery app file is missing: {relative}")
    size = path.stat().st_size
    if size != int(entry.get("size") or 0):
        raise RecoveryError(f"recovery app file size mismatch: {relative}")
    if str(entry.get("check") or "") == "sha256":
        if _sha256_file(path) != str(entry.get("sha256") or ""):
            raise RecoveryError(f"recovery app file SHA-256 mismatch: {relative}")


def verify_recovery_archive(path: Path) -> RecoveryReport:
    """Verify an archive's hashes, schema and install metadata.

    The archive must be a directory.  Every file except ``SHA256SUMS`` must be
    listed exactly once and match its hash; generated metadata is cross-checked
    against the captured install manifest and state.
    """
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise RecoveryError(f"recovery archive is not a directory: {root}")
    try:
        root = root.resolve()
    except OSError as exc:
        raise RecoveryError(f"could not resolve recovery archive: {exc}") from exc

    payload = load_recovery_manifest(root / RECOVERY_NAME)
    sums = _parse_sums(root / SUMS_NAME)
    actual = _archive_files(root)
    listed = [relative for _digest, relative in sums]
    actual_without_sums = [relative for relative in actual if relative != SUMS_NAME]
    if actual_without_sums != listed:
        missing = sorted(set(listed) - set(actual_without_sums))
        extra = sorted(set(actual_without_sums) - set(listed))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unlisted: " + ", ".join(extra))
        raise RecoveryError(f"{SUMS_NAME} does not match archive contents ({'; '.join(details)})")
    for digest, relative in sums:
        file_path = root.joinpath(*PurePosixPath(relative).parts)
        if _sha256_file(file_path) != digest:
            raise RecoveryError(f"recovery SHA-256 mismatch: {relative}")

    manifest_path = root / install_manifest.MANIFEST_NAME
    try:
        manifest = install_manifest.load_manifest(manifest_path)
    except install_manifest.ManifestError as exc:
        raise RecoveryError(str(exc)) from exc
    manifest_digest = install_manifest.manifest_digest(manifest)
    if manifest_digest != str(payload["manifest_digest"]):
        raise RecoveryError("recovery and captured install manifest digests differ")
    install = payload["install"]
    if manifest_digest != str(install["manifest_digest"]):
        raise RecoveryError("recovery install manifest digest differs")
    assets = payload["assets"]
    if assets.get("runtimes") != (manifest.get("runtimes") or []):
        raise RecoveryError("recovery runtime assets differ from captured manifest")
    if assets.get("models") != (manifest.get("models") or []):
        raise RecoveryError("recovery model assets differ from captured manifest")
    artifact = manifest.get("artifact") or {}
    if str(artifact.get("version") or "") != str(install["version"]):
        raise RecoveryError("recovery version differs from captured install manifest")

    state_path = root / STATE_NAME
    if state_path.is_symlink() or not state_path.is_file():
        raise RecoveryError(f"recovery archive has no regular {STATE_NAME}")
    try:
        raw_state = state_path.read_bytes()
        state = json.loads(raw_state.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"invalid captured {STATE_NAME}: {exc}") from exc
    state = _mapping(state, STATE_NAME)
    if state.get("schema") != STATE_SCHEMA:
        raise RecoveryError(f"unsupported captured {STATE_NAME} schema")
    if state.get("status") != "committed":
        raise RecoveryError("captured install state is not committed")
    if str(state.get("manifest_digest") or "") != manifest_digest:
        raise RecoveryError("captured install state and manifest digests differ")
    if str(state.get("install_id") or "") != str(install["install_id"]):
        raise RecoveryError("captured install ID differs from recovery metadata")
    if str(state.get("destination") or "") != str(install["destination"]):
        raise RecoveryError(
            "captured destination differs from recovery metadata"
        )

    entries = _app_manifest_files(manifest)
    app_relative = _app_entrypoint(manifest)
    app_entry = entries.get(app_relative)
    if app_entry is None:
        raise RecoveryError(
            f"install manifest app entrypoint is not listed: {app_relative}"
        )
    if not app_entry.get("required"):
        raise RecoveryError(
            f"install manifest app entrypoint is not required: {app_relative}"
        )
    if app_relative not in actual:
        raise RecoveryError(
            f"recovery archive is missing the manifest app entrypoint: "
            f"{app_relative}"
        )
    _verify_app_file(root, app_relative, entries)
    for relative, entry in entries.items():
        if entry.get("required") and relative not in actual:
            raise RecoveryError(f"recovery app file is missing: {relative}")
    app_files = [
        relative
        for relative in actual
        if relative not in GENERATED_FILES and relative != "config.json"
    ]
    if not app_files:
        raise RecoveryError("recovery archive contains no app files")
    for relative in app_files:
        _verify_app_file(root, relative, entries)

    config_sanitized = False
    config_path = root / "config.json"
    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_file():
            raise RecoveryError("recovery config.json is not a regular file")
        try:
            raw_config = config_path.read_bytes()
            parsed_config = json.loads(raw_config.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"recovery config.json is invalid: {exc}") from exc
        if not isinstance(parsed_config, dict):
            raise RecoveryError("recovery config.json must contain a JSON object")
        if sanitize_config(parsed_config) != parsed_config:
            raise RecoveryError("recovery config.json contains unsanitized credentials")
        config_sanitized = True

    total = sum(
        root.joinpath(*PurePosixPath(relative).parts).stat().st_size
        for relative in actual
    )
    return RecoveryReport(
        path=root,
        version=str(install["version"]),
        install_id=str(install["install_id"]),
        manifest_digest=manifest_digest,
        files=len(actual),
        bytes=total,
        config_sanitized=config_sanitized,
    )


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _restore_target(
    archive: Path,
    payload: Mapping[str, Any],
    destination: Optional[Path],
) -> Path:
    """Resolve a restore path and refuse paths that could corrupt the archive."""
    if destination is None:
        raw_destination = str(payload["install"]["destination"])
        target = Path(raw_destination).expanduser()
        if not target.is_absolute():
            raise RecoveryError(
                "recovery destination from the archive is not absolute; "
                "pass --out with an absolute path"
            )
    else:
        target = Path(destination).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target

    if target.is_symlink():
        raise RecoveryError(f"unsafe recovery destination is a symlink: {target}")
    try:
        target = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RecoveryError(f"could not resolve recovery destination: {exc}") from exc

    archive = Path(archive).resolve()
    if target == archive or _contains(archive, target) or _contains(target, archive):
        raise RecoveryError(f"unsafe recovery destination overlaps the archive: {target}")
    if target.parent == target:
        raise RecoveryError(f"unsafe recovery destination is a filesystem root: {target}")
    if any(part == ".offgrid-install" for part in target.parts):
        raise RecoveryError(
            f"unsafe recovery destination is inside a transaction directory: {target}"
        )
    if not target.parent.is_dir():
        raise RecoveryError(
            f"recovery destination parent is not a directory: {target.parent}"
        )
    if not os.access(target.parent, os.W_OK):
        raise RecoveryError(
            f"recovery destination parent is not writable: {target.parent}"
        )
    return target


def _asset_hash_matches(path: Path, expected: str) -> bool:
    try:
        return install_manifest.sha256_file(path) == expected
    except OSError:
        return False


def _missing_required_assets(
    root: Path, manifest: Mapping[str, Any]
) -> List[str]:
    """Check the assets that the recovery archive intentionally did not copy."""
    missing: List[str] = []
    for entry in manifest.get("runtimes") or []:
        if not isinstance(entry, dict) or not entry.get("required"):
            continue
        relative = str(entry.get("path") or "")
        directory = root.joinpath(*PurePosixPath(relative).parts)
        if directory.is_symlink() or not directory.is_dir():
            missing.append(f"missing runtime: {relative}")
            continue
        binary = str(entry.get("binary") or "")
        if binary:
            binary_path = directory.joinpath(*PurePosixPath(binary).parts)
            if binary_path.is_symlink() or not binary_path.is_file():
                missing.append(f"missing runtime binary: {relative}/{binary}")
            else:
                expected = int(entry.get("binary_size") or 0)
                if binary_path.stat().st_size != expected:
                    missing.append(f"runtime size mismatch: {relative}/{binary}")
                elif not _asset_hash_matches(
                    binary_path, str(entry.get("binary_sha256") or "")
                ):
                    missing.append(
                        f"runtime hash mismatch: {relative}/{binary}"
                    )
        fingerprint = str(entry.get("fingerprint") or "")
        if fingerprint:
            fingerprint_path = root.joinpath(
                *PurePosixPath(fingerprint).parts
            )
            if fingerprint_path.is_symlink() or not fingerprint_path.is_file():
                missing.append(f"missing runtime fingerprint: {fingerprint}")
            elif not _asset_hash_matches(
                fingerprint_path, str(entry.get("fingerprint_sha256") or "")
            ):
                missing.append(
                    f"runtime fingerprint hash mismatch: {fingerprint}"
                )
    for entry in manifest.get("models") or []:
        if not isinstance(entry, dict) or not entry.get("required"):
            continue
        relative = str(entry.get("path") or "")
        model_path = root.joinpath(*PurePosixPath(relative).parts)
        if model_path.is_symlink() or not model_path.is_file():
            missing.append(f"missing model: {relative}")
            continue
        expected = int(entry.get("size") or 0)
        if model_path.stat().st_size != expected:
            missing.append(f"model size mismatch: {relative}")
        elif not _asset_hash_matches(
            model_path, str(entry.get("sha256") or "")
        ):
            missing.append(f"model hash mismatch: {relative}")
    return missing


def _resolve_asset_source(asset_source: Optional[Path]) -> Optional[Path]:
    """Resolve an explicit root containing surviving models/ and runtime/."""
    if asset_source is None:
        return None
    source = Path(asset_source).expanduser()
    try:
        source = source.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RecoveryError(f"could not resolve asset source: {exc}") from exc
    if not source.is_dir():
        raise RecoveryError(f"asset source is not a directory: {source}")
    return source


def _current_install_metadata(
    target: Path, file_restore: bool
) -> Tuple[Optional[Mapping[str, Any]], Optional[Mapping[str, Any]], str]:
    """Read committed destination install metadata for lineage comparison.

    Returns ``(state, manifest, problem)``.  An empty problem means the
    metadata is consistent; ``"unmanaged"`` means there is no install state at
    all; any other value describes why an existing state is unusable.
    """
    if file_restore:
        from . import install_transaction

        root = install_transaction.transaction_paths(target).state_dir
    else:
        root = target
    manifest_path = root / install_manifest.MANIFEST_NAME
    state_path = root / STATE_NAME
    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    state_exists = state_path.exists() or state_path.is_symlink()
    if not manifest_exists and not state_exists:
        return None, None, "unmanaged"
    if not manifest_exists or not state_exists:
        return None, None, "destination install metadata is incomplete"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None, None, "destination install manifest is not a regular file"
    if state_path.is_symlink() or not state_path.is_file():
        return None, None, "destination install state is not a regular file"
    try:
        manifest = install_manifest.load_manifest(manifest_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError, install_manifest.ManifestError) as exc:
        return None, None, f"destination install metadata is invalid: {exc}"
    if not isinstance(state, dict):
        return None, None, "destination install state is not a JSON object"
    if state.get("schema") != STATE_SCHEMA:
        return None, None, "destination install state has an unsupported schema"
    if state.get("status") != "committed":
        return None, None, "destination install state is not committed"
    try:
        digest = install_manifest.manifest_digest(manifest)
    except (ValueError, RuntimeError) as exc:
        return None, None, f"destination install manifest is invalid: {exc}"
    if str(state.get("manifest_digest") or "") != digest:
        return None, None, "destination install state and manifest digest differ"
    if not str(state.get("install_id") or ""):
        return None, None, "destination install state has no install ID"
    return state, manifest, ""


def _state_timestamp(state: Mapping[str, Any]) -> str:
    return str(state.get("updated") or state.get("created") or "")


def _restore_lineage(
    root: Path,
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    target: Path,
    file_restore: bool,
) -> _RestoreLineage:
    """Compare destination install identity with the archived install.

    A destination whose previous install is the archive is newer; a
    destination that is the archive's previous install is older.  Other
    install IDs are reported as a different lineage, with a timestamp-derived
    newer/older hint when both states provide one.  Only same-install restores
    avoid confirmation; an absent destination is a normal new install, while
    an unmanaged or unknown destination requires explicit confirmation.
    """
    install = payload.get("install")
    if not isinstance(install, Mapping):
        install = {}
    archive_id = str(install.get("install_id") or "")
    archive_version = str(
        install.get("version")
        or (manifest.get("artifact") or {}).get("version")
        or ""
    )
    archive_previous = payload.get("previous_install")
    if not isinstance(archive_previous, Mapping):
        archive_previous = {}
    archive_previous_id = str(archive_previous.get("install_id") or "")
    try:
        archive_state = _load_state(root)[1]
    except (RecoveryError, OSError):
        archive_state = {}
    if not archive_previous_id:
        previous = archive_state.get("previous_install")
        if isinstance(previous, Mapping):
            archive_previous_id = str(previous.get("install_id") or "")
    archive_created = _state_timestamp(archive_state)

    if not target.exists() and not target.is_symlink():
        return _RestoreLineage(
            relation="absent",
            reason="destination is absent",
            confirmation_required=False,
            archive_install_id=archive_id,
            archive_version=archive_version,
            archive_created=archive_created,
        )

    state, current_manifest, problem = _current_install_metadata(
        target, file_restore
    )
    if state is None or current_manifest is None:
        relation = "unmanaged" if problem == "unmanaged" else "unknown"
        reason = (
            "destination has no managed install metadata"
            if relation == "unmanaged"
            else f"destination lineage is unknown: {problem}"
        )
        return _RestoreLineage(
            relation=relation,
            reason=reason,
            confirmation_required=True,
            archive_install_id=archive_id,
            archive_version=archive_version,
            archive_created=archive_created,
        )

    destination_id = str(state.get("install_id") or "")
    destination_version = str(
        (current_manifest.get("artifact") or {}).get("version") or ""
    )
    destination_created = _state_timestamp(state)
    previous = state.get("previous_install")
    destination_previous_id = (
        str(previous.get("install_id") or "")
        if isinstance(previous, Mapping)
        else ""
    )

    relation = "different-lineage"
    destination_newer = False
    destination_older = False
    if destination_id and destination_id == archive_id:
        relation = "same"
        reason = "destination is the same install as the archive"
    elif destination_previous_id and destination_previous_id == archive_id:
        relation = "newer"
        destination_newer = True
        reason = "destination is newer than the archive"
    elif archive_previous_id and archive_previous_id == destination_id:
        relation = "older"
        destination_older = True
        reason = "destination is older than the archive"
    else:
        reason = "destination is from a different install lineage"
        if destination_created and archive_created:
            if destination_created > archive_created:
                destination_newer = True
                reason += "; destination is newer than the archive"
            elif destination_created < archive_created:
                destination_older = True
                reason += "; destination is older than the archive"

    return _RestoreLineage(
        relation=relation,
        reason=reason,
        confirmation_required=relation in {"newer", "older", "different-lineage"},
        destination_newer=destination_newer,
        destination_older=destination_older,
        archive_install_id=archive_id,
        destination_install_id=destination_id,
        archive_version=archive_version,
        destination_version=destination_version,
        archive_created=archive_created,
        destination_created=destination_created,
    )


def _restore_preflight(
    archive: Path,
    destination: Optional[Path] = None,
    *,
    verified: Optional[RecoveryReport] = None,
    asset_source: Optional[Path] = None,
    allow_missing_assets: bool = False,
) -> _RestorePreflight:
    """Verify an archive and resolve every restore-time safety decision.

    Both the read-only readiness inventory and the real restore use this
    function so a destination shape, required asset or archive kind cannot be
    accepted by one path and rejected by the other.
    """
    archive_path = Path(archive).expanduser()
    if verified is None:
        verified = verify_recovery_archive(archive_path)
    root = verified.path
    payload = load_recovery_manifest(root / RECOVERY_NAME)
    manifest = install_manifest.load_manifest(
        root / install_manifest.MANIFEST_NAME
    )
    target = _restore_target(root, payload, destination)
    asset_source_root = _resolve_asset_source(asset_source)
    if asset_source_root is not None:
        source_missing = _missing_required_assets(asset_source_root, manifest)
        if source_missing:
            raise RecoveryError(
                "asset source is missing required assets: "
                + "; ".join(source_missing)
            )
        _validate_asset_source_symlinks(asset_source_root, manifest)

    missing_assets = _missing_required_assets(target, manifest)
    if (
        missing_assets
        and not allow_missing_assets
        and asset_source_root is None
    ):
        raise RecoveryError(
            "recovery destination is missing required assets: "
            + "; ".join(missing_assets)
        )

    app_relative = _app_entrypoint(manifest)
    pyz = root.joinpath(*PurePosixPath(app_relative).parts)
    if pyz.suffix.lower() != ".pyz":
        pyz = root / "offgrid.pyz"
    archive_is_pyz = (
        pyz.is_file()
        and not pyz.is_symlink()
        and not (root / "offgrid").is_dir()
    )
    target_exists = target.exists()
    if target_exists and not (target.is_file() or target.is_dir()):
        raise RecoveryError(
            f"recovery destination is not a directory or pyz file: {target}"
        )
    file_restore = target.is_file() or (
        not target_exists
        and (
            target.suffix.lower() == ".pyz"
            or (
                archive_is_pyz
                and str((manifest.get("artifact") or {}).get("kind") or "")
                == "portable"
            )
        )
    )
    if file_restore and not archive_is_pyz:
        raise RecoveryError(
            "recovery archive contains a directory install; "
            f"destination is a file: {target}"
        )
    lineage = _restore_lineage(
        root, payload, manifest, target, file_restore
    )
    return _RestorePreflight(
        verified=verified,
        payload=payload,
        manifest=manifest,
        target=target,
        pyz=pyz,
        file_restore=file_restore,
        missing_assets=tuple(missing_assets),
        asset_source=asset_source_root,
        lineage=lineage,
    )


def _restore_source(
    archive: Path,
    manifest: Mapping[str, Any],
    parent: Path,
    name: str,
) -> Path:
    """Build the app-only bundle that the install transaction restores."""
    try:
        source = Path(
            tempfile.mkdtemp(
                prefix=f".{name}.recovery-restore-",
                dir=str(parent),
            )
        )
    except OSError as exc:
        raise RecoveryError(f"could not stage recovery source: {exc}") from exc
    try:
        entries: List[Mapping[str, Any]] = []
        entries.extend(
            entry for entry in manifest.get("files") or [] if isinstance(entry, dict)
        )
        entries.extend(
            entry
            for entry in manifest.get("user_state") or []
            if isinstance(entry, dict)
        )
        for entry in entries:
            relative = str(entry.get("path") or "")
            if not relative:
                continue
            source_path = archive.joinpath(*PurePosixPath(relative).parts)
            if source_path.is_symlink():
                raise RecoveryError(
                    f"recovery app file is a symlink: {relative}"
                )
            if not source_path.is_file():
                continue
            target_path = source.joinpath(*PurePosixPath(relative).parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
        shutil.copy2(
            archive / install_manifest.MANIFEST_NAME,
            source / install_manifest.MANIFEST_NAME,
        )
        return source
    except RecoveryError:
        shutil.rmtree(source, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(source, ignore_errors=True)
        raise RecoveryError(f"could not stage recovery files: {exc}") from exc


def _validate_asset_source_symlinks(
    source: Path, manifest: Mapping[str, Any]
) -> None:
    """Reject symlinks in required runtime trees before copying any asset."""
    for entry in manifest.get("runtimes") or []:
        if not isinstance(entry, dict) or not entry.get("required"):
            continue
        relative = str(entry.get("path") or "")
        directory = source.joinpath(*PurePosixPath(relative).parts)
        if not directory.is_dir() or directory.is_symlink():
            continue
        for current, dirnames, filenames in os.walk(str(directory)):
            current_path = Path(current)
            for name in list(dirnames) + list(filenames):
                path = current_path / name
                if path.is_symlink():
                    raise RecoveryError(
                        "asset source contains a symlink: "
                        f"{path.relative_to(source).as_posix()}"
                    )


def _copy_recovery_runtime(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (
        f".{target.name}.recovery-assets-{uuid.uuid4().hex}"
    )
    try:
        shutil.copytree(source, temporary, symlinks=False)
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        os.replace(temporary, target)
    except OSError as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise RecoveryError(
            f"could not reuse runtime assets {source}: {exc}"
        ) from exc


def _copy_recovery_asset_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (
        f".{target.name}.recovery-asset-{uuid.uuid4().hex}"
    )
    try:
        shutil.copy2(source, temporary)
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        os.replace(temporary, target)
    except OSError as exc:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        raise RecoveryError(
            f"could not reuse asset {source}: {exc}"
        ) from exc


def _materialize_recovery_assets(preflight: _RestorePreflight) -> None:
    """Copy verified surviving assets into the destination before restore."""
    source = preflight.asset_source
    if source is None:
        return
    target = preflight.target
    root = target.parent if preflight.file_restore else target
    _validate_asset_source_symlinks(source, preflight.manifest)

    runtimes = [
        entry
        for entry in preflight.manifest.get("runtimes") or []
        if isinstance(entry, dict) and entry.get("required")
    ]
    models = [
        entry
        for entry in preflight.manifest.get("models") or []
        if isinstance(entry, dict) and entry.get("required")
    ]
    if not runtimes and not models:
        return
    root.mkdir(parents=True, exist_ok=True)

    for entry in runtimes:
        if _plan_runtime_asset(root, entry).status == "ready":
            continue
        relative = str(entry.get("path") or "")
        if not relative:
            continue
        _copy_recovery_runtime(
            source.joinpath(*PurePosixPath(relative).parts),
            root.joinpath(*PurePosixPath(relative).parts),
        )
    for entry in models:
        if _plan_model_asset(root, entry).status == "ready":
            continue
        relative = str(entry.get("path") or "")
        if not relative:
            continue
        _copy_recovery_asset_file(
            source.joinpath(*PurePosixPath(relative).parts),
            root.joinpath(*PurePosixPath(relative).parts),
        )


def restore_recovery_archive(
    archive: Path,
    destination: Optional[Path] = None,
    *,
    force: bool = False,
    asset_source: Optional[Path] = None,
    confirm: bool = False,
    expect_plan: Optional[str] = None,
) -> RecoveryRestoreReport:
    """Verify and restore an app-only archive.

    The archive contains only immutable app files.  When *asset_source* is
    supplied, required models and runtimes there are checked against the
    archive's recorded hashes, then copied into the destination before the
    install transaction starts.  Otherwise the destination's existing models
    and runtimes are preserved.  Before activation, the transaction snapshots
    the app and metadata currently at the destination so the restore can
    itself be undone.  A destination that is newer, older or from a different
    install lineage is refused unless *confirm* is true.
    """
    from . import install_transaction

    preflight = _restore_preflight(
        archive, destination, asset_source=asset_source
    )
    verified = preflight.verified
    root = verified.path
    payload = preflight.payload
    manifest = preflight.manifest
    target = preflight.target
    pyz = preflight.pyz
    file_restore = preflight.file_restore
    lineage = preflight.lineage
    plan = _build_restore_plan(preflight, force=force, confirm=confirm)
    if expect_plan is not None:
        expected = str(expect_plan).strip()
        if not expected:
            raise RecoveryError("expected recovery plan ID is empty")
        if plan.plan_id != expected:
            raise RecoveryError(
                "recovery plan changed since it was reviewed; run the dry-run "
                "preview again before restoring"
            )
        if not plan.ready:
            raise RecoveryError("recovery plan is not ready: " + plan.reason)
    if (
        lineage is not None
        and lineage.confirmation_required
        and not confirm
    ):
        raise RecoveryError(
            "restore requires explicit confirmation: " + lineage.reason
        )

    try:
        _materialize_recovery_assets(preflight)
        replace_existing = target.exists() or target.is_symlink()
        if file_restore:
            installed = install_transaction.install_file(
                pyz,
                target,
                force=replace_existing or force,
                safety_snapshot=True,
                install_id=str(payload["install"]["install_id"]),
            )
        else:
            source = _restore_source(root, manifest, target.parent, target.name)
            try:
                installed = install_transaction.install_directory(
                    source,
                    target,
                    force=replace_existing or force,
                    allow_missing_assets=True,
                    preserve_unlisted_assets=True,
                    safety_snapshot=True,
                    install_id=str(payload["install"]["install_id"]),
                )
            finally:
                shutil.rmtree(source, ignore_errors=True)
    except RecoveryError:
        raise
    except install_transaction.InstallError as exc:
        raise RecoveryError(f"could not restore recovery archive: {exc}") from exc
    except OSError as exc:
        raise RecoveryError(f"could not restore recovery archive: {exc}") from exc

    return RecoveryRestoreReport(
        archive=root,
        destination=Path(installed.destination),
        version=verified.version,
        install_id=installed.install_id,
        manifest_digest=verified.manifest_digest,
        files=verified.files,
        bytes=verified.bytes,
        safety_snapshot=installed.recovery_bundle,
        safety_generation=installed.recovery_generation,
        asset_source=str(preflight.asset_source or ""),
        **_lineage_fields(lineage),
    )
