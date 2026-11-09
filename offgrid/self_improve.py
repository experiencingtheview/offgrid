"""Gated self-improvement: propose in a copy, apply only after approval.

The model never edits the live tree while looking for an improvement. It works
in a temporary copy, and the result is stored as a pending request under
``.offgrid/self-improve/requests``. Nothing in the live workspace changes until
a human approves that request.

Every applied request carries a snapshot of the files it touches. Verification
failure rolls the request back automatically, and ``restore`` can return an
applied request to its pre-approval state later. Snapshots contain only the
files in the request, not the whole repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .orchestrator import Team
from .private import ensure_private_dir, write_private_text
from .providers import LocalProvider, ProviderError
from .ui import UI
from .workspace import IGNORE_DIRS, IGNORE_SUFFIXES, Workspace, WorkspaceError

STATE_DIR = Path(".offgrid") / "self-improve"
REQUESTS_DIR = STATE_DIR / "requests"
REQUEST_FILE = "request.json"
BEFORE_DIR = "before"
AFTER_DIR = "after"

# Directories that are not useful to an offline self-review and can be large or
# private. IGNORE_DIRS covers the workspace's usual build and environment dirs.
EXTRA_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "offgrid-library",
    "logs",
    "run",
    "offgrid-usb",
    "runtime",
    "models",
}
SECRET_SUFFIXES = (".ogv", ".pem", ".key", ".p12", ".pfx")

RESERVED_PATHS = {
    ".git",
    ".hg",
    ".svn",
    ".offgrid",
    "models",
    "runtime",
    "dist",
    "build",
    "target",
    "node_modules",
    "__pycache__",
    "vendor",
    "offgrid-library",
    "logs",
    "run",
    "offgrid-usb",
}

SELF_IMPROVEMENT_TASK = """\
Improve OffGrid itself. Work only inside this workspace.

Find one concrete, small, low-risk improvement that makes the application more
useful, correct, or robust. It can be a bug fix, a missing edge case, a test, or
a documentation correction. Prefer a focused change over a rewrite. Read the
surrounding code and tests before editing. Keep the standard-library-only
runtime and offline-by-default behaviour intact. Do not add dependencies, do
not weaken or delete a test just to make it pass, and do not change the
security boundaries. Do not modify .offgrid or files outside this workspace.

The change is a proposal: a human will review and approve it before it reaches
the real project. Implement it fully, then call finish with a short summary and
the reason it matters. Do not commit or push.
"""

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,100}$")


class ImproveError(Exception):
    """Raised when a self-improvement request cannot be created or applied."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_rel(value: str) -> str:
    """Return a workspace-relative path, refusing traversal and reserved dirs."""
    text = str(value or "").strip()
    if not text:
        raise ImproveError("empty path in self-improvement request")
    path = Path(text)
    if path.is_absolute() or getattr(path, "drive", ""):
        raise ImproveError(f"self-improvement path must be relative: {text}")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ImproveError(f"unsafe self-improvement path: {text}")
    if parts[0] in RESERVED_PATHS:
        raise ImproveError(f"self-improvement cannot change reserved path: {text}")
    return str(path)


def _is_secret_name(name: str) -> bool:
    if name == ".env" or (name.startswith(".env.") and not name.endswith(".example")):
        return True
    return name.lower().endswith(SECRET_SUFFIXES)


def _copy_ignore(directory: str, names: List[str]) -> set:
    ignored = set()
    for name in names:
        if name in IGNORE_DIRS or name in EXTRA_IGNORE_DIRS:
            ignored.add(name)
        elif name.endswith(IGNORE_SUFFIXES) or name.endswith((".pyz", ".whl", ".egg-info")):
            ignored.add(name)
        elif _is_secret_name(name):
            ignored.add(name)
    return ignored


def _time_label(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "unknown"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Request:
    """One pending or historical self-improvement request."""

    directory: Path
    data: Dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data.get("id") or self.directory.name)

    @property
    def state(self) -> str:
        return str(self.data.get("state") or "unknown")

    def save(self) -> None:
        ensure_private_dir(self.directory)
        write_private_text(
            self.directory / REQUEST_FILE,
            json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )


class SelfImprover:
    """Generate, inspect, approve, reject and restore self-improvement requests."""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        provider: Optional[LocalProvider] = None,
        ui: Optional[UI] = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.provider = provider
        self.ui = ui or UI(config)
        self.requests_dir = workspace.root / REQUESTS_DIR
        self._proposal_lock = threading.Lock()

    # -- paths and state -------------------------------------------------
    def _request_dir(self, request_id: str) -> Path:
        if not _SAFE_ID.fullmatch(str(request_id or "")):
            raise ImproveError(f"invalid self-improvement request id: {request_id!r}")
        return self.requests_dir / str(request_id)

    def _live_path(self, rel: str) -> Path:
        """Resolve safely, and refuse to write through a file symlink."""
        lexical = self.workspace.root / rel
        if lexical.is_symlink():
            raise ImproveError(f"refusing to change a symlink: {rel}")
        try:
            return self.workspace.resolve(rel)
        except WorkspaceError as exc:
            raise ImproveError(str(exc)) from exc

    @staticmethod
    def _staged_path(workspace: Workspace, rel: str) -> Path:
        lexical = workspace.root / rel
        if lexical.is_symlink():
            raise ImproveError(f"the staging run changed a symlink: {rel}")
        try:
            return workspace.resolve(rel)
        except WorkspaceError as exc:
            raise ImproveError(str(exc)) from exc

    def _load_dir(self, directory: Path) -> Request:
        path = directory / REQUEST_FILE
        if not path.is_file():
            raise ImproveError(f"self-improvement request is missing {REQUEST_FILE}: {directory}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImproveError(f"could not read self-improvement request {directory.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise ImproveError(f"self-improvement request is not an object: {directory.name}")
        if str(data.get("id") or directory.name) != directory.name:
            raise ImproveError(f"self-improvement request id does not match its folder: {directory.name}")
        return Request(directory, data)

    def get(self, request_id: str) -> Request:
        directory = self._request_dir(request_id)
        if not directory.is_dir():
            raise ImproveError(f"no such self-improvement request: {request_id}")
        return self._load_dir(directory)

    def all_requests(self) -> List[Request]:
        if not self.requests_dir.is_dir():
            return []
        found: List[Request] = []
        try:
            entries = list(self.requests_dir.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                found.append(self._load_dir(entry))
            except ImproveError:
                continue
        return sorted(
            found, key=lambda item: _as_float(item.data.get("created")), reverse=True
        )

    def pending(self) -> List[Request]:
        return [item for item in self.all_requests() if item.state == "pending"]

    def invalid_requests(self) -> List[str]:
        """Return readable descriptions of request folders that cannot be loaded."""
        if not self.requests_dir.is_dir():
            return []
        broken: List[str] = []
        try:
            entries = list(self.requests_dir.iterdir())
        except OSError as exc:
            return [f"{self.requests_dir}: {exc}"]
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                self._load_dir(entry)
            except (ImproveError, OSError) as exc:
                broken.append(f"{entry.name}: {exc}")
        return sorted(broken)

    def latest_applied(self) -> Optional[Request]:
        for item in self.all_requests():
            if item.state == "applied":
                return item
        return None

    def _check_not_pending(self) -> None:
        waiting = self.pending()
        if waiting:
            raise ImproveError(
                f"a self-improvement request is already waiting for approval: {waiting[0].id}"
            )

    # -- proposal generation ---------------------------------------------
    def _task(self, hint: str) -> str:
        text = SELF_IMPROVEMENT_TASK
        hint = (hint or "").strip()
        if hint:
            text += f"\n\nFOCUS FOR THIS PASS\n{hint[:2000]}\n"
        return text

    def _stage(self, staging: Path) -> None:
        try:
            shutil.copytree(
                str(self.workspace.root),
                str(staging),
                ignore=_copy_ignore,
                symlinks=True,
            )
        except (OSError, shutil.Error) as exc:
            raise ImproveError(f"could not copy the workspace for self-improvement: {exc}") from exc

        # A symlink in the staging tree must not point outside it. Internal
        # links are harmless and the workspace will resolve them normally.
        for path in staging.rglob("*"):
            if not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=True)
                target.relative_to(staging.resolve())
            except (OSError, ValueError):
                raise ImproveError(f"workspace symlink escapes the workspace: {path}") from None

    def _staging_config(self, staging: Path, quiet: bool) -> Config:
        values = self.config.as_dict()
        values.update(
            {
                "workspace": str(staging),
                "command_policy": "deny",
                "network": "deny",
                "offline": True,
                "fleet": False,
                "council": False,
                "allow_public_council": False,
                "memory": False,
                "reflect": False,
                "review_lessons": False,
                "scale": False,
                "speak": False,
                "verbose": 0 if quiet else max(0, self.config.verbose),
                "color": False if quiet else self.config.color,
                "self_improve_idle": False,
            }
        )
        return Config(**values)

    def _validate_changed(self, changed: List[str], staging_ws: Workspace) -> List[str]:
        clean: List[str] = []
        for raw in changed:
            rel = _safe_rel(raw)
            live = self._live_path(rel)
            staged = self._staged_path(staging_ws, rel)
            if live.exists() and not live.is_file():
                raise ImproveError(f"self-improvement target is not a regular file: {rel}")
            if staged.exists() and not staged.is_file():
                raise ImproveError(f"self-improvement produced a non-file: {rel}")
            clean.append(rel)
        return sorted(set(clean))

    def stage(
        self,
        source: str = "manual",
        quiet: bool = False,
    ) -> "ProposalSession":
        """Start a live staging copy that can accept more than one turn."""
        if self.provider is None:
            raise ImproveError("a local model provider is required to propose an improvement")
        if not self._proposal_lock.acquire(blocking=False):
            raise ImproveError("another self-improvement proposal is already being generated")
        temporary = None
        try:
            self._check_not_pending()
            temporary = tempfile.TemporaryDirectory(prefix="offgrid-self-improve-")
            staging = Path(temporary.name) / "workspace"
            self._stage(staging)
            staging_config = self._staging_config(staging, quiet)
            staging_ui = UI(staging_config)
            staging_workspace = Workspace(str(staging))
            team = Team(
                self.provider,
                staging_workspace,
                staging_config,
                staging_ui,
            )
            return ProposalSession(
                improver=self,
                temporary=temporary,
                workspace=staging_workspace,
                config=staging_config,
                ui=staging_ui,
                team=team,
                source=source,
            )
        except BaseException:
            if temporary is not None:
                temporary.cleanup()
            self._proposal_lock.release()
            raise

    def propose(
        self,
        hint: str = "",
        source: str = "manual",
        quiet: bool = False,
    ) -> Request:
        """Run one turn in a staging copy and store a pending request."""
        with self.stage(source=source, quiet=quiet) as staged:
            staged.run_turn(hint)
            return staged.finish()

    def _persist(
        self,
        staging_workspace: Workspace,
        changed: List[str],
        task: str,
        hint: str,
        source: str,
        result: Any,
        conversation: Optional[List[str]] = None,
    ) -> Request:
        request_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        directory = self._request_dir(request_id)
        try:
            ensure_private_dir(directory)
            before_dir = directory / BEFORE_DIR
            after_dir = directory / AFTER_DIR
            files: List[Dict[str, Any]] = []
            for rel in changed:
                live = self._live_path(rel)
                staged = self._staged_path(staging_workspace, rel)
                item: Dict[str, Any] = {
                    "path": rel,
                    "before_exists": live.is_file(),
                    "before_sha256": "",
                    "after_exists": staged.is_file(),
                    "after_sha256": "",
                }
                if item["before_exists"]:
                    destination = before_dir / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, destination)
                    item["before_sha256"] = _sha256(destination)
                if item["after_exists"]:
                    destination = after_dir / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staged, destination)
                    item["after_sha256"] = _sha256(destination)
                files.append(item)

            summary = str(getattr(result, "summary", "") or "").strip()
            diff = staging_workspace.diff(max_chars=100000)
            data: Dict[str, Any] = {
                "version": 1,
                "id": request_id,
                "created": time.time(),
                "state": "pending",
                "source": source,
                "task_hint": hint[:2000],
                "task": task,
                "conversation": [str(item)[:2000] for item in (conversation or ())][:100],
                "summary": summary[:12000],
                "diff": diff,
                "files": files,
                "verify_command": str(getattr(result, "verify_command", "") or ""),
                "verify_ok": getattr(result, "verify_ok", None),
                "verify_output": str(getattr(result, "verify_output", "") or "")[:8000],
                "review_verdict": str(getattr(result, "review_verdict", "") or ""),
                "review_issues": list(getattr(result, "review_issues", []) or [])[:30],
                "security": list(getattr(result, "security", []) or [])[:30],
            }
            request = Request(directory, data)
            request.save()
            return request
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    # -- rendering -------------------------------------------------------
    @staticmethod
    def describe(request: Request, include_diff: bool = True, max_diff: int = 30000) -> str:
        data = request.data
        lines = [
            f"request {request.id} [{request.state}]",
            f"created {_time_label(data.get('created') or 0)}",
            f"summary {str(data.get('summary') or '(no summary)').strip()}",
        ]
        files = data.get("files") or []
        if files:
            lines.append("files:")
            for item in files:
                action = "delete" if not item.get("after_exists") else "write"
                lines.append(f"  {action} {item.get('path')}")
        command = str(data.get("verify_command") or "")
        if command:
            lines.append(f"verify {command}")
        else:
            lines.append("verify not available in the staging run")
        review = str(data.get("review_verdict") or "")
        if review:
            lines.append(f"review {review}")
        if data.get("state") == "applied" and data.get("verify_ok") is True:
            lines.append("applied and verified")
        if data.get("state") == "rolled_back":
            lines.append("rolled back; the workspace was restored")
        if data.get("state") == "restored":
            lines.append("restored to the pre-approval version")
        if data.get("failure"):
            lines.append(f"failure {data['failure']}")
        if include_diff:
            diff = str(data.get("diff") or "")
            if len(diff) > max_diff:
                diff = diff[:max_diff] + f"\n... diff trimmed at {max_diff} chars ..."
            lines.extend(["", diff or "(no diff recorded)"])
        return "\n".join(lines)

    # -- apply and rollback ----------------------------------------------
    def _check_conflicts(self, request: Request, force: bool) -> None:
        conflicts: List[str] = []
        for item in request.data.get("files") or []:
            rel = _safe_rel(str(item.get("path") or ""))
            live = self._live_path(rel)
            exists = live.is_file()
            if exists != bool(item.get("before_exists")):
                conflicts.append(f"{rel} existence changed")
                continue
            if exists and _sha256(live) != str(item.get("before_sha256") or ""):
                conflicts.append(f"{rel} content changed")
        if conflicts and not force:
            listed = ", ".join(conflicts[:6])
            raise ImproveError(
                "the workspace changed after this proposal was made: "
                f"{listed}. Re-propose, or pass --force to apply over it."
            )

    def _refresh_before(self, request: Request) -> None:
        before_dir = request.directory / BEFORE_DIR
        shutil.rmtree(before_dir, ignore_errors=True)
        ensure_private_dir(before_dir)
        for item in request.data.get("files") or []:
            rel = _safe_rel(str(item.get("path") or ""))
            live = self._live_path(rel)
            if live.is_file():
                destination = before_dir / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, destination)
                item["before_exists"] = True
                item["before_sha256"] = _sha256(destination)
            else:
                item["before_exists"] = False
                item["before_sha256"] = ""
        request.save()

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.offgrid-{secrets.token_hex(4)}.tmp"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(str(temporary), str(destination))
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def _apply_file(self, request: Request, item: Dict[str, Any]) -> None:
        rel = _safe_rel(str(item.get("path") or ""))
        live = self._live_path(rel)
        if item.get("after_exists"):
            source = request.directory / AFTER_DIR / rel
            if not source.is_file():
                raise ImproveError(f"proposal is missing its new version of {rel}")
            self._atomic_copy(source, live)
        else:
            if live.exists() and not live.is_file():
                raise ImproveError(f"cannot delete non-file path: {rel}")
            if live.is_file():
                live.unlink()

    def _restore_files(self, request: Request) -> None:
        for item in request.data.get("files") or []:
            rel = _safe_rel(str(item.get("path") or ""))
            live = self._live_path(rel)
            if item.get("before_exists"):
                source = request.directory / BEFORE_DIR / rel
                if not source.is_file():
                    raise ImproveError(f"snapshot is missing the previous version of {rel}")
                self._atomic_copy(source, live)
            else:
                if live.is_symlink():
                    live.unlink()
                elif live.is_file():
                    live.unlink()
                elif live.exists():
                    raise ImproveError(f"cannot remove non-file path: {rel}")

    def _roll_back(self, request: Request, reason: str) -> None:
        error = ""
        try:
            self._restore_files(request)
        except Exception as exc:  # noqa: BLE001 - report a failed recovery honestly
            error = f"; restore also failed: {exc}"
        request.data["state"] = "rollback_failed" if error else "rolled_back"
        request.data["rolled_back_at"] = time.time()
        request.data["failure"] = reason + error
        request.save()

    def approve(self, request_id: str, force: bool = False) -> Request:
        request = self.get(request_id)
        if request.state != "pending":
            raise ImproveError(f"request {request.id} is {request.state}, not pending")
        self._check_conflicts(request, force)
        self._refresh_before(request)
        try:
            for item in request.data.get("files") or []:
                self._apply_file(request, item)
        except Exception as exc:
            self._roll_back(request, f"apply failed: {exc}")
            raise ImproveError(f"could not apply {request.id}: {exc}") from exc

        command = str(request.data.get("verify_command") or "")
        ok: Optional[bool] = None
        output = ""
        if command and self.config.command_policy == "ask" and not sys.stdin.isatty():
            output = "verification skipped: no terminal to approve the command"
        elif command:
            try:
                live_team = Team(self.provider, self.workspace, self.config, self.ui)
                ok, output = live_team.verify(command)
            except KeyboardInterrupt:
                self._roll_back(request, "verification interrupted")
                raise
            except Exception as exc:  # noqa: BLE001 - always recover the live tree
                self._roll_back(request, f"verification could not run: {exc}")
                raise ImproveError(
                    f"could not verify {request.id}; the previous version was restored: {exc}"
                ) from exc
        request.data["verify_ok"] = ok
        request.data["verify_output"] = output[:8000]
        if ok is False:
            self._roll_back(request, "verification failed")
            return request
        request.data["state"] = "applied"
        request.data["applied_at"] = time.time()
        request.data["failure"] = ""
        request.save()
        return request

    def _check_after(self, request: Request, force: bool) -> None:
        conflicts: List[str] = []
        for item in request.data.get("files") or []:
            rel = _safe_rel(str(item.get("path") or ""))
            live = self._live_path(rel)
            exists = live.is_file()
            if exists != bool(item.get("after_exists")):
                conflicts.append(f"{rel} existence changed")
                continue
            if exists and _sha256(live) != str(item.get("after_sha256") or ""):
                conflicts.append(f"{rel} content changed")
        if conflicts and not force:
            listed = ", ".join(conflicts[:6])
            raise ImproveError(
                "the workspace changed after this request was applied: "
                f"{listed}. Pass --force only if you want to discard those later edits."
            )

    def restore(self, request_id: str, force: bool = False) -> Request:
        request = self.get(request_id)
        if request.state == "rollback_failed":
            if not force:
                raise ImproveError(
                    f"{request.id} had an incomplete rollback; inspect it and pass --force "
                    "to retry the snapshot restore"
                )
        elif request.state != "applied":
            raise ImproveError(f"request {request.id} is {request.state}, not applied")
        else:
            self._check_after(request, force)
        try:
            self._restore_files(request)
        except Exception as exc:
            raise ImproveError(f"could not restore {request.id}: {exc}") from exc
        request.data["state"] = "restored"
        request.data["restored_at"] = time.time()
        request.save()
        return request

    def reject(self, request_id: str) -> Request:
        request = self.get(request_id)
        if request.state != "pending":
            raise ImproveError(f"request {request.id} is {request.state}, not pending")
        request.data["state"] = "rejected"
        request.data["rejected_at"] = time.time()
        request.save()
        return request


class ProposalSession:
    """A private staging copy that survives across conversation turns."""

    def __init__(
        self,
        improver: SelfImprover,
        temporary: Any,
        workspace: Workspace,
        config: Config,
        ui: UI,
        team: Team,
        source: str,
    ) -> None:
        self.improver = improver
        self.temporary = temporary
        self.workspace = workspace
        self.config = config
        self.ui = ui
        self.team = team
        self.source = source
        self.turns: List[str] = []
        self.last_result: Any = None
        self._closed = False

    @property
    def changed(self) -> List[str]:
        return self.workspace.changed_files()

    def run_turn(self, hint: str = "") -> Any:
        """Run one instruction while preserving the staged files and history."""
        if self._closed:
            raise ImproveError("the self-improvement staging copy is closed")
        hint = str(hint or "").strip()
        task = self.improver._task(hint)
        result = self.team.run(task, verify_override=self.config.verify_command)
        if hint:
            self.turns.append(hint)
        self.last_result = result
        return result

    def finish(self) -> Request:
        """Turn the current staged state into the pending approval request."""
        if self._closed:
            raise ImproveError("the self-improvement staging copy is closed")
        if self.last_result is None:
            raise ImproveError("there is no staged turn yet; describe a change first")
        changed = self.improver._validate_changed(
            self.workspace.changed_files(), self.workspace
        )
        if not changed:
            raise ImproveError(
                "the model did not change any files, so no approval request was created"
            )
        turns = tuple(self.turns)
        request = self.improver._persist(
            staging_workspace=self.workspace,
            changed=changed,
            task=self.improver._task("\n\n".join(turns)),
            hint="\n\n".join(turns),
            source=self.source,
            result=self.last_result,
            conversation=list(turns),
        )
        self.close()
        return request

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.temporary.cleanup()
        finally:
            self.improver._proposal_lock.release()

    def __enter__(self) -> "ProposalSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class ImprovementTerminal:
    """Interactive, offline-first front end for the gated workflow.

    Each plain-language line is another turn in one private staging copy. The
    live tree is untouched until the user runs ``/done`` and then explicitly
    approves the resulting request. The terminal never applies a proposal
    automatically.
    """

    HELP = """\
  describe a change; repeat for more turns
  /done             create the approval request from the staged turns
  /cancel           discard the staged conversation
  /help             show this help
  /list             list pending and recent requests
  /show [ID]        show a request and its diff
  /approve [ID]     review and apply a pending request
  /reject [ID]      discard a pending request
  /restore [ID]     undo an applied request
  /quit             leave without applying anything
"""

    def __init__(self, improver: SelfImprover, ui: UI) -> None:
        self.improver = improver
        self.ui = ui
        self._staged: Optional[ProposalSession] = None

    def run(self) -> int:
        self._welcome()
        try:
            while True:
                try:
                    prompt = "staging> " if self._staged is not None else "improve> "
                    line = self.ui.ask(prompt)
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/") or text.lower() in ("quit", "exit"):
                    if self._command(text):
                        break
                    continue
                if self._staged is not None:
                    self._run_turn(text)
                else:
                    self._propose(text)
        finally:
            self._discard_staged(announce=bool(self._staged))
        return 0

    def _welcome(self) -> None:
        print()
        print(
            "  "
            + self.ui.paint(
                "OffGrid self-improvement terminal", "\033[1m", "\033[35m"
            )
        )
        self.ui.info("offline-first: the team works in a private copy")
        self.ui.info("each line is another turn; /done creates the approval request")
        self.ui.info("nothing reaches the live tree until you approve it")
        self.ui.info("/help for commands, /quit to leave")
        self._show_pending()

    def _show_pending(self) -> None:
        pending = self.improver.pending()
        if not pending:
            self.ui.info("no pending request")
            return
        self.ui.warn(f"{len(pending)} pending request(s)")
        for item in pending[:5]:
            print(f"  {item.id}  {self._summary(item)}")
        self.ui.info("review it with /show, then /approve or /reject")

    def _propose(self, hint: str) -> None:
        pending = self.improver.pending()
        if pending:
            self.ui.warn(f"request {pending[0].id} is waiting for approval")
            self.ui.info("use /show, /approve or /reject before starting another")
            return
        self.ui.info("working in a private copy; the live tree is unchanged")
        try:
            self._staged = self.improver.stage(source="terminal")
        except ImproveError as exc:
            self.ui.warn(str(exc))
            return
        except KeyboardInterrupt:
            print()
            self.ui.warn("proposal interrupted; the live tree is unchanged")
            return
        self._run_turn(hint)

    def _run_turn(self, hint: str) -> None:
        if self._staged is None:
            return
        try:
            self._staged.run_turn(hint)
        except ProviderError as exc:
            self.ui.error(str(exc))
            return
        except ImproveError as exc:
            self.ui.warn(str(exc))
            return
        except KeyboardInterrupt:
            print()
            self._discard_staged()
            self.ui.warn("turn interrupted; the staging copy was discarded")
            return
        self.ui.ok(
            f"staged turn {len(self._staged.turns)}; "
            f"{len(self._staged.changed)} file(s) changed in the private copy"
        )
        self.ui.info("keep going, or /done to create the approval request; /cancel to discard")

    def _finish_staged(self) -> None:
        if self._staged is None:
            self.ui.info("no staged conversation; describe a change first")
            return
        try:
            request = self._staged.finish()
        except ImproveError as exc:
            self.ui.warn(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - keep the staging session open
            self.ui.error(f"could not create the approval request: {exc}")
            return
        self._staged = None
        self._review(request)

    def _discard_staged(self, announce: bool = True) -> None:
        if self._staged is None:
            return
        session = self._staged
        self._staged = None
        session.close()
        if announce:
            self.ui.info("discarded staged changes; the live tree was not touched")

    def _review(self, request: Request) -> None:
        print()
        print(SelfImprover.describe(request))
        self.ui.info("the live tree is unchanged")
        choice = self.ui.ask(
            "  type 'approve' to apply, 'reject' to discard, or Enter to keep pending: "
        ).strip().lower()
        if choice == "approve":
            self._apply(request)
        elif choice == "reject":
            self._reject(request)
        else:
            self.ui.info(f"{request.id} is pending; inspect it later with /show")

    def _command(self, text: str) -> bool:
        parts = text.split(maxsplit=1)
        name = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if name in ("/quit", "/exit", "quit", "exit"):
            return True
        if name == "/done":
            self._finish_staged()
            return False
        if name == "/cancel":
            if self._staged is None:
                self.ui.info("no staged conversation to cancel")
            else:
                self._discard_staged()
            return False
        if name in ("/help", "/h"):
            print(self.HELP.rstrip())
            return False
        if name in ("/list", "/pending"):
            self._list()
            return False
        if name == "/show":
            self._show(argument)
            return False
        if name == "/approve":
            self._approve(argument)
            return False
        if name == "/reject":
            request = self._find(argument, pending_only=True)
            if request is not None:
                self._reject(request)
            return False
        if name in ("/restore", "/undo"):
            self._restore(argument)
            return False
        self.ui.warn(f"unknown command {name}; type /help")
        return False

    def _list(self) -> None:
        requests = self.improver.all_requests()
        pending = [item for item in requests if item.state == "pending"]
        recent = [item for item in requests if item.state != "pending"][:10]
        if not pending and not recent:
            self.ui.info("no self-improvement requests yet")
            return
        if pending:
            print()
            print(self.ui.paint("  waiting for your approval", "\033[1m"))
            for item in pending[:10]:
                print(f"  {item.id}  {self._summary(item)}")
        if recent:
            print()
            print(self.ui.paint("  recent requests", "\033[1m"))
            for item in recent:
                print(f"  {item.id}  [{item.state}]  {self._summary(item)}")

    def _show(self, request_id: str) -> None:
        request = self._find(request_id)
        if request is not None:
            print(SelfImprover.describe(request))

    def _find(
        self,
        request_id: str,
        pending_only: bool = False,
        applied_only: bool = False,
    ) -> Optional[Request]:
        try:
            if request_id:
                return self.improver.get(request_id)
            if pending_only:
                pending = self.improver.pending()
                if not pending:
                    self.ui.info("no pending self-improvement request")
                    return None
                return pending[0]
            if applied_only:
                applied = self.improver.latest_applied()
                if applied is None:
                    self.ui.info("no applied self-improvement request")
                    return None
                return applied
            requests = self.improver.all_requests()
            if not requests:
                self.ui.info("no self-improvement requests yet")
                return None
            pending = [item for item in requests if item.state == "pending"]
            return pending[0] if pending else requests[0]
        except ImproveError as exc:
            self.ui.warn(str(exc))
            return None

    def _approve(self, request_id: str) -> None:
        request = self._find(request_id, pending_only=True)
        if request is None:
            return
        if request.state != "pending":
            self.ui.warn(f"request {request.id} is {request.state}, not pending")
            return
        print(SelfImprover.describe(request))
        answer = self.ui.ask("  type 'approve' to apply this request: ").strip().lower()
        if answer != "approve":
            self.ui.info("cancelled; the live tree was not changed")
            return
        self._apply(request)

    def _apply(self, request: Request) -> None:
        try:
            result = self.improver.approve(request.id)
        except ProviderError as exc:
            self.ui.error(str(exc))
            return
        except ImproveError as exc:
            self.ui.error(str(exc))
            return
        except KeyboardInterrupt:
            self.ui.warn(
                "approval interrupted; check the request state before continuing"
            )
            return
        if result.state == "applied":
            self.ui.ok(f"applied {result.id}; restore with /restore {result.id}")
            if result.data.get("verify_ok") is True:
                self.ui.ok("verification passed")
            else:
                self.ui.warn("applied but not verified")
        else:
            self.ui.error(
                f"{result.id} was rolled back: "
                f"{result.data.get('failure') or 'verification failed'}"
            )

    def _reject(self, request: Request) -> None:
        try:
            result = self.improver.reject(request.id)
        except ImproveError as exc:
            self.ui.error(str(exc))
            return
        self.ui.ok(f"rejected {result.id}; the live tree was not changed")

    def _restore(self, request_id: str) -> None:
        request = self._find(request_id, applied_only=True)
        if request is None:
            return
        print(SelfImprover.describe(request, include_diff=False))
        try:
            result = self.improver.restore(request.id)
        except ImproveError as exc:
            self.ui.error(str(exc))
            return
        self.ui.ok(f"restored {result.id} to its pre-approval version")

    @staticmethod
    def _summary(request: Request) -> str:
        return str(request.data.get("summary") or "(no summary)").strip()[:120]


class IdleWatcher:
    """Create pending proposals after a quiet period, never apply them.

    Chat pauses the watcher while a user task runs and shares ``work_lock`` so
    a proposal and a user run do not use the model or workspace concurrently.
    """

    def __init__(
        self,
        improver: SelfImprover,
        minutes: float,
        notify: Optional[Callable[[Request], None]] = None,
    ) -> None:
        self.improver = improver
        self.minutes = max(0.05, float(minutes))
        self.notify = notify
        self.work_lock = threading.Lock()
        self.last_error = ""
        self.last_request: Optional[Request] = None
        self._stop = threading.Event()
        self._activity = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last = time.monotonic()

    def touch(self) -> None:
        self._activity.set()

    @contextmanager
    def busy(self):
        """Keep a user run and an idle proposal from happening together."""
        self.pause()
        self.work_lock.acquire()
        try:
            yield
        finally:
            self.work_lock.release()
            self.resume()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self.touch()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._last = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop,
            name="offgrid-idle-improve",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(0.2):
            if self._activity.is_set():
                self._activity.clear()
                self._last = time.monotonic()
                continue
            if self._paused.is_set():
                self._last = time.monotonic()
                continue
            if time.monotonic() - self._last < self.minutes:
                continue
            self._last = time.monotonic()
            try:
                if self.improver.pending():
                    continue
                with self.work_lock:
                    if (
                        self._stop.is_set()
                        or self._paused.is_set()
                        or self._activity.is_set()
                    ):
                        self._activity.clear()
                        self._last = time.monotonic()
                        continue
                    request = self.improver.propose(source="idle", quiet=True)
            except ProviderError as exc:
                self.last_error = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - idle work must never kill chat
                self.last_error = str(exc)
                continue
            self.last_request = request
            if self.notify is not None:
                try:
                    self.notify(request)
                except Exception:
                    pass
