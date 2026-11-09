"""Configuration: sensible defaults, overridable by file, env and flags."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .primary import PRINCIPAL

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
    fleet_exec: bool = False  # let agents run commands on registered peers
    # -- learning sync between installations ------------------------------
    sync_peers: Tuple[str, ...] = ()
    sync_port: int = 8778
    sync_timeout: int = 30
    sync_interval: int = 900  # scheduled sync pass interval, in seconds
    sync_allow_http: bool = False  # HTTPS is required for public peers
    sync_token_file: str = ".offgrid/sync-token"
    sync_log: str = ".offgrid/sync.jsonl"
    command_policy: str = "ask"  # ask | auto | deny
    network: str = "allow"  # allow | deny
    offline: bool = False  # hard offline: no remote hosts, no network tools
    allow_remote: bool = False
    verify_command: str = ""
    recon: bool = True
    verbose: int = 2  # 0 quiet, 1 normal, 2 verbose
    color: bool = True
    speak: bool = False  # read each final response aloud through an OS voice
    voice: str = ""  # system voice name; empty uses the OS default
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
    principal: str = PRINCIPAL  # fixed; __setattr__ prevents a config override
    household: Tuple[str, ...] = ("the dog",)
    caretaker: str = ""  # local user on this installation; blank means "the local user"
    caretaker_household: Tuple[str, ...] = ()

    # -- security and trust -----------------------------------------------
    security_review: bool = True  # review the changed files at the end of every run
    security_history: str = ".offgrid/security.jsonl"  # append-only review log
    watch_file: str = ".offgrid/watch.json"  # private connection baseline

    # -- gated self-improvement -------------------------------------------
    self_improve_idle: bool = False  # propose when chat is idle; never apply
    self_improve_idle_minutes: float = 15.0
    self_improve_retention_days: float = 30.0  # prune terminal request snapshots
    self_improve_keep: int = 20  # always keep the newest terminal requests

    # -- server self-maintenance ------------------------------------------
    maintenance_interval: float = 3600.0  # seconds between self-debug passes
    maintenance_improve: bool = False  # also prepare a pending improvement

    # -- loading ---------------------------------------------------------
    def __setattr__(self, name: str, value: Any) -> None:
        """Keep the principal identity fixed, whatever the source of config."""
        if name == "principal":
            value = PRINCIPAL
        super().__setattr__(name, value)

    @property
    def config_warnings(self) -> Tuple[str, ...]:
        """Load-time notices, such as a file trying to rename the principal."""
        return getattr(self, "_config_warnings", ())

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
            f"fleet-exec={'on' if self.fleet_exec else 'off'} "
            f"sync={'on' if self.sync_peers else 'off'} "
            f"speech={'on' if self.speak else 'off'} "
            f"council={'on' if self.council else 'off'} "
            f"self-improve={'idle' if self.self_improve_idle else 'manual'} "
            f"maintenance={self.maintenance_interval:g}s"
            + (f" (auto)" if self.council and self.council_auto else "")
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


def _ignored_principal_warning(source: str, value: Any) -> str:
    """Explain that a supplied principal value never took effect."""
    shown = " ".join(str(value).split()) or "(empty)"
    if len(shown) > 60:
        shown = shown[:57] + "..."
    return (
        f"{source} sets principal to {shown!r}; it is ignored because the "
        f"principal is fixed as {PRINCIPAL}"
    )


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
        "OFFGRID_VOICE": ("voice", str),
        "OFFGRID_CARETAKER": ("caretaker", str),
        "OFFGRID_SYNC_PORT": ("sync_port", int),
        "OFFGRID_SYNC_INTERVAL": ("sync_interval", int),
        "OFFGRID_IMPROVE_IDLE_MINUTES": ("self_improve_idle_minutes", float),
        "OFFGRID_IMPROVE_RETENTION_DAYS": ("self_improve_retention_days", float),
        "OFFGRID_IMPROVE_KEEP": ("self_improve_keep", int),
        "OFFGRID_MAINTENANCE_INTERVAL": ("maintenance_interval", float),
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
    if env.get("OFFGRID_SPEAK", "").lower() in ("1", "true", "yes"):
        out["speak"] = True
    if env.get("OFFGRID_IMPROVE_WHEN_IDLE", "").lower() in ("1", "true", "yes"):
        out["self_improve_idle"] = True
    if env.get("OFFGRID_MAINTENANCE_IMPROVE", "").lower() in ("1", "true", "yes"):
        out["maintenance_improve"] = True
    if env.get("OFFGRID_COUNCIL", "").lower() in ("1", "true", "yes"):
        out["council"] = True
    if env.get("OFFGRID_COUNCIL_PUBLIC", "").lower() in ("1", "true", "yes"):
        out["allow_public_council"] = True
    if env.get("OFFGRID_COUNCIL_AUTO", "").lower() in ("0", "false", "no"):
        out["council_auto"] = False
    if env.get("OFFGRID_FLEET", "").lower() in ("1", "true", "yes"):
        out["fleet"] = True
    if env.get("OFFGRID_FLEET_EXEC", "").lower() in ("1", "true", "yes"):
        out["fleet"] = True
        out["fleet_exec"] = True
    if env.get("OFFGRID_NO_SCALE", "").lower() in ("1", "true", "yes"):
        out["scale"] = False
    if env.get("OFFGRID_FLEET_PORT"):
        try:
            out["fleet_port"] = int(env["OFFGRID_FLEET_PORT"])
        except ValueError:
            pass
    if env.get("OFFGRID_FLEET_TOKEN"):
        out["fleet_token"] = env["OFFGRID_FLEET_TOKEN"]
    if env.get("OFFGRID_HOUSEHOLD"):
        out["household"] = tuple(
            part.strip() for part in env["OFFGRID_HOUSEHOLD"].split(",") if part.strip()
        )
    if env.get("OFFGRID_CARETAKER_HOUSEHOLD"):
        out["caretaker_household"] = tuple(
            part.strip()
            for part in env["OFFGRID_CARETAKER_HOUSEHOLD"].split(",")
            if part.strip()
        )
    if env.get("OFFGRID_SYNC_PEERS"):
        out["sync_peers"] = tuple(
            part.strip() for part in env["OFFGRID_SYNC_PEERS"].split(",") if part.strip()
        )
    if env.get("OFFGRID_SYNC_ALLOW_HTTP", "").lower() in ("1", "true", "yes"):
        out["sync_allow_http"] = True
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
    warnings = []
    for path in _config_paths(workspace):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            if "principal" in data and data["principal"] != PRINCIPAL:
                warnings.append(
                    _ignored_principal_warning(str(path), data["principal"])
                )
            config.merged(data)
    env_principal = os.environ.get("OFFGRID_PRINCIPAL")
    if "OFFGRID_PRINCIPAL" in os.environ and env_principal != PRINCIPAL:
        warnings.append(_ignored_principal_warning("OFFGRID_PRINCIPAL", env_principal))
    config._config_warnings = tuple(warnings)
    config.merged(_from_env())
    if overrides:
        config.merged(overrides)
    if config.offline:
        config.network = "deny"
    return config
