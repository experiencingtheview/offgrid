"""Configuration: sensible defaults, overridable by file, env and flags."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Popular local OpenAI-compatible servers, in the order we probe them.
CANDIDATE_ENDPOINTS: Tuple[str, ...] = (
    "http://127.0.0.1:8080/v1",   # llama.cpp llama-server
    "http://127.0.0.1:11434/v1",  # Ollama
    "http://127.0.0.1:1234/v1",   # LM Studio
    "http://127.0.0.1:8000/v1",   # vLLM / llama-cpp-python
    "http://127.0.0.1:5000/v1",   # text-generation-webui
)

CONFIG_FILENAMES = (".offgrid.json", "offgrid.json", "config.json")


@dataclass
class Config:
    """Every knob in one place. All of it works offline."""

    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048
    request_timeout: int = 600
    native_tools: Optional[bool] = None  # None = auto-detect
    workspace: str = "."
    max_turns: int = 12
    parallel: int = 1

    # Effort: quality first, then power, then speed. -1 means "use the profile".
    effort: str = "best"
    plan_candidates: int = -1
    review_passes: int = -1
    review_rounds: int = -1
    test_fix_rounds: int = -1
    quality_gate: bool = True
    quality_rounds: int = -1
    escalate: bool = True
    escalation_model: str = ""

    # -- this machine, and the other machines you own ---------------------
    scale: bool = True  # let measured hardware raise or narrow the effort profile
    context_chars: int = -1  # -1 = from the measured capacity
    fleet: bool = False  # route model calls to peers on the LAN
    fleet_port: int = 8776
    fleet_token: str = ""
    fleet_file: str = ".offgrid/fleet.json"
    fleet_timeout: int = 180
    command_policy: str = "ask"  # ask | auto | deny
    network: str = "allow"  # allow | deny
    offline: bool = False  # hard offline: no remote hosts, no network tools
    allow_remote: bool = False
    verify_command: str = ""
    recon: bool = True
    verbose: int = 2  # 0 quiet, 1 normal, 2 verbose
    color: bool = True
    session_dir: str = ".offgrid"
    max_output: int = 6000
    max_agent_history: int = 24

    # -- memory, learning, offline library, council ----------------------
    memory: bool = True
    memory_dir: str = ".offgrid"
    lexicon_file: str = ".offgrid/lexicon.json"  # project-specific synonym groups
    memory_limit: int = 6
    reflect: bool = True
    review_lessons: bool = True  # ask before new lessons are allowed to influence runs
    library_dir: str = "offgrid-library"
    council: bool = False
    council_members: Tuple[str, ...] = ("local",)
    allow_public_council: bool = False
    council_timeout: int = 90
    council_max_chars: int = 2000
    # When an agent is about to admit it does not know, ask the council first.
    council_auto: bool = True
    council_auto_limit: int = 2  # per agent run, so a stuck agent cannot loop

    # -- whose side this software is on -----------------------------------
    principal: str = "Zach"
    household: Tuple[str, ...] = ("my dog",)

    # -- security and trust -----------------------------------------------
    security_review: bool = True  # review the changed files at the end of every run
    security_history: str = ".offgrid/security.jsonl"  # append-only review log
    watch_file: str = ".offgrid/watch.json"  # private connection baseline

    # -- loading ---------------------------------------------------------
    def merged(self, overrides: Dict[str, Any]) -> "Config":
        known = {f.name for f in fields(self)}
        for key, value in overrides.items():
            if key in known and value is not None:
                setattr(self, key, value)
        return self

    def resolved_endpoint(self) -> str:
        return (self.endpoint or CANDIDATE_ENDPOINTS[0]).rstrip("/")

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def describe(self) -> str:
        return (
            f"endpoint={self.resolved_endpoint()} model={self.model or '(auto)'} "
            f"parallel={self.parallel} turns={self.max_turns} "
            f"commands={self.command_policy} network={self.network} "
            f"effort={self.effort} memory={'on' if self.memory else 'off'} "
            f"scale={'on' if self.scale else 'off'} fleet={'on' if self.fleet else 'off'} "
            f"council={'on' if self.council else 'off'}"
            + (f"(auto) " if self.council and self.council_auto else " ")
        )


def _config_paths(workspace: Path) -> list:
    """Later files win. Nearest config to the workspace wins overall."""
    paths = []
    env_path = os.environ.get("OFFGRID_CONFIG")
    if env_path:
        paths.append(Path(env_path).expanduser())
    bundle = Path(__file__).resolve().parent.parent / "config.json"
    if bundle.is_file():
        paths.append(bundle)
    for name in CONFIG_FILENAMES:
        candidate = workspace / name
        if candidate.is_file():
            paths.append(candidate)
    return paths


def _from_env() -> Dict[str, Any]:
    env = os.environ
    out: Dict[str, Any] = {}
    mapping = {
        "OFFGRID_ENDPOINT": ("endpoint", str),
        "OFFGRID_MODEL": ("model", str),
        "OFFGRID_API_KEY": ("api_key", str),
        "OFFGRID_TEMPERATURE": ("temperature", float),
        "OFFGRID_MAX_TOKENS": ("max_tokens", int),
        "OFFGRID_MAX_TURNS": ("max_turns", int),
        "OFFGRID_PARALLEL": ("parallel", int),
        "OFFGRID_COMMAND_POLICY": ("command_policy", str),
        "OFFGRID_VERIFY": ("verify_command", str),
        "OFFGRID_TIMEOUT": ("request_timeout", int),
    }
    for var, (key, caster) in mapping.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        try:
            out[key] = caster(raw)
        except ValueError:
            continue
    if env.get("OFFGRID_OFFLINE", "").lower() in ("1", "true", "yes"):
        out["offline"] = True
        out["network"] = "deny"
    if env.get("OFFGRID_NO_MEMORY", "").lower() in ("1", "true", "yes"):
        out["memory"] = False
    if env.get("OFFGRID_COUNCIL", "").lower() in ("1", "true", "yes"):
        out["council"] = True
    if env.get("OFFGRID_COUNCIL_PUBLIC", "").lower() in ("1", "true", "yes"):
        out["allow_public_council"] = True
    if env.get("OFFGRID_COUNCIL_AUTO", "").lower() in ("0", "false", "no"):
        out["council_auto"] = False
    if env.get("OFFGRID_FLEET", "").lower() in ("1", "true", "yes"):
        out["fleet"] = True
    if env.get("OFFGRID_NO_SCALE", "").lower() in ("1", "true", "yes"):
        out["scale"] = False
    if env.get("OFFGRID_FLEET_PORT"):
        try:
            out["fleet_port"] = int(env["OFFGRID_FLEET_PORT"])
        except ValueError:
            pass
    if env.get("OFFGRID_FLEET_TOKEN"):
        out["fleet_token"] = env["OFFGRID_FLEET_TOKEN"]
    if env.get("OFFGRID_CONTEXT"):
        try:
            out["context_chars"] = int(env["OFFGRID_CONTEXT"])
        except ValueError:
            pass
    if env.get("OFFGRID_LIBRARY"):
        out["library_dir"] = env["OFFGRID_LIBRARY"]
    if env.get("OFFGRID_LEXICON"):
        out["lexicon_file"] = env["OFFGRID_LEXICON"]
    if env.get("OFFGRID_COUNCIL_MEMBERS"):
        out["council_members"] = tuple(
            part.strip() for part in env["OFFGRID_COUNCIL_MEMBERS"].split(",") if part.strip()
        )
    if env.get("NO_COLOR"):
        out["color"] = False
    return out


def load_config(workspace: Path, overrides: Optional[Dict[str, Any]] = None) -> Config:
    """Merge defaults <- config files <- env <- CLI flags, in that order."""
    config = Config()
    for path in _config_paths(workspace):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            config.merged(data)
    config.merged(_from_env())
    if overrides:
        config.merged(overrides)
    if config.offline:
        config.network = "deny"
    return config
