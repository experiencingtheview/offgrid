"""Self-debug, housekeeping, and gated improvement for long-running deployments.

This is the server-side companion to the chat idle watcher. A pass:

1. checks the workspace, private state, request files, model server and free disk;
2. removes only old *terminal* self-improvement requests (rejected, restored or
   rolled back) and repairs private directory permissions;
3. optionally asks the local model for one improvement, which is stored as a
   pending request.

Nothing here applies a model-written change. Approval remains an explicit
``offgrid improve approve`` action.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .private import ensure_private_dir, mode_of
from .providers import LocalProvider, ProviderError
from .self_improve import ImproveError, Request, SelfImprover
from .ui import UI
from .workspace import Workspace

PRUNABLE_STATES = frozenset({"rejected", "restored", "rolled_back"})
MIN_FREE_GB = 1.0


def _created(request: Request) -> float:
    try:
        return float(request.data.get("created") or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Check:
    """One deterministic health check."""

    name: str
    ok: bool
    detail: str
    fix: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class MaintenanceReport:
    """The result of one self-debug and maintenance pass."""

    timestamp: float
    checks: List[Check] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    improvement: Optional[Request] = None
    improvement_skipped: str = ""
    error: str = ""

    @property
    def healthy(self) -> bool:
        return not self.error and all(check.ok for check in self.checks)

    def as_dict(self) -> Dict[str, Any]:
        improvement: Optional[Dict[str, Any]] = None
        if self.improvement is not None:
            data = self.improvement.data
            improvement = {
                "id": self.improvement.id,
                "state": self.improvement.state,
                "summary": str(data.get("summary") or "")[:2000],
                "files": [str(item.get("path") or "") for item in (data.get("files") or [])],
                "created": data.get("created"),
            }
        return {
            "timestamp": self.timestamp,
            "healthy": self.healthy,
            "checks": [check.as_dict() for check in self.checks],
            "actions": list(self.actions),
            "improvement": improvement,
            "improvement_skipped": self.improvement_skipped,
            "error": self.error,
        }


class SelfDebugger:
    """Read-only checks that explain why a deployment is unhealthy."""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        improver: SelfImprover,
        provider: Optional[LocalProvider] = None,
        provider_error: str = "",
        min_free_gb: float = MIN_FREE_GB,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.improver = improver
        self.provider = provider
        self.provider_error = provider_error
        self.min_free_gb = max(0.0, float(min_free_gb))

    def run(self) -> List[Check]:
        return [
            self._workspace(),
            self._state(),
            self._requests(),
            self._model(),
            self._disk(),
        ]

    def _workspace(self) -> Check:
        root = self.workspace.root
        if not root.is_dir():
            return Check("workspace", False, f"not a directory: {root}", "choose a valid --workspace")
        if not os.access(str(root), os.W_OK):
            return Check(
                "workspace",
                False,
                f"not writable: {root}",
                "fix ownership or permissions on the workspace",
            )
        return Check("workspace", True, str(root))

    def _state(self) -> Check:
        state = self.workspace.root / self.config.session_dir
        try:
            if not state.exists():
                return Check("state", True, "not created yet (maintenance will create it)")
            if state.is_symlink():
                return Check(
                    "state",
                    False,
                    f"state directory is a symlink: {state}",
                    "replace the symlink with a real private directory",
                )
            if not state.is_dir():
                return Check(
                    "state",
                    False,
                    f"not a directory: {state}",
                    "move the file aside or choose a different session_dir",
                )
            mode = mode_of(state)
            if mode is not None and mode & 0o077:
                return Check(
                    "state",
                    False,
                    f"permissions {mode:o} expose local state: {state}",
                    "run maintenance or chmod 700 the state directory",
                )
            return Check("state", True, f"private state directory: {state}")
        except OSError as exc:
            return Check("state", False, f"could not inspect {state}: {exc}", "check the state directory")

    def _requests(self) -> Check:
        if self.improver.requests_dir.is_symlink():
            return Check(
                "requests",
                False,
                f"request directory is a symlink: {self.improver.requests_dir}",
                "replace it with a real private directory",
            )
        try:
            invalid = self.improver.invalid_requests()
            requests = self.improver.all_requests()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not crash the service
            return Check("requests", False, f"could not read requests: {exc}", "inspect .offgrid/self-improve")
        if invalid:
            first = invalid[0]
            return Check(
                "requests",
                False,
                f"{len(invalid)} unreadable request(s); first: {first}",
                "inspect or remove the broken request folder",
            )
        pending = [item for item in requests if item.state == "pending"]
        failed = [item for item in requests if item.state == "rollback_failed"]
        if failed:
            return Check(
                "requests",
                False,
                f"{len(failed)} request(s) need rollback recovery",
                "run `offgrid improve restore ID --force` after inspecting the files",
            )
        detail = f"{len(requests)} request(s); {len(pending)} pending"
        return Check("requests", True, detail)

    def _model(self) -> Check:
        if self.provider_error:
            return Check("model", False, self.provider_error, "start the local model server and retry")
        if self.provider is None:
            return Check(
                "model",
                False,
                "no model provider available",
                "start a local OpenAI-compatible model server",
            )
        try:
            ok, detail = self.provider.health()
        except Exception as exc:  # noqa: BLE001 - health checks are best effort
            return Check("model", False, f"health check failed: {exc}", "check the local model server")
        if not ok:
            return Check("model", False, str(detail), "check the local model server and endpoint")
        return Check("model", True, str(detail))

    def _disk(self) -> Check:
        try:
            free = shutil.disk_usage(str(self.workspace.root)).free / (1024 ** 3)
        except OSError as exc:
            return Check("disk", False, f"could not measure free space: {exc}", "check the workspace disk")
        free = round(free, 1)
        if free < self.min_free_gb:
            return Check(
                "disk",
                False,
                f"{free:.1f} GB free; need at least {self.min_free_gb:.1f} GB",
                "free space before the next model or self-improvement run",
            )
        return Check("disk", True, f"{free:.1f} GB free")


class Maintainer:
    """Run safe housekeeping and optionally create one pending improvement."""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        improver: SelfImprover,
        provider: Optional[LocalProvider] = None,
        provider_error: str = "",
        ui: Optional[UI] = None,
        retention_days: float = 30.0,
        keep: int = 20,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.improver = improver
        self.provider = provider
        self.provider_error = provider_error
        self.ui = ui or UI(config)
        try:
            self.retention_days = max(0.0, float(retention_days))
        except (TypeError, ValueError):
            self.retention_days = 30.0
        try:
            self.keep = max(0, int(keep))
        except (TypeError, ValueError):
            self.keep = 20
        self.debugger = SelfDebugger(
            workspace,
            config,
            improver,
            provider=provider,
            provider_error=provider_error,
        )

    def housekeeping(self) -> List[str]:
        """Repair private state and prune only old terminal requests."""
        actions: List[str] = []
        state = self.workspace.root / self.config.session_dir
        if state.is_symlink():
            return [f"refusing to follow symlinked state directory {state}"]
        if self.improver.requests_dir.is_symlink():
            return [
                f"refusing to follow symlinked request directory {self.improver.requests_dir}"
            ]
        try:
            existed = state.is_dir()
            previous_mode = mode_of(state)
            ensure_private_dir(state)
            if not existed:
                actions.append(f"created private state directory {state}")
            elif previous_mode is not None and previous_mode & 0o077:
                actions.append(f"tightened permissions on {state}")

            requests_existed = self.improver.requests_dir.is_dir()
            ensure_private_dir(self.improver.requests_dir)
            if not requests_existed:
                actions.append("created the self-improvement request directory")
        except OSError as exc:
            actions.append(f"could not prepare local state: {exc}")
            return actions

        if self.retention_days > 0:
            actions.extend(self._prune_requests())
        return actions

    def _prune_requests(self) -> List[str]:
        actions: List[str] = []
        try:
            requests = self.improver.all_requests()
        except Exception as exc:  # noqa: BLE001 - housekeeping must not crash the service
            return [f"could not inspect self-improvement requests: {exc}"]

        terminal = [item for item in requests if item.state in PRUNABLE_STATES]
        terminal.sort(key=_created, reverse=True)
        protected = {item.id for item in terminal[: self.keep]}
        now = time.time()
        root = self.improver.requests_dir.resolve()
        for item in terminal:
            if item.id in protected:
                continue
            created = _created(item)
            if created <= 0:
                continue
            age_days = (now - created) / 86400
            if age_days < self.retention_days:
                continue
            try:
                directory = item.directory.resolve()
                directory.relative_to(root)
            except (OSError, ValueError):
                continue
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                actions.append(f"could not prune request {item.id}: {exc}")
                continue
            actions.append(f"pruned {item.state} request {item.id} ({age_days:.1f} days old)")
        return actions

    def run_once(self, improve: bool = False) -> MaintenanceReport:
        report = MaintenanceReport(timestamp=time.time())
        report.actions = self.housekeeping()
        report.checks = self.debugger.run()

        if not improve:
            return report
        if not report.healthy:
            failed = ", ".join(check.name for check in report.checks if not check.ok)
            report.improvement_skipped = (
                f"self-debug checks failed ({failed}); fix or inspect them first"
                if failed
                else "self-maintenance reported an error"
            )
            return report
        if self.provider_error:
            report.improvement_skipped = self.provider_error
            return report
        if self.provider is None:
            report.improvement_skipped = "no model provider is available"
            return report
        try:
            pending = self.improver.pending()
        except Exception as exc:  # noqa: BLE001 - a broken request is reported by the checks
            report.error = f"could not inspect pending requests: {exc}"
            return report
        if pending:
            report.improvement_skipped = (
                f"request {pending[0].id} is already waiting for approval"
            )
            return report

        try:
            report.improvement = self.improver.propose(
                source="maintenance",
                quiet=True,
            )
        except (ImproveError, ProviderError, OSError) as exc:
            report.error = f"self-improvement failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - keep the service alive on unexpected failures
            report.error = f"self-improvement failed: {type(exc).__name__}: {exc}"
        return report


class MaintenanceLoop:
    """Render passes and keep running until interrupted."""

    def __init__(
        self,
        maintainer: Maintainer,
        interval: float = 3600.0,
        improve: bool = False,
        as_json: bool = False,
        ui: Optional[UI] = None,
    ) -> None:
        self.maintainer = maintainer
        self.interval = max(0.05, float(interval))
        self.improve = improve
        self.as_json = as_json
        self.ui = ui or maintainer.ui

    def run_once(self) -> MaintenanceReport:
        report = self.maintainer.run_once(improve=self.improve)
        self.render(report)
        return report

    def run_forever(self) -> int:
        try:
            while True:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 - a supervisor should keep the loop alive
                    if self.as_json:
                        print(
                            json.dumps(
                                {
                                    "timestamp": time.time(),
                                    "healthy": False,
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            ),
                            flush=True,
                        )
                    else:
                        self.ui.error(f"self-maintenance pass failed: {exc}")
                time.sleep(self.interval)
        except KeyboardInterrupt:
            if not self.as_json:
                self.ui.info("self-maintenance stopped")
            return 130

    def render(self, report: MaintenanceReport) -> None:
        if self.as_json:
            print(json.dumps(report.as_dict()), flush=True)
            return
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.timestamp))
        print()
        print(self.ui.paint("  self-maintenance pass", "\033[1m"))
        print(f"  {stamp}")
        for check in report.checks:
            mark = "ok  " if check.ok else "FAIL"
            print(f"  {mark} {check.name}: {check.detail}")
            if not check.ok and check.fix:
                print(f"       fix: {check.fix}")
        for action in report.actions:
            print(f"  ... {action}")
        if report.improvement is not None:
            summary = str(report.improvement.data.get("summary") or "").strip()
            print(
                f"  ... pending self-improvement {report.improvement.id}: "
                f"{summary[:160] or '(no summary)'}"
            )
            print(
                f"      review: offgrid improve show {report.improvement.id}  "
                f"approve: offgrid improve approve {report.improvement.id}"
            )
        if report.improvement_skipped:
            print(f"  ... improvement skipped: {report.improvement_skipped}")
        if report.error:
            print(f"  !! {report.error}")
        if report.healthy:
            self.ui.ok("healthy")
        else:
            self.ui.warn("attention needed; no code was changed")
        sys.stdout.flush()
