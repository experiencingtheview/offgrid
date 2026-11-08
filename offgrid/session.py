"""Run logs, kept on disk so a session can be inspected (or resumed) later.

Plain JSONL, no database, nothing outside the workspace's .offgrid directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .orchestrator import RunResult
from .private import append_private_text, ensure_private_dir


class SessionLog:
    def __init__(self, workspace_root: Path, session_dir: str = ".offgrid") -> None:
        self.dir = Path(workspace_root) / session_dir
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"{stamp}.jsonl"
        self.session_id = stamp
        self._started = False

    def _ensure(self) -> None:
        if self._started:
            return
        try:
            ensure_private_dir(self.dir)
            self._started = True
        except OSError:
            self._started = False

    def event(self, kind: str, payload: Dict[str, Any]) -> None:
        self._ensure()
        if not self._started:
            return
        record = {"t": round(time.time(), 3), "kind": kind, "data": payload}
        try:
            append_private_text(self.path, json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def record_run(self, result: RunResult) -> None:
        self.event("run", result.as_dict())

    @staticmethod
    def latest(workspace_root: Path, session_dir: str = ".offgrid") -> Optional[Path]:
        directory = Path(workspace_root) / session_dir
        if not directory.is_dir():
            return None
        logs: List[Path] = sorted(directory.glob("*.jsonl"))
        return logs[-1] if logs else None

    @staticmethod
    def load_runs(path: Path) -> List[Dict[str, Any]]:
        runs: List[Dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("kind") == "run":
                        runs.append(record.get("data") or {})
        except OSError:
            pass
        return runs
